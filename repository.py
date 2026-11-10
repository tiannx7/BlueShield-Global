from datetime import datetime, timezone
from math import isfinite
import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.db_models import AISPosition, AISPositionHistory, VesselIdentity, VesselRecord
from backend.models import Vessel


def _parse_received_at(value) -> datetime:
    """Return a timezone-aware UTC receive timestamp."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def save_vessel(db: Session, vessel: Vessel) -> VesselRecord:
    """Insert or update a demo vessel."""
    record = db.get(VesselRecord, vessel.id)
    if record is None:
        record = VesselRecord(id=vessel.id)
        db.add(record)

    for field in (
        "name", "vessel_type", "latitude", "longitude", "speed_knots",
        "heading", "status", "data_source",
    ):
        setattr(record, field, getattr(vessel, field, None))

    try:
        db.commit()
        db.refresh(record)
    except Exception:
        db.rollback()
        raise
    return record


def get_all_vessels(db: Session) -> list[Vessel]:
    records = db.query(VesselRecord).order_by(VesselRecord.id).all()
    return [
        Vessel(
            id=record.id,
            name=record.name,
            vessel_type=record.vessel_type,
            latitude=record.latitude,
            longitude=record.longitude,
            speed_knots=record.speed_knots,
            heading=record.heading,
            status=record.status,
            data_source=record.data_source,
        )
        for record in records
    ]


def _supported_values(model, values: dict) -> dict:
    """Keep only columns supported by the installed SQLAlchemy model."""
    columns = getattr(getattr(model, "__table__", None), "columns", None)
    if columns is None:
        return values
    allowed = {column.key for column in columns}
    return {key: value for key, value in values.items() if key in allowed}


def save_ais_position(db: Session, position: dict) -> AISPosition:
    """Upsert latest AIS position and append one history observation.

    Retry only SQLite lock/busy errors. The complete upsert + history insert is
    retried as one transaction so the two tables cannot drift apart.
    """
    mmsi = str(position.get("mmsi", "")).strip()
    if not mmsi:
        raise ValueError("AIS position is missing MMSI")

    try:
        latitude = float(position["latitude"])
        longitude = float(position["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("AIS position has invalid coordinates") from exc

    if (
        not isfinite(latitude)
        or not isfinite(longitude)
        or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
    ):
        raise ValueError("AIS position has invalid latitude/longitude")

    received_at = _parse_received_at(position.get("received_at"))
    values = {
        "mmsi": mmsi,
        "name": position.get("name"),
        "latitude": latitude,
        "longitude": longitude,
        "speed_knots": position.get("speed_knots"),
        "course": position.get("course"),
        "heading": position.get("heading"),
        "navigation_status": position.get("navigation_status"),
        "position_timestamp": position.get("position_timestamp"),
        "received_at": received_at,
        "data_source": position.get("data_source") or "aisstream",
    }

    max_attempts = 6
    for attempt in range(max_attempts):
        try:
            record = db.get(AISPosition, mmsi)
            if record is None:
                record = AISPosition(mmsi=mmsi)
                db.add(record)

            for field, value in values.items():
                if hasattr(record, field) and value is not None:
                    setattr(record, field, value)

            # Preserve every received observation for historical screening.
            history_values = _supported_values(AISPositionHistory, values)
            db.add(AISPositionHistory(**history_values))

            db.commit()
            # expire_on_commit=False means the caller can safely read record.mmsi
            # without issuing an extra SELECT immediately after the write.
            return record

        except OperationalError as exc:
            db.rollback()
            message = str(exc).lower()
            is_lock_error = "database is locked" in message or "database is busy" in message
            if not is_lock_error or attempt >= max_attempts - 1:
                raise

            delay = min(0.25 * (2 ** attempt), 4.0)
            time.sleep(delay)

        except Exception:
            db.rollback()
            raise

    raise RuntimeError(f"Could not save AIS position for MMSI {mmsi}")


def save_vessel_identity(db: Session, identity: dict) -> VesselIdentity:
    """Upsert the latest static AIS identity without altering position history."""
    mmsi = str(identity.get("mmsi", "")).strip()
    if not mmsi:
        raise ValueError("AIS static report is missing MMSI")

    record = db.get(VesselIdentity, mmsi)
    if record is None:
        record = VesselIdentity(mmsi=mmsi)
        db.add(record)

    dimensions = identity.get("dimensions")
    values = {
        "imo": identity.get("imo"),
        "call_sign": identity.get("call_sign"),
        "ship_name": identity.get("ship_name"),
        "ship_type": str(identity.get("ship_type")) if identity.get("ship_type") is not None else None,
        "destination": identity.get("destination"),
        "draught": identity.get("draught"),
        "eta": identity.get("eta"),
        "dimensions_json": __import__("json").dumps(dimensions, ensure_ascii=False) if dimensions is not None else None,
        "ais_version": identity.get("ais_version"),
        "last_static_received_at": _parse_received_at(identity.get("received_at")),
        "data_source": identity.get("data_source") or "aisstream",
    }
    for field, value in values.items():
        if value is not None and hasattr(record, field):
            setattr(record, field, value)
    try:
        db.commit()
        db.refresh(record)
        return record
    except Exception:
        db.rollback()
        raise
