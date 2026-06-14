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


def test_profile_saves_academic_level_and_tone():
    main._jobs.clear()
    main._users.clear()
    client = TestClient(main.app)
    user = {"id": 333, "first_name": "Анна", "username": "anna333"}
    init_data = make_init_data(user)

    response = client.post(
        "/api/profile",
        json={
            "init_data": init_data,
            "name": "Анна Смирнова",
            "group": "3БМ1.24",
            "academic_level": "master",
            "tone": "casual",
            "preferred_format": "pptx",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    profile = payload["profile"]
    assert profile["academic_level"] == "master"
    assert profile["tone"] == "casual"
    assert profile["preferred_format"] == "pptx"


def test_submit_job_contains_output_format_and_tone():
    main._jobs.clear()
    main._users.clear()
    client = TestClient(main.app)
    user = {"id": 444, "first_name": "Test", "username": "test444"}
    init_data = make_init_data(user)

    client.post(
        "/api/profile",
        json={
            "init_data": init_data,
            "name": "Test User",
            "group": "1А1.24",
            "academic_level": "college",
            "tone": "neutral",
            "preferred_format": "docx",
        },
    )

    submit = client.post(
        "/api/submit",
        json={
            "init_data": init_data,
            "subject": "SMM",
            "task": "Тестовое задание",
            "urgent": False,
            "output_format": "txt",
        },
    )
    assert submit.status_code == 200
    submitted = submit.json()
    assert submitted["output_format"] == "txt"

    job_id = submitted["job_id"]
    job_entry = main._jobs[job_id]
    assert job_entry["academic_level"] == "college"
    assert job_entry["tone"] == "neutral"
    assert job_entry["output_format"] == "txt"


def test_worker_gets_job_with_metadata():
    main._jobs.clear()
    main._users.clear()
    client = TestClient(main.app)
    user = {"id": 555, "first_name": "Worker", "username": "worker555"}
    init_data = make_init_data(user)

    client.post(
        "/api/submit",
        json={
            "init_data": init_data,
            "subject": "SMM",
            "task": "Задание для воркера",
            "urgent": False,
        },
    )

    response = client.get(
        "/worker/jobs",
        headers={"x-worker-secret": main.WORKER_SECRET},
    )
    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert len(jobs) > 0
    job = jobs[0]
    assert "output_format" in job
    assert "academic_level" in job
    assert "tone" in job
