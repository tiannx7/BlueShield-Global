from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from backend.models import Vessel

# AIS SOG is represented in knots. 102.2 knots is the largest ordinary
# encoded value; 102.3 is the AIS "not available" code in common AIS formats.
AIS_MAX_SPEED_KNOTS = 102.2
STALE_AFTER_SECONDS = 30 * 60


def _as_finite_float(value: Any) -> float | None:
    """Convert a value to a finite float, otherwise return None."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse datetime/ISO text and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _risk_result(
    vessel_id: str,
    vessel_name: str,
    speed_knots: float | None,
    status: str,
    data_source: str,
    mmsi: str | None = None,
) -> dict:
    """
    Preserve the legacy demo-vessel endpoint's illustrative rule output.
    This score is not a validated maritime safety or threat model.
    """
    speed = _as_finite_float(speed_knots)
    if speed is None:
        result = {
            "vessel_id": vessel_id,
            "vessel_name": vessel_name,
            "score": None,
            "level": "unknown",
            "reasons": ["AIS speed is unavailable or invalid; no score calculated."],
            "assessment_type": "illustrative_rules_based_review",
            "data_source": data_source,
            "warning": "Illustrative review only; not a validated maritime safety or threat assessment.",
        }
        if mmsi:
            result["mmsi"] = mmsi
        return result

    score = 10
    reasons: list[str] = []

    if speed > 20:
        score += 20
        reasons.append("Reported speed is above 20 knots; review vessel type and context.")
    if speed > 30:
        score += 25
        reasons.append("Reported speed is above 30 knots; verify the AIS source and track.")
    if str(status).lower() == "underway" and speed == 0:
        score += 15
        reasons.append("AIS reports underway status with zero reported speed.")

    score = min(score, 100)
    level = "low" if score < 25 else "moderate" if score < 50 else "high"
    if not reasons:
        reasons.append("No configured review rules were triggered.")

    result = {
        "vessel_id": vessel_id,
        "vessel_name": vessel_name,
        "score": score,
        "level": level,
        "reasons": reasons,
        "assessment_type": "illustrative_rules_based_review",
        "data_source": data_source,
        "warning": "Illustrative review only; not a validated maritime safety or threat assessment.",
    }
    if mmsi:
        result["mmsi"] = mmsi
    return result


def assess_vessel(vessel: Vessel) -> dict:
    """Keep the existing demo-vessel risk endpoint compatible."""
    return _risk_result(
        vessel_id=str(vessel.id),
        vessel_name=str(vessel.name),
        speed_knots=vessel.speed_knots,
        status=str(vessel.status),
        data_source=str(vessel.data_source),
    )


def assess_ais_position(position: dict) -> dict:
    """
    Screen one AIS report for data-quality and review indicators.

    This deliberately does not infer illegal fishing, criminal intent, or
    navigational danger from a single AIS position.
    """
    mmsi = str(position.get("mmsi") or "")
    name = position.get("name") or (f"MMSI {mmsi}" if mmsi else "Unknown vessel")
    now = datetime.now(timezone.utc)

    signals: list[dict] = []
    score = 0

    lat = _as_finite_float(position.get("latitude"))
    lon = _as_finite_float(position.get("longitude"))
    speed = _as_finite_float(position.get("speed_knots"))
    course = _as_finite_float(position.get("course"))
    heading = _as_finite_float(position.get("heading"))

    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        score += 50
        signals.append({
            "code": "INVALID_POSITION",
            "severity": "high",
            "message": "Latitude or longitude is missing, non-numeric, or outside valid geographic bounds.",
        })

    if position.get("speed_knots") is not None:
        if speed is None or speed < 0 or speed > AIS_MAX_SPEED_KNOTS:
            score += 30
            signals.append({
                "code": "INVALID_SPEED_VALUE",
                "severity": "medium",
                "message": "Reported speed is outside the accepted AIS numeric range or is not a valid number; treat it as unverified.",
            })
        elif speed > 60:
            score += 20
            signals.append({
                "code": "EXTREME_REPORTED_SPEED",
                "severity": "medium",
                "message": f"Reported speed is {speed:.1f} knots; verify vessel type, source data, and adjacent positions before interpreting it.",
            })
        elif speed > 35:
            score += 10
            signals.append({
                "code": "HIGH_REPORTED_SPEED",
                "severity": "low",
                "message": f"Reported speed is {speed:.1f} knots; compare with vessel type and track context.",
            })

    if position.get("course") is not None:
        if course is None or not (0 <= course < 360):
            score += 20
            signals.append({
                "code": "INVALID_COURSE_VALUE",
                "severity": "medium",
                "message": "Reported course is outside the valid 0–359.9 degree range or is not numeric.",
            })

    # AIS heading 511 commonly means "not available"; do not treat it as a
    # genuine heading. Other values outside 0–359 are invalid.
    if position.get("heading") is not None and heading is not None:
        if heading != 511 and not (0 <= heading <= 359):
            score += 10
            signals.append({
                "code": "INVALID_HEADING_VALUE",
                "severity": "low",
                "message": "Reported true heading is outside the valid 0–359 degree range.",
            })

    received_at = _parse_datetime(position.get("received_at"))
    if received_at is None:
        received_at = _parse_datetime(position.get("position_timestamp"))

    age_seconds = None
    position_status = "unknown"
    if received_at is not None:
        age_seconds = int((now - received_at).total_seconds())
        if age_seconds < -300:
            score += 10
            signals.append({
                "code": "FUTURE_TIMESTAMP",
                "severity": "low",
                "message": "AIS timestamp is more than five minutes in the future; verify clock and source metadata.",
            })
            position_status = "unknown"
        else:
            age_seconds = max(0, age_seconds)
            if age_seconds <= 900:
                position_status = "fresh"
            elif age_seconds <= STALE_AFTER_SECONDS:
                position_status = "delayed"
            else:
                position_status = "stale"
                score += 20
                signals.append({
                    "code": "STALE_POSITION",
                    "severity": "medium",
                    "message": f"Position was last received {age_seconds // 60} minutes ago; it should not be treated as a current location.",
                })

    score = min(score, 100)
    if score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    elif score > 0:
        level = "low"
    else:
        level = "normal"

    if not signals:
        summary = "No configured AIS data-quality review indicators were triggered."
    else:
        summary = "One or more AIS data-quality indicators require verification."

    return {
        "mmsi": mmsi or None,
        "name": name,
        "score": score,
        "level": level,
        "signals": signals,
        "summary": summary,
        "assessment_type": "ais_data_quality_and_review_screening",
        "data_source": position.get("data_source") or "aisstream",
        "received_at": position.get("received_at"),
        "position_timestamp": position.get("position_timestamp"),
        "age_seconds": age_seconds,
        "position_status": position_status,
        "warning": (
            "Screening indicators are not proof of illegal fishing, intent, "
            "a legal violation, or navigational danger. Verify with independent evidence."
        ),
    }
