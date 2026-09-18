"""Config flow: find or name the gateway, scan the bus, store what was found.

The gateway can be chosen two ways, at setup and again later:

* **Discover** — no IP needed. A scan probe is broadcast on the LAN and
  every host that relays genuine module frames back is offered as a
  gateway (plus its subnet-broadcast address).
* **Manual** — type the gateway or broadcast IP.

After setup both are reachable from the entry's *Configure* dialog and
from *Reconfigure*; the chosen gateway is verified with a bus scan before
it replaces the stored one, and the entry reloads.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DEVICES,
    CONF_GATEWAY,
    CONF_GATEWAY_SEARCH,
    CONF_SCAN_DURATION,
    DEFAULT_GATEWAY,
    DEFAULT_GATEWAY_SEARCH,
    DEFAULT_SCAN_DURATION,
    DOMAIN,
)
from .pysmartg4 import SmartG4Bus
from .pysmartg4.discovery import (
    DiscoveredGateway,
    discover,
    discover_gateways,
    merge_device_lists,
)

SCAN_DURATION_FIELD = vol.All(vol.Coerce(int), vol.Range(min=5, max=120))
GATEWAY_SEARCH_FIELD = vol.All(vol.Coerce(int), vol.Range(min=3, max=60))

STEP_DISCOVER_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_GATEWAY_SEARCH, default=DEFAULT_GATEWAY_SEARCH
        ): GATEWAY_SEARCH_FIELD,
    }
)

GATEWAY_MENU = ["discover", "manual"]


def _title(gateway: str) -> str:
    return f"Smart-G4 ({gateway})"


async def _async_scan_bus(
    gateway: str, duration: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Bind, scan the bus through `gateway`, return (devices, error_key)."""
    bus = SmartG4Bus(gateway=gateway)
    try:
        await bus.connect()
    except OSError:
        return [], "cannot_connect"
    try:
        devices = await discover(bus, duration=duration)
    finally:
        bus.close()
    if not devices:
        return [], "no_devices"
    return [d.as_dict() for d in devices], None


async def _async_find_gateways(
    duration: int,
) -> tuple[list[DiscoveredGateway], str | None]:
    """Listen on the limited-broadcast address: works with no IP known."""
    bus = SmartG4Bus(gateway=DEFAULT_GATEWAY)
    try:
        await bus.connect()
    except OSError:
        return [], "cannot_connect"
    try:
        gateways = await discover_gateways(bus, duration=duration)
    finally:
        bus.close()
    if not gateways:
        return [], "no_gateways"
    return gateways, None


def _gateway_options(gateways: list[DiscoveredGateway]) -> list[SelectOptionDict]:
    """Every discovered gateway, then one broadcast entry per subnet."""
    options: list[SelectOptionDict] = []
    for gateway in gateways:
        count = len(gateway.devices)
        plural = "s" if count != 1 else ""
        options.append(
            SelectOptionDict(
                value=gateway.ip, label=f"{gateway.ip} — {count} module{plural} heard"
            )
        )
    seen: set[str] = set()
    for gateway in gateways:
        if gateway.broadcast in seen:
            continue
        seen.add(gateway.broadcast)
        options.append(
            SelectOptionDict(
                value=gateway.broadcast,
                label=f"{gateway.broadcast} — subnet broadcast (all gateways)",
            )
        )
    return options


class _GatewaySteps:
    """Steps shared by setup, reconfigure and the options dialog.

    `discover` finds gateways and hands over to `select`; `manual` takes a
    typed address. Both end in `_async_finish`, which the concrete flow
    implements: it returns a flow result, or an error key to show on the
    form the user just submitted.
    """

    _gateways: list[DiscoveredGateway] = []

    async def _async_finish(
        self, gateway: str, scan_duration: int
    ) -> ConfigFlowResult | str:
        raise NotImplementedError

    def _current_gateway(self) -> str:
        return DEFAULT_GATEWAY

    async def async_step_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._gateways, error = await _async_find_gateways(
                user_input[CONF_GATEWAY_SEARCH]
            )
            if error is None:
                return await self.async_step_select()
            errors["base"] = error
        return self.async_show_form(
            step_id="discover", data_schema=STEP_DISCOVER_SCHEMA, errors=errors
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_finish(
                user_input[CONF_GATEWAY], user_input[CONF_SCAN_DURATION]
            )
            if not isinstance(result, str):
                return result
            errors["base"] = result
        options = _gateway_options(self._gateways)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_GATEWAY, default=options[0]["value"]
                ): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                ),
                vol.Required(
                    CONF_SCAN_DURATION, default=DEFAULT_SCAN_DURATION
                ): SCAN_DURATION_FIELD,
            }
        )
        return self.async_show_form(
            step_id="select",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(self._gateways))},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_finish(
                user_input[CONF_GATEWAY].strip(), user_input[CONF_SCAN_DURATION]
            )
            if not isinstance(result, str):
                return result
            errors["base"] = result
        schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY, default=self._current_gateway()): str,
                vol.Required(
                    CONF_SCAN_DURATION, default=DEFAULT_SCAN_DURATION
                ): SCAN_DURATION_FIELD,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )


class SmartG4ConfigFlow(_GatewaySteps, ConfigFlow, domain=DOMAIN):
    """Pick the gateway (found or typed), then discover the bus."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SmartG4OptionsFlow":
        return SmartG4OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=GATEWAY_MENU)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reconfigure", menu_options=GATEWAY_MENU
        )

    def _current_gateway(self) -> str:
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry().data[CONF_GATEWAY]
        return DEFAULT_GATEWAY

    async def _async_finish(
        self, gateway: str, scan_duration: int
    ) -> ConfigFlowResult | str:
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            for other in self._async_current_entries(include_ignore=False):
                if (
                    other.entry_id != entry.entry_id
                    and other.data.get(CONF_GATEWAY) == gateway
                ):
                    raise AbortFlow("already_configured")
            devices, error = await _async_scan_bus(gateway, scan_duration)
            if error:
                return error
            merged, _ = merge_device_lists(entry.data[CONF_DEVICES], devices)
            return self.async_update_reload_and_abort(
                entry,
                title=_title(gateway),
                data={**entry.data, CONF_GATEWAY: gateway, CONF_DEVICES: merged},
            )

        self._async_abort_entries_match({CONF_GATEWAY: gateway})
        devices, error = await _async_scan_bus(gateway, scan_duration)
        if error:
            return error
        return self.async_create_entry(
            title=_title(gateway),
            data={CONF_GATEWAY: gateway, CONF_DEVICES: devices},
        )


class SmartG4OptionsFlow(_GatewaySteps, OptionsFlow):
    """Configure → rescan the bus, or find / change the gateway."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["rescan", *GATEWAY_MENU]
        )

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            from . import _async_rescan_entry

            entry = self.config_entry
            if getattr(entry, "runtime_data", None) is None:
                return self.async_abort(reason="not_loaded")
            await _async_rescan_entry(
                self.hass, entry, user_input[CONF_SCAN_DURATION]
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="rescan",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_DURATION, default=DEFAULT_SCAN_DURATION
                    ): SCAN_DURATION_FIELD
                }
            ),
        )

    def _current_gateway(self) -> str:
        return self.config_entry.data[CONF_GATEWAY]

    async def _async_finish(
        self, gateway: str, scan_duration: int
    ) -> ConfigFlowResult | str:
        entry = self.config_entry
        for other in self.hass.config_entries.async_entries(DOMAIN):
            if (
                other.entry_id != entry.entry_id
                and other.data.get(CONF_GATEWAY) == gateway
            ):
                return "already_configured"
        devices, error = await _async_scan_bus(gateway, scan_duration)
        if error:
            return error
        merged, _ = merge_device_lists(entry.data[CONF_DEVICES], devices)
        self.hass.config_entries.async_update_entry(
            entry,
            title=_title(gateway),
            data={**entry.data, CONF_GATEWAY: gateway, CONF_DEVICES: merged},
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(title="", data={})
