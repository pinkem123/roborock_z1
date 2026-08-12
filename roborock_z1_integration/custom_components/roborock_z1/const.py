"""Constants for the Roborock Z1 Mower integration."""

DOMAIN = "roborock_z1"

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"

# Config-entry key caching the account's home data (devices/products), so the
# rate-limited home-data endpoint is only called on first setup. Delete and
# re-add the integration to force a fresh fetch.
CONF_HOME_DATA = "home_data"

# The Z1 reports pv=1.0: it speaks Roborock's V1 RPC protocol (the classic
# vacuum protocol), which python-roborock explicitly rejects for non-vacuum
# devices. We drive it directly. Status polling uses this V1 RPC method:
METHOD_GET_STATUS = "get_status"

# Safety-net poll: re-request status this often (also acts as a heartbeat).
STATUS_POLL_SECONDS = 60

# Consumables (blade lifespan) are not pushed spontaneously; poll the V1
# "get_consumable" RPC at this slower interval.
METHOD_GET_CONSUMABLE = "get_consumable"
CONSUMABLE_POLL_SECONDS = 1800

# The Z1 answers "get_status" with vacuum-style field names. Map them onto
# python-roborock's MowerStatus field names when merging.
RPC_RESULT_KEY_MAP = {
    "state": "mow_state",
}

# Mapping of the Z1's mow_state codes to HA activities, decoded on a real
# device (fw A.03.0894_CE) and from official app notifications. All
# job-active states map to "mowing" since HA's LawnMowerActivity has no finer
# distinction; the true sub-state is always visible on the "Mower state
# (raw code)" diagnostic sensor and the `state_description` attribute.
MOW_STATE_TO_ACTIVITY = {
    0: "docked",   # no active task — reported during the return trip AND while docked
    51: "mowing",  # calibrating position
    52: "mowing",  # leaving the dock
    55: "mowing",  # actively cutting
    56: "mowing",  # edge cutting
    57: "mowing",  # moving to another destination
    59: "error",   # stuck
    61: "paused",  # rain delay
    66: "error",   # failed to return to the charging station
    67: "error",   # mower overturned
}

# Human-readable descriptions (from the official app's notifications where
# available), exposed as the mower entity's `state_description` attribute.
MOW_STATE_DESCRIPTIONS = {
    0: "Idle / no active task",
    51: "Calibrate position",
    52: "Leaving dock",
    55: "Mowing",
    56: "Edge cutting",
    57: "Moving to another destination",
    59: "Mower stuck. Please move it to a flat open area and restart.",
    61: "Paused Raining",
    66: "Failed to return to the charging station. Please clear any obstacles.",
    67: "Mower overturned",
}

# Codes observed on real hardware but not yet decoded (they display as
# "unknown"): 58, 76, 77. To identify one, note the timestamp on the
# "Mower state (raw code)" sensor history and cross-check what the app
# showed / what the mower was doing at that moment, then move it into the
# maps above.
