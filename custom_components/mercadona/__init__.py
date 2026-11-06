"""Mercadona (Despensa): Home Assistant front-end of the Despensa service."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import DespensaClient, DespensaError
from .const import (
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
    EVENT_UNRESOLVED,
    SERVICE_ADD_PRODUCT,
    SERVICE_CHOOSE,
    SERVICE_MORE,
)
from .coordinator import DespensaCoordinator, MercadonaConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.TODO, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ADD_PRODUCT_SCHEMA = vol.All(
    vol.Schema({
        vol.Optional("ean"): cv.string,
        vol.Optional("product_id"): cv.string,
        vol.Optional("text"): cv.string,
        vol.Optional("qty", default=1): vol.All(vol.Coerce(float), vol.Range(min=0.001,
                                                                             max=50)),
        vol.Optional("mode", default="ensure"): vol.In(["ensure", "add"]),
        vol.Optional("source", default="ha"): vol.Match(r"^[a-z0-9_]+$"),
        vol.Optional("learn_text"): cv.string,
    }),
    cv.has_at_least_one_key("ean", "product_id", "text"),
)


async def async_add_and_notify(hass: HomeAssistant, coordinator: DespensaCoordinator,
                               **fields: Any) -> dict[str, Any]:
    """Call POST /v1/items; fire `mercadona_unresolved` when there is doubt."""
    try:
        result = await coordinator.client.add_item(**fields)
    except DespensaError as exc:
        raise HomeAssistantError(f"Despensa: {exc}") from exc
    if result.get("result") == "unresolved":
        hass.bus.async_fire(EVENT_UNRESOLVED, {
            "query": result.get("query"),
            "reason": result.get("reason"),
            "candidates": result.get("candidates", []),
            "request_id": result.get("request_id"),
            "more": result.get("more", False),
            "qty": fields.get("qty", 1),
            "mode": fields.get("mode", "ensure"),
            "source": fields.get("source"),
            "is_text": bool(fields.get("text")),
        })
    else:
        await coordinator.async_request_refresh()
    return result


REQUEST_SCHEMA = vol.Schema({vol.Required("request_id"): vol.Match(r"^[0-9a-f]{6}$")})
CHOOSE_SCHEMA = REQUEST_SCHEMA.extend({vol.Required("product_id"): cv.string})


def _coordinator(hass: HomeAssistant) -> DespensaCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("La integración Mercadona no está cargada")
    coordinator: DespensaCoordinator = entries[0].runtime_data
    return coordinator


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def choose(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        try:
            result = await coordinator.client.choose(call.data["request_id"],
                                                     call.data["product_id"])
        except DespensaError as exc:
            raise HomeAssistantError(f"Despensa: {exc}") from exc
        await coordinator.async_request_refresh()
        return result

    async def more(call: ServiceCall) -> ServiceResponse:
        try:
            return await _coordinator(hass).client.more(call.data["request_id"])
        except DespensaError as exc:
            raise HomeAssistantError(f"Despensa: {exc}") from exc

    hass.services.async_register(DOMAIN, SERVICE_CHOOSE, choose, schema=CHOOSE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_MORE, more, schema=REQUEST_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)

    async def add_product(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        given = [k for k in ("ean", "product_id", "text") if call.data.get(k)]
        if len(given) != 1:
            raise ServiceValidationError("Indica exactamente uno: ean, product_id o text")
        return await async_add_and_notify(hass, coordinator, **dict(call.data))

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_PRODUCT, add_product, schema=ADD_PRODUCT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MercadonaConfigEntry) -> bool:
    client = DespensaClient(async_get_clientsession(hass), entry.data[CONF_URL],
                            entry.data[CONF_TOKEN])
    coordinator = DespensaCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MercadonaConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
