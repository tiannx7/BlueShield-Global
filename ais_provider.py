
import asyncio
import json
import os

import websockets
from dotenv import load_dotenv

load_dotenv()

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"


async def test_ais_connection():
    api_key = os.getenv("AISSTREAM_API_KEY")

    if not api_key:
        print("ERROR: API key not loaded from .env")
        return

    subscription = {
        "APIKey": api_key,
        "BoundingBoxes": [
    [[48, -6], [52, 3]]
],
        "FilterMessageTypes": ["PositionReport"],
    }

    try:
        async with websockets.connect(
            AISSTREAM_URL,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            print("WebSocket connected.")
            await websocket.send(json.dumps(subscription))
            print("Subscription sent.")

            try:
                async with asyncio.timeout(60):
                    while True:
                        raw = await websocket.recv()

                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            print("Received non-JSON message.")
                            continue

                        message_type = data.get("MessageType", "Unknown")
                        print("Message type received:", message_type)

                        if message_type == "SubscriptionConfirmation":
                            print("Subscription accepted:", data.get("Message"))
                        elif message_type == "PositionReport":
                            metadata = data.get("MetaData", {})
                            position = data.get("Message", {}).get(
                                "PositionReport", {}
                            )

                            print("Vessel:", metadata.get("ShipName", "Unknown"))
                            print("MMSI:", metadata.get("MMSI"))
                            print(
                                "Latitude:",
                                metadata.get("Latitude", position.get("Latitude")),
                            )
                            print(
                                "Longitude:",
                                metadata.get("Longitude", position.get("Longitude")),
                            )
                            print("Speed (knots):", position.get("Sog"))
                            print("Position received successfully!")
                            return

                        elif message_type.lower() in ("error", "subscriptionerror"):
                            print("Service error:", data)
                            return

            except TimeoutError:
                print("No position report received in 60 seconds.")
                print("Check whether the subscription was accepted.")

    except Exception as exc:
        print("Connection error:", type(exc).__name__)
        print("Details:", str(exc)[:300])


if __name__ == "__main__":
    asyncio.run(test_ais_connection())
