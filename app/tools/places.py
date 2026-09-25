"""Location tools: geocoding, nearby places, real weather, real local time.

Notable fixes vs. the original:
  * `get_weather` was a stub returning "90 degrees and sunny" for everywhere
    except San Francisco. It now calls Open-Meteo (no API key required) for a
    real current reading plus a multi-day forecast.
  * `get_current_time` only knew about San Francisco and returned an apology for
    every other city on earth. It now resolves the IANA timezone for any place.
  * Geocoding required a Google Maps key; Open-Meteo's geocoder is the fallback
    so the agent degrades gracefully instead of erroring out.
"""

from __future__ import annotations

import datetime as _dt
import json
from zoneinfo import ZoneInfo

from .. import config
from . import http

_OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
_GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
_PLACES_NEARBY = "https://places.googleapis.com/v1/places:searchNearby"

# WMO weather interpretation codes -> short human text.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle",
    53: "moderate drizzle", 55: "dense drizzle", 56: "light freezing drizzle",
    57: "dense freezing drizzle", 61: "light rain", 63: "moderate rain",
    65: "heavy rain", 66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains", 80: "light rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "light snow showers",
    86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(message: str) -> str:
    return json.dumps({"error": message})


async def _resolve(place: str) -> dict:
    """Best-effort geocode of a free-text place name.

    Tries Google (richer, needs a key) then Open-Meteo (keyless). Returns a dict
    with name/latitude/longitude/timezone, or {"error": ...}.
    """
    key = config.maps_api_key()
    if key:
        data = await http.get_json(
            _GOOGLE_GEOCODE, params={"address": place, "key": key}
        )
        if not data.get("error") and data.get("status") == "OK" and data.get("results"):
            top = data["results"][0]
            loc = top.get("geometry", {}).get("location", {})
            if loc.get("lat") is not None:
                return {
                    "name": top.get("formatted_address", place),
                    "latitude": loc["lat"],
                    "longitude": loc["lng"],
                    "source": "google_geocoding",
                }

    data = await http.get_json(
        _OPEN_METEO_GEOCODE, params={"name": place, "count": 1, "language": "en"}
    )
    if data.get("error"):
        return {"error": f"Could not look up '{place}': {data['error']}"}
    results = data.get("results") or []
    if not results:
        return {"error": f"No location found matching '{place}'."}
    top = results[0]
    label = ", ".join(
        p for p in (top.get("name"), top.get("admin1"), top.get("country")) if p
    )
    return {
        "name": label or place,
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "timezone": top.get("timezone"),
        "source": "open_meteo_geocoding",
    }


async def geocode_address(address: str) -> str:
    """Convert an address or landmark name into latitude/longitude coordinates.

    Args:
        address: Address or landmark to geocode (e.g. 'Kyoto Station, Japan').

    Returns:
        JSON with the resolved name, latitude, longitude, and data source.
    """
    if not address or not address.strip():
        return _err("Provide a place name or address to geocode.")
    resolved = await _resolve(address.strip())
    if resolved.get("error"):
        return _err(resolved["error"])
    return _ok(resolved)


async def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "restaurant",
    radius_meters: float = 1500.0,
    max_results: int = 8,
    keyword: str = "",
) -> str:
    """Find nearby places of a given type around a coordinate.

    Args:
        latitude: Latitude coordinate.
        longitude: Longitude coordinate.
        place_type: Place type, e.g. 'restaurant', 'tourist_attraction', 'cafe',
            'lodging', 'museum', 'park'.
        radius_meters: Search radius in metres (50-50000, default 1500).
        max_results: How many results to return (1-20, default 8).
        keyword: Optional free-text filter, e.g. 'vegan' or 'ramen'. Use this to
            respect dietary needs the user has told you about.

    Returns:
        JSON list of places with name, address, rating, price level, and whether
        they are currently open.
    """
    key = config.maps_api_key()
    if not key:
        return _err(
            "Nearby search needs GOOGLE_MAPS_API_KEY. I can still suggest places "
            "from the destination catalog and general knowledge."
        )

    # Clamp to the ranges the Places API accepts, rather than letting the API
    # 400 on a model-invented radius.
    radius = max(50.0, min(float(radius_meters), 50_000.0))
    count = max(1, min(int(max_results), 20))

    body: dict = {
        "includedTypes": [place_type.lower().strip().replace(" ", "_")],
        "maxResultCount": count,
        "rankPreference": "POPULARITY",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(latitude), "longitude": float(longitude)},
                "radius": radius,
            }
        },
    }
    data = await http.post_json(
        _PLACES_NEARBY,
        json_body=body,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": ",".join(
                [
                    "places.displayName",
                    "places.formattedAddress",
                    "places.location",
                    "places.rating",
                    "places.userRatingCount",
                    "places.priceLevel",
                    "places.currentOpeningHours.openNow",
                    "places.websiteUri",
                    "places.primaryTypeDisplayName",
                ]
            ),
        },
    )
    if data.get("error"):
        return _err(f"Nearby search failed: {data['error']}")

    needle = keyword.lower().strip()
    places = []
    for p in data.get("places", []):
        name = (p.get("displayName") or {}).get("text", "Unknown")
        kind = (p.get("primaryTypeDisplayName") or {}).get("text")
        if needle and needle not in f"{name} {kind or ''}".lower():
            continue
        loc = p.get("location", {})
        places.append(
            {
                "name": name,
                "type": kind,
                "address": p.get("formattedAddress"),
                "rating": p.get("rating"),
                "review_count": p.get("userRatingCount"),
                "price_level": p.get("priceLevel"),
                "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
                "website": p.get("websiteUri"),
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
            }
        )

    if not places:
        return _ok(
            {
                "places": [],
                "note": f"No '{place_type}' results within {radius:g}m"
                + (f" matching '{keyword}'." if needle else "."),
            }
        )
    places.sort(key=lambda p: (p["rating"] or 0), reverse=True)
    return _ok({"places": places, "count": len(places)})


async def get_weather(location: str, forecast_days: int = 3) -> str:
    """Get the real current weather and a short forecast for any location.

    Args:
        location: City, region, or landmark (e.g. 'Kyoto', 'Bali, Indonesia').
        forecast_days: Days of daily forecast to include (1-7, default 3).

    Returns:
        JSON with current conditions and a daily high/low/precipitation forecast.
    """
    if not location or not location.strip():
        return _err("Provide a location to get weather for.")

    resolved = await _resolve(location.strip())
    if resolved.get("error"):
        return _err(resolved["error"])

    days = max(1, min(int(forecast_days), 7))
    data = await http.get_json(
        _OPEN_METEO_FORECAST,
        params={
            "latitude": resolved["latitude"],
            "longitude": resolved["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": days,
        },
    )
    if data.get("error"):
        return _err(f"Weather lookup failed: {data['error']}")

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    out = {
        "location": resolved["name"],
        "timezone": data.get("timezone"),
        "current": {
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_pct": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_kmh": current.get("wind_speed_10m"),
            "conditions": _WMO.get(current.get("weather_code"), "unknown"),
            "observed_at": current.get("time"),
        },
        "forecast": [
            {
                "date": date,
                "high_c": daily.get("temperature_2m_max", [None] * days)[i],
                "low_c": daily.get("temperature_2m_min", [None] * days)[i],
                "rain_chance_pct": daily.get(
                    "precipitation_probability_max", [None] * days
                )[i],
                "conditions": _WMO.get(
                    daily.get("weather_code", [None] * days)[i], "unknown"
                ),
            }
            for i, date in enumerate(daily.get("time", []))
        ],
    }
    return _ok(out)


async def get_current_time(location: str) -> str:
    """Get the current local time and UTC offset for any city worldwide.

    Args:
        location: City or place name (e.g. 'Tokyo', 'Serengeti, Tanzania').

    Returns:
        JSON with the resolved place, IANA timezone, local time, and UTC offset.
    """
    if not location or not location.strip():
        return _err("Provide a city to get the local time for.")

    resolved = await _resolve(location.strip())
    if resolved.get("error"):
        return _err(resolved["error"])

    tz_name = resolved.get("timezone")
    if not tz_name:
        # The Google geocoder doesn't return a timezone; Open-Meteo's forecast
        # endpoint resolves one from the coordinate.
        probe = await http.get_json(
            _OPEN_METEO_FORECAST,
            params={
                "latitude": resolved["latitude"],
                "longitude": resolved["longitude"],
                "current": "temperature_2m",
                "timezone": "auto",
            },
        )
        tz_name = probe.get("timezone")

    if not tz_name:
        return _err(f"Could not determine the timezone for '{location}'.")

    try:
        now = _dt.datetime.now(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - unknown tz string from upstream.
        return _err(f"Unrecognised timezone '{tz_name}' for '{location}'.")

    offset = now.utcoffset() or _dt.timedelta(0)
    hours, remainder = divmod(int(offset.total_seconds()), 3600)
    return _ok(
        {
            "location": resolved["name"],
            "timezone": tz_name,
            "local_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "day_of_week": now.strftime("%A"),
            "utc_offset": f"{hours:+03d}:{abs(remainder) // 60:02d}",
            "iso8601": now.isoformat(),
        }
    )
