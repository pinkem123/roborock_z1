"""Config flow for the Roborock Z1 Mower integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from roborock.exceptions import RoborockException
from roborock.web_api import RoborockApiClient

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_USERNAME

from .const import CONF_BASE_URL, CONF_USER_DATA, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"


class RoborockZ1ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow: email -> emailed verification code -> tokens."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: RoborockApiClient | None = None
        self._username: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            await self.async_set_unique_id(self._username.lower())
            self._abort_if_unique_id_configured()
            self._client = RoborockApiClient(username=self._username)
            try:
                await self._client.request_code()
            except RoborockException as err:
                _LOGGER.error("Failed to request code: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_USERNAME): str}),
            errors=errors,
        )

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._client is not None and self._username is not None
        if user_input is not None:
            try:
                user_data = await self._client.code_login(user_input[CONF_CODE])
            except RoborockException as err:
                _LOGGER.error("Code login failed: %s", err)
                errors["base"] = "invalid_auth"
            else:
                # base_url is an *async* property in python-roborock — it must
                # be awaited, otherwise a coroutine object gets stored in the
                # config entry and later blows up as the API base URL.
                base_url = await self._client.base_url
                return self.async_create_entry(
                    title=self._username,
                    data={
                        CONF_USERNAME: self._username,
                        CONF_USER_DATA: user_data.as_dict(),
                        CONF_BASE_URL: base_url,
                    },
                )

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            errors=errors,
        )
