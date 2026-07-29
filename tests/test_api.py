from app.db import create_api_key, get_request, revoke_api_key


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_extract_requires_api_key(client, sample_jpeg_bytes):
    r = client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    assert r.status_code == 401


def test_extract_rejects_unknown_key(client, sample_jpeg_bytes):
    r = client.post(
        "/v1/extract",
        files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")},
        headers={"X-API-Key": "hsv_not-a-real-key"},
    )
    assert r.status_code == 401


def test_extract_rejects_revoked_key(client, sample_jpeg_bytes):
    key = create_api_key(client.app.state.db, "revoke-me")
    revoke_api_key(client.app.state.db, key["id"])

    r = client.post(
        "/v1/extract",
        files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")},
        headers={"X-API-Key": key["plaintext"]},
    )
    assert r.status_code == 401


def test_extract_rejects_empty_file(auth_client, sample_jpeg_bytes):
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", b"", "image/jpeg")})
    assert r.status_code == 400


def test_extract_rejects_oversized_file(auth_client, monkeypatch, sample_jpeg_bytes):
    auth_client.app.state.settings.max_upload_bytes = len(sample_jpeg_bytes) - 1
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    assert r.status_code == 413


def test_extract_rejects_unsupported_content_type(auth_client):
    r = auth_client.post("/v1/extract", files={"file": ("i.txt", b"hello world", "text/plain")})
    assert r.status_code == 415


def test_extract_happy_path_with_default_fixture(auth_client, sample_jpeg_bytes):
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "usable"
    assert body["confidence"] == 1.0
    assert len(body["line_items"]) == 2
    assert body["usage"]["cached"] is False
    assert body["usage"]["attempts"] == 1


def test_extract_omits_usage_when_disabled(auth_client, sample_jpeg_bytes):
    auth_client.app.state.settings.expose_usage_in_response = False
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["usage"] is None

    # Audit log / dashboard still get full usage regardless of the toggle.
    logged = get_request(auth_client.app.state.db, body["request_id"])
    assert logged["model"] is not None
    assert logged["tokens_total"] > 0


def test_extract_reports_cost_vnd_from_configured_rate(auth_client, sample_jpeg_bytes, app_module):
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    usage = r.json()["usage"]
    expected = round(usage["cost_usd"] * app_module.settings.usd_to_vnd_rate)
    assert usage["cost_vnd"] == expected


def test_extract_happy_path_with_real_sample_image(auth_client, sample_invoice_png_bytes):
    files = {"file": ("invoice.png", sample_invoice_png_bytes, "image/png")}
    r = auth_client.post("/v1/extract", files=files)
    assert r.status_code == 200
    assert r.json()["status"] == "usable"


def test_extract_honors_mock_fixture_header(auth_client, sample_jpeg_bytes):
    r = auth_client.post(
        "/v1/extract",
        files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")},
        headers={"X-Mock-Fixture": "pretax_basic"},
    )
    assert r.status_code == 200
    assert r.json()["status"] in {"usable", "needs_human_review"}


def test_extract_second_identical_request_is_cache_hit(auth_client, sample_jpeg_bytes):
    files = {"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")}
    first = auth_client.post("/v1/extract", files=files)
    second = auth_client.post("/v1/extract", files=files)
    assert first.json()["usage"]["cached"] is False
    assert second.json()["usage"]["cached"] is True


def test_extract_logs_to_requests_table(auth_client, sample_jpeg_bytes):
    r = auth_client.post("/v1/extract", files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")})
    request_id = r.json()["request_id"]

    row = get_request(auth_client.app.state.db, request_id)
    assert row is not None
    assert row["status"] == "usable"
    assert row["source"] == "api"
    assert row["filename"] == "i.jpg"
    assert row["cache_hit"] == 0
    assert row["line_count"] == 2


def test_extract_cache_hit_logs_avoided_cost_not_zero(auth_client, sample_jpeg_bytes):
    files = {"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")}
    first = auth_client.post("/v1/extract", files=files)
    second = auth_client.post("/v1/extract", files=files)

    first_row = get_request(auth_client.app.state.db, first.json()["request_id"])
    second_row = get_request(auth_client.app.state.db, second.json()["request_id"])

    # Client-facing usage is zeroed on a cache hit ("cache hits cost
    # nothing"), but the audit log must retain what the call avoided so the
    # dashboard can compute cost saved by cache.
    assert second.json()["usage"]["cost_usd"] == 0.0
    assert second_row["cost_source"] == "cache_hit"
    assert second_row["cost_usd"] == first_row["cost_usd"]
    assert second_row["tokens_total"] == first_row["tokens_total"]
    assert second_row["cost_usd"] > 0


def test_extract_missing_mock_fixture_returns_unusable_not_500(auth_client, sample_jpeg_bytes):
    r = auth_client.post(
        "/v1/extract",
        files={"file": ("i.jpg", sample_jpeg_bytes, "image/jpeg")},
        headers={"X-Mock-Fixture": "does-not-exist"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "unusable"
