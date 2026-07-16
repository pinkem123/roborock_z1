# Roborock Z1 Mower — Home Assistant Integration

Custom integration for the Roborock Z1 robot lawn mower (`roborock.mower.a282`), built on python-roborock and Roborock's cloud MQTT. Provides a native `lawn_mower` entity (Start / Pause / Return to dock) plus battery, mowing-progress, and blade-lifespan sensors.

Tested working on a real Z1 (July 2026). The official `roborock` integration does not support mowers yet — when it does (see python-roborock issue #757), migrate to it.

## Install

1. Copy `custom_components/roborock_z1/` into `config/custom_components/`.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Roborock Z1 Mower**.
4. Log in with your Roborock account email + emailed verification code.

Home data is cached in the config entry after the first fetch (Roborock rate-limits that endpoint aggressively). Added a new device to your account? Delete and re-add the integration.

## Protocol notes (discovered on a real device)

The Z1 reports `pv=1.0` and communicates over Roborock's cloud MQTT using the classic V1 message framing, with mower-specific semantics:

- **Status polling:** the V1 RPC `get_status` is answered, but only with a legacy placeholder `{msg_ver, msg_seq, state: 0, battery: 0}`. The integration polls it as a heartbeat and discards the placeholder.
- **Real telemetry** arrives as unsolicited dps push updates using codes 120–145 (matching python-roborock's `RoborockMowerDataProtocol` / `MowerStatus`): battery=121, mow_state=123, mow_start_type=132, mow_progress=139, blade_lifespan=140, ...
- **Commands are dps writes, not RPC methods** (named methods like `app_start` return `unknown_method`). Confirmed working: START `{"dps":{"201":1}}` and DOCK `{"dps":{"202":1}}`; PAUSE (203) and RESUME (204) use the same pattern.
- Sensors restore their last value across HA restarts, since the mower only pushes on change.

`MOW_STATE_TO_ACTIVITY` in `const.py` maps `mow_state` codes to HA activities. Only `0 = docked/idle` is confirmed by observation; the rest are provisional. Watch the `mow_state_raw` attribute on the mower entity while it mows / pauses / returns and adjust the map.

## Debugging

```yaml
logger:
  logs:
    custom_components.roborock_z1: debug
```

Debug logging shows every message with its raw payload.

## Contributing upstream

These findings (V1 framing on a mower, the get_status placeholder, dps-write commands 201/202) are not documented anywhere upstream. Consider sharing your traces at https://github.com/Python-roborock/python-roborock/issues/757 — it directly helps official Home Assistant support for the Z1.

## Integration icon

Entity icons are built in (`icons.json`): robot-mower for the mower entity, plus themed icons for progress, blade lifespan, and the raw state sensor.

The brand logo on the Integrations page cannot be shipped inside a custom component — Home Assistant only serves those from the central [home-assistant/brands](https://github.com/home-assistant/brands) repository. Ready-made files are included in `brands/roborock_z1/`: the official Roborock brand images (icon 256/512 + wordmark logo 256/512), taken from the brands repo's own `core_integrations/roborock` so this integration matches the core Roborock integration's look. To get the logo showing:

1. Fork https://github.com/home-assistant/brands
2. Copy `brands/roborock_z1/` into `custom_integrations/roborock_z1/` in the fork
3. Open a PR — once merged, the icon appears automatically (no integration update needed)

Until then HA shows a generic placeholder; purely cosmetic.
