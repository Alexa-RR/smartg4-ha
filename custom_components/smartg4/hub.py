"""Bus connection, live state cache, and HA event bridge."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMAND_OPCODES,
    EVENT_COMMAND,
    POLL_INTERVAL,
    POLLABLE_TYPES,
    SIGNAL_UPDATE,
)
from .pysmartg4 import SmartG4Bus, opcode_name
from .pysmartg4.packet import DeviceAddress, Packet

_LOGGER = logging.getLogger(__name__)


class SmartG4Hub:
    """One S-BUS connection shared by all entities of a config entry."""

    def __init__(
        self, hass: HomeAssistant, gateway: str, devices: list[dict[str, Any]]
    ) -> None:
        self.hass = hass
        self.devices = devices
        self.bus = SmartG4Bus(gateway=gateway)
        # (address str, channel number) -> bool | None
        self.states: dict[tuple[str, int], bool | None] = {}
        self._unsubscribe = None
        self._poll_task: asyncio.Task | None = None

    async def async_start(self) -> None:
        await self.bus.connect()
        self._unsubscribe = self.bus.on_packet(self._on_packet)
        self._poll_task = self.hass.loop.create_task(self._poll_loop())

    async def async_stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        self.bus.close()

    # -- state updates -------------------------------------------------------

    @callback
    def _on_packet(self, packet: Packet, parsed: dict[str, Any] | None) -> None:
        if packet.source == self.bus.sender:
            return
        source = str(packet.source)

        if parsed and packet.opcode == 0xEFFF:
            # Periodic scene/channel status broadcast (0x07D3 modules).
            for channel in parsed["channels"]:
                self.states[(source, channel["number"])] = channel["status"]
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(source))
        elif parsed and packet.opcode == 0x0034:
            # ReadChannelStatus response: list of levels.
            for channel in parsed["channels"]:
                self.states[(source, channel["number"])] = channel["level"] > 0
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(source))
        elif parsed and packet.opcode == 0x0032:
            # SingleChannelControl response (anyone's command, incl. panels).
            if parsed.get("success"):
                self.states[(source, parsed["channel"])] = parsed["level"] > 0
                async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(source))

        if packet.opcode in COMMAND_OPCODES:
            # Surface bus commands (wall-panel presses, scene calls...) so
            # automations can react without vendor software in the loop.
            self.hass.bus.async_fire(
                EVENT_COMMAND,
                {
                    "source": source,
                    "target": str(packet.target),
                    "opcode": f"0x{packet.opcode:04X}",
                    "command": COMMAND_OPCODES[packet.opcode],
                    "name": opcode_name(packet.opcode),
                    "data": parsed if parsed is not None else packet.payload.hex(),
                },
            )

    # -- polling -------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll modules that answer 0x0033; broadcast-only modules push."""
        targets = [
            device["address"]
            for device in self.devices
            if int(device["device_type"], 16) in POLLABLE_TYPES
        ]
        while True:
            for address in targets:
                try:
                    await self.bus.read_channels(DeviceAddress.parse(address))
                except (TimeoutError, asyncio.TimeoutError):
                    _LOGGER.debug("no channel-status reply from %s", address)
                except Exception:  # noqa: BLE001 - keep the loop alive
                    _LOGGER.exception("error polling %s", address)
                # Small gap so unicast replies don't collide on RS-485.
                await asyncio.sleep(0.3)
            await asyncio.sleep(POLL_INTERVAL)

    # -- control -------------------------------------------------------------

    async def async_set_channel(self, address: str, channel: int, on: bool) -> None:
        target = DeviceAddress.parse(address)
        level = 100 if on else 0
        try:
            await self.bus.set_channel(target, channel, level)
        except (TimeoutError, asyncio.TimeoutError):
            # Some modules (0x07D3) don't ack 0x0031; they broadcast the new
            # state via 0xEFFF within seconds, which corrects us if needed.
            _LOGGER.debug("no 0x0032 ack from %s ch %s", address, channel)
        self.states[(address, channel)] = on
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(address))
