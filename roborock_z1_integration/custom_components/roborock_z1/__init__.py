"""The Roborock Z1 Mower integration (built on python-roborock)."""
from __future__ import annotations

import dataclasses
import json
import logging
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
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_BASE_URL,
    CONF_HOME_DATA,
    CONF_USER_DATA,
    DOMAIN,
    METHOD_DOCK,
    METHOD_GET_STATUS,
    METHOD_PAUSE,
    METHOD_RESUME,
    METHOD_START,
    RPC_RESULT_KEY_MAP,
    STATUS_POLL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LAWN_MOWER, Platform.SENSOR]

# DPS id -> MowerStatus field name, derived from python-roborock's dataclass.
_DPS_TO_FIELD: dict[int, str] = {
    f.metadata["dps"].value: f.name
    for f in dataclasses.fields(MowerStatus)
    if "dps" in f.metadata
}


class RockMowZ1Device:
    """One Z1 mower: holds status and talks to it over the cloud MQTT channel."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HomeDataDevice,
        product: HomeDataProduct,
        channel: MqttChannel,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.product = product
        self.channel = channel
        self.status = MowerStatus()
        self._unsub = None
        self._unsub_poll = None
        self._got_first_status = False
        self._last_rpc_result: dict | None = None

    @property
    def duid(self) -> str:
        return self.device.duid

    @property
    def signal(self) -> str:
        return f"{DOMAIN}_update_{self.duid}"

    @property
    def available(self) -> bool:
        return self.channel.is_connected

    async def async_start(self) -> None:
        self._unsub = await self.channel.subscribe(self._message_received)
        # B01-family devices only publish state when asked: request an initial
        # full DPS dump, then keep polling as a safety net.
        await self.async_request_status()

        async def _poll(_now) -> None:
            await self.async_request_status()

        self._unsub_poll = async_track_time_interval(
            self.hass, _poll, timedelta(seconds=STATUS_POLL_SECONDS)
        )

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_poll:
            self._unsub_poll()
            self._unsub_poll = None

    async def async_request_status(self) -> None:
        """Ask the device for its current status via V1 RPC."""
        try:
            await self._send_rpc(METHOD_GET_STATUS, [])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Status request failed for %s: %s", self.duid, err)

    @callback
    def _message_received(self, message: RoborockMessage) -> None:
        """Decode a DPS payload from the device and merge into status."""
        _LOGGER.debug(
            "Message from %s: protocol=%s payload=%r",
            self.duid,
            message.protocol,
            message.payload[:400] if message.payload else None,
        )
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
                _LOGGER.debug("RPC result from %s: %r", self.duid, result)
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
        updated = False
        field_names = {f.name for f in dataclasses.fields(MowerStatus)}
        for key, value in result.items():
            key = RPC_RESULT_KEY_MAP.get(key, key)
            if key not in field_names:
                continue
            # don't overwrite a known battery level with a zero reading
            if key == "battery" and not value and self.status.battery:
                continue
            setattr(self.status, key, value)
            updated = True
        return updated

    async def _send_dps_write(self, code: int, value: Any) -> None:
        """Send a command as a raw dps write (the mechanism suggested by
        python-roborock's RoborockMowerDataProtocol command codes 201-205,
        observed from app traffic)."""
        import time

        message = RoborockMessage(
            protocol=RoborockMessageProtocol.RPC_REQUEST,
            version=b"1.0",
            payload=json.dumps({"t": int(time.time()), "dps": {str(code): value}}).encode(),
        )
        await self.channel.publish(message)
        _LOGGER.debug("Published dps write %s=%s to %s", code, value, self.duid)

    async def _send_rpc(self, method: str, params: list | dict | None) -> None:
        """Send a V1 RPC (the classic {"dps": {"101": ...}} envelope)."""
        message = RequestMessage(method=method, params=params).encode_message(
            RoborockMessageProtocol.RPC_REQUEST
        )
        await self.channel.publish(message)
        _LOGGER.debug("Published RPC %s to %s", method, self.duid)

    async def async_start_mowing(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.START), 1)

    async def async_pause(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.PAUSE), 1)

    async def async_resume(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.RESUME), 1)

    async def async_dock(self) -> None:
        await self._send_dps_write(int(RoborockMowerDataProtocol.DOCK), 1)


def _extract_dps(message: RoborockMessage) -> dict | None:
    """Pull the dps dict out of an incoming message (plain JSON, with a
    fallback for trailing padding bytes)."""
    if not message.payload:
        return None
    for data in (message.payload, message.payload.rstrip(b"\\x00").rstrip()):
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
    # Entries created by older versions may hold a garbage base_url (a
    # stringified coroutine). Only use it if it looks like a real URL;
    # otherwise let the client rediscover it.
    base_url = entry.data.get(CONF_BASE_URL)
    if not (isinstance(base_url, str) and base_url.startswith("http")):
        base_url = None
    api = RoborockApiClient(username=entry.data[CONF_USERNAME], base_url=base_url)

    # Roborock's home-data endpoint is aggressively rate-limited, so fetch it
    # once and cache it in the config entry; reuse the cache on later setups
    # (devices rarely change). Delete + re-add the integration to force a
    # fresh fetch.
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
        "Roborock account: %d device(s), %d product(s)",
        len(all_devices),
        len(products),
    )
    for dev in all_devices:
        prod = products.get(dev.product_id)
        _LOGGER.info(
            "Device name=%s model=%s category=%s pv=%s online=%s",
            dev.name,
            prod.model if prod else "?",
            prod.category if prod else "NO PRODUCT MATCH",
            dev.pv,
            getattr(dev, "online", "?"),
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
        z1 = RockMowZ1Device(hass, entry, dev, product, channel)
        await z1.async_start()
        devices.append(z1)
        _LOGGER.info(
            "Connected to mower %s (%s, pv=%s), requesting status",
            dev.name,
            product.model,
            dev.pv,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "devices": devices,
        "mqtt_session": mqtt_session,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        for device in data["devices"]:
            await device.async_stop()
        await data["mqtt_session"].close()
    return unload_ok
