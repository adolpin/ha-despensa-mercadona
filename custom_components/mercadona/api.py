"""Async client for the Despensa local API."""

from __future__ import annotations

from typing import Any

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)


class DespensaError(Exception):
    """Base error."""


class DespensaAuthError(DespensaError):
    """Invalid token."""


class DespensaConnectionError(DespensaError):
    """Service unreachable or failing."""


class DespensaClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method, f"{self._url}{path}", headers=self._headers, timeout=TIMEOUT,
                **kwargs,
            ) as resp:
                if resp.status == 401:
                    raise DespensaAuthError("Token no válido")
                if resp.status >= 400:
                    text = await resp.text()
                    raise DespensaConnectionError(f"HTTP {resp.status}: {text[:200]}")
                return await resp.json()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise DespensaConnectionError(str(exc) or type(exc).__name__) from exc

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/health")

    async def cart(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/cart")

    async def add_item(self, **fields: Any) -> dict[str, Any]:
        body = {k: v for k, v in fields.items() if v not in (None, "")}
        return await self._request("POST", "/v1/items", json=body)

    async def remove_item(self, product_id: str, source: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/items/{product_id}",
                                   params={"source": source})

    async def choose(self, request_id: str, product_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/requests/{request_id}/choose",
                                   json={"product_id": product_id})

    async def more(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/requests/{request_id}/more")

    async def segment(self, text: str) -> list[str]:
        r = await self._request("GET", "/v1/segment", params={"q": text})
        return list(r.get("items") or [text])

    async def search(self, query: str, n: int = 5) -> dict[str, Any]:
        return await self._request("GET", "/v1/search", params={"q": query, "n": n})
