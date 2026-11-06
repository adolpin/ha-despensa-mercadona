"""Shared device info."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN


def device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Mercadona",
        manufacturer="Despensa",
        model="Carrito de Mercadona",
        entry_type=DeviceEntryType.SERVICE,
    )
