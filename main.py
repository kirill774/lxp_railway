# -*- coding: utf-8 -*-
"""
Railway бэкенд — принимает задания от пользователей,
отдаёт их локальному воркеру, возвращает результаты.
Очередь хранится в памяти (Railway не даёт persistent FS на free tier).
"""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "change-me-please").strip()
FREE_IDS      = {int(x) for x in os.getenv("FREE_USER_IDS", "1016718472").split(",") if x.strip().isdigit()}
PAYMENT_CARD  = os.getenv("PAYMENT_CARD", "0000 0000 0000 0000")
PAYMENT_PHONE = os.getenv("PAYMENT_PHONE", "+7 999 000 00 00")
PAYMENT_NAME  = os.getenv("PAYMENT_NAME", "Кирилл П.")

PRICES = {
    "kt":         {"label": "Контрольная точка (КТ)", "rub": 300},
    "practice":   {"label": "Практическая работа",    "rub": 350},
    "essay":      {"label": "Реферат / эссе",          "rub": 450},
    "project":    {"label": "Проектная работа",        "rub": 600},
    "assignment": {"label": "Задание",                 "rub": 400},
}

SUBJECTS = [
    "SMM для бизнеса", "Контент-маркетинг", "Event-маркетинг",
    "Брендинг. Стратегии", "Английский язык А2", "Фотосъёмка",
    "Adobe Photoshop", "Другой предмет",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("railway")

# ─── In-memory storage ────────────────────────────────────────────────────────

# users: {str(tg_id): {...}}
_users: dict[str, dict] = {}

# queue: {job_id: {status, task, result, ...}}
# status: pending → processing → done | error
_jobs: dict[str, dict] = {}

# orders: list of dicts
_orders: list[dict] = []


def get_user(uid: int) -> dict | None:
    return _users.get(str(uid))

def save_user(uid: int, data: dict) -> None:
    _users[str(uid)] = data

def get_user_orders(uid: int) -> list:
    return [o for o in _orders if o.get("user_id") == uid]

# ─── Helpers ──────────────────────────────────────────────────────────────────

import re, unicodedata

def detect_type(task: str) -> str:
    t = unicodedata.normalize("NFC", task.lower())
    if any(w in t for w in ["проект", "project", "курсов"]): return "project"
    if any(w in t for w in ["практич", "практик", "лабор", "practice"]): return "practice"
    if any(w in t for w in ["реферат", "эссе", "essay"]): return "essay"
    if re.search(r'\bкт\b', t) or any(w in t for w in ["контрольная точка", "mid-term"]): return "kt"
    if re.search(r'\bkt\b', t) or re.search(r'\bтест\b', t): return "kt"
    return "assignment"

def calc_price(task: str, urgent: bool = False) -> tuple[str, int]:
    ptype = detect_type(task)
    price = PRICES[ptype]["rub"]
    if urgent: price = int(price * 1.5)
    return ptype, price

def validate_init_data(init_data: str) -> dict | None:
    try:
        parsed = {}
        for part in init_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                parsed[k] = unquote(v)
        received_hash = parsed.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

def worker_auth(secret: str) -> bool:
    return hmac.compare_digest(secret, WORKER_SECRET)

# ─── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(title="LXP Railway API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── Pydantic ─────────────────────────────────────────────────────────────────

class ProfileIn(BaseModel):
    init_data: str
    name: str
    group: str
    lxp_login: str = ""

class TaskIn(BaseModel):
    init_data: str
    subject: str
    task: str
    urgent: bool = False

class PaymentIn(BaseModel):
    init_data: str
    order_id: str

class WorkerResultIn(BaseModel):
    job_id: str
    answer: str
    error: str = ""

# ─── Public API ───────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    pending = sum(1 for j in _jobs.values() if j["status"] == "pending")
    processing = sum(1 for j in _jobs.values() if j["status"] == "processing")
    return {"ok": True, "version": "1.0", "pending": pending, "processing": processing}

@app.get("/api/subjects")
async def subjects():
    return {"subjects": SUBJECTS, "prices": PRICES}

@app.post("/api/profile")
async def save_profile(body: ProfileIn):
    user = validate_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    uid = user["id"]
    profile = {
        "tg_id": uid, "tg_username": user.get("username", ""),
        "name": body.name, "group": body.group,
        "lxp_login": body.lxp_login,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_user(uid, profile)
    return {"ok": True, "profile": profile}

@app.get("/api/profile")
async def get_profile(init_data: str):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    uid = user["id"]
    return {
        "ok": True,
        "profile": get_user(uid),
        "free": uid in FREE_IDS,
        "orders": get_user_orders(uid),
    }

@app.post("/api/estimate")
async def estimate(body: dict):
    ptype, price = calc_price(body.get("task",""), body.get("urgent", False))
    return {"type": ptype, "label": PRICES[ptype]["label"], "price": price}

@app.post("/api/submit")
async def submit_task(body: TaskIn):
    user = validate_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")

    uid = user["id"]
    profile = get_user(uid) or {"name": user.get("first_name",""), "group": ""}
    free = uid in FREE_IDS
    task = f"{body.subject}\n{body.task}".strip()
    ptype, price = calc_price(task, body.urgent)

    job_id = str(uuid.uuid4())[:16]
    order_id = f"ord_{int(time.time())}_{uid}"

    # Создаём задание в очереди
    _jobs[job_id] = {
        "job_id":    job_id,
        "order_id":  order_id,
        "user_id":   uid,
        "name":      profile["name"],
        "group":     profile.get("group", ""),
        "task":      task,
        "subject":   body.subject,
        "urgent":    body.urgent,
        "status":    "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result":    None,
        "error":     None,
    }

    logger.info("Job %s created for user %s: %s", job_id, uid, task[:60])

    # Ждём результата (polling до 130 сек)
    deadline = time.time() + 130
    while time.time() < deadline:
        job = _jobs.get(job_id, {})
        if job.get("status") == "done":
            answer = job["result"]
            # Сохраняем заказ
            _orders.append({
                "order_id": order_id, "user_id": uid,
                "username": user.get("username",""),
                "name": profile["name"], "group": profile.get("group",""),
                "subject": body.subject, "task": body.task[:200],
                "price": 0 if free else price,
                "urgent": body.urgent, "free": free, "paid": free,
                "status": "delivered",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            payment = None if free else {
                "card": PAYMENT_CARD, "phone": PAYMENT_PHONE,
                "name": PAYMENT_NAME, "amount": price, "order_id": order_id,
            }
            return {
                "ok": True, "answer": answer,
                "order_id": order_id,
                "price": 0 if free else price,
                "free": free, "payment": payment,
            }
        elif job.get("status") == "error":
            raise HTTPException(500, job.get("error", "Worker error"))
        import asyncio
        await asyncio.sleep(1)

    # Таймаут
    _jobs[job_id]["status"] = "error"
    _jobs[job_id]["error"] = "Timeout"
    raise HTTPException(504, "AI не ответил вовремя. Попробуй снова.")

@app.post("/api/payment_confirm")
async def payment_confirm(body: PaymentIn):
    user = validate_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    for o in _orders:
        if o.get("order_id") == body.order_id and o.get("user_id") == user["id"]:
            o["paid"] = True
            return {"ok": True}
    raise HTTPException(404, "Order not found")

@app.get("/api/orders")
async def orders(init_data: str):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    return {"ok": True, "orders": get_user_orders(user["id"])}

# ─── Worker API (защищён WORKER_SECRET) ──────────────────────────────────────

@app.get("/worker/jobs")
async def worker_get_jobs(x_worker_secret: str = Header(...)):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")
    pending = [j for j in _jobs.values() if j["status"] == "pending"]
    # Берём первые 3 задания и помечаем как processing
    result = []
    for job in pending[:3]:
        _jobs[job["job_id"]]["status"] = "processing"
        result.append(job)
    return {"jobs": result}

@app.post("/worker/result")
async def worker_post_result(body: WorkerResultIn, x_worker_secret: str = Header(...)):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")
    job = _jobs.get(body.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if body.error:
        _jobs[body.job_id]["status"] = "error"
        _jobs[body.job_id]["error"]  = body.error
        logger.error("Job %s failed: %s", body.job_id, body.error)
    else:
        _jobs[body.job_id]["status"] = "done"
        _jobs[body.job_id]["result"] = body.answer
        logger.info("Job %s done (%d chars)", body.job_id, len(body.answer))
    return {"ok": True}

@app.get("/worker/status")
async def worker_status(x_worker_secret: str = Header(...)):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")
    return {
        "jobs_total":      len(_jobs),
        "jobs_pending":    sum(1 for j in _jobs.values() if j["status"] == "pending"),
        "jobs_processing": sum(1 for j in _jobs.values() if j["status"] == "processing"),
        "jobs_done":       sum(1 for j in _jobs.values() if j["status"] == "done"),
        "jobs_error":      sum(1 for j in _jobs.values() if j["status"] == "error"),
        "users":           len(_users),
        "orders":          len(_orders),
    }

# ─── Static (Mini App) ────────────────────────────────────────────────────────

_static = Path(__file__).parent / "miniapp"
if _static.exists():
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
else:
    @app.get("/")
    async def root():
        return HTMLResponse("<h1>LXP API</h1><p>Mini App files not found.</p>")
