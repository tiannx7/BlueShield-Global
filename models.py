
from typing import Literal

from pydantic import BaseModel, Field


class Vessel(BaseModel):
    id: str
    name: str
    vessel_type: Literal[
        "cargo",
        "tanker",
        "container",
        "passenger",
        "fishing",
        "other",
    ]

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    speed_knots: float = Field(ge=0, le=100)
    heading: float = Field(ge=0, lt=360)

    status: Literal[
        "underway",
        "anchored",
        "stopped",
    ]

    data_source: str = "simulation"
