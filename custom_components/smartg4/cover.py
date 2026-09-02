"""Shutters / curtains as cover entities.

A shutter is a pair of relay channels (up, down) that the module itself
interlocks and drops at the end of travel — which is why driving the raw
channels, exactly as the wall panels do, is the reliable way to move
them. Which pairs are shutters (and how long they take) is read from the
modules at setup; see pysmartg4/curtains.py.

There is no position feedback on the bus, so position is estimated from
the module's own configured travel time.
"""

from __future__ import annotations

import asyncio
import time

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SmartG4ConfigEntry
from .const import DOMAIN, SIGNAL_UPDATE
from .hub import SmartG4Hub

PARALLEL_UPDATES = 0

# A real shutter runs for tens of seconds. Relay reads that change within
# this window of a movement starting are the optimistic write and the
# module's polled/broadcast state racing at a movement edge — noise, not a
# genuine stop or reversal. Ignoring them stops the state from thrashing.
MIN_MOVE_TIME = 2.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartG4ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities = [
        SmartG4Cover(hub, device, group)
        for device in hub.devices
        for group in hub.curtains.get(device["address"], [])
    ]
    async_add_entities(entities)


class SmartG4Cover(CoverEntity, RestoreEntity):
    """One shutter: an up channel, a down channel and a travel time."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, hub: SmartG4Hub, device: dict, group: dict) -> None:
        self._hub = hub
        self._address: str = device["address"]
        self._up: int = group["up_channel"]
        self._down: int = group["down_channel"]
        self._travel: int = group["travel_time"]
        self._attr_name = hub.channel_name(self._address, self._up, strip_updown=True)
        self._attr_unique_id = (
            f"{DOMAIN}_{self._address.replace('.', '_')}_cover{group['group']}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            name=device.get("remark") or f"Module {self._address}",
            manufacturer="Smart-G4",
            model=device.get("type_name"),
        )
        # No position feedback exists on the bus: start unknown and track
        # movement against the module's own travel time.
        self._position: float | None = None
        self._moving: str | None = None
        self._started: float = 0.0
        self._target: float | None = None
        self._task: asyncio.Task | None = None
        # True while we are driving the relays ourselves, so the resulting
        # state echo on SIGNAL_UPDATE doesn't restart movement tracking.
        self._commanding: bool = False

    # -- state ---------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        if self._position is None:
            return None
        return int(round(self._position))

    @property
    def is_closed(self) -> bool | None:
        if self._position is None:
            return None
        return self._position <= 1

    @property
    def is_opening(self) -> bool:
        return self._moving == "open"

    @property
    def is_closing(self) -> bool:
        return self._moving == "close"

    async def async_added_to_hass(self) -> None:
        # Restore the last estimated position so a restart doesn't blank
        # the shutter out to "unknown" until its next full run.
        if (last := await self.async_get_last_state()) is not None:
            restored = last.attributes.get(ATTR_CURRENT_POSITION)
            if restored is not None:
                try:
                    self._position = float(restored)
                except (TypeError, ValueError):
                    self._position = None
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE.format(self._address), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        """Follow the relays: a wall press moves the shutter too."""
        if self._commanding:
            # Our own command drove these relays; we already track it.
            return
        up = self._hub.states.get((self._address, self._up))
        down = self._hub.states.get((self._address, self._down))
        moving = "open" if up else "close" if down else None
        if moving == self._moving:
            return
        if (
            self._moving is not None
            and time.monotonic() - self._started < MIN_MOVE_TIME
        ):
            # Too soon after this movement began to be a real change — the
            # relays are just settling. Ignore, or we thrash the estimate.
            return
        self._settle()
        if moving:
            self._start(moving, target=100.0 if moving == "open" else 0.0)
        self.async_write_ha_state()

    # -- movement ------------------------------------------------------------

    def _elapsed_position(self) -> float | None:
        if self._moving is None or self._position is None:
            return self._position
        moved = (time.monotonic() - self._started) / self._travel * 100
        if self._moving == "open":
            return min(100.0, self._position + moved)
        return max(0.0, self._position - moved)

    def _settle(self) -> None:
        """Freeze the estimate where the shutter actually is."""
        if self._moving is not None:
            self._position = self._elapsed_position()
        self._moving = None
        self._target = None
        if self._task:
            self._task.cancel()
            self._task = None

    def _start(self, direction: str, target: float) -> None:
        if self._position is None:
            # Unknown start: assume the far end, so a full run calibrates.
            self._position = 0.0 if direction == "open" else 100.0
        self._moving = direction
        self._started = time.monotonic()
        self._target = target
        self._task = self.hass.async_create_task(self._run_until_target())

    async def _run_until_target(self) -> None:
        """Stop at the requested position; a full run is left to the module."""
        try:
            while self._moving and self._target is not None:
                position = self._elapsed_position()
                if position is None:
                    return
                reached = (
                    position >= self._target - 0.5
                    if self._moving == "open"
                    else position <= self._target + 0.5
                )
                if reached:
                    if 0 < self._target < 100:
                        # Our own stop: suppress the resulting relay echo so
                        # _handle_update doesn't cancel this task mid-finish.
                        self._commanding = True
                        try:
                            await self._hub.async_set_channel(
                                self._address, self._up, False
                            )
                            await self._hub.async_set_channel(
                                self._address, self._down, False
                            )
                        finally:
                            self._commanding = False
                    self._position = self._target
                    self._moving = None
                    self._target = None
                    self._task = None
                    self.async_write_ha_state()
                    return
                self.async_write_ha_state()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    # -- commands ------------------------------------------------------------

    async def async_open_cover(self, **kwargs) -> None:
        self._settle()
        self._commanding = True
        try:
            await self._hub.async_set_channel(self._address, self._down, False)
            await self._hub.async_set_channel(self._address, self._up, True)
        finally:
            self._commanding = False
        self._start("open", 100.0)
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs) -> None:
        self._settle()
        self._commanding = True
        try:
            await self._hub.async_set_channel(self._address, self._up, False)
            await self._hub.async_set_channel(self._address, self._down, True)
        finally:
            self._commanding = False
        self._start("close", 0.0)
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs) -> None:
        self._settle()
        self._commanding = True
        try:
            await self._hub.async_set_channel(self._address, self._up, False)
            await self._hub.async_set_channel(self._address, self._down, False)
        finally:
            self._commanding = False
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs) -> None:
        target = float(kwargs[ATTR_POSITION])
        current = self._elapsed_position()
        if current is None:
            # Calibrate first: a full close gives a known reference point.
            await self.async_close_cover()
            return
        if abs(target - current) < 1:
            return
        if target > current:
            await self.async_open_cover()
        else:
            await self.async_close_cover()
        self._target = target
