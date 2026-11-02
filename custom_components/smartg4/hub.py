"""Bus connection, live state cache, and HA event bridge."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMAND_OPCODES,
    CONF_PANEL_BUTTONS,
    EVENT_COMMAND,
    POLL_INTERVAL,
    POLLABLE_TYPES,
    SIGNAL_PRESS,
    SIGNAL_UPDATE,
    channel_count_for,
)
from .pysmartg4 import SmartG4Bus, opcode_name
from .pysmartg4.curtains import read_curtain_config
from .pysmartg4.device_catalog import lookup as catalog_lookup
from .pysmartg4.naming import read_channel_names
from .pysmartg4.packet import DeviceAddress, Packet
from .pysmartg4.panel_control import (
    persist_led_levels,
    read_led_levels,
    set_backlight_level,
    set_led_level,
)
from .pysmartg4.vendor_program import (
    READ_RESPONSE as PANEL_READ_RESPONSE,
    build_read_frame as build_panel_read_frame,
    parse_response as parse_panel_response,
)

# Panels answer programming/config frames (0xE000 reads, 0xE010/0xE012 LED)
# only from the vendor PC identity: device 254 on the panel's own subnet,
# announcing the virtual type 0xFFFE. The integration's own bus address is
# ignored by panels (verified live), so these reads must use this identity.
PROGRAM_SOURCE_DEVICE = 254
PROGRAM_SOURCE_TYPE = 0xFFFE


# Panel key function codes (KeyFunType) <-> the bus opcode a press emits, and
# which parsed fields must match the stored command's p1/p2.
_FUNCTION_FOR_OPCODE: dict[int, tuple[int, tuple[str, ...]]] = {
    0x0031: (0x59, ("channel",)),          # single channel: p1 = channel
    0x0002: (0x55, ("area", "scene")),     # scene: p1 = area, p2 = scene
    0x001A: (0x56, ("area", "sequence")),  # sequence: p1 = area, p2 = seq
    0xE01C: (0x58, ("switch",)),           # universal switch: p1 = switch
    0xE3E0: (0x5C, ()),                    # curtain: match on target only
    0xE3D8: (0x5F, ()),                    # panel control: target only
}
_PANEL_TYPES_ALWAYS = {0x0095, 0x0119}
_PANEL_BUTTON_LIMIT = {0x0095: 16}

_LOGGER = logging.getLogger(__name__)


class SmartG4Hub:
    """One S-BUS connection shared by all entities of a config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        gateway: str,
        devices: list[dict[str, Any]],
        cached_curtains: dict[str, list[dict[str, Any]]] | None = None,
        cached_panel_buttons: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        # address -> [{index, label, commands:[{function,target,p1,p2,p3}]}]
        self.panel_buttons: dict[str, list[dict[str, Any]]] = {}
        # address -> {"led": 0-100, "backlight": 0-100} from the panel's
        # stored settings record (0xE010); updated on every set.
        self.panel_led: dict[str, dict[str, int]] = {}
        self._cached_panel_buttons = cached_panel_buttons or {}
        self.hass = hass
        self.devices = devices
        self.bus = SmartG4Bus(gateway=gateway)
        # (address str, channel number) -> bool | None
        self.states: dict[tuple[str, int], bool | None] = {}
        # (address, channel) -> last known level 0-100 (dimmer brightness)
        self.levels: dict[tuple[str, int], int] = {}
        # address -> [{group, up_channel, down_channel, travel_time}]
        self.curtains: dict[str, list[dict[str, Any]]] = {}
        # Last-known shutter topology from the config entry; used as a
        # fallback when a module doesn't answer 0xDC23 this boot so entity
        # topology stays stable across restarts.
        self._cached_curtains = cached_curtains or {}
        # (address, channel) -> name stored in the module
        self.names: dict[tuple[str, int], str] = {}
        self._unsubscribe = None
        self._poll_task: asyncio.Task | None = None
        self._panel_task: asyncio.Task | None = None

    async def async_start(self) -> None:
        await self.bus.connect()
        self._unsubscribe = self.bus.on_packet(self._on_packet)
        await self._async_load_config()
        # Panel button and LED reads are slow (many 0xE000/0xE010 exchanges,
        # each with its own timeout) and must NOT run inside config-entry
        # setup: Home Assistant cancels an entry that takes too long to set
        # up ("Bootstrap stage 2 timeout"). Seed the button tables from the
        # cached copy so the event entities exist immediately, then read the
        # panels live in the background (see start_panel_refresh).
        self._seed_panel_buttons_from_cache()
        self._poll_task = self.hass.loop.create_task(self._poll_loop())

    # -- panel LED / backlight -----------------------------------------------

    def _vendor_kw(self, target: DeviceAddress) -> dict[str, Any]:
        return {
            "source": DeviceAddress(target.subnet, PROGRAM_SOURCE_DEVICE),
            "source_type": PROGRAM_SOURCE_TYPE,
            "source_ip": self.bus._local_ip,  # noqa: SLF001
            "retries": 1,
        }

    def _seed_panel_buttons_from_cache(self) -> None:
        """Populate button tables from the cached copy without bus I/O.

        Lets the event entities be created at setup time; the live read in
        the background then fills in / corrects the command mappings.
        """
        for device in self.devices:
            device_type = int(device["device_type"], 16)
            if not self.is_panel(device_type):
                continue
            address = device["address"]
            cached = self._cached_panel_buttons.get(address)
            if cached:
                self.panel_buttons[address] = cached
            else:
                self.panel_buttons[address] = [
                    {"index": i, "label": "", "commands": []}
                    for i in range(1, self.panel_button_count(device_type) + 1)
                ]

    def start_panel_refresh(self, entry) -> None:
        """Kick the background live read of panel buttons + LED levels.

        Called after the entry's platforms are set up, so the dispatcher
        update it fires lands on entities that already exist.
        """
        self._panel_task = self.hass.loop.create_task(
            self._async_refresh_panels(entry)
        )

    async def _async_refresh_panels(self, entry) -> None:
        try:
            await self._async_load_panel_buttons()
            await self._async_load_panel_led()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background best effort
            _LOGGER.exception("smartg4: background panel read failed")
            return
        if self.panel_buttons != entry.data.get(CONF_PANEL_BUTTONS, {}):
            self.hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_PANEL_BUTTONS: self.panel_buttons},
            )
        for address in list(self.panel_buttons) + list(self.panel_led):
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(address))

    async def _async_load_panel_led(self) -> None:
        """Read each panel's stored LED / backlight levels (0xE010)."""
        for device in self.devices:
            if not self.is_panel(int(device["device_type"], 16)):
                continue
            address = device["address"]
            target = DeviceAddress.parse(address)
            try:
                levels = await read_led_levels(
                    self.bus, target, **self._vendor_kw(target)
                )
            except Exception:  # noqa: BLE001 - best effort
                levels = None
            if levels:
                self.panel_led[address] = {
                    "led": levels["led"], "backlight": levels["backlight"],
                }
            else:
                _LOGGER.debug("%s: no LED level answer (0xE010)", address)

    async def async_set_panel_level(self, address: str, kind: str, level: int) -> None:
        """Set a panel's LED ('led') or LCD ('backlight') brightness, 0-100.

        Live first (0xE3D8 type 14 / 13) so the change shows at once, then
        persisted into the panel's settings record (0xE012) so it survives a
        power cycle. State is updated optimistically either way.
        """
        target = DeviceAddress.parse(address)
        level = max(0, min(100, int(level)))
        setter = set_led_level if kind == "led" else set_backlight_level
        acked = await setter(self.bus, target, level)
        if not acked:
            _LOGGER.debug("%s: no 0xE3D9 ack for %s level", address, kind)
        try:
            stored = await persist_led_levels(
                self.bus, target, **{kind: level}, **self._vendor_kw(target)
            )
        except Exception:  # noqa: BLE001
            stored = False
        if not stored:
            _LOGGER.debug("%s: %s level not persisted (0xE012)", address, kind)
        self.panel_led.setdefault(address, {})[kind] = level
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(address))

    # -- panels --------------------------------------------------------------

    @staticmethod
    def is_panel(device_type: int) -> bool:
        if device_type in _PANEL_TYPES_ALWAYS:
            return True
        info = catalog_lookup(device_type)
        return bool(info and info.is_panel)

    @staticmethod
    def panel_button_count(device_type: int) -> int:
        if device_type in _PANEL_BUTTON_LIMIT:
            return _PANEL_BUTTON_LIMIT[device_type]
        info = catalog_lookup(device_type)
        if info and info.channels:
            return info.channels
        return 8

    async def _read_panel_button(
        self, target: DeviceAddress, button: int, page: int = 1,
        timeout: float = 2.0, retries: int = 2,
    ) -> dict[str, Any] | None:
        """One 0xE000 read (capture-free vendor protocol); None if silent."""
        frame = build_panel_read_frame(
            button, page, target,
            source=DeviceAddress(target.subnet, PROGRAM_SOURCE_DEVICE),
            source_type=PROGRAM_SOURCE_TYPE,
            source_ip=self.bus._local_ip,  # noqa: SLF001
        )
        for _ in range(retries):
            future: asyncio.Future = self.hass.loop.create_future()

            def on_raw(data: bytes, _addr) -> None:
                if future.done():
                    return
                parsed = parse_panel_response(data)
                if (
                    parsed
                    and parsed.get("opcode") == PANEL_READ_RESPONSE
                    and parsed.get("button") == button
                    and parsed.get("source") == str(target)
                ):
                    future.set_result(parsed)

            unsubscribe = self.bus.on_raw(on_raw)
            try:
                self.bus.send_raw(frame)
                return await asyncio.wait_for(future, timeout)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            finally:
                unsubscribe()
        return None

    async def _async_load_panel_buttons(self) -> None:
        """Read every panel's button table so presses map to buttons.

        Uses the panel-programming protocol (0xE000) — the same data the
        vendor tool shows. A panel that doesn't answer this boot keeps its
        cached table so its event entities stay stable.
        """
        for device in self.devices:
            device_type = int(device["device_type"], 16)
            if not self.is_panel(device_type):
                continue
            address = device["address"]
            target = DeviceAddress.parse(address)
            buttons: list[dict[str, Any]] = []
            answered = False
            for index in range(1, self.panel_button_count(device_type) + 1):
                commands = []
                # Pages are 1-based on the wire (verified live); only the
                # first entry is field-proven, later ones are best effort.
                for page in range(1, 9):
                    entry = await self._read_panel_button(target, index, page)
                    if entry is None:
                        break
                    answered = True
                    fn = entry.get("function", 0)
                    if fn in (0, 0xFF):
                        break
                    commands.append(
                        {
                            "function": fn,
                            "target": entry["target"],
                            "p1": entry["p1"],
                            "p2": entry["p2"],
                            "p3": entry["p3"],
                        }
                    )
                buttons.append({"index": index, "label": "", "commands": commands})
            if answered:
                # Keep labels we may have cached (labels come from a
                # different read; the live table only has commands).
                cached = {b["index"]: b for b in self._cached_panel_buttons.get(address, [])}
                for b in buttons:
                    if not b["label"] and cached.get(b["index"], {}).get("label"):
                        b["label"] = cached[b["index"]]["label"]
                self.panel_buttons[address] = buttons
                _LOGGER.debug("%s: %d button(s) read live", address, len(buttons))
            elif self._cached_panel_buttons.get(address):
                self.panel_buttons[address] = self._cached_panel_buttons[address]
                _LOGGER.warning(
                    "%s: panel did not answer button reads this boot — "
                    "reusing cached table", address,
                )
            else:
                # Still expose the keys as events; matching uses the
                # command's target only until a table is read.
                self.panel_buttons[address] = [
                    {"index": i, "label": "", "commands": []}
                    for i in range(1, self.panel_button_count(device_type) + 1)
                ]

    def _match_button(
        self, address: str, packet: Packet, parsed: dict[str, Any] | None
    ) -> int | None:
        """Which button of `address` sends this command, if we know."""
        spec = _FUNCTION_FOR_OPCODE.get(packet.opcode)
        if not spec or parsed is None:
            return None
        function, fields = spec
        target = str(packet.target)
        for button in self.panel_buttons.get(address, []):
            for cmd in button["commands"]:
                if cmd["function"] != function or cmd["target"] != target:
                    continue
                want = [cmd["p1"], cmd["p2"]][: len(fields)]
                if all(parsed.get(f) == w for f, w in zip(fields, want)):
                    return button["index"]
        return None

    async def _async_load_config(self) -> None:
        """Ask each output module for its channel names and shutter pairs."""
        for device in self.devices:
            channels = channel_count_for(int(device["device_type"], 16))
            if not channels:
                continue
            address = device["address"]
            target = DeviceAddress.parse(address)
            try:
                names = await read_channel_names(self.bus, target, channels)
            except Exception:  # noqa: BLE001 - config reads are best-effort
                names = []
            for index, name in enumerate(names, start=1):
                if name:
                    self.names[(address, index)] = name
            try:
                groups = await read_curtain_config(self.bus, target)
            except Exception:  # noqa: BLE001
                groups = []
            if groups:
                self.curtains[address] = [
                    self._orient_group(address, g.as_dict()) for g in groups
                ]
                _LOGGER.debug(
                    "%s: %d shutter group(s) configured", address, len(groups)
                )
            elif self._cached_curtains.get(address):
                # The module didn't answer this boot. Keep its known
                # shutters rather than silently turning them into switches.
                self.curtains[address] = self._cached_curtains[address]
                _LOGGER.warning(
                    "%s: no shutter config answer this boot — reusing "
                    "%d cached shutter group(s)",
                    address,
                    len(self.curtains[address]),
                )

    # Channel-name direction markers. A shutter's two relays are labelled
    # by the module itself (e.g. "Drape Office U" / "... D", Hebrew עולה /
    # יורד). The default group layout assumes up=odd/down=even, but some
    # modules are wired the other way — and not even uniformly (1.107 has
    # both orientations), so the label is the only reliable source.
    _UP_MARKERS = (" u", " up", "עולה", "עליה", "עלייה")
    _DOWN_MARKERS = (" d", " down", "יורד", "ירידה")

    @classmethod
    def _channel_direction(cls, name: str | None) -> str | None:
        """'up', 'down', or None from a channel's own label."""
        if not name:
            return None
        low = name.lower()
        for marker in cls._DOWN_MARKERS:
            if low.endswith(marker) or marker in name:
                return "down"
        for marker in cls._UP_MARKERS:
            if low.endswith(marker) or marker in name:
                return "up"
        return None

    def _orient_group(self, address: str, group: dict[str, Any]) -> dict[str, Any]:
        """Fix a shutter's up/down channels using the module's own labels.

        Falls back to the incoming (up=odd) layout when the labels are
        silent, and to a cached orientation when even the names are
        missing this boot, so a nameless read never flips a shutter.
        """
        up, down = group["up_channel"], group["down_channel"]
        up_dir = self._channel_direction(self.names.get((address, up)))
        down_dir = self._channel_direction(self.names.get((address, down)))
        if (up_dir == "down" and down_dir in ("up", None)) or (
            down_dir == "up" and up_dir is None
        ):
            up, down = down, up
        elif up_dir is None and down_dir is None:
            cached = next(
                (
                    c
                    for c in self._cached_curtains.get(address, [])
                    if {c["up_channel"], c["down_channel"]} == {up, down}
                ),
                None,
            )
            if cached:
                up, down = cached["up_channel"], cached["down_channel"]
        return {**group, "up_channel": up, "down_channel": down}

    def channel_name(
        self, address: str, channel: int, strip_updown: bool = False
    ) -> str:
        """The module's own name for a channel, else a numbered fallback."""
        name = self.names.get((address, channel))
        if not name:
            return f"Channel {channel}"
        if strip_updown:
            # "Drape Salon U" and "... D" are one shutter, not two entities.
            for suffix in (" U", " D", " Up", " Down"):
                if name.endswith(suffix):
                    return name[: -len(suffix)].strip()
        return name

    def is_curtain_channel(self, address: str, channel: int) -> bool:
        return any(
            channel in (g["up_channel"], g["down_channel"])
            for g in self.curtains.get(address, [])
        )

    async def async_stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._panel_task:
            self._panel_task.cancel()
            self._panel_task = None
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
                self.levels[(source, channel["number"])] = channel["level"]
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(source))
        elif parsed and packet.opcode == 0x0032:
            # SingleChannelControl response (anyone's command, incl. panels).
            if parsed.get("success"):
                self.states[(source, parsed["channel"])] = parsed["level"] > 0
                self.levels[(source, parsed["channel"])] = parsed["level"]
                async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(source))

        if packet.opcode in COMMAND_OPCODES and source in self.panel_buttons:
            # A wall panel sent a command: resolve which key and fire its
            # event entity (see event.py).
            button = self._match_button(source, packet, parsed)
            if button is not None:
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_PRESS.format(source),
                    button,
                    {
                        "target": str(packet.target),
                        "opcode": f"0x{packet.opcode:04X}",
                        "command": COMMAND_OPCODES[packet.opcode],
                        "data": parsed,
                    },
                )

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
        await self.async_set_level(address, channel, 100 if on else 0)

    async def async_set_level(
        self, address: str, channel: int, level: int, transition: int = 0
    ) -> None:
        """Set a channel to 0-100 % (dimmers ramp over `transition` seconds)."""
        target = DeviceAddress.parse(address)
        level = max(0, min(100, int(level)))
        try:
            await self.bus.set_channel(target, channel, level, transition)
        except (TimeoutError, asyncio.TimeoutError):
            # Some modules (0x07D3) don't ack 0x0031; they broadcast the new
            # state via 0xEFFF within seconds, which corrects us if needed.
            _LOGGER.debug("no 0x0032 ack from %s ch %s", address, channel)
        self.states[(address, channel)] = level > 0
        self.levels[(address, channel)] = level
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(address))
