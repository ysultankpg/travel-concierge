"""Central, environment-driven configuration.

Everything that used to be a hardcoded sandbox constant lives here and reads
from the environment, with a sensible fallback so local `adk web` still works.

Why this file exists: `PROJECT_ID`/`BUCKET_NAME` were literals pointing at a
qwiklabs lab project. Anyone cloning the repo got cryptic 403s from Firestore
and GCS instead of a clear "you didn't set GOOGLE_CLOUD_PROJECT" message.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Model ids are overridable so you can trade cost for quality without a code
# change (and so an eval run can pin a specific version).
TEXT_MODEL = os.environ.get("TRAVEL_TEXT_MODEL", "gemini-flash-latest")
IMAGE_MODEL = os.environ.get("TRAVEL_IMAGE_MODEL", "gemini-3.1-flash-lite-image")
VIDEO_MODEL = os.environ.get("TRAVEL_VIDEO_MODEL", "gemini-omni-flash-preview")

# Region for the generative media calls. `global` has the widest model coverage.
MEDIA_LOCATION = os.environ.get("TRAVEL_MEDIA_LOCATION", "global")
MEMORY_LOCATION = os.environ.get("TRAVEL_MEMORY_LOCATION", "us-east1")

# Network timeout (seconds) applied to every outbound HTTP call in tools/.
HTTP_TIMEOUT = float(os.environ.get("TRAVEL_HTTP_TIMEOUT", "10"))

# Firestore collections.
DESTINATIONS_COLLECTION = "destinations"
SAVED_TRIPS_COLLECTION = "saved_trips"


@functools.lru_cache(maxsize=1)
def _deployment_metadata() -> dict:
    path = _REPO_ROOT / "deployment_metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@functools.lru_cache(maxsize=1)
def project_id() -> str:
    """Resolve the GCP project from env, then deployment metadata, then ADC."""
    for key in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT", "PROJECT_ID"):
        value = os.environ.get(key)
        if value:
            return value

    # deployment_metadata.json embeds the project number in the resource name:
    # projects/<project>/locations/<loc>/reasoningEngines/<id>
    resource = _deployment_metadata().get("remote_agent_runtime_id", "")
    if resource.startswith("projects/"):
        parts = resource.split("/")
        if len(parts) > 1 and parts[1]:
            return parts[1]

    try:  # Last resort: whatever ADC is pointed at.
        import google.auth

        _, detected = google.auth.default()
        if detected:
            return detected
    except Exception:  # noqa: BLE001 - config must never crash on import.
        pass

    raise RuntimeError(
        "No GCP project configured. Set GOOGLE_CLOUD_PROJECT (see .env.example)."
    )


@functools.lru_cache(maxsize=1)
def media_bucket() -> str:
    """Bucket for generated images/video. Defaults to a per-project name."""
    explicit = os.environ.get("TRAVEL_MEDIA_BUCKET")
    if explicit:
        return explicit.replace("gs://", "").strip("/")
    return f"travel-concierge-media-{project_id()}"


@functools.lru_cache(maxsize=1)
def agent_engine_id() -> str | None:
    """Bare Agent Engine id (last path segment), or None when running locally."""
    resource = os.environ.get("AGENT_ENGINE_RESOURCE_NAME") or _deployment_metadata().get(
        "remote_agent_runtime_id", ""
    )
    return resource.rsplit("/", 1)[-1] if resource else None


@functools.lru_cache(maxsize=1)
def agent_engine_resource() -> str | None:
    """Full Agent Engine resource name, or None when running locally."""
    return (
        os.environ.get("AGENT_ENGINE_RESOURCE_NAME")
        or _deployment_metadata().get("remote_agent_runtime_id")
        or None
    )


def maps_api_key() -> str:
    """Not cached: a redeploy can rotate this without a cold start."""
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
