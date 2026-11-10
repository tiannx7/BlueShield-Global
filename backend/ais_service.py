import asyncio
import logging

from backend.ais_stream import stream_positions
from backend.database import SessionLocal
from backend.repository import save_ais_position, save_vessel_identity

logger = logging.getLogger("blueshield.ais")

AIS_STATUS = {
    "state": "starting",
    "last_message_at": None,
    "last_saved_mmsi": None,
    "saved_positions": 0,
    "saved_static_reports": 0,
    "message_types": {},
    "reconnect_count": 0,
    "last_error": None,
}


def _save_event_in_own_session(event: dict) -> str:
    db = SessionLocal()
    try:
        if event.get("event_type") == "static":
            saved = save_vessel_identity(db, event)
            return str(saved.mmsi)
        saved = save_ais_position(db, event)
        if not getattr(saved, "mmsi", None):
            raise RuntimeError("save_ais_position did not return a saved AIS record")
        return str(saved.mmsi)
    finally:
        db.close()


async def run_ais_service():
    """Continuously ingest AIS positions and static identity reports."""
    retry_delay = 5
    while True:
        try:
            AIS_STATUS["state"] = "connecting"
            async for event in stream_positions():
                saved_mmsi = await asyncio.to_thread(_save_event_in_own_session, event)
                AIS_STATUS["last_message_at"] = event.get("received_at")
                AIS_STATUS["last_saved_mmsi"] = saved_mmsi
                msg_type = event.get("ais_message_type", "unknown")
                counts = AIS_STATUS.setdefault("message_types", {})
                counts[msg_type] = counts.get(msg_type, 0) + 1
                if event.get("event_type") == "static":
                    AIS_STATUS["saved_static_reports"] = int(AIS_STATUS.get("saved_static_reports", 0)) + 1
                else:
                    AIS_STATUS["saved_positions"] = int(AIS_STATUS.get("saved_positions", 0)) + 1
                AIS_STATUS["last_error"] = None
                AIS_STATUS["state"] = "connected"
                retry_delay = 5
            AIS_STATUS["state"] = "reconnecting"
            AIS_STATUS["reconnect_count"] += 1
            logger.warning("AIS stream ended; reconnecting in %s seconds", retry_delay)
        except asyncio.CancelledError:
            AIS_STATUS["state"] = "stopped"
            raise
        except Exception as exc:
            AIS_STATUS["state"] = "reconnecting"
            AIS_STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            AIS_STATUS["reconnect_count"] += 1
            logger.exception("AIS stream/save failure; reconnecting in %s seconds", retry_delay)
        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)
