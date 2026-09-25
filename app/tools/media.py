"""Generative media tools: destination images and short video clips.

Fixes vs. the original:
  * `generate_destination_video` fell back to a hardcoded 44-byte MP4 header
    when generation failed, uploaded *that* to GCS, and returned the URL as if
    it had succeeded. The user got a "here's your video" link to a file no
    player can open. It now reports the failure honestly.
  * Model-supplied filenames went straight into a GCS object path, so a filename
    like `../../secrets` or an absolute path was possible. Filenames are now
    sanitised and namespaced per session.
  * Blocking GCS uploads ran on the event loop; they now run in a thread.
  * `upload_travel_media` accepted arbitrary content with no size cap.
"""

from __future__ import annotations

import json
import re

from google import genai
from google.adk.tools import ToolContext
from google.cloud import storage
from google.genai import types

from .. import config
from . import http

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_TEXT_BYTES = 256 * 1024


def _safe_filename(name: str, default_ext: str) -> str:
    """Reduce a model-supplied name to a single safe path segment."""
    base = _SAFE_NAME.sub("_", (name or "").strip().split("/")[-1]).strip("._")
    if not base:
        base = f"asset{default_ext}"
    if "." not in base:
        base = f"{base}{default_ext}"
    return base[:120]


def _session_prefix(tool_context: ToolContext | None) -> str:
    """Namespace uploads per session so concurrent users can't overwrite
    each other's `destination_photo.png`."""
    for attr in ("_invocation_context", "invocation_context"):
        ctx = getattr(tool_context, attr, None)
        session = getattr(ctx, "session", None)
        sid = getattr(session, "id", None) or getattr(ctx, "session_id", None)
        if sid:
            return f"sessions/{_SAFE_NAME.sub('_', str(sid))[:64]}"
    return "sessions/anonymous"


def _media_client() -> genai.Client:
    return genai.Client(
        vertexai=True, project=config.project_id(), location=config.MEDIA_LOCATION
    )


async def _upload(path: str, data: bytes, content_type: str) -> str:
    def _put() -> str:
        client = storage.Client(project=config.project_id())
        blob = client.bucket(config.media_bucket()).blob(path)
        blob.upload_from_string(data, content_type=content_type)
        return f"https://storage.googleapis.com/{config.media_bucket()}/{path}"

    return await http.to_thread(_put)


async def generate_destination_image(
    prompt: str,
    filename: str = "destination.png",
    tool_context: ToolContext = None,
) -> str:
    """Generate a photoreal preview image of a travel destination.

    Args:
        prompt: Rich visual description — include the place, time of day,
            season, weather, and viewpoint for a good result.
        filename: Target filename, e.g. 'kyoto_pagoda.png'.
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON with a public `url` on success, or an `error` explaining the
        failure. Never returns a URL for a file that failed to generate.
    """
    if not prompt or not prompt.strip():
        return json.dumps({"error": "Provide a description of the image to generate."})

    name = _safe_filename(filename, ".png")
    try:
        response = await http.to_thread(
            lambda: _media_client().models.generate_content(
                model=config.IMAGE_MODEL,
                contents=(
                    "Generate a beautiful, photorealistic travel photograph. "
                    "No text, watermarks, or logos in the image. "
                    f"Subject: {prompt.strip()}"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Image generation failed: {exc}"})

    image_bytes, mime_type = None, "image/png"
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                image_bytes = inline.data
                mime_type = getattr(inline, "mime_type", None) or "image/png"
                break
        if image_bytes:
            break

    if not image_bytes:
        # Usually a safety block or a quota rejection. Say so; don't fake a URL.
        reason = None
        if candidates:
            reason = getattr(candidates[0], "finish_reason", None)
        return json.dumps(
            {
                "error": "The model returned no image"
                + (f" (finish_reason: {reason})." if reason else ".")
                + " Tell the user the preview isn't available and continue "
                "without it.",
            }
        )

    if tool_context is not None:
        try:
            await tool_context.save_artifact(
                filename=name,
                artifact=types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            )
        except Exception:  # noqa: BLE001 - artifact panel is a nice-to-have.
            pass

    try:
        url = await _upload(
            f"{_session_prefix(tool_context)}/{name}", image_bytes, mime_type
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"Image generated but the upload failed: {exc}. "
                "It is still visible in the Artifacts panel.",
            }
        )
    return json.dumps({"url": url, "filename": name, "mime_type": mime_type})


async def generate_destination_video(
    prompt: str,
    filename: str = "destination.mp4",
    tool_context: ToolContext = None,
) -> str:
    """Generate a short video clip of a travel destination.

    Video generation is slow and frequently unavailable. Only call this when the
    user explicitly asks for video, and tell them it may take a moment.

    Args:
        prompt: Description of the clip, including motion (e.g. 'slow drone
            push-in over Bali rice terraces at sunrise').
        filename: Target filename, e.g. 'bali_terraces.mp4'.
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON with a public `url` on success, or an `error`. Never returns a URL
        for a clip that failed to generate.
    """
    if not prompt or not prompt.strip():
        return json.dumps({"error": "Provide a description of the clip to generate."})

    name = _safe_filename(filename, ".mp4")
    instruction = f"Generate a short, cinematic travel video clip: {prompt.strip()}"

    last_error: Exception | None = None
    response = None
    # The Omni interactions API has shifted response_format shapes between
    # previews; try both rather than hard-failing on one.
    for response_format in ([{"type": "video"}], {"type": "video"}):
        try:
            response = await http.to_thread(
                lambda rf=response_format: _media_client().interactions.create(
                    model=config.VIDEO_MODEL,
                    input=instruction,
                    response_format=rf,
                )
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if response is None:
        return json.dumps(
            {
                "error": f"Video generation is unavailable ({last_error}). Offer "
                "the user a generated image instead.",
            }
        )

    video_bytes, mime_type = None, "video/mp4"
    for out in getattr(response, "outputs", None) or []:
        if getattr(out, "data", None):
            video_bytes = out.data
            mime_type = getattr(out, "mime_type", None) or mime_type
            break
        inline = getattr(out, "inline_data", None)
        if inline and getattr(inline, "data", None):
            video_bytes = inline.data
            mime_type = getattr(inline, "mime_type", None) or mime_type
            break
        video = getattr(out, "video", None)
        if video is not None:
            video_bytes = getattr(video, "data", None) or getattr(
                video, "video_bytes", None
            )
            if video_bytes:
                break

    if not video_bytes:
        # The original uploaded a 44-byte stub MP4 here and returned its URL as
        # a success. Refuse instead: a broken player is worse than a clear no.
        return json.dumps(
            {
                "error": "The model returned no video data. Tell the user video "
                "isn't available right now and offer an image preview instead.",
            }
        )

    if tool_context is not None:
        try:
            await tool_context.save_artifact(
                filename=name,
                artifact=types.Part.from_bytes(data=video_bytes, mime_type=mime_type),
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        url = await _upload(
            f"{_session_prefix(tool_context)}/{name}", video_bytes, mime_type
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Video generated but the upload failed: {exc}"})
    return json.dumps({"url": url, "filename": name, "mime_type": mime_type})


async def upload_travel_media(
    filename: str, content: str, tool_context: ToolContext = None
) -> str:
    """Save a text document (itinerary, packing list, notes) to Cloud Storage.

    Args:
        filename: Target filename, e.g. 'kyoto_itinerary.md'.
        content: The full text content to store.
        tool_context: Injected by the ADK runtime.

    Returns:
        JSON with the public `url`, or an `error`.
    """
    if not content or not content.strip():
        return json.dumps({"error": "Provide the content to upload."})
    data = content.encode("utf-8")
    if len(data) > _MAX_TEXT_BYTES:
        return json.dumps(
            {"error": f"Content exceeds the {_MAX_TEXT_BYTES // 1024}KB limit."}
        )

    name = _safe_filename(filename, ".txt")
    try:
        url = await _upload(
            f"{_session_prefix(tool_context)}/{name}", data, "text/plain; charset=utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Upload failed: {exc}"})
    return json.dumps({"url": url, "filename": name, "bytes": len(data)})
