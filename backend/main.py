from datetime import datetime, timezone
import asyncio
import json
import os
import threading
import time
from datetime import timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import Table, Column, String, Text, MetaData, select, insert, update, or_

from backend.database import Base, SessionLocal, engine
from backend.models import Vessel
from backend.db_models import AISPosition, AISPositionHistory, VesselIdentity
from backend.simulator import get_demo_vessels, advance_vessel
from backend.repository import save_vessel, get_all_vessels
from backend.risk_engine import assess_vessel, assess_ais_position
from backend.environment import (
    estimate_environmental_impact,
    estimate_ais_environmental_impact,
)
from backend.ais_service import run_ais_service, AIS_STATUS


# Nugen integration is centralized in backend/nugen_service.py.
# The service reads credentials from the backend environment and never exposes them.
from backend.nugen_service import (
    get_nugen_status,
    analyze_vessel_with_nugen,
    NugenConfigurationError,
    NugenAPIError,
)


# Persistent Nugen auto-analysis state prevents repeated paid/API calls after
# every AIS message and survives backend restarts.
_nugen_meta = MetaData()
NUGEN_AUTO_RUNS = Table(
    "nugen_auto_runs",
    _nugen_meta,
    Column("mmsi", String(20), primary_key=True),
    Column("signature", String(128), nullable=False),
    Column("status", String(30), nullable=False),
    Column("last_attempt_at", String(40), nullable=False),
    Column("last_success_at", String(40), nullable=True),
    Column("result_json", Text, nullable=True),
    Column("error", Text, nullable=True),
)

NUGEN_AUTO_INTERVAL_SECONDS = max(30, int(os.getenv("NUGEN_AUTO_INTERVAL_SECONDS", "90")))
NUGEN_AUTO_COOLDOWN_SECONDS = max(300, int(os.getenv("NUGEN_AUTO_COOLDOWN_SECONDS", "1800")))
NUGEN_AUTO_MAX_PER_CYCLE = max(1, min(5, int(os.getenv("NUGEN_AUTO_MAX_PER_CYCLE", "1"))))
NUGEN_AUTO_ANALYSIS_ENABLED = os.getenv("NUGEN_AUTO_ANALYSIS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
# Candidate generation is shared by the Nugen worker, status endpoint and manual
# analysis. Cache plain dictionaries briefly to avoid repeatedly rescanning AIS.
_NUGEN_CANDIDATE_CACHE: dict = {"created": 0.0, "items": {}}
_NUGEN_CANDIDATE_LOCK = threading.Lock()
_NUGEN_CANDIDATE_TTL_SECONDS = 45


app = FastAPI(title="BlueShield AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ais_record_to_dict(record: AISPosition) -> dict:
    """Convert an AIS database row to the input shape used by AIS engines."""
    return {
        "mmsi": str(record.mmsi),
        "name": record.name,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "speed_knots": record.speed_knots,
        "course": record.course,
        "heading": record.heading,
        "navigation_status": record.navigation_status,
        "position_timestamp": record.position_timestamp,
        "received_at": (
            record.received_at.isoformat() if record.received_at else None
        ),
        "data_source": record.data_source or "aisstream",
    }


def get_ais_record_or_404(mmsi: str, db: Session) -> AISPosition:
    record = db.get(AISPosition, str(mmsi))
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No AIS position found for MMSI {mmsi}",
        )
    return record


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    _nugen_meta.create_all(bind=engine)
    # Existing SQLite databases do not gain new model indexes automatically.
    # Create lightweight indexes in-place; this preserves all existing AIS data.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_ais_positions_received_at "
            "ON ais_positions (received_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_ais_history_mmsi_received "
            "ON ais_position_history (mmsi, received_at DESC)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_ais_history_received_mmsi "
            "ON ais_position_history (received_at DESC, mmsi)"
        )

    db = SessionLocal()
    try:
        if not get_all_vessels(db):
            for vessel in get_demo_vessels():
                save_vessel(db, vessel)
    finally:
        db.close()

    app.state.ais_task = asyncio.create_task(run_ais_service())
    app.state.nugen_auto_task = asyncio.create_task(_nugen_auto_worker())  # dormant unless explicitly enabled


@app.on_event("shutdown")
async def shutdown():
    nugen_task = getattr(app.state, "nugen_auto_task", None)
    if nugen_task is not None:
        nugen_task.cancel()
        try:
            await nugen_task
        except asyncio.CancelledError:
            pass
    task = getattr(app.state, "ais_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/")
def root():
    """Serve the BlueShield dashboard at the site root."""
    dashboard_file = (
        Path(__file__).resolve().parent.parent
        / "web"
        / "dashboard.html"
    )

    if dashboard_file.is_file():
        return FileResponse(dashboard_file)

    return {
        "message": "BlueShield AI API is running",
        "dashboard": "Dashboard file not found",
        "expected_path": str(dashboard_file),
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/vessels", response_model=list[Vessel])
def vessels(db: Session = Depends(get_db)):
    return get_all_vessels(db)


@app.post("/api/simulation/tick", response_model=list[Vessel])
def simulation_tick(db: Session = Depends(get_db)):
    current = get_all_vessels(db)
    updated = []

    for vessel in current:
        next_vessel = advance_vessel(vessel)
        save_vessel(db, next_vessel)
        updated.append(next_vessel)

    return updated


# Existing demo-vessel endpoints remain available.
@app.get("/api/risks")
def risks(db: Session = Depends(get_db)):
    return [assess_vessel(vessel) for vessel in get_all_vessels(db)]


@app.get("/api/vessels/{vessel_id}/risk")
def vessel_risk(vessel_id: str, db: Session = Depends(get_db)):
    vessel = next(
        (v for v in get_all_vessels(db) if str(v.id) == vessel_id),
        None,
    )
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return assess_vessel(vessel)


@app.get("/api/environment")
def environment(db: Session = Depends(get_db)):
    return [
        estimate_environmental_impact(vessel)
        for vessel in get_all_vessels(db)
    ]


@app.get("/api/vessels/{vessel_id}/environment")
def vessel_environment(vessel_id: str, db: Session = Depends(get_db)):
    vessel = next(
        (v for v in get_all_vessels(db) if str(v.id) == vessel_id),
        None,
    )
    if vessel is None:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return estimate_environmental_impact(vessel)


# Live AIS position feed.
@app.get("/api/ais/positions")
def list_ais_positions(
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Return latest AIS positions with server-receipt freshness labels."""
    now = datetime.now(timezone.utc)
    records = (
        db.query(AISPosition)
        .order_by(AISPosition.received_at.desc())
        .limit(limit)
        .all()
    )

    results = []
    for record in records:
        received = record.received_at
        if received is not None and received.tzinfo is None:
            # SQLite may return naive datetime even for timezone=True columns.
            received = received.replace(tzinfo=timezone.utc)

        age_seconds = None
        if received is not None:
            age_seconds = max(
                0,
                int((now - received.astimezone(timezone.utc)).total_seconds()),
            )

        if age_seconds is None:
            position_status = "unknown"
        elif age_seconds <= 900:
            position_status = "fresh"
        elif age_seconds <= 1800:
            position_status = "delayed"
        else:
            position_status = "stale"

        results.append({
            "mmsi": str(record.mmsi),
            "name": record.name,
            "latitude": record.latitude,
            "longitude": record.longitude,
            "speed_knots": record.speed_knots,
            "course": record.course,
            "heading": record.heading,
            "navigation_status": record.navigation_status,
            "position_timestamp": record.position_timestamp,
            "received_at": received.isoformat() if received else None,
            "data_source": record.data_source or "aisstream",
            "age_seconds": age_seconds,
            "position_status": position_status,
        })

    return results

@app.get("/api/ais/search")
def search_ais_vessels(
    q: str = Query(min_length=2, max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Search the full current AIS registry on demand, outside the fast map page."""
    term = q.strip()
    if len(term) < 2:
        return []
    pattern = f"%{term}%"
    records = (db.query(AISPosition)
        .filter(or_(AISPosition.mmsi.like(pattern), AISPosition.name.ilike(pattern)))
        .order_by(AISPosition.received_at.desc())
        .limit(limit)
        .all())
    results = []
    for record in records:
        position = ais_record_to_dict(record)
        results.append({
            **position,
            "risk": assess_ais_position(position),
        })
    return results


# New live AIS risk and environmental calculation endpoints.
@app.get("/api/ais/risks")
def ais_risks(limit: int = Query(default=500, ge=1, le=5000), db: Session = Depends(get_db)):
    records = db.query(AISPosition).order_by(AISPosition.received_at.desc()).limit(limit).all()
    return [
        assess_ais_position(ais_record_to_dict(record))
        for record in records
    ]


@app.get("/api/ais/{mmsi}/risk")
def ais_vessel_risk(mmsi: str, db: Session = Depends(get_db)):
    record = get_ais_record_or_404(mmsi, db)
    return assess_ais_position(ais_record_to_dict(record))


@app.get("/api/ais/environment")
def ais_environment(limit: int = Query(default=500, ge=1, le=5000), db: Session = Depends(get_db)):
    records = db.query(AISPosition).order_by(AISPosition.received_at.desc()).limit(limit).all()
    return [
        estimate_ais_environmental_impact(ais_record_to_dict(record))
        for record in records
    ]


@app.get("/api/ais/{mmsi}/environment")
def ais_vessel_environment(mmsi: str, db: Session = Depends(get_db)):
    record = get_ais_record_or_404(mmsi, db)
    return estimate_ais_environmental_impact(ais_record_to_dict(record))


@app.get("/api/ais/assessments")
def ais_assessments(limit: int = Query(default=500, ge=1, le=5000), db: Session = Depends(get_db)):
    """Return bounded risk and environmental estimates joined by MMSI."""
    records = (
        db.query(AISPosition)
        .order_by(AISPosition.received_at.desc())
        .limit(limit)
        .all()
    )
    results = []
    for record in records:
        position = ais_record_to_dict(record)
        results.append({
            "mmsi": position["mmsi"],
            "name": position["name"] or f"Vessel {position['mmsi']}",
            "risk": assess_ais_position(position),
            "environment": estimate_ais_environmental_impact(position),
        })
    return results


@app.get("/api/ais/status")
def ais_status(db: Session = Depends(get_db)):
    """Report stream uptime counters plus durable SQLite history totals."""
    result = dict(AIS_STATUS)
    result["process_saved_positions"] = result.get("saved_positions", 0)
    result["saved_positions"] = db.query(AISPositionHistory).count()
    result["tracked_vessels"] = db.query(AISPosition).count()
    result["history_observations"] = result["saved_positions"]
    result["coverage_scope"] = "global_bbox_requested"
    result["note"] = (
        "saved_positions/history_observations are durable SQLite totals; "
        "process_saved_positions counts only since this backend process started. "
        "Actual global coverage depends on AISStream account delivery and quotas."
    )
    result["saved_static_reports"] = int(AIS_STATUS.get("saved_static_reports", 0))
    result["vessel_identities"] = db.query(VesselIdentity).count()
    return result


@app.get("/api/ais/identities")
def list_ais_identities(
    q: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """List known AIS static identities, optionally filtered by name/MMSI/IMO."""
    query = db.query(VesselIdentity)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(
                VesselIdentity.mmsi.ilike(term),
                VesselIdentity.ship_name.ilike(term),
                VesselIdentity.imo.ilike(term),
                VesselIdentity.call_sign.ilike(term),
            )
        )
    rows = query.order_by(VesselIdentity.last_static_received_at.desc()).limit(limit).all()
    return [
        {
            "mmsi": row.mmsi,
            "imo": row.imo,
            "call_sign": row.call_sign,
            "ship_name": row.ship_name,
            "ship_type": row.ship_type,
            "destination": row.destination,
            "draught": row.draught,
            "eta": row.eta,
            "dimensions": json.loads(row.dimensions_json) if row.dimensions_json else None,
            "ais_version": row.ais_version,
            "last_static_received_at": row.last_static_received_at.isoformat() if row.last_static_received_at else None,
            "data_source": row.data_source,
        }
        for row in rows
    ]


@app.get("/api/ais/identity/{mmsi}")
def get_ais_identity(mmsi: str, db: Session = Depends(get_db)):
    row = db.get(VesselIdentity, str(mmsi))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No static AIS identity found for MMSI {mmsi}")
    return {
        "mmsi": row.mmsi,
        "imo": row.imo,
        "call_sign": row.call_sign,
        "ship_name": row.ship_name,
        "ship_type": row.ship_type,
        "destination": row.destination,
        "draught": row.draught,
        "eta": row.eta,
        "dimensions": json.loads(row.dimensions_json) if row.dimensions_json else None,
        "ais_version": row.ais_version,
        "last_static_received_at": row.last_static_received_at.isoformat() if row.last_static_received_at else None,
        "data_source": row.data_source,
    }


@app.get("/api/ai/nugen/status")
def nugen_status():
    """Safe Nugen readiness status; credentials are never returned."""
    return get_nugen_status()


def _ais_navigation_status_label(value):
    """Return the standard AIS navigation-status meaning without guessing."""
    if value is None:
        return {"code": None, "label": "not_reported"}
    raw = str(value).strip()
    try:
        code = int(float(raw))
    except (TypeError, ValueError):
        return {"code": raw, "label": "unknown_or_nonstandard"}
    labels = {
        0: "under_way_using_engine",
        1: "at_anchor",
        2: "not_under_command",
        3: "restricted_manoeuvrability",
        4: "constrained_by_draught",
        5: "moored",
        6: "aground",
        7: "engaged_in_fishing_self_reported",
        8: "under_way_sailing",
        9: "reserved",
        10: "reserved",
        11: "power_driven_vessel_towing_astern",
        12: "power_driven_vessel_pushing_ahead_or_towing_alongside",
        13: "reserved",
        14: "ais_sart_active",
        15: "undefined",
    }
    return {"code": code, "label": labels.get(code, "unknown_or_nonstandard")}


def _build_nugen_track_context(mmsi: str, db: Session, lookback_hours: int = 6) -> dict:
    """Build bounded track metrics, report-gap and freshness context for Nugen."""
    from datetime import timedelta
    from math import isfinite

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    # Fetch one extra row to detect whether the 240-observation cap truncated history.
    fetched = (
        db.query(AISPositionHistory)
        .filter(AISPositionHistory.mmsi == str(mmsi))
        .filter(AISPositionHistory.received_at >= cutoff)
        .order_by(AISPositionHistory.received_at.desc())
        .limit(241)
        .all()
    )
    history_window_capped = len(fetched) > 240
    rows = fetched[:240]
    rows.reverse()

    valid = []
    for row in rows:
        try:
            lat, lon = float(row.latitude), float(row.longitude)
        except (TypeError, ValueError):
            continue
        if isfinite(lat) and isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            valid.append(row)

    if not valid:
        return {
            "lookback_hours": lookback_hours,
            "observations": 0,
            "track_available": False,
            "history_window_capped": history_window_capped,
            "latest_received_age_seconds": None,
            "note": "No valid received history points were found in this lookback window.",
            "samples": [],
        }

    times = []
    for row in valid:
        stamp = row.received_at
        if stamp is None:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        times.append(stamp.astimezone(timezone.utc))
    first = times[0] if times else None
    last = times[-1] if times else None
    duration_minutes = max(0.0, (last - first).total_seconds() / 60.0) if first and last else None
    latest_age_seconds = max(0, int((now - last).total_seconds())) if last else None

    gaps_minutes = [
        max(0.0, (later - earlier).total_seconds() / 60.0)
        for earlier, later in zip(times, times[1:])
    ]
    max_gap_minutes = max(gaps_minutes) if gaps_minutes else 0.0
    gaps_over_15m = sum(1 for gap in gaps_minutes if gap > 15.0)

    speeds = []
    unavailable_speed_count = 0
    for row in valid:
        try:
            speed = float(row.speed_knots)
            if isfinite(speed) and 0 <= speed <= 102.2:
                speeds.append(speed)
            else:
                unavailable_speed_count += 1
        except (TypeError, ValueError):
            unavailable_speed_count += 1

    center_lat = sum(float(x.latitude) for x in valid) / len(valid)
    center_lon = sum(float(x.longitude) for x in valid) / len(valid)
    radius_nm = max(
        _haversine_nm(center_lat, center_lon, float(x.latitude), float(x.longitude))
        for x in valid
    )

    if len(valid) <= 18:
        sampled = valid
    else:
        indexes = [round(i * (len(valid) - 1) / 17) for i in range(18)]
        sampled = [valid[i] for i in indexes]

    samples = []
    for row in sampled:
        received = row.received_at
        if received and received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        samples.append({
            "received_at": received.isoformat() if received else None,
            "position_timestamp": row.position_timestamp,
            "latitude": round(float(row.latitude), 6),
            "longitude": round(float(row.longitude), 6),
            "speed_knots": row.speed_knots,
            "course": row.course,
            "navigation_status": _ais_navigation_status_label(row.navigation_status),
        })

    nav_statuses = sorted(
        {_ais_navigation_status_label(x.navigation_status)["code"] for x in valid
         if x.navigation_status is not None},
        key=lambda value: str(value),
    )
    nav_interpretations = sorted({
        _ais_navigation_status_label(x.navigation_status)["label"] for x in valid
        if x.navigation_status is not None
    })

    return {
        "lookback_hours": lookback_hours,
        "observations": len(valid),
        "track_available": True,
        "history_window_capped": history_window_capped,
        "duration_minutes": round(duration_minutes, 1) if duration_minutes is not None else None,
        "latest_received_age_seconds": latest_age_seconds,
        "latest_received_age_minutes": round(latest_age_seconds / 60, 1) if latest_age_seconds is not None else None,
        "average_speed_knots": round(sum(speeds) / len(speeds), 2) if speeds else None,
        "valid_speed_observation_count": len(speeds),
        "unavailable_speed_observation_count": unavailable_speed_count,
        "movement_radius_nm": round(radius_nm, 2),
        "max_received_report_gap_minutes": round(max_gap_minutes, 1),
        "received_report_gaps_over_15_minutes": gaps_over_15m,
        "navigation_status_values": nav_statuses,
        "navigation_status_interpretations": nav_interpretations,
        "first_received_at": first.isoformat() if first else None,
        "last_received_at": last.isoformat() if last else None,
        "sample_count_sent": len(samples),
        "samples": samples,
        "limitations": (
            "Received AIS history only. Sampling may omit intermediate reports; "
            "report gaps can conceal activity. Receipt time is not necessarily the "
            "vessel's exact transmission time."
        ),
    }


def _compute_nugen_candidate_map(db: Session) -> dict[str, dict]:
    """Find actionable recent candidates without scanning the full AIS registry."""
    candidates: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=30)
    records = (db.query(AISPosition)
        .filter(AISPosition.received_at >= cutoff)
        .order_by(AISPosition.received_at.desc())
        .limit(10000)
        .all())

    allowed_single_signals = {
        "EXTREME_REPORTED_SPEED", "HIGH_REPORTED_SPEED", "INVALID_POSITION",
    }
    for record in records:
        position = ais_record_to_dict(record)
        assessment = assess_ais_position(position)
        signals = [signal for signal in assessment.get("signals", [])
                   if signal.get("code") in allowed_single_signals]
        if signals:
            mmsi = str(record.mmsi)
            candidates[mmsi] = {
                "source": "deterministic_single_position_screening",
                "score": assessment.get("score"),
                "level": assessment.get("level"),
                "signals": signals,
                "classification": "AIS_DATA_QUALITY_OR_REVIEW_FLAG",
            }

    # Historical screening is deliberately separate from the fast map load.
    behavior = _behavior_assessment(db, lookback_hours=6, max_observations=8000)
    for alert in behavior.get("alerts", []):
        mmsi = str(alert.get("mmsi") or "").strip()
        if not mmsi:
            continue
        signal_codes = {str(x.get("code")) for x in alert.get("signals", []) if isinstance(x, dict)}
        if not (signal_codes - {"STATIONARY_POSITION_PATTERN"}):
            continue
        latest = db.get(AISPosition, mmsi)
        if latest is None or latest.received_at is None:
            continue
        received = latest.received_at
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        if (now - received.astimezone(timezone.utc)).total_seconds() > 1800:
            continue
        existing = candidates.get(mmsi, {})
        combined = list(existing.get("signals", []))
        existing_codes = {x.get("code") for x in combined if isinstance(x, dict)}
        combined.extend(x for x in alert.get("signals", [])
                        if isinstance(x, dict) and x.get("code") not in existing_codes)
        candidates[mmsi] = {
            **existing,
            "source": ("deterministic_single_position_and_historical_screening" if existing
                       else "deterministic_historical_behavior_screening"),
            "score": existing.get("score"),
            "level": existing.get("level", "review"),
            "signals": combined,
            "behavior": {
                "classification": alert.get("classification"),
                "confidence": alert.get("confidence"),
                "observations": alert.get("observations"),
                "duration_minutes": alert.get("duration_minutes"),
                "average_speed_knots": alert.get("average_speed_knots"),
                "movement_radius_nm": alert.get("movement_radius_nm"),
            },
            "classification": alert.get("classification"),
        }
    return candidates


def _nugen_candidate_map(db: Session) -> dict[str, dict]:
    """Short-lived shared candidate cache for status and auto-analysis calls."""
    now = time.monotonic()
    if now - _NUGEN_CANDIDATE_CACHE["created"] < _NUGEN_CANDIDATE_TTL_SECONDS:
        return _NUGEN_CANDIDATE_CACHE["items"]
    with _NUGEN_CANDIDATE_LOCK:
        now = time.monotonic()
        if now - _NUGEN_CANDIDATE_CACHE["created"] < _NUGEN_CANDIDATE_TTL_SECONDS:
            return _NUGEN_CANDIDATE_CACHE["items"]
        items = _compute_nugen_candidate_map(db)
        _NUGEN_CANDIDATE_CACHE.update({"created": time.monotonic(), "items": items})
        return items


def _get_nugen_candidate(mmsi: str, db: Session) -> dict | None:
    return _nugen_candidate_map(db).get(str(mmsi))


def _run_nugen_for_mmsi(mmsi: str, db: Session, candidate: dict | None = None) -> dict:
    latest = get_ais_record_or_404(mmsi, db)
    if candidate is None:
        # Manual investigator requests may analyse any real AIS vessel. This is
        # distinct from automatic analysis, which remains restricted to flags.
        candidate = _get_nugen_candidate(mmsi, db) or {
            "source": "manual_investigator_request",
            "classification": "MANUAL_AIS_REVIEW",
            "score": None,
            "level": "unscored",
            "signals": [],
        }
    position = ais_record_to_dict(latest)
    position["navigation_status_interpretation"] = _ais_navigation_status_label(
        position.get("navigation_status")
    )
    assessment = assess_ais_position(position)
    track_context = _build_nugen_track_context(str(mmsi), db)
    assessment["deterministic_trigger"] = candidate
    try:
        result = analyze_vessel_with_nugen(position, assessment, track_context)
    except NugenConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NugenAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "provider": "Nugen",
        "status": "inference_completed",
        "model": result["model"],
        "mmsi": str(mmsi),
        "name": latest.name or f"Vessel {mmsi}",
        "analysis_scope": "latest_position_plus_historical_track",
        "trigger": {"source": candidate.get("source"), "classification": candidate.get("classification"), "signals": candidate.get("signals", [])},
        "track_context": {key: value for key, value in track_context.items() if key != "samples"},
        "analysis": result["analysis"],
        "evidence_audit": result.get("evidence_audit", {"passed": None, "flags": []}),
        "usage": result.get("usage"),
        "warning": "AI decision support only. AIS history and model interpretation cannot establish fishing activity, intent, illegality, or wrongdoing. Navigation status is self-reported AIS metadata.",
    }


@app.post("/api/ai/nugen/analyze/{mmsi}")
def nugen_analyze_vessel(mmsi: str, db: Session = Depends(get_db)):
    """Manually analyse any vessel with a stored live AIS position, flagged or not."""
    manual_request = {
        "source": "manual_investigator_request",
        "classification": "MANUAL_AIS_REVIEW",
        "score": None,
        "level": "unscored",
        "signals": [],
    }
    # Do not build/scan the global flagged-candidate map for a manual request.
    # The latest AIS position and bounded track are enough for this vessel review.
    return _run_nugen_for_mmsi(mmsi, db, manual_request)


@app.post("/api/ai/nugen/analyze")
def nugen_analyze(db: Session = Depends(get_db)):
    """Analyse the highest-priority currently flagged vessel only."""
    candidates = _nugen_candidate_map(db)
    if not candidates:
        raise HTTPException(status_code=404, detail="No vessels are currently eligible for Nugen analysis.")
    ordered = sorted(
        candidates.items(),
        key=lambda item: (
            item[1].get("level") == "high",
            item[1].get("score") or 0,
            len(item[1].get("signals", [])),
        ),
        reverse=True,
    )
    mmsi, candidate = ordered[0]
    return _run_nugen_for_mmsi(mmsi, db, candidate)



def _nugen_auto_cycle() -> dict:
    """Analyse a small number of flagged vessels, with persistent cooldown."""
    if not get_nugen_status().get("configured"):
        return {"status": "disabled", "reason": "Nugen credentials are not configured.", "processed": 0}

    db = SessionLocal()
    processed = []
    skipped = 0
    try:
        candidates = _nugen_candidate_map(db)
        now = datetime.now(timezone.utc)
        for mmsi, candidate in sorted(
            candidates.items(),
            key=lambda item: (
                item[1].get("level") == "high",
                item[1].get("score") or 0,
                len(item[1].get("signals", [])),
            ),
            reverse=True,
        ):
            if len(processed) >= NUGEN_AUTO_MAX_PER_CYCLE:
                break

            latest = db.get(AISPosition, mmsi)
            if latest is None:
                continue
            signature_payload = {
                "received_at": latest.received_at.isoformat() if latest.received_at else None,
                "score": candidate.get("score"),
                "level": candidate.get("level"),
                "signals": sorted(
                    str(x.get("code")) for x in candidate.get("signals", [])
                    if isinstance(x, dict)
                ),
                "classification": candidate.get("classification"),
            }
            signature = __import__("hashlib").sha256(
                json.dumps(signature_payload, sort_keys=True).encode("utf-8")
            ).hexdigest()
            with engine.begin() as conn:
                prior = conn.execute(
                    select(NUGEN_AUTO_RUNS).where(NUGEN_AUTO_RUNS.c.mmsi == mmsi)
                ).mappings().first()

            if prior:
                if prior["signature"] == signature and prior["status"] == "completed":
                    skipped += 1
                    continue
                try:
                    last_attempt = datetime.fromisoformat(prior["last_attempt_at"])
                    if last_attempt.tzinfo is None:
                        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
                    if (now - last_attempt).total_seconds() < NUGEN_AUTO_COOLDOWN_SECONDS:
                        skipped += 1
                        continue
                except (TypeError, ValueError):
                    pass

            attempted_at = now.isoformat()
            with engine.begin() as conn:
                values = {
                    "mmsi": mmsi,
                    "signature": signature,
                    "status": "processing",
                    "last_attempt_at": attempted_at,
                    "error": None,
                }
                if prior:
                    conn.execute(update(NUGEN_AUTO_RUNS).where(
                        NUGEN_AUTO_RUNS.c.mmsi == mmsi
                    ).values(**{k: v for k, v in values.items() if k != "mmsi"}))
                else:
                    conn.execute(insert(NUGEN_AUTO_RUNS).values(**values))

            try:
                result = _run_nugen_for_mmsi(mmsi, db, candidate)
                completed_at = datetime.now(timezone.utc).isoformat()
                with engine.begin() as conn:
                    conn.execute(update(NUGEN_AUTO_RUNS).where(
                        NUGEN_AUTO_RUNS.c.mmsi == mmsi
                    ).values(
                        status="completed",
                        last_success_at=completed_at,
                        result_json=json.dumps(result, ensure_ascii=False),
                        error=None,
                    ))
                processed.append({"mmsi": mmsi, "status": "completed"})
            except Exception as exc:
                with engine.begin() as conn:
                    conn.execute(update(NUGEN_AUTO_RUNS).where(
                        NUGEN_AUTO_RUNS.c.mmsi == mmsi
                    ).values(status="failed", error=f"{type(exc).__name__}: {exc}"[:1500]))
                processed.append({"mmsi": mmsi, "status": "failed", "error": str(exc)[:300]})
    finally:
        db.close()

    return {
        "status": "completed",
        "eligible_vessels": len(candidates),
        "processed": processed,
        "skipped_by_dedup_or_cooldown": skipped,
        "max_per_cycle": NUGEN_AUTO_MAX_PER_CYCLE,
        "cooldown_seconds": NUGEN_AUTO_COOLDOWN_SECONDS,
    }


async def _nugen_auto_worker():
    """Optional periodic Nugen worker; off by default to protect user credits."""
    await asyncio.sleep(10)
    while True:
        try:
            if NUGEN_AUTO_ANALYSIS_ENABLED:
                await asyncio.to_thread(_nugen_auto_cycle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the AIS ingestion service alive if an inference cycle fails.
            print(f"[Nugen auto-screening] cycle error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(NUGEN_AUTO_INTERVAL_SECONDS)


@app.get("/api/ai/nugen/auto/status")
def nugen_auto_status(db: Session = Depends(get_db)):
    candidates = _nugen_candidate_map(db)
    with engine.begin() as conn:
        rows = conn.execute(
            select(NUGEN_AUTO_RUNS).order_by(NUGEN_AUTO_RUNS.c.last_attempt_at.desc())
        ).mappings().all()
    return {
        "enabled": bool(get_nugen_status().get("inference_enabled")) and NUGEN_AUTO_ANALYSIS_ENABLED,
        "manual_analysis_enabled": bool(get_nugen_status().get("inference_enabled")),
        "provider": "Nugen",
        "policy": "manual_selected_vessel_by_default; optional_flagged_vessels_only_auto_run",
        "eligible_vessels_now": len(candidates),
        "interval_seconds": NUGEN_AUTO_INTERVAL_SECONDS,
        "cooldown_seconds": NUGEN_AUTO_COOLDOWN_SECONDS,
        "max_per_cycle": NUGEN_AUTO_MAX_PER_CYCLE,
        "runs": [
            {
                "mmsi": row["mmsi"],
                "status": row["status"],
                "last_attempt_at": row["last_attempt_at"],
                "last_success_at": row["last_success_at"],
                "error": row["error"],
            }
            for row in rows[:100]
        ],
    }


@app.get("/api/ai/nugen/auto/results")
def nugen_auto_results(limit: int = Query(default=50, ge=1, le=200)):
    with engine.begin() as conn:
        rows = conn.execute(
            select(NUGEN_AUTO_RUNS)
            .where(NUGEN_AUTO_RUNS.c.status == "completed")
            .order_by(NUGEN_AUTO_RUNS.c.last_success_at.desc())
            .limit(limit)
        ).mappings().all()
    results = []
    for row in rows:
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except json.JSONDecodeError:
            result = None
        if result:
            results.append(result)
    return {"policy": "flagged_vessels_only", "count": len(results), "results": results}



def _behavior_assessment(db: Session, lookback_hours: int = 6, max_observations: int = 8000) -> dict:
    """Screen representative per-vessel AIS tracks without a global row cap.

    max_observations is retained for API compatibility. It no longer caps the
    fleet-wide query; tracks are selected per MMSI and sampled across each
    vessel's full lookback window. Work is bounded to the most recently active
    1,000 eligible vessels, processed in batches of 200.
    """
    from datetime import timedelta
    from math import isfinite
    from types import SimpleNamespace
    from sqlalchemy import func, case

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    lat_ok = AISPositionHistory.latitude.between(-90, 90)
    lon_ok = AISPositionHistory.longitude.between(-180, 180)
    valid_position = (
        AISPositionHistory.latitude.is_not(None)
        & AISPositionHistory.longitude.is_not(None)
        & lat_ok & lon_ok
    )
    valid_speed = (
        AISPositionHistory.speed_knots.is_not(None)
        & AISPositionHistory.speed_knots.between(0, 102.2)
    )

    total_window_observations = (
        db.query(func.count(AISPositionHistory.mmsi))
        .filter(AISPositionHistory.received_at >= cutoff)
        .scalar()
    ) or 0

    # Aggregate the complete lookback window per vessel before sampling.
    summaries = (
        db.query(
            AISPositionHistory.mmsi.label("mmsi"),
            func.count(case((valid_position, 1))).label("valid_positions"),
            func.count(case((valid_speed, 1))).label("valid_speeds"),
            func.min(AISPositionHistory.received_at).label("first_seen"),
            func.max(AISPositionHistory.received_at).label("last_seen"),
            func.count().label("observations"),
        )
        .filter(AISPositionHistory.received_at >= cutoff)
        .group_by(AISPositionHistory.mmsi)
        .order_by(func.max(AISPositionHistory.received_at).desc())
        .all()
    )

    def as_utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    diagnostics = {
        "vessels_in_window": len(summaries),
        "tracks_below_minimum_observations": 0,
        "tracks_below_minimum_duration": 0,
        "tracks_without_valid_speed": 0,
        "tracks_with_no_valid_coordinates": 0,
        "rejected_tracks": 0,
        "examples": [],
        "vessels_sampled": 0,
        "per_vessel_sample_limit": 240,
        "vessel_limit": 1000,
        "batch_size": 200,
        "batches_processed": 0,
        "global_history_limit": None,
        "history_truncated": False,
    }

    eligible_mmsis = []
    summary_by_mmsi = {}
    for item in summaries:
        mmsi = str(item.mmsi)
        valid_count = int(item.valid_positions or 0)
        first_seen, last_seen = as_utc(item.first_seen), as_utc(item.last_seen)
        duration = (
            max(0.0, (last_seen - first_seen).total_seconds() / 60.0)
            if first_seen and last_seen else 0.0
        )
        reason = None
        if valid_count == 0:
            diagnostics["tracks_with_no_valid_coordinates"] += 1
            reason = "no_valid_coordinates"
        elif valid_count < 6:
            diagnostics["tracks_below_minimum_observations"] += 1
            reason = "below_six_valid_observations"
        elif duration < 30:
            diagnostics["tracks_below_minimum_duration"] += 1
            reason = "below_30_minute_span"
        elif int(item.valid_speeds or 0) == 0:
            diagnostics["tracks_without_valid_speed"] += 1
            reason = "no_valid_speed"

        if reason:
            if len(diagnostics["examples"]) < 10:
                diagnostics["examples"].append({
                    "mmsi": mmsi,
                    "observations_in_window": int(item.observations or 0),
                    "valid_position_observations": valid_count,
                    "duration_minutes": round(duration, 1),
                    "reason": reason,
                })
            continue
        eligible_mmsis.append(mmsi)
        summary_by_mmsi[mmsi] = item

    diagnostics["rejected_tracks"] = (
        diagnostics["tracks_below_minimum_observations"]
        + diagnostics["tracks_below_minimum_duration"]
        + diagnostics["tracks_without_valid_speed"]
        + diagnostics["tracks_with_no_valid_coordinates"]
    )
    diagnostics["eligible_vessels_before_sampling"] = len(eligible_mmsis)

    # The aggregate list is ordered by latest activity. Bound CPU/memory while
    # ensuring high-frequency reporters cannot monopolize observation slots.
    selected_mmsis = eligible_mmsis[:diagnostics["vessel_limit"]]
    diagnostics["vessels_sampled"] = len(selected_mmsis)
    diagnostics["vessel_limit_reached"] = len(eligible_mmsis) > len(selected_mmsis)

    alerts = []
    eligible_tracks = 0
    batch_size = diagnostics["batch_size"]

    for offset in range(0, len(selected_mmsis), batch_size):
        batch_mmsis = selected_mmsis[offset:offset + batch_size]
        # NTILE gives each selected vessel up to 240 evenly distributed points
        # over its entire six-hour track, including early and recent positions.
        bucketed = (
            db.query(
                AISPositionHistory,
                func.ntile(240).over(
                    partition_by=AISPositionHistory.mmsi,
                    order_by=AISPositionHistory.received_at.asc(),
                ).label("sample_bucket"),
            )
            .filter(AISPositionHistory.received_at >= cutoff)
            .filter(AISPositionHistory.mmsi.in_(batch_mmsis))
            .filter(valid_position)
            .subquery()
        )
        bucket_rank = func.row_number().over(
            partition_by=(bucketed.c.mmsi, bucketed.c.sample_bucket),
            order_by=bucketed.c.received_at.asc(),
        )
        history_columns = [
            bucketed.c[column.key] for column in AISPositionHistory.__table__.columns
        ]
        sampled_query = db.query(
            *history_columns, bucket_rank.label("bucket_rank")
        ).subquery()
        sampled_rows = (
            db.query(sampled_query)
            .filter(sampled_query.c.bucket_rank == 1)
            .order_by(sampled_query.c.mmsi, sampled_query.c.received_at.asc())
            .all()
        )
        rows_by_mmsi = {mmsi: [] for mmsi in batch_mmsis}
        column_names = [column.key for column in AISPositionHistory.__table__.columns]
        for result_row in sampled_rows:
            values = result_row._mapping
            row = SimpleNamespace(**{name: values[name] for name in column_names})
            rows_by_mmsi.setdefault(str(row.mmsi), []).append(row)

        diagnostics["batches_processed"] += 1

        for mmsi in batch_mmsis:
            track = rows_by_mmsi.get(mmsi, [])
            if len(track) < 6:
                continue
            first, last = as_utc(track[0].received_at), as_utc(track[-1].received_at)
            if not first or not last:
                continue
            duration_min = max(0.0, (last - first).total_seconds() / 60.0)
            if duration_min < 30:
                continue

            speeds = []
            for point in track:
                try:
                    speed = float(point.speed_knots)
                    if isfinite(speed) and 0 <= speed <= 102.2:
                        speeds.append(speed)
                except (TypeError, ValueError):
                    continue
            if not speeds:
                continue

            eligible_tracks += 1
            avg_speed = sum(speeds) / len(speeds)
            center_lat = sum(float(x.latitude) for x in track) / len(track)
            center_lon = sum(float(x.longitude) for x in track) / len(track)
            radius_nm = max(
                _haversine_nm(center_lat, center_lon, float(x.latitude), float(x.longitude))
                for x in track
            )
            nav_codes = {
                str(x.navigation_status).strip()
                for x in track if x.navigation_status is not None
            }
            signals = []
            has_declared_fishing_status = "7" in nav_codes
            if has_declared_fishing_status:
                signals.append({
                    "code": "AIS_DECLARED_FISHING_STATUS",
                    "message": "At least one AIS report declares navigation status 'engaged in fishing'. This is self-reported AIS metadata, not independent confirmation.",
                })

            if avg_speed <= 0.5 and radius_nm <= 0.1:
                if not any(
                    signal.get("code") == "STATIONARY_POSITION_PATTERN"
                    for signal in signals
                ):
                    signals.append({
                        "code": "STATIONARY_POSITION_PATTERN",
                        "message": "Repeated AIS reports remained nearly stationary within a compact area. This can be consistent with fishing, waiting, servicing, or other activity; it is not proof of fishing.",
                    })
                classification, confidence = "STATIONARY_ACTIVITY_REVIEW_REQUIRED", "very_low"
            elif avg_speed <= 5.0 and radius_nm <= 1.5:
                signals.append({
                    "code": "SLOW_LOCALIZED_MOVEMENT",
                    "message": "Repeated low-speed movement stayed within a compact area. This can be consistent with fishing, waiting, servicing, or other activity; it is not proof of fishing.",
                })
                classification, confidence = "LOW_SPEED_LOCALIZED_ACTIVITY_REVIEW_REQUIRED", "low"
            else:
                classification, confidence = "AIS_BEHAVIOR_REVIEW_REQUIRED", "low"

            if signals:
                has_fishing_related_indicator = has_declared_fishing_status or any(
                    signal.get("code") == "SLOW_LOCALIZED_MOVEMENT" for signal in signals
                )
                mpa_matches = []
                if has_fishing_related_indicator:
                    try:
                        mpa_matches = zoneguard_engine.check_position(
                            float(track[-1].latitude), float(track[-1].longitude)
                        )
                    except Exception:
                        mpa_matches = []

                has_slow_movement = any(
                    signal.get("code") == "SLOW_LOCALIZED_MOVEMENT" for signal in signals
                )
                if mpa_matches:
                    signals.append({
                        "code": "MPA_POSITION_OVERLAP",
                        "severity": "review",
                        "message": "The latest received AIS coordinate intersects a locally loaded mapped marine protected-area boundary. This is geographic context only; zone restrictions, vessel permissions, fishing activity and legal violation have not been established.",
                    })
                    if has_declared_fishing_status and has_slow_movement:
                        classification, confidence = "PRIORITY_FISHING_INVESTIGATION", "low"
                    elif has_declared_fishing_status:
                        classification, confidence = "FISHING_ACTIVITY_IN_MPA_REVIEW", "low"
                    else:
                        classification, confidence = "MPA_ACTIVITY_REVIEW", "very_low"

                latest_point = track[-1]
                latest_received = as_utc(latest_point.received_at)
                alerts.append({
                    "mmsi": mmsi,
                    "name": latest_point.name or f"Vessel {mmsi}",
                    "observations": int(summary_by_mmsi[mmsi].valid_positions),
                    "sampled_observations": len(track),
                    "duration_minutes": round(duration_min, 1),
                    "average_speed_knots": round(avg_speed, 2),
                    "movement_radius_nm": round(radius_nm, 2),
                    "signals": signals,
                    "classification": classification,
                    "protected_area_matches": mpa_matches,
                    "latest_position": {
                        "latitude": float(latest_point.latitude),
                        "longitude": float(latest_point.longitude),
                        "received_at": latest_received.isoformat() if latest_received else None,
                        "position_timestamp": latest_point.position_timestamp,
                        "speed_knots": latest_point.speed_knots,
                        "navigation_status": latest_point.navigation_status,
                    },
                    "latitude": float(latest_point.latitude),
                    "longitude": float(latest_point.longitude),
                    "latest_received_at": latest_received.isoformat() if latest_received else None,
                    "position_timestamp": latest_point.position_timestamp,
                    "confidence": confidence,
                    "data_source": "received_ais_history",
                    "warning": "Review indicator only. AIS movement and self-reported navigation status cannot establish fishing activity, intent, legality, or wrongdoing. MPA overlap does not establish that fishing is prohibited or unauthorized.",
                })

    alerts.sort(
        key=lambda item: (item["confidence"] == "very_low", item["duration_minutes"]),
        reverse=True,
    )
    return {
        "status": "ready",
        "lookback_hours": lookback_hours,
        "history_observations": int(total_window_observations),
        "history_truncated": False,
        "history_limit": None,
        "eligible_tracks": eligible_tracks,
        "vessels_with_patterns": len(alerts),
        "vessels_in_window": len(summaries),
        "invalid_coordinate_observations": max(
            0, int(total_window_observations)
            - sum(int(item.valid_positions or 0) for item in summaries)
        ),
        "diagnostics": diagnostics,
        "alerts": alerts,
        "limitations": (
            "Screening aggregates the full lookback window per vessel, then samples up to "
            "240 valid positions per selected vessel. The most recently active 1,000 "
            "eligible vessels are processed in batches of 200; older eligible vessels may "
            "be deferred when that limit is reached. Requires at least six valid received "
            "observations over 30 minutes. Stationary tracks are separated from low-speed "
            "localized movement. These patterns are not proof of fishing or wrongdoing."
        ),
    }


@app.get("/api/ai/behavior")
def ai_behavior(
    lookback_hours: int = Query(default=6, ge=1, le=72),
    max_observations: int = Query(default=8000, ge=1000, le=100000),
    db: Session = Depends(get_db),
):
    return _behavior_assessment(db, lookback_hours, max_observations)





@app.get("/api/ai/intelligence")
def get_ai_intelligence(
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Screen the latest bounded AIS fleet and return every vessel assessment."""
    now = datetime.now(timezone.utc)
    records = (db.query(AISPosition)
        .order_by(AISPosition.received_at.desc())
        .limit(limit)
        .all())
    alerts = []
    assessments = []
    for record in records:
        position = ais_record_to_dict(record)
        assessment = assess_ais_position(position)
        item = {
            "mmsi": str(record.mmsi),
            "name": record.name or f"MMSI {record.mmsi}",
            "latitude": record.latitude,
            "longitude": record.longitude,
            "speed_knots": record.speed_knots,
            "course": record.course,
            "received_at": record.received_at.isoformat() if record.received_at else None,
            "position_age_seconds": assessment.get("age_seconds"),
            "score": assessment.get("score"),
            "level": assessment.get("level"),
            "signals": assessment.get("signals", []),
            "summary": assessment.get("summary"),
            "assessment_type": assessment.get("assessment_type"),
            "position_status": assessment.get("position_status"),
            "environment": estimate_ais_environmental_impact(position),
            "data_source": record.data_source or "aisstream",
            "interpretation": "Review indicator only; not proof of wrongdoing.",
        }
        assessments.append(item)
        if item["signals"]:
            alerts.append(item)
    alerts.sort(key=lambda item: (item["score"] or 0), reverse=True)
    return {
        "engine": "BlueShield deterministic AIS screening v3",
        "status": "ready",
        "inference_type": "rule_based_not_machine_learning",
        "nugen": get_nugen_status(),
        "analyzed_vessels": len(records),
        "returned_vessels": len(records),
        "requested_limit": limit,
        "vessels_with_signals": len(alerts),
        "high_priority": sum(1 for item in alerts if item["level"] == "high"),
        "medium_priority": sum(1 for item in alerts if item["level"] == "medium"),
        "generated_at": now.isoformat(),
        "disclaimer": "Rule-based AIS data-quality screening only. Stale reports are not current locations; AIS flags require independent verification.",
        "assessments": assessments,
        "alerts": alerts,
    }



def _haversine_nm(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance in nautical miles."""
    from math import radians, sin, cos, asin, sqrt
    lat1, lat2 = radians(a_lat), radians(b_lat)
    dlat, dlon = radians(b_lat-a_lat), radians(b_lon-a_lon)
    h = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 3440.065 * 2 * asin(min(1.0, sqrt(h)))

@app.get("/api/ai/screening")
def ai_screening_alias(db: Session = Depends(get_db)):
    """Compatibility alias for older Overview builds."""
    return get_ai_intelligence(limit=500, db=db)


@app.get("/api/ai/fishing")
def ai_fishing_alias(lookback_hours: int = Query(default=6, ge=1, le=72), db: Session = Depends(get_db)):
    """Compatibility alias for fishing-behaviour pattern screening."""
    return _behavior_assessment(db, lookback_hours)

# ZoneGuard: global marine protected-area geospatial screening.
from backend import zoneguard as zoneguard_engine

@app.get("/api/zoneguard/status")
def zoneguard_status():
    return zoneguard_engine.status()

@app.post("/api/zoneguard/sync")
def zoneguard_sync():
    try:
        return zoneguard_engine.sync_global_zones()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Global MPA sync failed: {exc}")

@app.get("/api/zoneguard/zones")
def zoneguard_zones(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0), search: str | None = None):
    return zoneguard_engine.list_zones(limit, offset, search)

@app.get("/api/zoneguard/assessments")
def zoneguard_assessments(limit: int = Query(default=500, ge=1, le=5000), db: Session = Depends(get_db)):
    records = (db.query(AISPosition).order_by(AISPosition.received_at.desc()).limit(limit).all())
    result = zoneguard_engine.assess_vessels([ais_record_to_dict(r) for r in records])
    result["requested_limit"] = limit
    result["tracked_vessels_total"] = db.query(AISPosition).count()
    return result

@app.get("/api/zoneguard/vessel/{mmsi}")
def zoneguard_vessel(mmsi: str, db: Session = Depends(get_db)):
    record = get_ais_record_or_404(mmsi, db)
    row = ais_record_to_dict(record)
    zones = zoneguard_engine.check_position(float(row["latitude"]), float(row["longitude"]))
    return {"mmsi": str(mmsi), "name": row.get("name"), "latitude": row["latitude"],
            "longitude": row["longitude"], "inside_mpa": bool(zones), "zones": zones,
            "classification": "PROTECTED_AREA_PRESENCE_REVIEW" if zones else "NO_MPA_POLYGON_MATCH",
            "warning": "MPA presence is not proof of fishing or a legal violation."}

# Static frontend mount MUST remain last; otherwise it intercepts API routes
# registered below it and returns misleading 404 responses.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="dashboard")
