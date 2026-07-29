import hashlib

import pytest

from app.db import (
    cache_get,
    cache_purge,
    cache_set,
    create_api_key,
    get_api_key,
    get_connection,
    get_request,
    insert_request,
    list_api_keys,
    revoke_api_key,
    verify_api_key,
)
from app.settings import Settings


@pytest.fixture
def db(tmp_path):
    from app.db import init_db

    conn = get_connection(Settings(database_path=str(tmp_path / "app.db")))
    init_db(conn)
    yield conn
    conn.close()


def _sample_request_row(**overrides) -> dict:
    row = {
        "request_id": "req-1",
        "created_at": "2026-07-29T03:00:00+00:00",
        "api_key_id": None,
        "api_key_label": None,
        "source": "api",
        "tenant_code": "1",
        "user_name": None,
        "filename": "invoice.jpg",
        "content_type": "image/jpeg",
        "file_bytes": 1234,
        "page_count": 1,
        "status": "usable",
        "confidence": 0.95,
        "price_basis": "pre_tax",
        "line_count": 2,
        "grand_total": 1_650_000,
        "cache_hit": 0,
        "model": "fake-model",
        "attempt_count": 1,
        "latency_ms": 500.0,
        "tokens_in": 100,
        "tokens_out": 50,
        "tokens_cached": 0,
        "tokens_total": 150,
        "cost_usd": 0.001,
        "cost_source": "computed",
        "usage_json": "{}",
        "response_json": "{}",
        "error": None,
    }
    row.update(overrides)
    return row


def test_create_api_key_stores_hash_not_plaintext(db):
    key = create_api_key(db, "my-label")

    assert key["plaintext"].startswith("hsv_")
    assert key["key_prefix"] == key["plaintext"][:12]

    row = get_api_key(db, key["id"])
    assert row["key_hash"] == hashlib.sha256(key["plaintext"].encode()).hexdigest()
    assert row["key_hash"] != key["plaintext"]
    assert "plaintext" not in row.keys()


def test_verify_api_key_roundtrip(db):
    key = create_api_key(db, "my-label")

    row = verify_api_key(db, key["plaintext"])
    assert row is not None
    assert row["id"] == key["id"]
    assert row["last_used_at"] is not None


def test_verify_api_key_rejects_wrong_key(db):
    create_api_key(db, "my-label")
    assert verify_api_key(db, "hsv_totally-wrong-key") is None


def test_verify_api_key_rejects_revoked_key(db):
    key = create_api_key(db, "my-label")
    revoke_api_key(db, key["id"])
    assert verify_api_key(db, key["plaintext"]) is None


def test_revoke_api_key_is_idempotent(db):
    key = create_api_key(db, "my-label")
    revoke_api_key(db, key["id"])
    first_revoked_at = get_api_key(db, key["id"])["revoked_at"]

    revoke_api_key(db, key["id"])
    assert get_api_key(db, key["id"])["revoked_at"] == first_revoked_at


def test_list_api_keys_returns_all(db):
    create_api_key(db, "a")
    create_api_key(db, "b")
    assert {row["label"] for row in list_api_keys(db)} == {"a", "b"}


def test_insert_and_get_request_roundtrip(db):
    insert_request(db, _sample_request_row())

    row = get_request(db, "req-1")
    assert row is not None
    assert row["status"] == "usable"
    assert row["confidence"] == 0.95
    assert row["line_count"] == 2
    assert row["cost_usd"] == 0.001


def test_get_request_missing_returns_none(db):
    assert get_request(db, "does-not-exist") is None


def test_cache_purge_removes_all_entries(db):
    cache_set(db, "key-1", {"a": 1})
    cache_set(db, "key-2", {"b": 2})

    removed = cache_purge(db)

    assert removed == 2
    assert cache_get(db, "key-1", ttl_days=0) is None
    assert cache_get(db, "key-2", ttl_days=0) is None


def test_cache_purge_on_empty_cache_returns_zero(db):
    assert cache_purge(db) == 0


def test_init_db_on_fresh_db_creates_tenant_code_column(db):
    cols = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
    assert "tenant_code" in cols


def test_init_db_on_fresh_db_creates_user_name_column(db):
    cols = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
    assert "user_name" in cols


def test_init_db_migrates_existing_requests_table_missing_tenant_code(tmp_path):
    from app.db import init_db

    conn = get_connection(Settings(database_path=str(tmp_path / "old.db")))
    # Simulate a pre-migration prod table: every column except tenant_code.
    conn.executescript("""
        CREATE TABLE requests (
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
    """)
    conn.commit()

    init_db(conn)  # must not raise

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
    assert "tenant_code" in cols
    assert "user_name" in cols
    conn.close()
