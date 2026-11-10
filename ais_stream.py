import asyncio
import json
import os
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

load_dotenv()

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
GLOBAL_BOUNDING_BOX = [[[-90, -180], [90, 180]]]

POSITION_MESSAGE_TYPES = [
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
]
STATIC_MESSAGE_TYPES = ["ShipStaticData", "StaticDataReport"]
AIS_MESSAGE_TYPES = POSITION_MESSAGE_TYPES + STATIC_MESSAGE_TYPES


def _configured_bounding_boxes():
    raw = os.getenv("AISSTREAM_BOUNDING_BOXES", "").strip()
    if not raw:
        return GLOBAL_BOUNDING_BOX
    try:
        boxes = json.loads(raw)
        if not isinstance(boxes, list) or not boxes:
            raise ValueError("AISSTREAM_BOUNDING_BOXES must be a non-empty JSON array")
        return boxes
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid AISSTREAM_BOUNDING_BOXES: {exc}") from exc


def normalize_position(data):
    """Normalize supported AIS position messages into BlueShield fields."""
    message_type = data.get("MessageType")
    if message_type not in POSITION_MESSAGE_TYPES:
        return None
    message = data.get("Message", {}).get(message_type, {})
    metadata = data.get("MetaData", {})
    mmsi = metadata.get("MMSI") or message.get("UserID")
    latitude, longitude = message.get("Latitude"), message.get("Longitude")
    if mmsi is None or latitude is None or longitude is None:
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {
        "event_type": "position",
        "mmsi": str(mmsi),
        "name": (metadata.get("ShipName") or "").strip() or None,
        "latitude": latitude,
        "longitude": longitude,
        "speed_knots": message.get("Sog"),
        "course": message.get("Cog"),
        "heading": message.get("TrueHeading"),
        "navigation_status": message.get("NavigationalStatus"),
        "position_timestamp": metadata.get("time_utc"),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "aisstream",
        "ais_message_type": message_type,
    }


def normalize_static_data(data):
    """Normalize AIS static/voyage reports; retain only identity metadata."""
    message_type = data.get("MessageType")
    if message_type not in STATIC_MESSAGE_TYPES:
        return None
    message = data.get("Message", {}).get(message_type, {}) or {}
    metadata = data.get("MetaData", {}) or {}
    mmsi = metadata.get("MMSI") or message.get("UserID")
    if mmsi is None:
        return None

    # ShipStaticData uses direct fields. StaticDataReport may split fields into
    # ReportA/ReportB; merge both without assuming every field is present.
    source = dict(message)
    for part_name in ("ReportA", "ReportB"):
        part = message.get(part_name)
        if isinstance(part, dict):
            source.update(part)

    def clean(value):
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip().replace("@", "").strip()
            return value or None
        return value

    dimensions = source.get("Dimension") or source.get("Dimensions")
    if isinstance(dimensions, dict):
        dimensions = {str(k): v for k, v in dimensions.items()}

    imo = clean(source.get("ImoNumber") or source.get("IMO") or source.get("Imo"))
    if imo in (0, "0"):
        imo = None

    draught = source.get("MaximumStaticDraught") or source.get("Draught")
    try:
        draught = float(draught) if draught is not None else None
    except (TypeError, ValueError):
        draught = None
    eta_value = source.get("Eta") or source.get("ETA")
    if isinstance(eta_value, dict):
        eta_value = json.dumps(eta_value, separators=(",", ":"), sort_keys=True)
    else:
        eta_value = clean(eta_value)

    return {
        "event_type": "static",
        "mmsi": str(mmsi),
        "imo": str(imo) if imo is not None else None,
        "call_sign": clean(source.get("CallSign") or source.get("Callsign")),
        "ship_name": clean(source.get("Name") or source.get("ShipName") or metadata.get("ShipName")),
        "ship_type": clean(source.get("Type") or source.get("ShipType")),
        "destination": clean(source.get("Destination")),
        "draught": draught,
        "eta": eta_value,
        "dimensions": dimensions,
        "ais_version": clean(source.get("AisVersion") or source.get("AISVersion")),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "aisstream",
        "ais_message_type": message_type,
    }


async def stream_positions(bounding_boxes=None):
    """Yield normalized position and static identity events from AISStream."""
    api_key = os.getenv("AISSTREAM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AISSTREAM_API_KEY is missing from the environment")
    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": bounding_boxes or _configured_bounding_boxes(),
        "FilterMessageTypes": AIS_MESSAGE_TYPES,
    }
    async with websockets.connect(
        AISSTREAM_URL,
        open_timeout=15,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=2**22,
    ) as websocket:
        await websocket.send(json.dumps(subscription))
        async for raw in websocket:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            if data.get("MessageType") == "Error":
                raise RuntimeError(f"AISStream error: {str(data)[:500]}")
            event = normalize_position(data) or normalize_static_data(data)
            if event is not None:
                yield event


async def test_stream():
    count = 0
    try:
        async with asyncio.timeout(60):
            async for event in stream_positions():
                print(f"AIS {event['event_type']} | MMSI={event['mmsi']} | type={event['ais_message_type']}")
                count += 1
                if count >= 5:
                    break
    except TimeoutError:
        print(f"Test timed out. Valid AIS messages received: {count}")
    except Exception as exc:
        print("AIS stream test failed:", type(exc).__name__, str(exc)[:300])


if __name__ == "__main__":
    asyncio.run(test_stream())
