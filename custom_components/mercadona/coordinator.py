"""Polls the cart and the service health."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DespensaAuthError, DespensaClient, DespensaError
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type MercadonaConfigEntry = ConfigEntry[DespensaCoordinator]


class DespensaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    config_entry: MercadonaConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MercadonaConfigEntry,
                 client: DespensaClient) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL,
                         config_entry=entry)
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            health, cart = await asyncio.gather(self.client.health(), self._cart())
        except DespensaAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except DespensaError as exc:
            raise UpdateFailed(f"Servicio Despensa no disponible: {exc}") from exc
        return {"health": health, "cart": cart}

    async def _cart(self) -> dict[str, Any] | None:
        # The service answers 503 when Mercadona fails and there is no cached cart;
        # keep health readable in that case.
        try:
            return await self.client.cart()
        except DespensaAuthError:
            raise
        except DespensaError as exc:
            _LOGGER.warning("No se pudo leer el carrito: %s", exc)
            return self.data.get("cart") if self.data else None
