from __future__ import annotations

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

SourceType = Literal["rtsp", "gb28181", "hikvision_nvr", "onvif"]
AlarmType = Literal["helmet", "no_helmet", "intrusion", "crowding"]


class OnvifChannelParams(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    profile_token: Optional[str] = Field(default=None, max_length=200)
    device_service_url: Optional[str] = Field(default=None, max_length=1000)
    transport: Literal["RTSP", "HTTP", "UDP"] = "RTSP"


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_type: SourceType
    stream_url: str = Field(min_length=1, max_length=1000)
    preview_url: Optional[str] = Field(default=None, max_length=1000)
    onvif_params: Optional[OnvifChannelParams] = None
    enabled: bool = True


class Channel(ChannelCreate):
    id: int
    created_at: str


class RuleCreate(BaseModel):
    channel_id: int
    model_id: Optional[int] = None
    alarm_type: AlarmType = "no_helmet"
    confidence: float = Field(default=0.6, ge=0, le=1)
    duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    roi_polygon: Optional[List[List[float]]] = None
    enabled: bool = True


class Rule(RuleCreate):
    id: int


class QuickChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    stream_url: str = Field(min_length=1, max_length=1000)
    source_type: SourceType = "rtsp"
    preview_url: Optional[str] = Field(default=None, max_length=1000)
    onvif_params: Optional[OnvifChannelParams] = None
    confidence: float = Field(default=0.6, ge=0, le=1)
    duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class QuickChannelResult(BaseModel):
    channel: Channel
    rule: Rule


class BatchChannelPreviewRow(BaseModel):
    line_number: int
    name: str
    source_type: str
    stream_url: str
    preview_url: Optional[str] = None
    valid: bool
    error: Optional[str] = None


class BatchChannelPreviewResult(BaseModel):
    rows: List[BatchChannelPreviewRow]
    total: int
    valid_count: int
    invalid_count: int


class BatchChannelCreateItem(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    stream_url: str = Field(min_length=1, max_length=1000)
    source_type: SourceType = "rtsp"
    preview_url: Optional[str] = Field(default=None, max_length=1000)


class BatchChannelCreateRequest(BaseModel):
    rows: List[BatchChannelCreateItem] = Field(default_factory=list)
    model_id: Optional[int] = None
    confidence: float = Field(default=0.6, ge=0, le=1)
    duration_seconds: float = Field(default=1.0, ge=0, le=3600)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class BatchChannelCreateFailure(BaseModel):
    index: int
    name: str
    error: str


class BatchChannelCreateResult(BaseModel):
    created_count: int
    failed_count: int
    channels: List[QuickChannelResult]
    failures: List[BatchChannelCreateFailure]


class DetectionCreate(BaseModel):
    channel_id: int
    detections: List[Dict] = Field(default_factory=list)
    snapshot_path: Optional[str] = None
    image_base64: Optional[str] = None


class ModelRegister(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    framework: Literal["onnx", "torchscript", "modelscope", "other"]
    source: str = Field(min_length=1, max_length=1000)
    labels: List[str] = Field(default_factory=list)


class ModelInfo(ModelRegister):
    id: int
    file_path: Optional[str]
    status: str
    created_at: str


class AlarmEvent(BaseModel):
    id: int
    channel_id: int
    rule_id: int
    alarm_type: str
    confidence: float
    status: str
    snapshot_path: Optional[str]
    alarm_position: Optional[Dict]
    created_at: str
    acknowledged_at: Optional[str]
    closed_at: Optional[str]


class ChannelPreviewInfo(BaseModel):
    channel_id: int
    source_type: SourceType
    stream_url: str
    preview_url: Optional[str]
    playable_in_browser: bool
    message: str


class OnvifProbeRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=128)
    profile_token: Optional[str] = Field(default=None, max_length=200)
    device_service_url: Optional[str] = Field(default=None, max_length=1000)
    transport: Literal["RTSP", "HTTP", "UDP"] = "RTSP"


class OnvifProbeResult(BaseModel):
    stream_url: str
    profile_token: str
    media_service_url: str
    device_service_url: str
    transport: str
