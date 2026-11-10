from backend.models import Vessel


# Illustrative assumptions for software testing only.
# These are not measured or validated vessel fuel-consumption rates.
REFERENCE_SPEED_KNOTS = 12.0

FUEL_RATE_LITRES_PER_HOUR = {
    "cargo": 1200.0,
    "tanker": 1500.0,
    "container": 1800.0,
    "passenger": 1000.0,
    "fishing": 150.0,
    "other": 300.0,
}

# AIS records in the current database do not contain a verified vessel class.
# "other" is therefore only a generic placeholder assumption for AIS estimates.
CO2_FACTOR_KG_PER_LITRE = 2.68
ENVIRONMENT_WARNING = (
    "Illustrative estimate only, not measured emissions. AIS does not provide "
    "actual fuel consumption or a verified vessel class. Do not use for "
    "regulatory reporting or verified environmental claims."
)


def _estimate(
    vessel_id: str,
    vessel_name: str,
    vessel_type: str,
    status: str,
    speed_knots: float | None,
    data_source: str,
    mmsi: str | None = None,
    vessel_type_known: bool = True,
) -> dict:
    if speed_knots is None:
        fuel = None
        co2 = None
    elif status != "underway" or speed_knots <= 0:
        fuel = 0.0
        co2 = 0.0
    else:
        reference_rate = FUEL_RATE_LITRES_PER_HOUR[vessel_type]
        multiplier = min(speed_knots / REFERENCE_SPEED_KNOTS, 1.5)
        fuel = reference_rate * multiplier
        co2 = fuel * CO2_FACTOR_KG_PER_LITRE

    result = {
        "vessel_id": vessel_id,
        "vessel_name": vessel_name,
        "vessel_type": vessel_type,
        "vessel_type_known": vessel_type_known,
        "status": status,
        "speed_knots": speed_knots,
        "estimated_fuel_litres_per_hour": (
            round(fuel, 2) if fuel is not None else None
        ),
        "estimated_co2_kg_per_hour": (
            round(co2, 2) if co2 is not None else None
        ),
        "assumptions": {
            "reference_speed_knots": REFERENCE_SPEED_KNOTS,
            "co2_factor_kg_per_litre": CO2_FACTOR_KG_PER_LITRE,
            "fuel_rates": "illustrative_demo_assumptions",
            "speed_scaling": "linear, capped at 1.5x",
            "scope": "propulsion estimate only",
            "unknown_ais_vessel_type_uses": "other placeholder rate",
        },
        "assessment_type": "illustrative_estimate_only",
        "data_source": data_source,
        "warning": ENVIRONMENT_WARNING,
    }
    if mmsi:
        result["mmsi"] = mmsi
    return result


def estimate_environmental_impact(vessel: Vessel) -> dict:
    """Return the existing illustrative estimate for a demo Vessel."""
    return _estimate(
        vessel_id=vessel.id,
        vessel_name=vessel.name,
        vessel_type=vessel.vessel_type,
        status=vessel.status,
        speed_knots=vessel.speed_knots,
        data_source=vessel.data_source,
    )


def estimate_ais_environmental_impact(position: dict) -> dict:
    """
    Estimate AIS vessel propulsion impact using the generic 'other' rate.
    AISPosition currently has no verified vessel type or fuel-consumption data.
    """
    mmsi = str(position.get("mmsi") or "")
    raw_status = position.get("navigation_status")
    status_code = str(raw_status).strip() if raw_status is not None else ""

    if status_code in {"0", "2", "3", "4", "7", "8", "11", "12"}:
        status = "underway"
    elif status_code in {"1", "5", "6"}:
        status = "anchored"
    else:
        status = "stopped"

    speed = position.get("speed_knots")
    try:
        speed = float(speed) if speed is not None else None
    except (TypeError, ValueError):
        speed = None
    if speed is not None and (speed < 0 or speed > 100):
        speed = None

    return _estimate(
        vessel_id=mmsi,
        vessel_name=position.get("name") or f"MMSI {mmsi}",
        vessel_type="other",
        status=status,
        speed_knots=speed,
        data_source=position.get("data_source") or "aisstream",
        mmsi=mmsi,
        vessel_type_known=False,
    )
