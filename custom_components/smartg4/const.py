"""Constants for the Smart-G4 (S-BUS) integration."""

DOMAIN = "smartg4"

CONF_GATEWAY = "gateway"
CONF_DEVICES = "devices"
CONF_SCAN_DURATION = "scan_duration"
# Last-known shutter topology, cached so a flaky boot (a module that
# fails to answer 0xDC23 that boot) doesn't demote a shutter back to two
# raw switch entities. address -> [curtain group dicts].
CONF_CURTAINS = "curtains"
# Last-known panel button tables (read live over the panel-programming
# protocol), cached so button event entities survive a boot on which a
# panel doesn't answer. address -> [{index, label, commands:[...]}].
CONF_PANEL_BUTTONS = "panel_buttons"

SIGNAL_PRESS = "smartg4_press_{}"

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


def channel_count_for(device_type: int) -> int:
    """Output channels for a module type.

    Live-confirmed CHANNEL_COUNTS win; otherwise the vendor catalog (397
    types from the Smart Cloud database) sizes modules we have never seen.
    Panels and non-module devices yield 0 so they get no channel entities.
    """
    if device_type in CHANNEL_COUNTS:
        return CHANNEL_COUNTS[device_type]
    from .pysmartg4.device_catalog import lookup  # noqa: PLC0415

    info = lookup(device_type)
    if info and (info.is_dimmer or info.is_relay):
        return info.channels
    return 0


def is_dimmer_type(device_type: int) -> bool:
    """True when the vendor catalog classes this module as a dimmer, so its
    channels become light entities with brightness instead of switches."""
    from .pysmartg4.device_catalog import lookup  # noqa: PLC0415

    info = lookup(device_type)
    return bool(info and info.is_dimmer)

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

# Gateway auto-discovery: how long to listen for gateways relaying bus
# traffic when the user doesn't know (or wants to change) the gateway IP.
CONF_GATEWAY_SEARCH = "gateway_search"
DEFAULT_GATEWAY_SEARCH = 5
