"""`todo.mercadona`: the Mercadona cart as a to-do list.

- Create (UI or Assist "añade leche a Mercadona"): resolve text, `ensure` mode.
- Complete or delete: remove the product from the cart (in-store flow).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import async_add_and_notify
from .api import DespensaError
from .const import SOURCE_TODO
from .coordinator import DespensaCoordinator, MercadonaConfigEntry
from .entity import device_info


async def async_setup_entry(hass: HomeAssistant, entry: MercadonaConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([MercadonaTodoList(entry.runtime_data)])


def _qty_label(qty: Any) -> str | None:
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None
    if q == 1:
        return None
    return f"Cantidad: {int(q) if q.is_integer() else str(q).replace('.', ',')}"


class MercadonaTodoList(CoordinatorEntity[DespensaCoordinator], TodoListEntity):
    _attr_has_entity_name = True
    _attr_name = None  # main entity of the device -> todo.mercadona
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, coordinator: DespensaCoordinator) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_cart"
        self._attr_device_info = device_info(entry_id)

    @property
    def todo_items(self) -> list[TodoItem] | None:
        cart = (self.coordinator.data or {}).get("cart")
        if cart is None:
            return None
        return [
            TodoItem(
                uid=str(line["product_id"]),
                summary=line.get("name") or str(line["product_id"]),
                status=TodoItemStatus.NEEDS_ACTION,
                description=_qty_label(line.get("quantity")),
            )
            for line in cart.get("lines", [])
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        text = (item.summary or "").strip()
        if not text:
            raise HomeAssistantError("Texto vacío")
        result = await async_add_and_notify(
            self.hass, self.coordinator, text=text, qty=1, mode="ensure", source=SOURCE_TODO
        )
        if result.get("result") == "unresolved":
            raise HomeAssistantError(
                f"Hay varias opciones para «{text}». Te pregunto cuál por mensaje."
                if result.get("candidates") else
                f"No encuentro «{text}» en Mercadona."
            )

    async def async_update_todo_item(self, item: TodoItem) -> None:
        if item.status == TodoItemStatus.COMPLETED and item.uid:
            await self._remove([item.uid])

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._remove(uids)

    async def _remove(self, uids: list[str]) -> None:
        try:
            for uid in uids:
                await self.coordinator.client.remove_item(uid, SOURCE_TODO)
        except DespensaError as exc:
            raise HomeAssistantError(f"Despensa: {exc}") from exc
        await self.coordinator.async_request_refresh()
