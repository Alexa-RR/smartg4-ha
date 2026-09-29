"""Panel LED and LCD backlight intensity as number entities.

Each wall panel gets two sliders, 0-100 %: the key LED brightness and the
LCD backlight brightness. Setting one sends the live ``Panel_Control``
command (0xE3D8, type 14 / 13) so the change is immediate, then stores the
new level in the panel's settings record (0xE010/0xE012) so it survives a
power cycle. The initial value is read from that record at startup.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartG4ConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE
from .hub import SmartG4Hub

PARALLEL_UPDATES = 1

KINDS = (
    ("led", "LED brightness", "mdi:led-on"),
    ("backlight", "Backlight brightness", "mdi:brightness-6"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartG4ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SmartG4PanelLevel] = []
    for device in hub.devices:
        if not hub.is_panel(int(device["device_type"], 16)):
            continue
        for kind, name, icon in KINDS:
            entities.append(SmartG4PanelLevel(hub, device, kind, name, icon))
    async_add_entities(entities)


class SmartG4PanelLevel(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, hub: SmartG4Hub, device: dict, kind: str, name: str, icon: str
    ) -> None:
        self._hub = hub
        self._address: str = device["address"]
        self._kind = kind
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = (
            f"{DOMAIN}_{self._address.replace('.', '_')}_{kind}_level"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=device.get("remark") or f"Panel {self._address}",
            manufacturer="Smart-G4",
            model=device.get("type_name"),
            serial_number=device.get("mac") or None,
        )

    @property
    def native_value(self) -> float | None:
        levels = self._hub.panel_led.get(self._address)
        if not levels:
            return None
        return levels.get(self._kind)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self._address), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        await self._hub.async_set_panel_level(self._address, self._kind, int(value))
