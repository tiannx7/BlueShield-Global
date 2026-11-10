
from backend.models import Vessel
import math


def get_demo_vessels() -> list[Vessel]:
    """Return simulated vessels for development and testing."""

    vessels = [
        Vessel(
            id="BS-V001",
            name="Demo Cargo Vessel",
            vessel_type="cargo",
            latitude=19.0760,
            longitude=72.8777,
            speed_knots=12.4,
            heading=85,
            status="underway",
        ),
        Vessel(
            id="BS-V002",
            name="Demo Tanker",
            vessel_type="tanker",
            latitude=18.9500,
            longitude=72.8000,
            speed_knots=9.8,
            heading=120,
            status="underway",
        ),
        Vessel(
            id="BS-V003",
            name="Demo Container Vessel",
            vessel_type="container",
            latitude=19.1500,
            longitude=72.9500,
            speed_knots=0.0,
            heading=0,
            status="anchored",
        ),
    ]

    return vessels

def advance_vessel(vessel: Vessel, elapsed_seconds: float) -> Vessel:
    """
    Advance a simulated vessel by a specified time interval.

    This is a simplified demo model, not a navigation model.
    """

    if elapsed_seconds <= 0:
        raise ValueError("elapsed_seconds must be positive")

    # Anchored or stopped vessels do not move in this demo.
    if vessel.status != "underway" or vessel.speed_knots == 0:
        return vessel

    # Convert speed and time into nautical miles travelled.
    distance_nm = vessel.speed_knots * elapsed_seconds / 3600.0

    # Convert heading from degrees to radians.
    heading_rad = math.radians(vessel.heading)

    # Approximate latitude change:
    # 1 degree of latitude is approximately 60 nautical miles.
    latitude_change = (
        distance_nm * math.cos(heading_rad) / 60.0
    )

    new_latitude = vessel.latitude + latitude_change

    # Avoid calculating longitude at or beyond the poles.
    safe_latitude = max(-89.9, min(89.9, vessel.latitude))
    latitude_rad = math.radians(safe_latitude)
    longitude_scale = max(abs(math.cos(latitude_rad)), 0.001)

    longitude_change = (
        distance_nm * math.sin(heading_rad)
        / (60.0 * longitude_scale)
    )

    new_longitude = vessel.longitude + longitude_change

    # Keep latitude and longitude within valid geographic ranges.
    new_latitude = max(-90.0, min(90.0, new_latitude))
    new_longitude = (
        (new_longitude + 180.0) % 360.0
    ) - 180.0

    # Return a new validated vessel object.
    return Vessel(
        id=vessel.id,
        name=vessel.name,
        vessel_type=vessel.vessel_type,
        latitude=new_latitude,
        longitude=new_longitude,
        speed_knots=vessel.speed_knots,
        heading=vessel.heading,
        status=vessel.status,
        data_source="simulation",
    )

