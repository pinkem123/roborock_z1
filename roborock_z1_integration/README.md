# Roborock Z1 Mower — Home Assistant Integration

Custom integration for the Roborock Z1 robot lawn mower (`roborock.mower.a282`), built on python-roborock and Roborock's cloud MQTT. Provides a native `lawn_mower` entity (Start / Pause / Return to dock) plus battery, mowing-progress, blade-lifespan, and raw-state sensors. Tested working on real hardware (2026).

The official `roborock` integration does not support mowers yet — when it does (see python-roborock issue #757), migrate to it.

## Install

1. Copy `custom_components/roborock_z1/` into `config/custom_components/`.
2. Restart Home Assistant (installs `python-roborock==5.25.0` automatically).
3. Settings → Devices & Services → Add Integration → **Roborock Z1 Mower**.
4. Log in with your Roborock account email + emailed verification code.

Home data is cached in the config entry after the first fetch (Roborock rate-limits that endpoint aggressively). Added a new device? Delete and re-add the integration.

## Protocol notes (established on real hardware)

The Z1 reports `pv=1.0` and speaks Roborock's classic V1 framing over cloud MQTT:

- **`get_status`** answers only with a legacy placeholder `{msg_ver, msg_seq, state: 0, battery: 0}` — polled every 60 s as a heartbeat and discarded. (`app_get_init_status` also answers, with firmware/region info.)
- **Real telemetry** arrives as unsolicited dps push updates using codes 120–145 (python-roborock's `RoborockMowerDataProtocol` / `MowerStatus`): battery=121, mow_state=123, mow_progress=139, blade_lifespan=140, …
- **Commands are dps writes, not RPC methods** (named methods return `unknown_method`). Confirmed: START `{"dps":{"201":1}}`, DOCK 202; PAUSE 203 / RESUME 204 use the same pattern.
- **Connection watchdog:** the heartbeat guarantees ≥1 message/min on a healthy link; after 5 min of silence the MQTT session is force-restarted (the library's own reconnect backoff can reach 6 h), after 15 min the integration reloads itself. Entities show unavailable while stale.
- Sensors restore their last value across HA restarts.
- Undiscovered reply formats surface automatically as `New RPC payload shape` warnings in the default log.

### mow_state codes (fw A.03.0894_CE)

| Code | Meaning | Entity activity |
|---|---|---|
| 0 | Idle / no active task (also during return trip) | Docked |
| 51 | Calibrate position | Mowing |
| 52 | Leaving dock | Mowing |
| 55 | Mowing | Mowing |
| 56 | Edge cutting | Mowing |
| 57 | Moving to another destination | Mowing |
| 59 | Mower stuck | Error |
| 61 | Paused Raining | Paused |
| 66 | Failed to return to charging station | Error |
| 67 | Mower overturned | Error |

Observed but not yet decoded: 58, 76, 77 (display as "unknown"). Human-readable texts are exposed as the mower entity's `state_description` attribute — handy for notifications.

## Integration icon

Entity icons are built in (`icons.json`). The Integrations-page brand logo is served locally from `custom_components/roborock_z1/brand/` (official Roborock brand images). Requires **Home Assistant 2026.3.0+**. No brands-repository submission needed.

## Debugging

```yaml
logger:
  logs:
    custom_components.roborock_z1: debug
```

## Contributing upstream

These findings are not documented anywhere upstream. Consider sharing at https://github.com/Python-roborock/python-roborock/issues/757 — it directly helps official Home Assistant support for the Z1.
