"""Wire contract shared with AnomallyFlow.

Field limits mirror ``qualityflow.detector.ServiceResult``. The service must
return exactly these keys: the consumer rejects extra fields and non-finite
numbers.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InspectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    request_id: str
    product_code: str
    image_sha256: str
    status: Literal["NORMAL", "ABNORMAL", "UNCERTAIN", "ERROR"]
    anomaly_score: float | None = Field(ge=0, le=1)
    quality_passed: bool
    confidence_note: str = Field(max_length=500)
    model_version: str = Field(min_length=1, max_length=80)
    threshold_version: str = Field(min_length=1, max_length=80)
    inference_ms: float = Field(ge=0, le=120000)
    defect_label: str | None = Field(default=None, max_length=80)
    anomaly_map_png_base64: str | None = Field(default=None, max_length=6_000_000)


class ProfileEntry(BaseModel):
    """Threshold band pinned by AnomallyFlow's ``DETECTOR_PROFILES_PATH``.

    Keep this file the single source of truth for versions and thresholds;
    the service and the worker must read the same copy.
    """

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    model_version: str = Field(min_length=1, max_length=80)
    threshold_version: str = Field(min_length=1, max_length=80)
    normal_max: float = Field(ge=0, le=1)
    abnormal_min: float = Field(ge=0, le=1)

    def ordered(self) -> bool:
        return self.normal_max < self.abnormal_min


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checkpoint: str
    sha256: str | None = None
