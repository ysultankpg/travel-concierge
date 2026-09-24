# Travel Concierge AI

![Travel Concierge AI Demo](./demo.gif)

A conversational AI travel concierge agent that helps travelers discover personalized destinations, plan custom itineraries, look up live places and geocoding data, manage trip bookmarks, and generate rich travel media. Built using the Google Agent Development Kit (ADK) and deployed to Agent Platform with A2UI rich interface rendering.

---

## Capabilities & Architecture

Based on the actual code implementation in [`app/agent.py`](file:///config/.gemini/antigravity/scratch/travel-concierge/app/agent.py) and [`frontend/`](file:///config/.gemini/antigravity/scratch/travel-concierge/frontend), the system integrates the following Google Cloud services, tools, and frameworks:

### Google Cloud Services & Infrastructure
* **Vertex AI Reasoning Engine (Agent Platform)**: Hosts the agent runtime and orchestrates tool execution.
* **Vertex AI Memory Bank (`VertexAiMemoryBankService`)**: Persists user travel preferences, dietary restrictions, budget constraints, and past travel history across conversations.
* **Google Cloud Firestore (`google-cloud-firestore`)**:
  * **Destinations Catalog**: Searches curated travel destinations filtered by category and daily budget (`search_destinations`, `get_destination_details`).
  * **Trip Bookmarks**: Saves user itinerary bookmarks and notes to the `saved_trips` collection (`save_trip_bookmark`).
* **Google Cloud Storage (`google-cloud-storage`)**:
  * Uploads generated travel media and assets to a public GCS bucket (`upload_travel_media`, `generate_destination_image`, `generate_destination_video`).
* **Agent Engine Code Execution Sandbox (`AgentEngineSandboxCodeExecutor`)**:
  * Executes Python code in a secure sandbox for dynamic itinerary budget calculations and math.

### Integrated Tools & APIs
* **Image Generation (`generate_destination_image`)**: Generates destination artwork using `gemini-3.1-flash-lite-image` in the `global` region, saves local session artifacts, and uploads bytes to Cloud Storage.
* **Video Generation (`generate_destination_video`)**: Generates short travel video clips using `gemini-omni-flash-preview` in the `global` region, saves local session artifacts, and uploads bytes to Cloud Storage.
* **Google Maps Geocoding API (`geocode_address`)**: Converts addresses and landmark names into latitude/longitude coordinates.
* **Google Places API New (`find_nearby_places`)**: Finds nearby hotels, restaurants, cafes, and attractions around coordinates.
* **Live Exchange Rates (`get_exchange_rates`)**: Fetches real-time currency conversion rates via Frankfurter API.
* **Weather & Time Utilities**: Provides local weather forecasts (`get_weather`) and timezone-aware local time (`get_current_time`).

### Frontend & Rich UI (A2UI)
* **A2UI v0.8 Schema Integration (`a2ui-agent-sdk`)**: Uses `A2uiSchemaManager` to stream structured UI cards (Cards, Columns, Rows, Text, Images) directly into the chat interface.
* **FastAPI A2A Proxy (`frontend/main.py`)**: Communicates with the deployed Agent Engine via the Agent-to-Agent (A2A) protocol (`a2a-sdk`).
* **Custom Chat UI (`frontend/static/index.html`)**: Responsive dialogue UI featuring an Ocean Teal theme, Google Fonts (`Plus Jakarta Sans`), live status indicator, and quick-prompt chips.

---

## Planned / Future Enhancements

The following feature listed in the initial project brief is planned for future iterations:
* **Cloud Trace Observability**: Full distributed tracing for agent tool invocations *(planned, not yet implemented)*.

---

## Local Setup & Development

Follow these steps to run the agent and frontend locally on your workstation:

### Prerequisites
* Python 3.11+
* `uv` package manager installed

### 1. Install Dependencies
```bash
uv pip install -r requirements.txt
```

### 2. Set Environment Variables
```bash
export GOOGLE_MAPS_API_KEY="your-google-maps-api-key"
export AGENT_ENGINE_RESOURCE_NAME="projects/<project-id>/locations/<region>/reasoningEngines/<engine-id>"
export AGENT_DIRECTORY="app"
```

### 3. Option A: Test with ADK Playground
Run the ADK web server to launch the interactive agent playground:
```bash
uv run adk web . --port 8080 --reload_agents
```

### 4. Option B: Run the Custom Web Frontend
Start the FastAPI proxy server to serve the rebranded chat interface:
```bash
cd frontend
uv run python main.py
```

---

## Deployment Instructions

To deploy the agent to Google Cloud Agent Platform using `agents-cli`:

```bash
agents-cli deploy --no-confirm-project --update-env-vars GOOGLE_MAPS_API_KEY=$GOOGLE_MAPS_API_KEY
```
