"""Sensors: Mercadona API status, days until the token expires, products in the cart."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DespensaCoordinator, MercadonaConfigEntry
from .entity import device_info


def _api_status(data: dict[str, Any]) -> str:
    h = data.get("health") or {}
    if h.get("mercadona_ok") is None:
        return "unknown"
    return "ok" if h["mercadona_ok"] else "error"


def _cart_count(data: dict[str, Any]) -> int | None:
    cart = data.get("cart")
    return len(cart.get("lines", [])) if cart else None


@dataclass(frozen=True, kw_only=True)
class DespensaSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attrs: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}


SENSORS: tuple[DespensaSensorDescription, ...] = (
    DespensaSensorDescription(
        key="api_status",
        translation_key="api_status",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "error", "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_api_status,
        attrs=lambda d: {k: (d.get("health") or {}).get(k) for k in (
            "last_ok", "last_error", "last_error_at", "last_probe", "pending_ops",
            "cart_writes", "catalog_indexed_at", "catalog_products", "catalog_with_ean",
        )},
    ),
    DespensaSensorDescription(
        key="token_days_left",
        translation_key="token_days_left",
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.get("health") or {}).get("token_days_left"),
        attrs=lambda d: {"token_expires": (d.get("health") or {}).get("token_expires")},
    ),
    DespensaSensorDescription(
        key="cart_count",
        translation_key="cart_count",
        state_class=SensorStateClass.MEASUREMENT,
        value=_cart_count,
        attrs=lambda d: {"stale": (d.get("cart") or {}).get("stale"),
                         "total": (d.get("cart") or {}).get("total")},
    ),
    DespensaSensorDescription(
        key="pending_ops",
        translation_key="pending_ops",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.get("health") or {}).get("pending_ops"),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: MercadonaConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(DespensaSensor(entry.runtime_data, d) for d in SENSORS)


class DespensaSensor(CoordinatorEntity[DespensaCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: DespensaSensorDescription

    def __init__(self, coordinator: DespensaCoordinator,
                 description: DespensaSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device_info(entry_id)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs(self.coordinator.data or {})
