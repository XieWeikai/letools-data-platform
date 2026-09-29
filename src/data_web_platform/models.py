from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, field_validator


class AlignmentParams(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task: str = "Plug in the Ethernet cable"
    fps: int = Field(default=50, ge=1, le=1000)
    max_gap_ms: float = Field(default=100, gt=0, le=1000)
    command_max_age_ms: float = Field(default=100, gt=0, le=1000)
    gripper_max_gap_ms: float = Field(default=150, gt=0, le=1000)
    state_resampling: Literal["lowpass", "linear"] = "lowpass"
    trim_stationary: bool = True
    motion_joint_threshold_rad: float = Field(default=.002, gt=0, le=1)
    motion_gripper_threshold: float = Field(default=.01, gt=0, le=1)

    @field_validator("task")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or len(value) > 4096:
            raise ValueError("task 必须填写，且不超过 4096 个字符")
        return value.strip()


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["check", "convert"]
    provider: str = "tianji"
    source_root: str
    recordings: list[str] = Field(min_length=1, max_length=10000)
    destination: str | None = None
    target_version: Literal["v2.1", "v3.0"] = "v2.1"
    params: AlignmentParams = Field(default_factory=AlignmentParams)
    workers: int = Field(default=2, ge=1, le=8)
    video_workers: int = Field(default=4, ge=1, le=8)
