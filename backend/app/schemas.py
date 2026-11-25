from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.gaussian_fusion import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS

Category = Literal['Pilz', 'Pflanze', 'Person', 'Ding', 'Ereignis', 'Unbekannt']
Environment = Literal['unknown', 'indoor', 'outdoor']
LocationSource = Literal['unknown', 'exif', 'manual', 'demo', 'browser']
TimeSource = Literal['unknown', 'exif', 'google', 'manual', 'demo', 'browser']


def from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class ObservationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    category: Category = 'Unbekannt'
    environment: Environment = 'unknown'
    description: str = Field(default='', max_length=10000)


class ObservationPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: Category | None = None
    environment: Environment | None = None
    description: str | None = Field(default=None, max_length=10000)


class MediaPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    sigma_meters: float | None = Field(default=None, gt=0)
    location_source: LocationSource | None = None
    observed_at_ms: int | None = Field(default=None, ge=MIN_TIMESTAMP_MS, le=MAX_TIMESTAMP_MS, strict=True)
    sigma_seconds: float | None = Field(default=None, gt=0)
    time_source: TimeSource | None = None

    @model_validator(mode='after')
    def patch_pairs(self):
        fields = self.model_fields_set
        if ('lat' in fields) != ('lon' in fields) or (self.lat is None) != (self.lon is None):
            raise ValueError('lat und lon müssen gemeinsam gesetzt oder gelöscht werden')
        return self


class ObservationMerge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_observation_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_members(self):
        if len(set(self.source_observation_ids)) != len(self.source_observation_ids):
            raise ValueError('IDs müssen eindeutig sein')
        return self
