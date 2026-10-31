from __future__ import annotations

import csv
import base64
from datetime import datetime, timezone
import io
import json
import binascii
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
from xml.etree import ElementTree as ET
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import get_connection, init_db, rows_to_dict
from .schemas import (
    AlarmEvent,
    BatchChannelCreateRequest,
    BatchChannelCreateResult,
    BatchChannelPreviewResult,
    Channel,
    ChannelPreviewInfo,
    ChannelCreate,
    DetectionCreate,
    ModelInfo,
    ModelRegister,
    OnvifProbeRequest,
    OnvifProbeResult,
    QuickChannelCreate,
    QuickChannelResult,
    Rule,
    RuleCreate,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Warning", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
MODEL_DIRECTORY = Path(__file__).resolve().parent.parent / "models"
EVENT_IMAGE_DIRECTORY = Path(__file__).resolve().parent.parent / "event-images"
ALLOWED_MODEL_SUFFIXES = {".onnx", ".pt", ".pth", ".torchscript", ".zip"}
ALLOWED_SOURCE_TYPES = {"rtsp", "gb28181", "hikvision_nvr", "onvif"}
ALLOWED_PREVIEW_SUFFIXES = (".mp4", ".webm", ".ogg", ".m3u8")
SOAP_NAMESPACES = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}


init_db()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse("app/static/index.html")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/onvif/probe-stream", response_model=OnvifProbeResult)
def probe_onvif_stream(payload: OnvifProbeRequest) -> Dict:
    return detect_onvif_stream(
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password=payload.password,
        profile_token=payload.profile_token,
        device_service_url=payload.device_service_url,
        transport=payload.transport,
    )


def is_browser_playable_url(url: Optional[str]) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def validate_onvif_params(source_type: str, onvif_params: Optional[Dict]) -> None:
    if source_type == "onvif" and onvif_params is None:
        raise HTTPException(status_code=422, detail="onvif_params is required when source_type is onvif")


def normalize_rule_row(row: Dict) -> Dict:
    row["enabled"] = bool(row["enabled"])
    row["roi_polygon"] = json.loads(row["roi_polygon"]) if row.get("roi_polygon") else None
    return row


def normalize_channel_row(row: Dict) -> Dict:
    row["enabled"] = bool(row["enabled"])
    row["onvif_params"] = json.loads(row["onvif_params"]) if row.get("onvif_params") else None
    return row


def normalize_event_row(row: Dict) -> Dict:
    row["alarm_position"] = json.loads(row["alarm_position"]) if row.get("alarm_position") else None
    return row


def save_event_image(image_base64: str) -> str:
    image_text = image_base64.strip()
    extension = ".jpg"
    if image_text.startswith("data:image/"):
        header, _, body = image_text.partition(",")
        image_text = body
        if "image/png" in header:
            extension = ".png"
        elif "image/webp" in header:
            extension = ".webp"
    try:
        image_bytes = base64.b64decode(image_text, validate=True)
    except binascii.Error as error:
        raise HTTPException(status_code=422, detail="image_base64 is invalid") from error
    EVENT_IMAGE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = EVENT_IMAGE_DIRECTORY / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid4().hex}{extension}"
    destination.write_bytes(image_bytes)
    return str(destination)


def onvif_device_service_url(host: str, port: int, device_service_url: Optional[str]) -> str:
    if device_service_url:
        return device_service_url
    return f"http://{host}:{port}/onvif/device_service"


def call_onvif_soap(url: str, username: str, password: str, payload: str) -> ET.Element:
    request = urllib.request.Request(
        url=url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        method="POST",
    )
    password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, url, username, password)
    opener = urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(password_mgr),
        urllib.request.HTTPBasicAuthHandler(password_mgr),
    )
    try:
        with opener.open(request, timeout=10) as response:
            response_xml = response.read()
    except urllib.error.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"onvif request failed: {error.code}") from error
    except urllib.error.URLError as error:
        raise HTTPException(status_code=502, detail=f"onvif request failed: {error.reason}") from error
    try:
        return ET.fromstring(response_xml)
    except ET.ParseError as error:
        raise HTTPException(status_code=502, detail="onvif response is not valid xml") from error


def detect_onvif_stream(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    profile_token: Optional[str],
    device_service_url: Optional[str],
    transport: str,
) -> Dict:
    service_url = onvif_device_service_url(host, port, device_service_url)
    capabilities_xml = call_onvif_soap(
        service_url,
        username,
        password,
        """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <s:Body>
    <tds:GetCapabilities>
      <tds:Category>All</tds:Category>
    </tds:GetCapabilities>
  </s:Body>
</s:Envelope>""",
    )
    media_xaddr = capabilities_xml.findtext(".//tds:Capabilities/tt:Media/XAddr", namespaces=SOAP_NAMESPACES)
    if not media_xaddr:
        raise HTTPException(status_code=502, detail="onvif media service not found")
    profiles_xml = call_onvif_soap(
        media_xaddr,
        username,
        password,
        """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
  <s:Body>
    <trt:GetProfiles />
  </s:Body>
</s:Envelope>""",
    )
    selected_profile_token = profile_token
    if not selected_profile_token:
        first_profile = profiles_xml.find(".//trt:Profiles", namespaces=SOAP_NAMESPACES)
        if first_profile is None or not first_profile.get("token"):
            raise HTTPException(status_code=502, detail="onvif profile not found")
        selected_profile_token = first_profile.get("token")
    stream_xml = call_onvif_soap(
        media_xaddr,
        username,
        password,
        f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <trt:GetStreamUri>
      <trt:StreamSetup>
        <tt:Stream>RTP-Unicast</tt:Stream>
        <tt:Transport>
          <tt:Protocol>{transport}</tt:Protocol>
        </tt:Transport>
      </trt:StreamSetup>
      <trt:ProfileToken>{selected_profile_token}</trt:ProfileToken>
    </trt:GetStreamUri>
  </s:Body>
</s:Envelope>""",
    )
    uri = stream_xml.findtext(".//trt:MediaUri/tt:Uri", namespaces=SOAP_NAMESPACES)
    if not uri:
        raise HTTPException(status_code=502, detail="onvif stream uri not found")
    parsed = urllib.parse.urlparse(uri)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=502, detail="onvif stream uri is invalid")
    return {
        "stream_url": uri,
        "profile_token": selected_profile_token,
        "media_service_url": media_xaddr,
        "device_service_url": service_url,
        "transport": transport,
    }


def point_in_polygon(x: float, y: float, polygon: List[List[float]]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) if (yj - yi) != 0 else 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def detection_center(item: Dict) -> Optional[List[float]]:
    bbox = item.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            x1 = float(bbox[0])
            y1 = float(bbox[1])
            x2 = float(bbox[2])
            y2 = float(bbox[3])
            return [(x1 + x2) / 2.0, (y1 + y2) / 2.0]
        except (TypeError, ValueError):
            return None
    if "x" in item and "y" in item:
        try:
            return [float(item["x"]), float(item["y"])]
        except (TypeError, ValueError):
            return None
    return None


def extract_alarm_position(item: Dict) -> Optional[Dict]:
    bbox = item.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            x1 = float(bbox[0])
            y1 = float(bbox[1])
            x2 = float(bbox[2])
            y2 = float(bbox[3])
            return {"bbox": [x1, y1, x2, y2], "center": [(x1 + x2) / 2.0, (y1 + y2) / 2.0]}
        except (TypeError, ValueError):
            return None
    if "x" in item and "y" in item:
        try:
            return {"point": [float(item["x"]), float(item["y"])]}
        except (TypeError, ValueError):
            return None
    return None


def create_channel_with_default_rule(
    *,
    name: str,
    source_type: str,
    stream_url: str,
    preview_url: Optional[str],
    onvif_params: Optional[Dict],
    model_id: Optional[int],
    confidence: float,
    duration_seconds: float,
    cooldown_seconds: int,
) -> Dict:
    with get_connection() as connection:
        channel_cursor = connection.execute(
            "INSERT INTO channels (name, source_type, stream_url, preview_url, onvif_params, enabled) VALUES (?, ?, ?, ?, ?, 1)",
            (name, source_type, stream_url, preview_url, json.dumps(onvif_params) if onvif_params else None),
        )
        channel = connection.execute(
            "SELECT * FROM channels WHERE id = ?",
            (channel_cursor.lastrowid,),
        ).fetchone()
        rule_cursor = connection.execute(
            """INSERT INTO alarm_rules
            (channel_id, model_id, alarm_type, confidence, duration_seconds, cooldown_seconds, roi_polygon, enabled)
            VALUES (?, ?, 'no_helmet', ?, ?, ?, NULL, 1)""",
            (
                channel["id"],
                model_id,
                confidence,
                duration_seconds,
                cooldown_seconds,
            ),
        )
        rule = connection.execute("SELECT * FROM alarm_rules WHERE id = ?", (rule_cursor.lastrowid,)).fetchone()
        channel_result = normalize_channel_row(dict(channel))
        rule_result = normalize_rule_row(dict(rule))
        return {"channel": channel_result, "rule": rule_result}


def parse_channel_csv(file: UploadFile) -> List[Dict]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".csv":
        raise HTTPException(status_code=415, detail="only .csv file is supported for batch import")
    raw = file.file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=400, detail="csv must be utf-8 encoded") from error
    reader = csv.DictReader(io.StringIO(content))
    headers = set(reader.fieldnames or [])
    required_headers = {"name", "stream_url"}
    if not headers:
        raise HTTPException(status_code=400, detail="csv header is required")
    if not required_headers.issubset(headers):
        missing = ",".join(sorted(required_headers - headers))
        raise HTTPException(status_code=400, detail=f"missing csv headers: {missing}")
    rows: List[Dict] = []
    for line_number, raw_row in enumerate(reader, start=2):
        name = (raw_row.get("name") or "").strip()
        source_type = (raw_row.get("source_type") or "rtsp").strip() or "rtsp"
        stream_url = (raw_row.get("stream_url") or "").strip()
        preview_url = (raw_row.get("preview_url") or "").strip() or None
        error_messages: List[str] = []
        if not name:
            error_messages.append("name is required")
        if not stream_url:
            error_messages.append("stream_url is required")
        if source_type not in ALLOWED_SOURCE_TYPES:
            error_messages.append("source_type must be rtsp/gb28181/hikvision_nvr/onvif")
        if preview_url and not is_browser_playable_url(preview_url):
            error_messages.append("preview_url must start with http:// or https://")
        error_text = "; ".join(error_messages) if error_messages else None
        rows.append(
            {
                "line_number": line_number,
                "name": name,
                "source_type": source_type,
                "stream_url": stream_url,
                "preview_url": preview_url,
                "valid": error_text is None,
                "error": error_text,
            }
        )
    return rows


@app.post("/api/v1/models", response_model=ModelInfo, status_code=201)
def register_model(payload: ModelRegister) -> dict:
    """Register a model repository or local artifact before assigning it to rules."""
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO models (name, framework, source, labels) VALUES (?, ?, ?, ?)",
            (payload.name, payload.framework, payload.source, json.dumps(payload.labels)),
        )
        row = connection.execute("SELECT * FROM models WHERE id = ?", (cursor.lastrowid,)).fetchone()
    result = dict(row)
    result["labels"] = json.loads(result["labels"])
    return result


@app.post("/api/v1/models/{model_id}/file", response_model=ModelInfo)
def upload_model(model_id: int, file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_MODEL_SUFFIXES:
        raise HTTPException(status_code=415, detail="unsupported model file type")
    MODEL_DIRECTORY.mkdir(exist_ok=True)
    with get_connection() as connection:
        model = connection.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        if model is None:
            raise HTTPException(status_code=404, detail="model not found")
        destination = MODEL_DIRECTORY / f"{model_id}-{uuid4().hex}{suffix}"
        with destination.open("wb") as target:
            while chunk := file.file.read(1024 * 1024):
                target.write(chunk)
        connection.execute(
            "UPDATE models SET file_path = ?, status = 'ready' WHERE id = ?",
            (str(destination), model_id),
        )
        row = connection.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    result = dict(row)
    result["labels"] = json.loads(result["labels"])
    return result


@app.get("/api/v1/models", response_model=List[ModelInfo])
def list_models() -> List[Dict]:
    with get_connection() as connection:
        result = rows_to_dict(connection.execute("SELECT * FROM models ORDER BY id DESC"))
    for row in result:
        row["labels"] = json.loads(row["labels"])
    return result


@app.post("/api/v1/channels", response_model=Channel, status_code=201)
def create_channel(payload: ChannelCreate) -> dict:
    if payload.preview_url and not is_browser_playable_url(payload.preview_url):
        raise HTTPException(status_code=422, detail="preview_url must start with http:// or https://")
    validate_onvif_params(payload.source_type, payload.onvif_params.model_dump() if payload.onvif_params else None)
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO channels (name, source_type, stream_url, preview_url, onvif_params, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (
                payload.name,
                payload.source_type,
                payload.stream_url,
                payload.preview_url,
                json.dumps(payload.onvif_params.model_dump()) if payload.onvif_params else None,
                int(payload.enabled),
            ),
        )
        row = connection.execute("SELECT * FROM channels WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return normalize_channel_row(dict(row))


@app.get("/api/v1/channels", response_model=List[Channel])
def list_channels() -> List[Dict]:
    with get_connection() as connection:
        rows = rows_to_dict(connection.execute("SELECT * FROM channels ORDER BY id DESC"))
    return [normalize_channel_row(row) for row in rows]


@app.get("/api/v1/channels/{channel_id}/preview", response_model=ChannelPreviewInfo)
def get_channel_preview(channel_id: int) -> Dict:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="channel not found")
    channel = dict(row)
    preview_url = channel.get("preview_url")
    if is_browser_playable_url(preview_url):
        suffix_message = ""
        lower_preview = preview_url.lower()
        if not any(ext in lower_preview for ext in ALLOWED_PREVIEW_SUFFIXES):
            suffix_message = "（建议使用 mp4/webm/m3u8 等浏览器可播放地址）"
        return {
            "channel_id": channel["id"],
            "source_type": channel["source_type"],
            "stream_url": channel["stream_url"],
            "preview_url": preview_url,
            "playable_in_browser": True,
            "message": f"可直接在浏览器预览{suffix_message}",
        }
    return {
        "channel_id": channel["id"],
        "source_type": channel["source_type"],
        "stream_url": channel["stream_url"],
        "preview_url": preview_url,
        "playable_in_browser": False,
        "message": "当前通道流地址通常需通过转码服务生成 HTTP 预览地址（如 HLS/MP4）",
    }


@app.post("/api/v1/channels/quick-create", response_model=QuickChannelResult, status_code=201)
def quick_create_channel(payload: QuickChannelCreate) -> dict:
    """Create one channel and its default no-helmet rule in one request."""
    if payload.preview_url and not is_browser_playable_url(payload.preview_url):
        raise HTTPException(status_code=422, detail="preview_url must start with http:// or https://")
    validate_onvif_params(payload.source_type, payload.onvif_params.model_dump() if payload.onvif_params else None)
    return create_channel_with_default_rule(
        name=payload.name,
        source_type=payload.source_type,
        stream_url=payload.stream_url,
        preview_url=payload.preview_url,
        onvif_params=payload.onvif_params.model_dump() if payload.onvif_params else None,
        model_id=None,
        confidence=payload.confidence,
        duration_seconds=payload.duration_seconds,
        cooldown_seconds=payload.cooldown_seconds,
    )


@app.post("/api/v1/channels/batch-preview", response_model=BatchChannelPreviewResult)
def preview_channels_csv(file: UploadFile = File(...)) -> Dict:
    rows = parse_channel_csv(file)
    valid_count = sum(1 for row in rows if row["valid"])
    total = len(rows)
    return {
        "rows": rows,
        "total": total,
        "valid_count": valid_count,
        "invalid_count": total - valid_count,
    }


@app.post("/api/v1/channels/batch-create", response_model=BatchChannelCreateResult, status_code=201)
def batch_create_channels(payload: BatchChannelCreateRequest) -> Dict:
    if not payload.rows:
        raise HTTPException(status_code=400, detail="rows cannot be empty")
    if payload.model_id is not None:
        with get_connection() as connection:
            model_exists = connection.execute(
                "SELECT 1 FROM models WHERE id = ?",
                (payload.model_id,),
            ).fetchone()
        if model_exists is None:
            raise HTTPException(status_code=404, detail="model not found")
    channels: List[Dict] = []
    for index, item in enumerate(payload.rows, start=1):
        if not item.name.strip():
            raise HTTPException(status_code=422, detail=f"row {index}: name is required")
        if not item.stream_url.strip():
            raise HTTPException(status_code=422, detail=f"row {index}: stream_url is required")
        if item.preview_url and not is_browser_playable_url(item.preview_url):
            raise HTTPException(status_code=422, detail=f"row {index}: preview_url must start with http:// or https://")
        created = create_channel_with_default_rule(
            name=item.name.strip(),
            source_type=item.source_type,
            stream_url=item.stream_url.strip(),
            preview_url=(item.preview_url or "").strip() or None,
            onvif_params=None,
            model_id=payload.model_id,
            confidence=payload.confidence,
            duration_seconds=payload.duration_seconds,
            cooldown_seconds=payload.cooldown_seconds,
        )
        channels.append(created)
    return {
        "created_count": len(channels),
        "failed_count": 0,
        "channels": channels,
        "failures": [],
    }


@app.post("/api/v1/rules", response_model=Rule, status_code=201)
def create_rule(payload: RuleCreate) -> dict:
    if payload.roi_polygon is not None:
        if len(payload.roi_polygon) < 3:
            raise HTTPException(status_code=422, detail="roi_polygon needs at least 3 points")
        for point in payload.roi_polygon:
            if len(point) != 2:
                raise HTTPException(status_code=422, detail="each roi point must be [x, y]")
            if point[0] < 0 or point[0] > 1 or point[1] < 0 or point[1] > 1:
                raise HTTPException(status_code=422, detail="roi_polygon points must be normalized in [0,1]")
    with get_connection() as connection:
        if connection.execute("SELECT 1 FROM channels WHERE id = ?", (payload.channel_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="channel not found")
        if payload.model_id is not None and connection.execute(
            "SELECT 1 FROM models WHERE id = ?", (payload.model_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="model not found")
        cursor = connection.execute(
            """INSERT INTO alarm_rules
            (channel_id, model_id, alarm_type, confidence, duration_seconds, cooldown_seconds, roi_polygon, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.channel_id,
                payload.model_id,
                payload.alarm_type,
                payload.confidence,
                payload.duration_seconds,
                payload.cooldown_seconds,
                json.dumps(payload.roi_polygon) if payload.roi_polygon else None,
                int(payload.enabled),
            ),
        )
        row = connection.execute("SELECT * FROM alarm_rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return normalize_rule_row(dict(row))


@app.get("/api/v1/rules", response_model=List[Rule])
def list_rules(channel_id: Optional[int] = Query(default=None)) -> List[Dict]:
    query = "SELECT * FROM alarm_rules"
    params: tuple = ()
    if channel_id is not None:
        query += " WHERE channel_id = ?"
        params = (channel_id,)
    query += " ORDER BY id DESC"
    with get_connection() as connection:
        result = rows_to_dict(connection.execute(query, params))
    return [normalize_rule_row(row) for row in result]


@app.post("/api/v1/detections", response_model=List[AlarmEvent])
def submit_detection(payload: DetectionCreate) -> List[Dict]:
    stored_snapshot_path = payload.snapshot_path
    if payload.image_base64:
        stored_snapshot_path = save_event_image(payload.image_base64)
    with get_connection() as connection:
        rules = connection.execute(
            "SELECT * FROM alarm_rules WHERE channel_id = ? AND enabled = 1",
            (payload.channel_id,),
        ).fetchall()
        created: List[Dict] = []
        now = datetime.now(timezone.utc).isoformat()
        for rule in rules:
            roi_polygon = json.loads(rule["roi_polygon"]) if rule["roi_polygon"] else None
            matching = [
                item for item in payload.detections
                if item.get("type") == rule["alarm_type"] and float(item.get("confidence", 0)) >= rule["confidence"]
            ]
            if roi_polygon:
                filtered: List[Dict] = []
                for item in matching:
                    center = detection_center(item)
                    if center is None:
                        continue
                    if point_in_polygon(center[0], center[1], roi_polygon):
                        filtered.append(item)
                matching = filtered
            if not matching:
                continue
            recent = connection.execute(
                """SELECT 1 FROM alarm_events
                WHERE rule_id = ? AND created_at >= datetime('now', ?)
                ORDER BY id DESC LIMIT 1""",
                (rule["id"], f"-{rule['cooldown_seconds']} seconds"),
            ).fetchone()
            if recent is not None:
                continue
            best_detection = max(matching, key=lambda item: float(item.get("confidence", 0)))
            confidence = float(best_detection.get("confidence", 0))
            alarm_position = extract_alarm_position(best_detection)
            cursor = connection.execute(
                """INSERT INTO alarm_events
                (channel_id, rule_id, alarm_type, confidence, snapshot_path, alarm_position, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload.channel_id,
                    rule["id"],
                    rule["alarm_type"],
                    confidence,
                    stored_snapshot_path,
                    json.dumps(alarm_position) if alarm_position else None,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM alarm_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
            created.append(normalize_event_row(dict(row)))
    return created


@app.get("/api/v1/events", response_model=List[AlarmEvent])
def list_events(status: Optional[str] = Query(default=None)) -> List[Dict]:
    query = "SELECT * FROM alarm_events"
    params: tuple = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY id DESC LIMIT 200"
    with get_connection() as connection:
        rows = rows_to_dict(connection.execute(query, params))
    return [normalize_event_row(row) for row in rows]


def update_event(event_id: int, status: str) -> dict:
    field = "acknowledged_at" if status == "acknowledged" else "closed_at"
    with get_connection() as connection:
        cursor = connection.execute(
            f"UPDATE alarm_events SET status = ?, {field} = CURRENT_TIMESTAMP WHERE id = ?",
            (status, event_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="event not found")
        row = connection.execute("SELECT * FROM alarm_events WHERE id = ?", (event_id,)).fetchone()
    return normalize_event_row(dict(row))


@app.get("/api/v1/events/{event_id}/image")
def get_event_image(event_id: int) -> FileResponse:
    with get_connection() as connection:
        row = connection.execute("SELECT snapshot_path FROM alarm_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    snapshot_path = row["snapshot_path"]
    if not snapshot_path:
        raise HTTPException(status_code=404, detail="event image not found")
    image_path = Path(snapshot_path)
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail="event image file not found")
    return FileResponse(image_path)


@app.post("/api/v1/events/{event_id}/acknowledge", response_model=AlarmEvent)
def acknowledge_event(event_id: int) -> dict:
    return update_event(event_id, "acknowledged")


@app.post("/api/v1/events/{event_id}/close", response_model=AlarmEvent)
def close_event(event_id: int) -> dict:
    return update_event(event_id, "closed")
