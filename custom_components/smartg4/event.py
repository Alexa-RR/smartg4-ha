"""Wall-panel buttons as event entities.

Each key of a panel becomes an event entity that fires ``press`` when the
panel sends the command that key is programmed with. The hub reads every
panel's button table over the panel-programming protocol at startup (the same
data the vendor tool shows), so a press is matched to its key by the command
it emits (target module + channel/scene/etc.).

Use them in automations as triggers ("Button 3 pressed") without the vendor
software in the loop, and independently of what the key natively controls.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartG4ConfigEntry
from .const import DOMAIN, SIGNAL_PRESS
from .hub import SmartG4Hub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartG4ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SmartG4ButtonEvent] = []
    for device in hub.devices:
        address = device["address"]
        for button in hub.panel_buttons.get(address, []):
            entities.append(SmartG4ButtonEvent(hub, device, button))
    async_add_entities(entities)


class SmartG4ButtonEvent(EventEntity):
    """One panel key."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["press"]

    def __init__(self, hub: SmartG4Hub, device: dict, button: dict[str, Any]) -> None:
        self._hub = hub
        self._address: str = device["address"]
        self._index: int = button["index"]
        label = button.get("label") or ""
        self._attr_name = f"Button {self._index}" + (f" {label}" if label else "")
        self._attr_unique_id = (
            f"{DOMAIN}_{self._address.replace('.', '_')}_btn{self._index}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=device.get("remark") or f"Panel {self._address}",
            manufacturer="Smart-G4",
            model=device.get("type_name"),
            serial_number=device.get("mac") or None,
        )
        self._attr_extra_state_attributes = {
            "commands": button.get("commands", []),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_PRESS.format(self._address), self._handle_press
            )
        )

    @callback
    def _handle_press(self, index: int, info: dict[str, Any]) -> None:
        if index != self._index:
            return
        self._trigger_event("press", info)
        self.async_write_ha_state()
