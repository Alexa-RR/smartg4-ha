"""Dimmer channels as light entities with brightness.

A module is a dimmer when the vendor catalog (Smart Cloud's own device table)
classes its type as a "scene controller"/dimmer. Its channels are driven with
SingleChannelControl (0x0031: channel, level 0-100, ramp seconds), the same
opcode relays use — only the level range differs. Relay modules keep their
switch entities (switch.py).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartG4ConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE, channel_count_for, is_dimmer_type
from .hub import SmartG4Hub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartG4ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SmartG4Light] = []
    for device in hub.devices:
        device_type = int(device["device_type"], 16)
        if not is_dimmer_type(device_type):
            continue
        for channel in range(1, channel_count_for(device_type) + 1):
            if hub.is_curtain_channel(device["address"], channel):
                continue
            entities.append(SmartG4Light(hub, device, channel))
    async_add_entities(entities)


class SmartG4Light(LightEntity):
    """One dimmer channel: on/off plus 0-100 % level, optional ramp."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.TRANSITION

    def __init__(self, hub: SmartG4Hub, device: dict, channel: int) -> None:
        self._hub = hub
        self._address: str = device["address"]
        self._channel = channel
        self._attr_name = hub.channel_name(self._address, channel)
        self._attr_unique_id = (
            f"{DOMAIN}_{self._address.replace('.', '_')}_ch{channel}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=device.get("remark") or f"Module {self._address}",
            manufacturer="Smart-G4",
            model=device.get("type_name"),
            serial_number=device.get("mac") or None,
        )

    @property
    def is_on(self) -> bool | None:
        return self._hub.states.get((self._address, self._channel))

    @property
    def brightness(self) -> int | None:
        level = self._hub.levels.get((self._address, self._channel))
        if level is None:
            return None
        return round(level * 255 / 100)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self._address), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            level = round(int(kwargs[ATTR_BRIGHTNESS]) * 100 / 255)
        else:
            # Restore the last non-zero level; default to full.
            last = self._hub.levels.get((self._address, self._channel)) or 0
            level = last if last > 0 else 100
        transition = int(kwargs.get(ATTR_TRANSITION, 0))
        await self._hub.async_set_level(
            self._address, self._channel, max(1, level), transition
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        transition = int(kwargs.get(ATTR_TRANSITION, 0))
        await self._hub.async_set_level(self._address, self._channel, 0, transition)
