"""Smart-G4 (S-BUS) integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICES, CONF_GATEWAY
from .hub import SmartG4Hub

PLATFORMS = [Platform.SWITCH]

type SmartG4ConfigEntry = ConfigEntry[SmartG4Hub]


async def async_setup_entry(hass: HomeAssistant, entry: SmartG4ConfigEntry) -> bool:
    hub = SmartG4Hub(hass, entry.data[CONF_GATEWAY], entry.data[CONF_DEVICES])
    await hub.async_start()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartG4ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
