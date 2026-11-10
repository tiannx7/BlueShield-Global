
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


def current_utc_time():
    return datetime.now(timezone.utc)


class VesselRecord(Base):
    __tablename__ = "vessels"

    id: Mapped[str] = mapped_column(
        String(50), primary_key=True
    )

    name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )

    vessel_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )

    latitude: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    longitude: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    speed_knots: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    heading: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30), nullable=False
    )

    data_source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="simulation"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=current_utc_time,
        onupdate=current_utc_time,
        nullable=False,
    )

class AISPosition(Base):
    """Latest received AIS position, keyed by the real vessel MMSI."""

    __tablename__ = "ais_positions"

    mmsi: Mapped[str] = mapped_column(
        String(20), primary_key=True
    )

    name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    latitude: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    longitude: Mapped[float] = mapped_column(
        Float, nullable=False
    )

    speed_knots: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    course: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    heading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )

    navigation_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )

    position_timestamp: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=current_utc_time,
        nullable=False,
    )

    data_source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="aisstream"
    )


class AISPositionHistory(Base):
    """Append-only received AIS observations for track-based screening."""

    __tablename__ = "ais_position_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mmsi: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    speed_knots: Mapped[float | None] = mapped_column(Float, nullable=True)
    course: Mapped[float | None] = mapped_column(Float, nullable=True)
    navigation_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    position_timestamp: Mapped[str | None] = mapped_column(String(50), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=current_utc_time, index=True, nullable=False)
    data_source: Mapped[str] = mapped_column(String(30), nullable=False, default="aisstream")


class VesselIdentity(Base):
    """Latest AIS static identity and voyage metadata, keyed by MMSI."""

    __tablename__ = "vessel_identity"

    mmsi: Mapped[str] = mapped_column(String(20), primary_key=True)
    imo: Mapped[str | None] = mapped_column(String(20), nullable=True)
    call_sign: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ship_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ship_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(100), nullable=True)
    draught: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta: Mapped[str | None] = mapped_column(String(50), nullable=True)
    dimensions_json: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ais_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_static_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=current_utc_time, nullable=False
    )
    data_source: Mapped[str] = mapped_column(String(30), nullable=False, default="aisstream")
