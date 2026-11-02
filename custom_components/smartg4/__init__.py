"""Smart-G4 (S-BUS) integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

import voluptuous as vol

from .const import (
    CONF_CURTAINS,
    CONF_DEVICES,
    CONF_GATEWAY,
    CONF_PANEL_BUTTONS,
    DEFAULT_SCAN_DURATION,
    DOMAIN,
)
from .hub import SmartG4Hub
from .pysmartg4.discovery import discover, merge_device_lists

PLATFORMS = [
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SWITCH,
]

type SmartG4ConfigEntry = ConfigEntry[SmartG4Hub]

RESCAN_SCHEMA = vol.Schema(
    {vol.Optional("duration", default=DEFAULT_SCAN_DURATION): cv.positive_int}
)


async def _async_rescan_entry(
    hass: HomeAssistant, entry: SmartG4ConfigEntry, duration: float
) -> int:
    """Scan the bus on a loaded entry, merge, reload if new devices appeared."""
    found = await discover(entry.runtime_data.bus, duration=min(duration, 120))
    merged, new = merge_device_lists(
        entry.data[CONF_DEVICES], [d.as_dict() for d in found]
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICES: merged}
    )
    if new:
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return new


async def async_setup_entry(hass: HomeAssistant, entry: SmartG4ConfigEntry) -> bool:
    hub = SmartG4Hub(
        hass,
        entry.data[CONF_GATEWAY],
        entry.data[CONF_DEVICES],
        cached_curtains=entry.data.get(CONF_CURTAINS, {}),
        cached_panel_buttons=entry.data.get(CONF_PANEL_BUTTONS, {}),
    )
    await hub.async_start()
    entry.runtime_data = hub
    # Persist the shutter topology and panel button tables we just read
    # (merged with any cached fallback) so the next boot is stable even if
    # a module is briefly unreachable.
    updates = {}
    if hub.curtains != entry.data.get(CONF_CURTAINS, {}):
        updates[CONF_CURTAINS] = hub.curtains
    if hub.panel_buttons != entry.data.get(CONF_PANEL_BUTTONS, {}):
        updates[CONF_PANEL_BUTTONS] = hub.panel_buttons
    if updates:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **updates}
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Panels are read live in the background now that platforms exist, so a
    # slow or silent panel never delays (and no longer fails) entry setup.
    hub.start_panel_refresh(entry)

    async def rescan(call: ServiceCall) -> None:
        for loaded in hass.config_entries.async_loaded_entries(DOMAIN):
            await _async_rescan_entry(hass, loaded, call.data["duration"])

    if not hass.services.has_service(DOMAIN, "rescan"):
        hass.services.async_register(
            DOMAIN, "rescan", rescan, schema=RESCAN_SCHEMA
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartG4ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
