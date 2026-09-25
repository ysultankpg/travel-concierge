"""FastAPI proxy for the deployed Travel Concierge A2A agent.

The browser talks ONLY to this proxy (same origin, no CORS, no GCP credentials in
the browser). The proxy authenticates with Application Default Credentials and
forwards chat to the deployed agent over the A2A protocol.

Fixes vs. the original proxy:
  * **Shared identity.** The UI never sent a user id, so every visitor was filed
    under the literal string "web-user" — they shared one A2A context and one
    memory bank. Two people using the deployed demo saw each other's
    conversation. Identity is now a signed, HttpOnly per-browser cookie.
  * **Unbounded context map.** `_contexts` was a module-level dict that grew
    forever, one entry per user, never evicted — a slow memory leak on a
    long-lived Cloud Run instance. It is now an LRU with a hard cap.
  * **CORS `allow_origins=["*"]` with `allow_credentials=True`**, which browsers
    reject and which defeats the point of a same-origin proxy. Origins are now an
    explicit allowlist, empty by default.
  * **Errors returned HTTP 200** with the raw exception string (type and message)
    rendered into the chat — an information leak that also made every failure
    look like a successful turn to any monitoring. Errors now use correct status
    codes, log the detail server-side, and return a generic message.
  * **No streaming.** The client waited for the entire turn, so a 30-second
    multi-tool plan showed a motionless "…". Replies now stream over SSE.
  * **No timeout ceiling or request cap**, so a hung upstream held a worker
    indefinitely.

Run:
  pip install -r requirements.txt
  export AGENT_ENGINE_RESOURCE_NAME="projects/.../locations/.../reasoningEngines/..."
  export AGENT_DIRECTORY="app"
  python main.py                 # -> http://localhost:8080
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextlib
import json
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Any
import uuid

import google.auth
import google.auth.transport.requests
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    FilePart,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TextPart,
    TransportProtocol,
)
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("travel-concierge-proxy")

RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE_NAME", "")
AGENT_DIRECTORY = os.environ.get("AGENT_DIRECTORY", "app")

# Secret for signing the identity cookie. Generated per-process when unset,
# which means local restarts issue new identities (fine for dev). Set
# SESSION_SECRET in production so identities survive a redeploy.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
_serializer = URLSafeSerializer(SESSION_SECRET, salt="travel-concierge-identity")

COOKIE_NAME = "tc_uid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 days

# Upstream timeout. A multi-tool travel plan with image generation is slow, but
# not unbounded.
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "180"))
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS", "4000"))
MAX_CONTEXTS = int(os.environ.get("MAX_CONTEXTS", "5000"))
MAX_CONCURRENT_UPSTREAM = int(os.environ.get("MAX_CONCURRENT_UPSTREAM", "32"))

_A2UI_MIME = "application/json+a2ui"

if RESOURCE:
    LOCATION = RESOURCE.split("/locations/")[1].split("/")[0]
    A2A_BASE = (
        f"https://{LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/"
        f"{RESOURCE}/api/a2a/{AGENT_DIRECTORY}"
    )
    A2A_CARD_URL = f"{A2A_BASE}/.well-known/agent-card.json"
else:  # Allow the module to import (and /healthz to answer) without config.
    LOCATION = A2A_BASE = A2A_CARD_URL = ""
    logger.warning("AGENT_ENGINE_RESOURCE_NAME is not set; /chat will return 503.")

try:
    _creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
except Exception as exc:  # noqa: BLE001 - surfaced per-request instead.
    _creds = None
    logger.warning("No Application Default Credentials available: %s", exc)

_creds_lock = asyncio.Lock()
_upstream_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPSTREAM)


async def _auth_headers() -> dict[str, str]:
    """Refresh the access token off the event loop, under a lock.

    The original refreshed on every request with a blocking call directly on the
    loop, and concurrent requests could refresh the shared credential object
    simultaneously.
    """
    if _creds is None:
        raise HTTPException(503, "Server is not configured with GCP credentials.")
    async with _creds_lock:
        if not _creds.valid or (
            _creds.expiry and _creds.expiry.timestamp() - time.time() < 300
        ):
            await asyncio.to_thread(
                _creds.refresh, google.auth.transport.requests.Request()
            )
        token = _creds.token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


app = FastAPI(title="Travel Concierge", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Explicit allowlist instead of "*" + credentials (which browsers reject).
_origins = [o.strip() for o in os.environ.get("ALLOW_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.exception_handler(Exception)
async def _json_errors(request: Request, exc: Exception):
    """Log the detail, return a generic message.

    The original echoed `type(exc).__name__: exc` straight into the chat bubble
    with HTTP 200 — leaking internals (resource names, auth errors, stack
    context) to anyone using the page.
    """
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Something went wrong on the server. Please try again."},
    )


class _LRU(OrderedDict):
    """Bounded map so long-lived instances don't leak one entry per visitor."""

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize

    def touch(self, key: str, value: str) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)


_contexts: _LRU = _LRU(MAX_CONTEXTS)
_card: AgentCard | None = None
_card_lock = asyncio.Lock()


def _identity(raw_cookie: str | None) -> tuple[str, bool]:
    """Return (user_id, is_new). Unsigns the cookie; mints one when absent."""
    if raw_cookie:
        try:
            value = _serializer.loads(raw_cookie)
            if isinstance(value, str) and value:
                return value, False
        except BadSignature:
            logger.info("Discarding a cookie with an invalid signature.")
    return f"u_{uuid.uuid4().hex}", True


def _set_identity_cookie(response: Response, user_id: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _serializer.dumps(user_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "1") != "0",
        path="/",
    )


async def _get_card(client: httpx.AsyncClient) -> AgentCard:
    global _card
    if _card is None:
        async with _card_lock:
            if _card is None:
                resp = await client.get(A2A_CARD_URL)
                resp.raise_for_status()
                card = AgentCard(**resp.json())
                # Agent Runtime serves no public card URL; point sends at the
                # passthrough base.
                card.url = A2A_BASE
                _card = card
    return _card


def _extract_parts(parts: list) -> list[dict]:
    """Turn A2A response parts into structured parts for the chat UI."""
    out: list[dict] = []
    for p in parts or []:
        root = getattr(p, "root", p)
        if isinstance(root, TextPart) and getattr(root, "text", None):
            out.append({"kind": "text", "text": root.text})
        elif getattr(root, "data", None) is not None:
            meta = getattr(root, "metadata", None) or {}
            mime = meta.get("mimeType") if isinstance(meta, dict) else None
            if mime == _A2UI_MIME:
                out.append({"kind": "a2ui", "data": root.data})
        elif isinstance(root, FilePart):
            uri = getattr(getattr(root, "file", None), "uri", None)
            if uri:
                out.append({"kind": "text", "text": uri})
    return out


def _validate(body: dict) -> str:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(400, "A non-empty 'message' is required.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(
            413, f"Message exceeds the {MAX_MESSAGE_CHARS}-character limit."
        )
    return message.strip()


async def _run_turn(message: str, user_id: str):
    """Yield (event_name, payload) tuples as the agent produces them."""
    if not RESOURCE:
        yield "error", {"message": "The agent backend is not configured."}
        return

    async with _upstream_semaphore:
        headers = await _auth_headers()
        async with httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(UPSTREAM_TIMEOUT)
        ) as client:
            card = await _get_card(client)
            factory = ClientFactory(
                ClientConfig(
                    supported_transports=[
                        TransportProtocol.jsonrpc,
                        TransportProtocol.http_json,
                    ],
                    httpx_client=client,
                )
            )
            a2a_client = factory.create(card)

            msg = Message(
                message_id=str(uuid.uuid4()),
                role=Role.user,
                parts=[Part(root=TextPart(text=message))],
                context_id=_contexts.get(user_id),
            )

            last_task = None
            streamed = False
            seen: set[str] = set()

            async for event in a2a_client.send_message(msg):
                if not isinstance(event, tuple):
                    continue
                task, update = event
                if task is not None:
                    last_task = task
                    ctx = getattr(task, "context_id", None)
                    if ctx:
                        _contexts.touch(user_id, ctx)
                    # Surface intermediate status so the UI can show what the
                    # agent is actually doing instead of a static ellipsis.
                    status = getattr(task, "status", None)
                    state = getattr(status, "state", None)
                    if state is not None:
                        label = getattr(state, "value", str(state))
                        if label not in seen:
                            seen.add(label)
                            yield "status", {"state": label}
                if isinstance(update, TaskArtifactUpdateEvent):
                    for part in _extract_parts(update.artifact.parts):
                        streamed = True
                        yield "part", part

            if not streamed and last_task is not None:
                for artifact in getattr(last_task, "artifacts", None) or []:
                    for part in _extract_parts(artifact.parts):
                        streamed = True
                        yield "part", part

            if not streamed:
                yield "part", {
                    "kind": "text",
                    "text": "I didn't manage to produce a reply for that. "
                    "Could you rephrase it?",
                }


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@app.post("/chat/stream")
async def chat_stream(request: Request, tc_uid: str | None = Cookie(default=None)):
    """Stream one turn as Server-Sent Events."""
    body = await request.json()
    message = _validate(body)
    user_id, is_new = _identity(tc_uid)

    async def generator():
        yield _sse("open", {"ok": True})
        try:
            async for name, payload in _run_turn(message, user_id):
                if await request.is_disconnected():
                    logger.info("Client disconnected; abandoning turn.")
                    break
                yield _sse(name, payload)
        except HTTPException as exc:
            yield _sse("error", {"message": exc.detail})
        except (httpx.TimeoutException, asyncio.TimeoutError):
            logger.warning("Upstream timed out after %ss", UPSTREAM_TIMEOUT)
            yield _sse(
                "error",
                {"message": "The agent took too long to respond. Please try again."},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Turn failed")
            yield _sse("error", {"message": "Something went wrong. Please try again."})
        finally:
            yield _sse("done", {"ok": True})

    response = StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # don't let a proxy buffer the stream
        },
    )
    if is_new:
        _set_identity_cookie(response, user_id)
    return response


@app.post("/chat")
async def chat(request: Request, tc_uid: str | None = Cookie(default=None)):
    """Non-streaming turn, kept for compatibility and simple clients."""
    body = await request.json()
    message = _validate(body)
    user_id, is_new = _identity(tc_uid)

    parts: list[dict] = []
    try:
        async for name, payload in _run_turn(message, user_id):
            if name == "part":
                parts.append(payload)
            elif name == "error":
                raise HTTPException(502, payload.get("message", "Upstream error."))
    except HTTPException:
        raise
    except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
        raise HTTPException(504, "The agent took too long to respond.") from exc

    response = JSONResponse({"parts": parts})
    if is_new:
        _set_identity_cookie(response, user_id)
    return response


@app.post("/session/reset")
async def reset_session(tc_uid: str | None = Cookie(default=None)):
    """Drop the server-side conversation context so the next turn starts fresh."""
    user_id, _ = _identity(tc_uid)
    _contexts.pop(user_id, None)
    return {"reset": True}


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "configured": bool(RESOURCE),
        "active_contexts": len(_contexts),
    }


@app.get("/readyz")
async def readyz():
    """Ready only when the upstream agent card is reachable."""
    if not RESOURCE:
        raise HTTPException(503, "AGENT_ENGINE_RESOURCE_NAME is not set.")
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(headers=headers, timeout=10) as client:
            card = await _get_card(client)
        return {"status": "ready", "agent": getattr(card, "name", "unknown")}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Readiness check failed: %s", exc)
        raise HTTPException(503, "Upstream agent is not reachable.") from exc


STATIC_DIR = Path(__file__).parent / "static"
# Keep this mount last so the API routes win.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


@app.on_event("shutdown")
async def _shutdown() -> None:
    with contextlib.suppress(Exception):
        _contexts.clear()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8080)),
    )
