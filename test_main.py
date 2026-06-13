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


def test_submit_returns_job_and_job_endpoint_returns_worker_result_once():
    main._jobs.clear()
    main._orders.clear()
    client = TestClient(main.app)
    user = {"id": 222, "first_name": "Student", "username": "student"}
    init_data = make_init_data(user)

    submit = client.post(
        "/api/submit",
        json={
            "init_data": init_data,
            "subject": "SMM",
            "task": "Практическая работа по контент-плану",
            "urgent": False,
        },
    )
    assert submit.status_code == 200
    submitted = submit.json()
    assert submitted["status"] == "pending"
    assert submitted["job_id"]
    assert submitted["answer"] == ""

    result = client.post(
        "/worker/result",
        headers={"x-worker-secret": main.WORKER_SECRET},
        json={"job_id": submitted["job_id"], "answer": "## Готовая работа"},
    )
    assert result.status_code == 200

    job = client.get(f"/api/jobs/{submitted['job_id']}", params={"init_data": init_data})
    assert job.status_code == 200
    payload = job.json()
    assert payload["status"] == "done"
    assert payload["answer"] == "## Готовая работа"
    assert len(main._orders) == 1

    second_read = client.get(f"/api/jobs/{submitted['job_id']}", params={"init_data": init_data})
    assert second_read.status_code == 200
    assert len(main._orders) == 1

    payment = client.post(
        "/api/payment_confirm",
        json={"init_data": init_data, "order_id": submitted["order_id"]},
    )
    assert payment.status_code == 200
    assert payment.json()["status"] == "manual_review"
    assert main._orders[0]["paid"] is False
    assert main._orders[0]["status"] == "payment_claimed_manual_review"
