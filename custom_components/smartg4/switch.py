"""Relay/dimmer channels as switch entities."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SmartG4ConfigEntry
from .const import CHANNEL_COUNTS, DOMAIN, SIGNAL_UPDATE
from .hub import SmartG4Hub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartG4ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SmartG4Switch] = []
    for device in hub.devices:
        channels = CHANNEL_COUNTS.get(int(device["device_type"], 16))
        if not channels:
            continue
        for channel in range(1, channels + 1):
            entities.append(SmartG4Switch(hub, device, channel))
    async_add_entities(entities)


class SmartG4Switch(SwitchEntity):
    """One relay/dimmer channel. HA's switch-as-light helper can re-type it."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: SmartG4Hub, device: dict, channel: int) -> None:
        self._hub = hub
        self._address: str = device["address"]
        self._channel = channel
        self._attr_name = f"Channel {channel}"
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

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self._address),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._hub.async_set_channel(self._address, self._channel, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._hub.async_set_channel(self._address, self._channel, False)
