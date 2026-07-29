from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.settings import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_prefix   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    request_id     TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    api_key_id     TEXT REFERENCES api_keys(id),
    api_key_label  TEXT,
    source         TEXT NOT NULL,

    filename       TEXT,
    content_type   TEXT,
    file_bytes     INTEGER,
    page_count     INTEGER,

    status         TEXT NOT NULL,
    confidence     REAL NOT NULL,
    price_basis    TEXT,
    line_count     INTEGER NOT NULL DEFAULT 0,
    grand_total    INTEGER,

    cache_hit      INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    attempt_count  INTEGER NOT NULL DEFAULT 1,
    latency_ms     REAL,

    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    tokens_cached  INTEGER NOT NULL DEFAULT 0,
    tokens_total   INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0.0,
    cost_source    TEXT,
    usage_json     TEXT,

    response_json  TEXT NOT NULL,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_created  ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_requests_key_time ON requests(api_key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_status   ON requests(status);

CREATE TABLE IF NOT EXISTS extraction_cache (
    cache_key     TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def get_connection(settings: Settings) -> sqlite3.Connection:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# ---- API keys ----


def create_api_key(conn: sqlite3.Connection, label: str) -> dict:
    plaintext = f"hsv_{secrets.token_urlsafe(32)}"
    key_id = str(uuid.uuid4())
    created_at = _now()
    conn.execute(
        "INSERT INTO api_keys (id, label, key_hash, key_prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        (key_id, label, _hash_key(plaintext), plaintext[:12], created_at),
    )
    conn.commit()
    return {
        "id": key_id,
        "label": label,
        "key_prefix": plaintext[:12],
        "created_at": created_at,
        "plaintext": plaintext,
    }


def verify_api_key(conn: sqlite3.Connection, plaintext: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (_hash_key(plaintext),)
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), row["id"]))
    conn.commit()
    return get_api_key(conn, row["id"])


def revoke_api_key(conn: sqlite3.Connection, key_id: str) -> None:
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), key_id)
    )
    conn.commit()


def list_api_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()


def get_api_key(conn: sqlite3.Connection, key_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()


# ---- Requests (audit log) ----


def insert_request(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO requests (
            request_id, created_at, api_key_id, api_key_label, source,
            filename, content_type, file_bytes, page_count,
            status, confidence, price_basis, line_count, grand_total,
            cache_hit, model, attempt_count, latency_ms,
            tokens_in, tokens_out, tokens_cached, tokens_total,
            cost_usd, cost_source, usage_json,
            response_json, error
        ) VALUES (
            :request_id, :created_at, :api_key_id, :api_key_label, :source,
            :filename, :content_type, :file_bytes, :page_count,
            :status, :confidence, :price_basis, :line_count, :grand_total,
            :cache_hit, :model, :attempt_count, :latency_ms,
            :tokens_in, :tokens_out, :tokens_cached, :tokens_total,
            :cost_usd, :cost_source, :usage_json,
            :response_json, :error
        )
        """,
        row,
    )
    conn.commit()


def get_request(conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()


# ---- Extraction cache ----
#
# Only written when the pipeline judges the result usable -- see
# app.pipeline.extract, which is also where the cache key is computed.


def cache_get(conn: sqlite3.Connection, cache_key: str, *, ttl_days: int) -> dict | None:
    row = conn.execute(
        "SELECT response_json, created_at FROM extraction_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if row is None:
        return None
    if ttl_days > 0:
        created_at = datetime.fromisoformat(row["created_at"])
        if datetime.now(UTC) - created_at > timedelta(days=ttl_days):
            return None
    return json.loads(row["response_json"])


def cache_set(conn: sqlite3.Connection, cache_key: str, response: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extraction_cache (cache_key, response_json, created_at) "
        "VALUES (?, ?, ?)",
        (cache_key, json.dumps(response), _now()),
    )
    conn.commit()


def cache_purge(conn: sqlite3.Connection) -> int:
    """Delete every extraction_cache row. Returns the number of rows removed."""
    cursor = conn.execute("DELETE FROM extraction_cache")
    conn.commit()
    return cursor.rowcount
