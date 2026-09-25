"""Firestore-backed destination catalog and trip bookmarks.

Fixes vs. the original:
  * `search_destinations` streamed the *entire* collection and filtered in
    Python, then returned `str(list_of_dicts)` — a Python repr the model had to
    re-parse. It now pushes filters into the query, supports sorting/limits, and
    returns real JSON.
  * `save_trip_bookmark` took `user_id` as a model-supplied argument. A model
    will happily invent one, writing a stranger's bookmark or silently
    fragmenting a user's saved trips. The id now comes from the session via
    `ToolContext`, and is not a model-controllable parameter at all.
  * There was no way to *read back* or delete saved trips, so the agent could
    write bookmarks it could never show the user again.
  * Blocking Firestore calls ran on the event loop; they now run in a thread.
"""

from __future__ import annotations

import datetime as _dt
import functools
import json

from google.adk.tools import ToolContext
from google.cloud import firestore

from .. import config
from . import http

_MAX_LIMIT = 50


@functools.lru_cache(maxsize=1)
def _db() -> firestore.Client:
    return firestore.Client(project=config.project_id())


def _resolve_user_id(tool_context: ToolContext | None) -> str | None:
    """Pull the caller's id from the session, never from the model.

    ADK exposes this in slightly different places across versions, so probe a
    few and fall back to session state.
    """
    if tool_context is None:
        return None
    for attr in ("_invocation_context", "invocation_context"):
        ctx = getattr(tool_context, attr, None)
        uid = getattr(ctx, "user_id", None)
        if uid:
            return str(uid)
    state = getattr(tool_context, "state", None)
    if state is not None:
        for key in ("user_id", "user:id", "app:user_id"):
            try:
                if state.get(key):
                    return str(state[key])
            except Exception:  # noqa: BLE001 - state impls vary.
                continue
    return None


async def search_destinations(
    category: str = "",
    max_budget_usd: int = 0,
    min_budget_usd: int = 0,
    tag: str = "",
    limit: int = 10,
) -> str:
    """Search the curated destination catalog.

    Args:
        category: Filter by category, e.g. 'Cultural & Historic',
            'Tropical & Wellness', 'Urban & Art', 'Wildlife & Safari'.
        max_budget_usd: Only destinations at or below this average daily budget.
        min_budget_usd: Only destinations at or above this average daily budget.
        tag: Filter by interest tag, e.g. 'temples', 'beaches', 'safari', 'food'.
        limit: Maximum destinations to return (1-50, default 10).

    Returns:
        JSON list of matching destinations, cheapest first.
    """
    count = max(1, min(int(limit or 10), _MAX_LIMIT))

    def _query() -> list[dict]:
        query = _db().collection(config.DESTINATIONS_COLLECTION)
        # Push the numeric range into Firestore rather than scanning everything.
        if max_budget_usd and max_budget_usd > 0:
            query = query.where(
                filter=firestore.FieldFilter("avg_budget_usd", "<=", int(max_budget_usd))
            )
        if min_budget_usd and min_budget_usd > 0:
            query = query.where(
                filter=firestore.FieldFilter("avg_budget_usd", ">=", int(min_budget_usd))
            )
        if tag.strip():
            query = query.where(
                filter=firestore.FieldFilter(
                    "tags", "array_contains", tag.strip().lower()
                )
            )
        query = query.order_by("avg_budget_usd").limit(count * 3)
        return [doc.to_dict() | {"id": doc.id} for doc in query.stream()]

    try:
        rows = await http.to_thread(_query)
    except Exception as exc:  # noqa: BLE001 - index/permission errors.
        return json.dumps(
            {"error": f"Catalog search failed ({type(exc).__name__}): {exc}"}
        )

    # Category is a free-text substring match, which Firestore can't express.
    needle = category.strip().lower()
    if needle:
        rows = [r for r in rows if needle in str(r.get("category", "")).lower()]

    rows = rows[:count]
    if not rows:
        return json.dumps(
            {
                "destinations": [],
                "note": "Nothing in the curated catalog matches those filters. "
                "Suggest loosening the budget or category, and offer ideas from "
                "general knowledge, labelled as not-from-catalog.",
            }
        )
    return json.dumps({"destinations": rows, "count": len(rows)}, default=str)


async def get_destination_details(destination_id: str) -> str:
    """Get full details for one catalog destination by id.

    Args:
        destination_id: Document id, e.g. 'kyoto', 'paris', 'bali', 'serengeti'.

    Returns:
        JSON destination record, or an error with the available ids.
    """
    doc_id = (destination_id or "").strip().lower()
    if not doc_id:
        return json.dumps({"error": "Provide a destination id."})

    def _fetch():
        snap = _db().collection(config.DESTINATIONS_COLLECTION).document(doc_id).get()
        if snap.exists:
            return snap.to_dict() | {"id": snap.id}
        # Help the model self-correct instead of dead-ending the user.
        available = [
            d.id
            for d in _db()
            .collection(config.DESTINATIONS_COLLECTION)
            .select([])
            .limit(_MAX_LIMIT)
            .stream()
        ]
        return {"_missing": True, "available_ids": available}

    try:
        result = await http.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Catalog lookup failed: {exc}"})

    if result.get("_missing"):
        return json.dumps(
            {
                "error": f"No destination with id '{doc_id}'.",
                "available_ids": result["available_ids"],
            }
        )
    return json.dumps(result, default=str)


async def save_trip_bookmark(
    destination_name: str,
    travel_dates: str = "",
    notes: str = "",
    tool_context: ToolContext = None,
) -> str:
    """Save a trip bookmark for the current user.

    The user id is taken from the active session automatically — never ask the
    user for it and never pass one in.

    Args:
        destination_name: Destination being bookmarked (e.g. 'Kyoto, Japan').
        travel_dates: Planned dates as free text (e.g. 'October 2026').
        notes: Preferences, constraints, or itinerary notes to remember.
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON confirmation including the bookmark id.
    """
    name = (destination_name or "").strip()
    if not name:
        return json.dumps({"error": "Provide a destination name to bookmark."})

    user_id = _resolve_user_id(tool_context)
    if not user_id:
        return json.dumps(
            {"error": "No active user session, so I can't save a bookmark."}
        )

    def _write() -> str:
        ref = _db().collection(config.SAVED_TRIPS_COLLECTION).document()
        ref.set(
            {
                "bookmark_id": ref.id,
                "user_id": user_id,
                "destination_name": name,
                "travel_dates": travel_dates.strip(),
                "notes": notes.strip(),
                "created_at": _dt.datetime.now(_dt.UTC).isoformat(),
            }
        )
        return ref.id

    try:
        bookmark_id = await http.to_thread(_write)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Could not save the bookmark: {exc}"})

    return json.dumps(
        {"saved": True, "bookmark_id": bookmark_id, "destination_name": name}
    )


async def list_trip_bookmarks(
    limit: int = 20, tool_context: ToolContext = None
) -> str:
    """List the current user's saved trip bookmarks, newest first.

    Args:
        limit: Maximum bookmarks to return (1-50, default 20).
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON list of the user's bookmarks.
    """
    user_id = _resolve_user_id(tool_context)
    if not user_id:
        return json.dumps({"error": "No active user session."})
    count = max(1, min(int(limit or 20), _MAX_LIMIT))

    def _read() -> list[dict]:
        query = (
            _db()
            .collection(config.SAVED_TRIPS_COLLECTION)
            .where(filter=firestore.FieldFilter("user_id", "==", user_id))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(count)
        )
        return [d.to_dict() for d in query.stream()]

    try:
        rows = await http.to_thread(_read)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Could not read bookmarks: {exc}"})

    # Don't leak the internal user id back into the model's context.
    for row in rows:
        row.pop("user_id", None)
    return json.dumps({"bookmarks": rows, "count": len(rows)}, default=str)


async def delete_trip_bookmark(
    bookmark_id: str, tool_context: ToolContext = None
) -> str:
    """Delete one of the current user's saved bookmarks.

    Args:
        bookmark_id: The bookmark id to delete (from `list_trip_bookmarks`).
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON confirmation, or an error if it isn't the user's bookmark.
    """
    user_id = _resolve_user_id(tool_context)
    if not user_id:
        return json.dumps({"error": "No active user session."})
    doc_id = (bookmark_id or "").strip()
    if not doc_id:
        return json.dumps({"error": "Provide a bookmark id."})

    def _delete() -> str:
        ref = _db().collection(config.SAVED_TRIPS_COLLECTION).document(doc_id)
        snap = ref.get()
        if not snap.exists:
            return "missing"
        # Ownership check: never let one session delete another user's data.
        if (snap.to_dict() or {}).get("user_id") != user_id:
            return "forbidden"
        ref.delete()
        return "deleted"

    try:
        outcome = await http.to_thread(_delete)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Could not delete the bookmark: {exc}"})

    if outcome == "missing":
        return json.dumps({"error": f"No bookmark with id '{doc_id}'."})
    if outcome == "forbidden":
        return json.dumps({"error": "That bookmark belongs to another user."})
    return json.dumps({"deleted": True, "bookmark_id": doc_id})
