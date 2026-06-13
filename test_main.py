import hashlib
import hmac
import json
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import main


def make_init_data(user: dict, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", main.BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{key}={quote(value)}" for key, value in fields.items())


def test_validate_init_data_accepts_signed_payload():
    user = {"id": 1016718472, "first_name": "Kirill", "username": "kirill774"}
    assert main.validate_init_data(make_init_data(user))["id"] == 1016718472


def test_validate_init_data_rejects_stale_payload():
    stale = int(time.time()) - 90000
    user = {"id": 1016718472, "first_name": "Kirill"}
    assert main.validate_init_data(make_init_data(user, auth_date=stale)) is None


def test_worker_auth_rejects_wrong_secret():
    assert main.worker_auth("wrong-secret") is False


def test_subjects_endpoint_returns_prices():
    client = TestClient(main.app)
    response = client.get("/api/subjects")
    assert response.status_code == 200
    payload = response.json()
    assert payload["prices"]["kt"]["stars"] == 390
