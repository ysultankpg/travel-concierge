"""Shared async HTTP helper for the agent's tools.

Every tool used to call `urllib.request.urlopen` — a *blocking* syscall — from
inside the agent's asyncio event loop. With one slow upstream (Places, the FX
API) that stalls the entire server, not just the one request. These helpers use
a single pooled `httpx.AsyncClient` with an explicit timeout instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .. import config

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()

_USER_AGENT = "TravelConciergeAgent/2.0"


async def get_client() -> httpx.AsyncClient:
    """Lazily create one shared, connection-pooled client."""
    global _client
    if _client is None or _client.is_closed:
        async with _lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(config.HTTP_TIMEOUT),
                    headers={"User-Agent": _USER_AGENT},
                    follow_redirects=True,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
    return _client


async def aclose() -> None:
    """Close the pool (used by tests and graceful shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return f"the request timed out after {config.HTTP_TIMEOUT:g}s"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the service returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"the network request failed ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"


async def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """GET JSON. Returns `{"error": ...}` instead of raising, so the model can
    explain the failure to the user rather than the turn dying."""
    client = await get_client()
    try:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as text.
        return {"error": _describe(exc)}


async def post_json(
    url: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict:
    """POST JSON. Same error contract as `get_json`."""
    client = await get_client()
    try:
        resp = await client.post(url, json=json_body, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": _describe(exc)}


async def to_thread(func, *args, **kwargs):
    """Run a blocking SDK call (Firestore, GCS) off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)
