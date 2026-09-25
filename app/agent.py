"""Travel Concierge agent definition.

This module is deliberately thin: configuration lives in `config.py`, the
persona and workflow in `prompts.py`, and each tool group in `tools/`. The
original version of this file was 463 lines mixing all four concerns, which made
the missing persona easy to overlook and the tools impossible to unit test.
"""

from __future__ import annotations

import logging

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import load_memory, preload_memory
from google.genai import types

from . import config, prompts, tools
from .a2ui_utils import a2ui_callback

logger = logging.getLogger(__name__)


def _build_code_executor() -> AgentEngineSandboxCodeExecutor | None:
    """Sandbox for budget arithmetic. Absent locally, which is fine."""
    resource = config.agent_engine_resource()
    if not resource:
        logger.info("No Agent Engine resource; code execution disabled.")
        return None
    try:
        return AgentEngineSandboxCodeExecutor(agent_engine_resource_name=resource)
    except Exception as exc:  # noqa: BLE001 - never block startup on this.
        logger.warning("Code executor unavailable: %s", exc)
        return None


def _build_memory_service() -> VertexAiMemoryBankService | None:
    """Deprecated shim — kept so external imports don't break.

    Long-term memory is now owned by `app.app_utils.services.get_memory_service`
    and attached to the Runner, which is the only place ADK actually reads it
    from. The original code built a memory service here and never passed it
    anywhere, so `load_memory` always saw an empty bank.
    """
    from .app_utils.services import get_memory_service

    service = get_memory_service()
    return service if isinstance(service, VertexAiMemoryBankService) else None


_schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

INSTRUCTION = _schema_manager.generate_system_prompt(
    role_description=prompts.role_description(),
    workflow_description=prompts.workflow_description(),
    ui_description=prompts.ui_description(),
    include_schema=True,
    include_examples=True,
)

TOOLS = [
    # Memory first: preload_memory injects known traveller context before the
    # model plans, load_memory lets it search explicitly.
    preload_memory,
    load_memory,
    # Catalog
    tools.search_destinations,
    tools.get_destination_details,
    # Trip bookmarks (user id comes from the session, not the model)
    tools.save_trip_bookmark,
    tools.list_trip_bookmarks,
    tools.delete_trip_bookmark,
    # Location and conditions
    tools.geocode_address,
    tools.find_nearby_places,
    tools.get_weather,
    tools.get_current_time,
    # Money
    tools.get_exchange_rates,
    tools.convert_currency,
    # Media
    tools.generate_destination_image,
    tools.generate_destination_video,
    tools.upload_travel_media,
]

root_agent = Agent(
    name="root_agent",
    description=(
        "A senior travel planner that recommends destinations, builds itineraries, "
        "checks live weather and exchange rates, and remembers traveller "
        "preferences and dietary restrictions."
    ),
    model=Gemini(
        model=config.TEXT_MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    code_executor=_build_code_executor(),
    after_model_callback=a2ui_callback,
    generate_content_config=types.GenerateContentConfig(
        # A2UI output is JSON against a fixed schema — near-zero temperature
        # keeps the model from improvising component names and producing a
        # surface the renderer drops on the floor.
        temperature=0.2,
        max_output_tokens=8192,
    ),
    tools=TOOLS,
)

app = App(
    root_agent=root_agent,
    name="app",
)
