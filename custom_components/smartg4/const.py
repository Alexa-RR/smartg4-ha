"""Constants for the Smart-G4 (S-BUS) integration."""

DOMAIN = "smartg4"

CONF_GATEWAY = "gateway"
CONF_DEVICES = "devices"
CONF_SCAN_DURATION = "scan_duration"

DEFAULT_GATEWAY = "255.255.255.255"
DEFAULT_SCAN_DURATION = 30

SIGNAL_UPDATE = "smartg4_update_{}"
EVENT_COMMAND = "smartg4_command"

# Channel counts by S-BUS device-type code, confirmed live on the bus.
# 0x01B8 answers 0x0033 channel reads; 0x07D3 broadcasts 0xEFFF status.
CHANNEL_COUNTS = {
    0x01B8: 12,  # 12-channel relay module
    0x07D3: 3,   # 3-channel relay/dimmer module
}

# Modules that answer ReadChannelStatus (0x0033) and can be polled.
POLLABLE_TYPES = {0x01B8}

POLL_INTERVAL = 30  # seconds

# Bus commands worth exposing as HA events (wall-panel presses, scenes...).
COMMAND_OPCODES = {
    0x0031: "single_channel",
    0x0002: "scene",
    0x001A: "sequence",
    0xE01C: "universal_switch",
    0xE3E0: "curtain",
    0xE3D8: "panel_control",
}
