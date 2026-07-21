"""Config flow: bind the bus, scan it, store what was found."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_DEVICES,
    CONF_GATEWAY,
    CONF_SCAN_DURATION,
    DEFAULT_GATEWAY,
    DEFAULT_SCAN_DURATION,
    DOMAIN,
)
from .pysmartg4 import SmartG4Bus
from .pysmartg4.discovery import discover

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY, default=DEFAULT_GATEWAY): str,
        vol.Required(CONF_SCAN_DURATION, default=DEFAULT_SCAN_DURATION): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=120)
        ),
    }
)


class SmartG4ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the gateway, then discover the bus (30 s by default)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SmartG4OptionsFlow":
        return SmartG4OptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            gateway = user_input[CONF_GATEWAY]
            self._async_abort_entries_match({CONF_GATEWAY: gateway})
            bus = SmartG4Bus(gateway=gateway)
            try:
                await bus.connect()
            except OSError:
                errors["base"] = "cannot_connect"
            else:
                try:
                    devices = await discover(
                        bus, duration=user_input[CONF_SCAN_DURATION]
                    )
                finally:
                    bus.close()
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    return self.async_create_entry(
                        title=f"Smart-G4 ({gateway})",
                        data={
                            CONF_GATEWAY: gateway,
                            CONF_DEVICES: [d.as_dict() for d in devices],
                        },
                    )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )


class SmartG4OptionsFlow(OptionsFlow):
    """Configure → rescan the bus and add newly found devices."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
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
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_DURATION, default=DEFAULT_SCAN_DURATION
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120))
                }
            ),
            errors=errors,
        )
