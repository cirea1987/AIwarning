from datetime import datetime, timezone
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import get_connection, init_db, rows_to_dict
from .schemas import (
    AlarmEvent, Channel, ChannelCreate, DetectionCreate, ModelInfo, ModelRegister, Rule, RuleCreate,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI Warning", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
MODEL_DIRECTORY = Path(__file__).resolve().parent.parent / "models"
ALLOWED_MODEL_SUFFIXES = {".onnx", ".pt", ".pth", ".torchscript", ".zip"}


init_db()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse("app/static/index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.get("/api/v1/models", response_model=list[ModelInfo])
def list_models() -> list[dict]:
    with get_connection() as connection:
        result = rows_to_dict(connection.execute("SELECT * FROM models ORDER BY id DESC"))
    for row in result:
        row["labels"] = json.loads(row["labels"])
    return result


@app.post("/api/v1/channels", response_model=Channel, status_code=201)
def create_channel(payload: ChannelCreate) -> dict:
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO channels (name, source_type, stream_url, enabled) VALUES (?, ?, ?, ?)",
            (payload.name, payload.source_type, payload.stream_url, int(payload.enabled)),
        )
        row = connection.execute("SELECT * FROM channels WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/v1/channels", response_model=list[Channel])
def list_channels() -> list[dict]:
    with get_connection() as connection:
        return rows_to_dict(connection.execute("SELECT * FROM channels ORDER BY id DESC"))


@app.post("/api/v1/rules", response_model=Rule, status_code=201)
def create_rule(payload: RuleCreate) -> dict:
    with get_connection() as connection:
        if connection.execute("SELECT 1 FROM channels WHERE id = ?", (payload.channel_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="channel not found")
        if payload.model_id is not None and connection.execute(
            "SELECT 1 FROM models WHERE id = ?", (payload.model_id,)
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="model not found")
        cursor = connection.execute(
            """INSERT INTO alarm_rules
            (channel_id, model_id, alarm_type, confidence, duration_seconds, cooldown_seconds, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.channel_id,
                payload.model_id,
                payload.alarm_type,
                payload.confidence,
                payload.duration_seconds,
                payload.cooldown_seconds,
                int(payload.enabled),
            ),
        )
        row = connection.execute("SELECT * FROM alarm_rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    return result


@app.get("/api/v1/rules", response_model=list[Rule])
def list_rules(channel_id: int | None = Query(default=None)) -> list[dict]:
    query = "SELECT * FROM alarm_rules"
    params: tuple = ()
    if channel_id is not None:
        query += " WHERE channel_id = ?"
        params = (channel_id,)
    query += " ORDER BY id DESC"
    with get_connection() as connection:
        result = rows_to_dict(connection.execute(query, params))
    for row in result:
        row["enabled"] = bool(row["enabled"])
    return result


@app.post("/api/v1/detections", response_model=list[AlarmEvent])
def submit_detection(payload: DetectionCreate) -> list[dict]:
    with get_connection() as connection:
        rules = connection.execute(
            "SELECT * FROM alarm_rules WHERE channel_id = ? AND enabled = 1",
            (payload.channel_id,),
        ).fetchall()
        created: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()
        for rule in rules:
            matching = [
                item for item in payload.detections
                if item.get("type") == rule["alarm_type"] and float(item.get("confidence", 0)) >= rule["confidence"]
            ]
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
            confidence = max(float(item.get("confidence", 0)) for item in matching)
            cursor = connection.execute(
                """INSERT INTO alarm_events
                (channel_id, rule_id, alarm_type, confidence, snapshot_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (payload.channel_id, rule["id"], rule["alarm_type"], confidence, payload.snapshot_path, now),
            )
            row = connection.execute("SELECT * FROM alarm_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
            created.append(dict(row))
    return created


@app.get("/api/v1/events", response_model=list[AlarmEvent])
def list_events(status: str | None = Query(default=None)) -> list[dict]:
    query = "SELECT * FROM alarm_events"
    params: tuple = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY id DESC LIMIT 200"
    with get_connection() as connection:
        return rows_to_dict(connection.execute(query, params))


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
    return dict(row)


@app.post("/api/v1/events/{event_id}/acknowledge", response_model=AlarmEvent)
def acknowledge_event(event_id: int) -> dict:
    return update_event(event_id, "acknowledged")


@app.post("/api/v1/events/{event_id}/close", response_model=AlarmEvent)
def close_event(event_id: int) -> dict:
    return update_event(event_id, "closed")
