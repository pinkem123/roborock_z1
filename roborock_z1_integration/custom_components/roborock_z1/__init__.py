"""The Roborock Z1 Mower integration (built on python-roborock).

The Z1 (roborock.mower.a282, pv=1.0) speaks Roborock's V1 protocol over the
cloud MQTT. Protocol facts established on real hardware:

- ``get_status`` is answered, but only with a legacy placeholder
  ``{msg_ver, msg_seq, state: 0, battery: 0}``; polled as a heartbeat and
  discarded.
- Real telemetry arrives as unsolicited dps push updates using codes 120-145
  (python-roborock's ``RoborockMowerDataProtocol`` / ``MowerStatus``).
- Commands are dps writes, not RPC methods: START ``{"dps":{"201":1}}``,
  DOCK 202, PAUSE 203, RESUME 204 (named methods return ``unknown_method``).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import timedelta
from typing import Any

from roborock.data import (
    HomeData,
    HomeDataDevice,
    HomeDataProduct,
    RoborockCategory,
    UserData,
)
from roborock.data.mower import MowerStatus
from roborock.devices.transport.mqtt_channel import MqttChannel
from roborock.mqtt.roborock_session import create_mqtt_session
from roborock.protocol import create_mqtt_params
from roborock.protocols.v1_protocol import RequestMessage
from roborock.roborock_message import (
    RoborockMessage,
    RoborockMessageProtocol,
    RoborockMowerDataProtocol,
)
from roborock.web_api import RoborockApiClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import (
    CONF_BASE_URL,
    CONF_HOME_DATA,
    CONF_USER_DATA,
    CONSUMABLE_POLL_SECONDS,
    DOMAIN,
    METHOD_GET_CONSUMABLE,
    METHOD_GET_STATUS,
    RPC_RESULT_KEY_MAP,
    STATUS_POLL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LAWN_MOWER, Platform.SENSOR]

# Watchdog thresholds. A healthy link yields at least one message per poll
# (the get_status heartbeat is always answered), so prolonged silence means
# the cloud MQTT session is dead even if it claims to be connected. The
# library's own reconnect backoff can grow to 6 hours; we intervene sooner.
STALE_RESTART_SECONDS = 300  # force session.restart() after 5 min of silence
STALE_RELOAD_SECONDS = 900   # reload the config entry after 15 min

# Roborock's cloud stores a per-device dps snapshot in home data
# (HomeDataDevice.device_status) — this is where the app's instantly-shown
# blade lifespan comes from. Refresh it once a day (the endpoint is
# aggressively rate-limited, so keep this rare).
HOME_DATA_REFRESH_SECONDS = 24 * 3600

# DPS id -> MowerStatus field name, derived from python-roborock's dataclass.
_DPS_TO_FIELD: dict[int, str] = {
    f.metadata["dps"].value: f.name
    for f in dataclasses.fields(MowerStatus)
    if "dps" in f.metadata
}

_STATUS_FIELD_NAMES = {f.name for f in dataclasses.fields(MowerStatus)}


class RockMowZ1Device:
    """One Z1 mower: holds status and talks to it over the cloud MQTT channel."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HomeDataDevice,
        product: HomeDataProduct,
        channel: MqttChannel,
        session,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.product = product
        self.channel = channel
        self.session = session
        self.status = MowerStatus()
        self._unsub = None
        self._unsub_poll = None
        self._unsub_consumable = None
        self._unsub_initial = None
        self._got_first_status = False
        self._last_rpc_result: dict | None = None
        self._last_rx: float | None = None
        self._restart_pending = False
        self._reload_scheduled = False
        self._logged_result_shapes: set[frozenset] = set()

    @property
    def duid(self) -> str:
        return self.device.duid

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_update_{self.entry.entry_id}_{self.duid}"

    @property
    def _silence(self) -> float | None:
        """Seconds since the last message from the device, if any received."""
        if self._last_rx is None:
            return None
        return time.monotonic() - self._last_rx

    @property
    def available(self) -> bool:
        # Connected AND recently heard from: a session can claim to be
        # connected while actually dead, and the heartbeat guarantees
        # regular traffic when the link is healthy.
        if not self.channel.is_connected:
            return False
        silence = self._silence
        return silence is None or silence < STALE_RESTART_SECONDS

    async def async_start(self) -> None:
        self._unsub = await self.channel.subscribe(self._message_received)
        # Request an initial status, then keep polling as a heartbeat.
        await self.async_request_status()

        async def _poll(_now) -> None:
            await self.async_request_status()
            await self._check_freshness()

        self._unsub_poll = async_track_time_interval(
            self.hass, _poll, timedelta(seconds=STATUS_POLL_SECONDS)
        )

        async def _probe(_now) -> None:
            await self.async_probe_consumable_methods()

        async def _poll_consumable(_now) -> None:
            await self.async_request_consumable()

        # one-time method probe shortly after startup, then slow-poll the
        # standard method
        self._unsub_initial = async_call_later(self.hass, 20, _probe)
        self._unsub_consumable = async_track_time_interval(
            self.hass, _poll_consumable, timedelta(seconds=CONSUMABLE_POLL_SECONDS)
        )

    async def async_stop(self) -> None:
        for attr in ("_unsub", "_unsub_poll", "_unsub_consumable", "_unsub_initial"):
            unsub = getattr(self, attr)
            if unsub:
                unsub()
                setattr(self, attr, None)

    async def _check_freshness(self) -> None:
        """Watchdog: recover a silently-dead cloud MQTT session."""
        silence = self._silence
        if silence is None or silence < STALE_RESTART_SECONDS:
            self._restart_pending = False
            return
        if silence >= STALE_RELOAD_SECONDS:
            if not self._reload_scheduled:
                self._reload_scheduled = True
                _LOGGER.warning(
                    "No messages from %s for %d s despite session restart; "
                    "reloading the integration",
                    self.duid,
                    int(silence),
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.entry.entry_id)
                )
            return
        if not self._restart_pending:
            self._restart_pending = True
            _LOGGER.warning(
                "No messages from %s for %d s; forcing MQTT session restart",
                self.duid,
                int(silence),
            )
            try:
                await self.session.restart()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Session restart failed: %s", err)

    async def async_request_status(self) -> None:
        """Ask the device for its current status via V1 RPC."""
        try:
            await self._send_rpc(METHOD_GET_STATUS, [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Status request failed for %s: %s", self.duid, err)

    async def async_request_consumable(self) -> None:
        """Ask the device for consumable data (blade lifespan)."""
        try:
            await self._send_rpc(METHOD_GET_CONSUMABLE, [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Consumable request failed for %s: %s", self.duid, err)

    async def async_probe_consumable_methods(self) -> None:
        """One-time discovery: try candidate consumable methods, announcing
        each at warning level so progress is visible in the default log.
        Replies (if any) surface via the payload-shape logging."""
        import asyncio

        candidates: list[tuple[str, list]] = [
            ("get_consumable", []),
            ("get_consumables", []),
            ("get_blade_life", []),
            ("get_blade_work_time", []),
            ("get_mower_consumable", []),
            ("get_prop", ["blade_work_time"]),
        ]
        for method, params in candidates:
            try:
                await self._send_rpc(method, params)
                _LOGGER.warning(
                    "Probing consumable method %r params=%s on %s",
                    method,
                    params,
                    self.duid,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Probe %r failed to send: %s", method, err)
            await asyncio.sleep(6)
        _LOGGER.warning(
            "Consumable probe finished on %s — any replies were logged as "
            "'New RPC payload shape' or 'RPC error' lines above",
            self.duid,
        )

    @callback
    def _message_received(self, message: RoborockMessage) -> None:
        """Decode a DPS payload from the device and merge into status."""
        _LOGGER.debug(
            "Message from %s: protocol=%s payload=%r",
            self.duid,
            message.protocol,
            message.payload[:400] if message.payload else None,
        )
        self._last_rx = time.monotonic()
        self._restart_pending = False
        self._reload_scheduled = False
        dps = _extract_dps(message)
        if not dps:
            return
        updated = False
        for key, value in dps.items():
            try:
                key_int = int(key)
            except (TypeError, ValueError):
                continue
            if key_int == 102:
                # V1 RPC response: value is a JSON string like
                # {"id": ..., "result": [{...}]}
                updated |= self._merge_rpc_result(value)
                continue
            field_name = _DPS_TO_FIELD.get(key_int)
            if field_name is not None:
                setattr(self.status, field_name, value)
                updated = True
        if updated:
            if not self._got_first_status:
                self._got_first_status = True
                _LOGGER.info("First status from %s: %s", self.duid, self.status)
            async_dispatcher_send(self.hass, self.signal)

    def _merge_rpc_result(self, raw: Any) -> bool:
        """Merge a V1 RPC response into status by matching field names."""
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return False
        if not isinstance(parsed, dict):
            return False
        if "error" in parsed:
            _LOGGER.warning("RPC error from %s: %s", self.duid, parsed["error"])
            return False
        result = parsed.get("result")
        if isinstance(result, list) and result:
            result = result[0]
        if not isinstance(result, dict):
            if result not in (None, "ok", ["ok"]):
                # string results like 'unknown_method' are protocol answers —
                # keep them visible in the default log
                _LOGGER.warning("RPC result from %s: %r", self.duid, result)
            return False
        if result != self._last_rpc_result:
            _LOGGER.debug("RPC status payload from %s: %s", self.duid, result)
            self._last_rpc_result = result
        # The Z1's get_status is a legacy shim that returns a zeroed
        # placeholder ({state: 0, battery: 0}); real telemetry arrives via
        # dps push updates. Never let the placeholder clobber real data.
        if (
            set(result) <= {"msg_ver", "msg_seq", "state", "battery"}
            and not result.get("battery")
            and not result.get("state")
        ):
            return False
        # Surface each new (non-placeholder) payload shape once in the
        # default log — this is how undocumented reply formats get found.
        shape = frozenset(result)
        if shape not in self._logged_result_shapes:
            self._logged_result_shapes.add(shape)
            _LOGGER.warning("New RPC payload shape from %s: %s", self.duid, result)
        updated = False
        for key, value in result.items():
            key = RPC_RESULT_KEY_MAP.get(key, key)
            if key not in _STATUS_FIELD_NAMES:
                continue
            # don't overwrite a known battery level with a zero reading
            if key == "battery" and not value and self.status.battery:
                continue
            setattr(self.status, key, value)
            updated = True
        return updated

    async def _send_rpc(self, method: str, params: list | dict | None) -> None:
        """Send a V1 RPC (the classic {"dps": {"101": ...}} envelope)."""
        message = RequestMessage(method=method, params=params).encode_message(
            RoborockMessageProtocol.RPC_REQUEST
        )
        await self.channel.publish(message)
        _LOGGER.debug("Published RPC %s to %s", method, self.duid)

    async def _send_dps_write(self, code: int, value: Any) -> None:
        """Send a command as a raw dps write — the Z1's actual command
        mechanism (confirmed on hardware; named methods return
        unknown_method)."""
        message = RoborockMessage(
            protocol=RoborockMessageProtocol.RPC_REQUEST,
            version=b"1.0",
            payload=json.dumps(
                {"t": int(time.time()), "dps": {str(code): value}}
            ).encode(),
        )
        await self.channel.publish(message)
        _LOGGER.debug("Published dps write %s=%s to %s", code, value, self.duid)

    async def async_start_mowing(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.START), 1)

    async def async_pause(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.PAUSE), 1)

    async def async_resume(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.RESUME), 1)

    async def async_dock(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.DOCK), 1)


def merge_device_status_snapshot(
    z1: "RockMowZ1Device", device_status: dict | None, *, initial: bool
) -> bool:
    """Merge the cloud-side dps snapshot into a device's status.

    On the initial seed every provided field is taken; on refreshes only
    still-unknown fields are filled — except blade_lifespan, which has no
    live-push path and is always updated from the snapshot.
    """
    if not isinstance(device_status, dict):
        _LOGGER.warning(
            "No cloud device_status snapshot for %s (got %r)", z1.duid, device_status
        )
        return False
    # Log the raw snapshot when it changes (discovery aid; the cloud
    # snapshot notably does NOT contain consumables/maintenance data —
    # that lives behind an app-only cloud endpoint).
    if device_status != getattr(z1, "_last_snapshot", None):
        z1._last_snapshot = dict(device_status)
        _LOGGER.info("Raw cloud device_status for %s: %s", z1.duid, device_status)
    updated = False
    for key, value in device_status.items():
        try:
            field = _DPS_TO_FIELD.get(int(key))
        except (TypeError, ValueError):
            continue
        if field is None or value is None:
            continue
        if not initial and field != "blade_lifespan":
            if getattr(z1.status, field) is not None:
                continue
        setattr(z1.status, field, value)
        updated = True
    if updated:
        _LOGGER.info(
            "Merged cloud snapshot for %s (initial=%s): blade_lifespan=%s "
            "battery=%s mow_state=%s",
            z1.duid,
            initial,
            z1.status.blade_lifespan,
            z1.status.battery,
            z1.status.mow_state,
        )
    return updated


def _extract_dps(message: RoborockMessage) -> dict | None:
    """Pull the dps dict out of an incoming message (plain JSON, with a
    fallback for trailing padding bytes)."""
    if not message.payload:
        return None
    for data in (message.payload, message.payload.rstrip(b"\x00").rstrip()):
        try:
            parsed = json.loads(data.decode("utf-8", errors="ignore"))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            dps = parsed.get("dps", parsed)
            if isinstance(dps, dict):
                return dps
    _LOGGER.debug("Unparsed message: protocol=%s", message.protocol)
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Z1 from a config entry."""
    user_data = UserData.from_dict(entry.data[CONF_USER_DATA])
    # Entries created by older versions may hold a garbage base_url. Only use
    # it if it looks like a real URL; otherwise let the client rediscover it.
    base_url = entry.data.get(CONF_BASE_URL)
    if not (isinstance(base_url, str) and base_url.startswith("http")):
        base_url = None
    api = RoborockApiClient(username=entry.data[CONF_USERNAME], base_url=base_url)

    # Roborock's home-data endpoint is aggressively rate-limited, so fetch it
    # once and cache it in the config entry; reuse the cache on later setups.
    cached = entry.data.get(CONF_HOME_DATA)
    if cached:
        home_data = HomeData.from_dict(cached)
        _LOGGER.debug("Using cached home data (skipping rate-limited API)")
    else:
        try:
            home_data = await api.get_home_data_v3(user_data)
        except Exception as err:  # noqa: BLE001 - surface any cloud failure as retry
            raise ConfigEntryNotReady(
                f"Could not fetch Roborock home data: {err}"
            ) from err
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOME_DATA: home_data.as_dict()}
        )

    all_devices = home_data.devices + home_data.received_devices
    products = {p.id: p for p in home_data.products}
    _LOGGER.info(
        "Roborock account: %d device(s), %d product(s)", len(all_devices), len(products)
    )
    for dev in all_devices:
        prod = products.get(dev.product_id)
        _LOGGER.info(
            "Device name=%s model=%s category=%s pv=%s",
            dev.name,
            prod.model if prod else "?",
            prod.category if prod else "NO PRODUCT MATCH",
            dev.pv,
        )

    mowers = [
        (dev, products[dev.product_id])
        for dev in all_devices
        if dev.product_id in products
        and products[dev.product_id].category == RoborockCategory.MOWER
    ]
    if not mowers:
        raise ConfigEntryNotReady("No mower found on this Roborock account")

    mqtt_params = create_mqtt_params(user_data.rriot)
    mqtt_session = await create_mqtt_session(mqtt_params)

    devices: list[RockMowZ1Device] = []
    for dev, product in mowers:
        channel = MqttChannel(
            mqtt_session, dev.duid, dev.local_key, user_data.rriot, mqtt_params
        )
        z1 = RockMowZ1Device(hass, entry, dev, product, channel, mqtt_session)
        # Seed status from the cloud-side snapshot before the first push —
        # this is notably the only source for blade_lifespan.
        merge_device_status_snapshot(z1, dev.device_status, initial=True)
        await z1.async_start()
        devices.append(z1)
        _LOGGER.info(
            "Connected to mower %s (%s, pv=%s), requesting status",
            dev.name,
            product.model,
            dev.pv,
        )

    async def _refresh_home_data(_now) -> None:
        """Daily: re-fetch home data to refresh the cloud snapshot
        (blade lifespan) and keep the cache current."""
        try:
            fresh = await api.get_home_data_v3(user_data)
        except Exception as err:  # noqa: BLE001 - rate limit etc.; retry tomorrow
            _LOGGER.debug("Daily home data refresh failed: %s", err)
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOME_DATA: fresh.as_dict()}
        )
        fresh_devices = {
            d.duid: d for d in fresh.devices + fresh.received_devices
        }
        for z1 in devices:
            fdev = fresh_devices.get(z1.duid)
            if fdev and merge_device_status_snapshot(
                z1, fdev.device_status, initial=False
            ):
                async_dispatcher_send(hass, z1.signal)

    unsub_refresh = async_track_time_interval(
        hass, _refresh_home_data, timedelta(seconds=HOME_DATA_REFRESH_SECONDS)
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "devices": devices,
        "mqtt_session": mqtt_session,
        "unsub_refresh": unsub_refresh,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        data["unsub_refresh"]()
        for device in data["devices"]:
            await device.async_stop()
        await data["mqtt_session"].close()
    return unload_ok
