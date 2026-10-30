from typing import Literal
from pydantic import BaseModel, Field

SourceType = Literal["rtsp", "gb28181", "hikvision_nvr"]
AlarmType = Literal["no_helmet"]


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_type: SourceType
    stream_url: str = Field(min_length=1, max_length=1000)
    enabled: bool = True


class Channel(ChannelCreate):
    id: int
    created_at: str


class RuleCreate(BaseModel):
    channel_id: int
    model_id: int | None = None
    alarm_type: AlarmType = "no_helmet"
    confidence: float = Field(default=0.6, ge=0, le=1)
    duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    enabled: bool = True


class Rule(RuleCreate):
    id: int


class DetectionCreate(BaseModel):
    channel_id: int
    detections: list[dict] = Field(default_factory=list)
    snapshot_path: str | None = None


class ModelRegister(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    framework: Literal["onnx", "torchscript", "modelscope", "other"]
    source: str = Field(min_length=1, max_length=1000)
    labels: list[str] = Field(default_factory=list)


class ModelInfo(ModelRegister):
    id: int
    file_path: str | None
    status: str
    created_at: str


class AlarmEvent(BaseModel):
    id: int
    channel_id: int
    rule_id: int
    alarm_type: str
    confidence: float
    status: str
    snapshot_path: str | None
    created_at: str
    acknowledged_at: str | None
    closed_at: str | None
