from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Dict, Iterator, List

DATABASE_PATH = Path(__file__).resolve().parent.parent / "aiwarning.db"


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('rtsp', 'gb28181', 'hikvision_nvr', 'onvif')),
                stream_url TEXT NOT NULL,
                preview_url TEXT,
                onvif_params TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                framework TEXT NOT NULL CHECK (framework IN ('onnx', 'torchscript', 'modelscope', 'other')),
                source TEXT NOT NULL,
                file_path TEXT,
                labels TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'registered' CHECK (status IN ('registered', 'ready', 'failed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alarm_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                model_id INTEGER REFERENCES models(id) ON DELETE SET NULL,
                alarm_type TEXT NOT NULL DEFAULT 'no_helmet',
                confidence REAL NOT NULL DEFAULT 0.6,
                duration_seconds REAL NOT NULL DEFAULT 1.0,
                cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                roi_polygon TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS alarm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                rule_id INTEGER NOT NULL REFERENCES alarm_rules(id) ON DELETE CASCADE,
                alarm_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'closed')),
                snapshot_path TEXT,
                alarm_position TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged_at TEXT,
                closed_at TEXT
            );
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(alarm_rules)")}
        if "model_id" not in columns:
            connection.execute("ALTER TABLE alarm_rules ADD COLUMN model_id INTEGER REFERENCES models(id) ON DELETE SET NULL")
        if "roi_polygon" not in columns:
            connection.execute("ALTER TABLE alarm_rules ADD COLUMN roi_polygon TEXT")
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(alarm_events)")}
        if "alarm_position" not in event_columns:
            connection.execute("ALTER TABLE alarm_events ADD COLUMN alarm_position TEXT")
        channel_columns = {row["name"] for row in connection.execute("PRAGMA table_info(channels)")}
        if "preview_url" not in channel_columns:
            connection.execute("ALTER TABLE channels ADD COLUMN preview_url TEXT")
        if "onvif_params" not in channel_columns:
            connection.execute("ALTER TABLE channels ADD COLUMN onvif_params TEXT")
        migrate_channels_source_type_constraint(connection)


def rows_to_dict(rows: Iterator[sqlite3.Row]) -> List[Dict]:
    return [dict(row) for row in rows]


def migrate_channels_source_type_constraint(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'channels'"
    ).fetchone()
    if row is None:
        return
    table_sql = (row["sql"] or "").lower()
    if "'onvif'" in table_sql:
        return
    connection.executescript(
        """
        CREATE TABLE channels_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ('rtsp', 'gb28181', 'hikvision_nvr', 'onvif')),
            stream_url TEXT NOT NULL,
            preview_url TEXT,
            onvif_params TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO channels_new (id, name, source_type, stream_url, preview_url, onvif_params, enabled, created_at)
        SELECT id, name, source_type, stream_url, preview_url, onvif_params, enabled, created_at FROM channels;
        DROP TABLE channels;
        ALTER TABLE channels_new RENAME TO channels;
        """
    )
