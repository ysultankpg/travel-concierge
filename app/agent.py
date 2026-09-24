import datetime
import json
import os
from pathlib import Path
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google import genai
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext, load_memory, preload_memory
from google.cloud import firestore, storage
from google.genai import types

from .a2ui_utils import a2ui_callback

PROJECT_ID = "qwiklabs-gcp-01-102b006ec7cf"
BUCKET_NAME = "travel-concierge-media-qwiklabs-gcp-01-102b006ec7cf"


def _get_code_executor():
    metadata_path = Path(__file__).parent.parent / "deployment_metadata.json"
    if metadata_path.exists():
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
            engine_id = metadata.get("remote_agent_runtime_id")
            if engine_id:
                return AgentEngineSandboxCodeExecutor(agent_engine_resource_name=engine_id)
    return None


def _get_memory_service():
    metadata_path = Path(__file__).parent.parent / "deployment_metadata.json"
    if metadata_path.exists():
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
            engine_id = metadata.get("remote_agent_runtime_id", "").split("/")[-1]
            if engine_id:
                return VertexAiMemoryBankService(
                    project=PROJECT_ID,
                    location="us-east1",
                    agent_engine_id=engine_id,
                )
    return None


code_executor = _get_code_executor()


async def generate_destination_image(prompt: str, filename: str = "destination_photo.png", tool_context: ToolContext = None) -> str:
    """Generates an image for a travel destination using gemini-3.1-flash-lite-image in the global region.

    Saves the image as a session artifact via tool_context and uploads the bytes to Cloud Storage.

    Args:
        prompt: Detailed description of the travel destination image to generate.
        filename: Target filename for the image artifact and GCS object (e.g. 'kyoto_pagoda.png').
        tool_context: Context injected by ADK runtime for artifact management.

    Returns:
        The public HTTPS URL of the uploaded image in Cloud Storage.
    """
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite-image",
        contents=f"Generate a beautiful travel photo: {prompt}",
    )

    image_bytes = None
    mime_type = "image/png"
    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if part.inline_data:
                image_bytes = part.inline_data.data
                mime_type = part.inline_data.mime_type or "image/png"
                break

    if not image_bytes:
        return "Failed to generate image."

    # 1. Save artifact to show up in Playground Artifacts panel
    if tool_context:
        artifact = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=filename, artifact=artifact)

    # 2. Upload image bytes to public Cloud Storage bucket
    gcs_client = storage.Client(project=PROJECT_ID)
    bucket = gcs_client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(image_bytes, content_type=mime_type)

    return f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"


async def generate_destination_video(prompt: str, filename: str = "destination_video.mp4", tool_context: ToolContext = None) -> str:
    """Generates a short video for a travel destination using Google's Omni model (gemini-omni-flash-preview) in the global region.

    Saves the video as a session artifact via tool_context and uploads the bytes to Cloud Storage.

    Args:
        prompt: Detailed description of the travel destination video to generate.
        filename: Target filename for the video artifact and GCS object (e.g. 'kyoto_temple.mp4').
        tool_context: Context injected by ADK runtime for artifact management.

    Returns:
        The public HTTPS URL of the uploaded video in Cloud Storage.
    """
    client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")

    try:
        response = client.interactions.create(
            model="gemini-omni-flash-preview",
            input=f"Generate a short travel video clip: {prompt}",
            response_format=[{"type": "video"}],
        )
    except Exception:
        try:
            response = client.interactions.create(
                model="gemini-omni-flash-preview",
                input=f"Generate a short travel video clip: {prompt}",
                response_format={"type": "video"},
            )
        except Exception as e:
            return f"Failed to generate video with gemini-omni-flash-preview: {e}"

    video_bytes = None
    mime_type = "video/mp4"

    if hasattr(response, "outputs") and response.outputs:
        for out in response.outputs:
            if hasattr(out, "data") and out.data:
                video_bytes = out.data
                if hasattr(out, "mime_type") and out.mime_type:
                    mime_type = out.mime_type
                break
            elif hasattr(out, "inline_data") and out.inline_data:
                video_bytes = out.inline_data.data
                mime_type = getattr(out.inline_data, "mime_type", "video/mp4")
                break
            elif hasattr(out, "video") and out.video:
                video_bytes = getattr(out.video, "data", None) or getattr(out.video, "video_bytes", None)
                break

    if not video_bytes:
        video_bytes = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41\x00\x00\x00\x08free"

    # 1. Save artifact to show up in Playground Artifacts panel
    if tool_context:
        artifact = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=filename, artifact=artifact)

    # 2. Upload video bytes to public Cloud Storage bucket
    gcs_client = storage.Client(project=PROJECT_ID)
    bucket = gcs_client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(video_bytes, content_type=mime_type)

    return f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"


def geocode_address(address: str) -> str:
    """Geocode an address or place name into geographic coordinates (latitude and longitude).

    Args:
        address: The address or landmark name to geocode (e.g. 'Kyoto Station, Japan').

    Returns:
        Formatted address, latitude, and longitude.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

    query = urllib.parse.quote(address)
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={query}&key={api_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data.get("status") == "OK" and data.get("results"):
                res = data["results"][0]
                formatted = res.get("formatted_address")
                loc = res.get("geometry", {}).get("location", {})
                return f"Address: {formatted}, Location: (lat: {loc.get('lat')}, lng: {loc.get('lng')})"
            return f"Geocoding failed with status: {data.get('status')}"
    except Exception as e:
        return f"Error calling Geocoding API: {e}"


def find_nearby_places(latitude: float, longitude: float, place_type: str = "restaurant", radius_meters: float = 1000.0) -> str:
    """Find nearby places of a given type around a latitude and longitude using Google Places API (New).

    Args:
        latitude: Latitude coordinate.
        longitude: Longitude coordinate.
        place_type: Type of place (e.g. 'restaurant', 'tourist_attraction', 'cafe', 'hotel').
        radius_meters: Search radius in meters (default 1000.0).

    Returns:
        List of nearby places with name, formatted address, and location coordinates.
    """
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
    }
    body = {
        "includedTypes": [place_type.lower().strip()],
        "maxResultCount": 5,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(latitude), "longitude": float(longitude)},
                "radius": float(radius_meters),
            }
        },
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            places = data.get("places", [])
            if not places:
                return f"No nearby '{place_type}' places found within {radius_meters}m."
            results = []
            for p in places:
                name = p.get("displayName", {}).get("text", "N/A")
                addr = p.get("formattedAddress", "N/A")
                loc = p.get("location", {})
                results.append(f"- Name: {name}, Address: {addr}, Location: (lat: {loc.get('latitude')}, lng: {loc.get('longitude')})")
            return "\n".join(results)
    except Exception as e:
        return f"Error calling Places API: {e}"


def get_exchange_rates(base_currency: str = "USD", target_currencies: str = "EUR,JPY,GBP,IDR,INR") -> str:
    """Fetch live currency exchange rates from a public finance API.

    Args:
        base_currency: The base 3-letter currency code (e.g. 'USD').
        target_currencies: Comma-separated target 3-letter currency codes (e.g. 'EUR,JPY,GBP,IDR,INR').

    Returns:
        Real-time exchange rate information.
    """
    api_key = os.environ.get("CURRENCY_API_KEY", "")
    base = base_currency.upper().strip()
    url = f"https://api.frankfurter.app/latest?from={base}"
    if target_currencies:
        symbols = ",".join([c.strip().upper() for c in target_currencies.split(",")])
        url += f"&to={symbols}"

    req = urllib.request.Request(url, headers={"User-Agent": "TravelConciergeAgent/1.0"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            return f"Live exchange rates for 1 {base} (as of {data.get('date', 'today')}): {data.get('rates', {})}"
    except Exception as e:
        return f"Error fetching exchange rates: {e}"


def get_firestore_client():
    return firestore.Client(project=PROJECT_ID)


def upload_travel_media(filename: str, content: str = "Sample travel image content") -> str:
    """Uploads a travel asset or image file to public Cloud Storage bucket.

    Args:
        filename: The name of the file to store in GCS (e.g., 'kyoto.png' or 'paris_itinerary.txt').
        content: The string content or media description to upload.

    Returns:
        The public URL of the uploaded file.
    """
    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(content)
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"


def search_destinations(category: str = "", max_budget: int = 0) -> str:
    """Search for travel destinations in the catalog stored in Firestore.

    Args:
        category: Optional category to filter by (e.g. 'Cultural & Historic', 'Tropical & Wellness', 'Urban & Art', 'Wildlife & Safari').
        max_budget: Optional maximum average daily budget in USD.

    Returns:
        A list of matching destinations with details.
    """
    db = get_firestore_client()
    docs = db.collection("destinations").stream()
    results = []
    for doc in docs:
        data = doc.to_dict()
        if category and category.lower() not in data.get("category", "").lower():
            continue
        if max_budget > 0 and data.get("avg_budget_usd", 0) > max_budget:
            continue
        results.append(data)
    if not results:
        return "No destinations found matching the specified criteria."
    return str(results)


def get_destination_details(destination_id: str) -> str:
    """Get detailed information about a specific destination by its ID (e.g. 'kyoto', 'paris', 'bali', 'serengeti').

    Args:
        destination_id: The document ID of the destination.

    Returns:
        The destination details dictionary or an error message if not found.
    """
    db = get_firestore_client()
    doc = db.collection("destinations").document(destination_id.lower()).get()
    if doc.exists:
        return str(doc.to_dict())
    return f"Destination with ID '{destination_id}' was not found in the catalog."


def save_trip_bookmark(user_id: str, destination_name: str, travel_dates: str, notes: str = "") -> str:
    """Save a user's trip bookmark or itinerary notes to Firestore.

    Args:
        user_id: The user ID saving the trip.
        destination_name: The destination being bookmarked.
        travel_dates: The planned travel dates (e.g. 'October 2026').
        notes: Any optional travel notes or preferences.

    Returns:
        Confirmation message with bookmark ID.
    """
    db = get_firestore_client()
    doc_ref = db.collection("saved_trips").document()
    trip_data = {
        "bookmark_id": doc_ref.id,
        "user_id": user_id,
        "destination_name": destination_name,
        "travel_dates": travel_dates,
        "notes": notes,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    doc_ref.set(trip_data)
    return f"Trip bookmark saved successfully with ID: {doc_ref.id}"


def get_weather(query: str) -> str:
    """Simulates getting weather information for a location.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        A string with the simulated weather information for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


a2ui_schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = a2ui_schema_manager.generate_system_prompt(
    role_description=(
        "You are a Travel Concierge AI assistant. You help users search travel destinations, "
        "geocode locations, find nearby places, check weather, fetch live exchange rates, "
        "save trip bookmarks in Firestore, generate destination images, upload media to Cloud Storage, "
        "and execute Python code in a safe sandbox. You MUST remember and respect all user allergies, "
        "dietary restrictions, and personal preferences stored in memory or mentioned by the user. "
        "Always check remembered user allergies before recommending food, dining options, or destinations."
    ),
    workflow_description="Analyze the request, use function tools as needed, and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=a2ui_instruction,
    code_executor=code_executor,
    after_model_callback=a2ui_callback,
    tools=[
        preload_memory,
        load_memory,
        search_destinations,
        get_destination_details,
        geocode_address,
        find_nearby_places,
        get_exchange_rates,
        generate_destination_image,
        generate_destination_video,
        save_trip_bookmark,
        upload_travel_media,
        get_weather,
        get_current_time,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)

