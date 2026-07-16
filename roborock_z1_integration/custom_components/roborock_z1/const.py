"""Constants for the Roborock Z1 Mower integration."""

DOMAIN = "roborock_z1"

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"

# The Z1 reports pv=1.0: it speaks Roborock's V1 RPC protocol (the classic
# vacuum protocol), which python-roborock explicitly rejects for non-vacuum
# devices. We drive it directly with V1 RPC methods instead. The method names
# below are educated guesses borrowed from the vacuum command set — if a
# command has no effect, watch the debug log for the device's RPC error and
# adjust here.
METHOD_GET_STATUS = "get_status"
METHOD_START = "app_start"
METHOD_PAUSE = "app_pause"
METHOD_RESUME = "app_resume"
METHOD_DOCK = "app_charge"

# Safety-net poll: re-request all DPS values this often.
STATUS_POLL_SECONDS = 60

# Best-effort mapping of the mower's `mow_state` DPS (123) to HA activities.
# These values are not yet documented upstream
# (https://github.com/Python-roborock/python-roborock/issues/757) — adjust as
# real values are discovered.
# Mapping of the Z1's mow_state codes to HA activities, decoded on a real
# device (fw A.03.0894_CE). All job-active states map to "mowing" since HA's
# LawnMowerActivity has no finer distinction; the true sub-state is always
# visible on the "Mower state (raw code)" diagnostic sensor.
MOW_STATE_TO_ACTIVITY = {
    0: "docked",   # no active task — reported during the return trip AND while docked
    51: "docked",  # task ended: some areas could not be reached
    52: "mowing",  # leaving the dock
    55: "mowing",  # actively cutting
    56: "mowing",  # edge cutting
    57: "mowing",  # driving to the next zone
    58: "paused",  # task paused
    59: "error",   # stuck
    61: "paused",  # rain delay
    66: "error",   # failed to return to the charging station
}

# Human-readable descriptions (from the official app's notifications where
# available), exposed as the mower entity's `state_description` attribute.
MOW_STATE_DESCRIPTIONS = {
    0: "Idle / no active task",
    51: "Some areas could not be reached. Mowing ended.",
    52: "Leaving the dock",
    55: "Mowing",
    56: "Edge cutting",
    57: "Driving to the next zone",
    58: "Paused",
    59: "Mower stuck. Please move it to a flat open area and restart.",
    61: "It's raining. Resume mowing after Rain Protection ends.",
    66: "Failed to return to the charging station. Please clear any obstacles.",
}

# Codes observed on real hardware but not yet decoded (they display as
# "unknown"): 76, 77. To identify one, note the timestamp on the
# "Mower state (raw code)" sensor history and cross-check what the app
# showed / what Hans was doing at that moment, then move it into the map.

# Config-entry key caching the account's home data (devices/products), so the
# rate-limited home-data endpoint is only called on first setup.
CONF_HOME_DATA = "home_data"

# The Z1 answers the V1 "get_status" RPC with vacuum-style field names.
# Map them onto python-roborock's MowerStatus field names when merging.
RPC_RESULT_KEY_MAP = {
    "state": "mow_state",
}
