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
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()
FREE_IDS      = {int(x) for x in os.getenv("FREE_USER_IDS", "1016718472").split(",") if x.strip().isdigit()}
PAYMENT_CARD  = os.getenv("PAYMENT_CARD", "").strip()
PAYMENT_PHONE = os.getenv("PAYMENT_PHONE", "").strip()
PAYMENT_NAME  = os.getenv("PAYMENT_NAME", "").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
NOTION_TITLE_PROPERTY = os.getenv("NOTION_TITLE_PROPERTY", "").strip()
GITHUB_REPO_URL = os.getenv("GITHUB_REPO_URL", "https://github.com/kirill774/lxp_railway").strip()
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("railway")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not WORKER_SECRET or WORKER_SECRET == "change-me-please":
    raise RuntimeError("WORKER_SECRET must be set to a strong secret")
if not all([PAYMENT_CARD, PAYMENT_PHONE, PAYMENT_NAME]):
    logger.warning("Payment contact details are incomplete; paid orders will show manual-check instructions only")

PRICES = {
    "kt":         {"label": "Контрольная точка (КТ)", "rub": 500,  "stars": 390},
    "practice":   {"label": "Практическая работа",    "rub": 700,  "stars": 540},
    "essay":      {"label": "Реферат / эссе",          "rub": 900,  "stars": 695},
    "project":    {"label": "Проектная работа",        "rub": 1500, "stars": 1155},
    "assignment": {"label": "Задание",                 "rub": 600,  "stars": 465},
}

SUBJECTS = [
    "SMM для бизнеса", "Контент-маркетинг", "Event-маркетинг",
    "Брендинг. Стратегии", "Английский язык А2", "Фотосъёмка",
    "Adobe Photoshop", "Другой предмет",
]

# ─── In-memory storage ────────────────────────────────────────────────────────

# users: {str(tg_id): {...}}
_users: dict[str, dict] = {}

# queue: {job_id: {status, task, result, ...}}
# status: pending → processing → done | error
_jobs: dict[str, dict] = {}

# orders: list of dicts
_orders: list[dict] = []
_notion_title_property_cache: str | None = None
_notion_properties_cache: dict[str, dict] | None = None


def get_user(uid: int) -> dict | None:
    return _users.get(str(uid))

def save_user(uid: int, data: dict) -> None:
    _users[str(uid)] = data

def get_user_orders(uid: int) -> list:
    return [o for o in _orders if o.get("user_id") == uid]

def build_job_response(job: dict, user: dict) -> dict:
    free = job["user_id"] in FREE_IDS
    price = 0 if free else job["price"]
    stars = 0 if free else job["stars"]
    payment = None if free else {
        "card": PAYMENT_CARD,
        "phone": PAYMENT_PHONE,
        "name": PAYMENT_NAME,
        "amount": price,
        "stars": stars,
        "order_id": job["order_id"],
        "status": "manual_review",
    }
    return {
        "ok": True,
        "job_id": job["job_id"],
        "order_id": job["order_id"],
        "status": job["status"],
        "answer": job.get("result") or "",
        "error": job.get("error") or "",
        "price": price,
        "stars": stars,
        "free": free,
        "payment": payment,
        "created_at": job["created_at"],
        "completed_at": job.get("completed_at"),
        "output_format": job.get("output_format", "docx"),
    }

def append_order_once(job: dict, user: dict) -> None:
    if any(o.get("order_id") == job["order_id"] for o in _orders):
        return
    free = job["user_id"] in FREE_IDS
    _orders.append({
        "order_id": job["order_id"],
        "user_id": job["user_id"],
        "username": user.get("username", ""),
        "name": job["name"],
        "group": job.get("group", ""),
        "subject": job["subject"],
        "task": job["task"][:200],
        "price": 0 if free else job["price"],
        "stars": 0 if free else job["stars"],
        "urgent": job["urgent"],
        "free": free,
        "paid": free,
        "status": "delivered" if free else "awaiting_manual_payment_review",
        "created_at": job["created_at"],
        "completed_at": job.get("completed_at"),
        "academic_level": job.get("academic_level", "bachelor"),
        "tone": job.get("tone", "formal"),
        "preferred_format": job.get("output_format", "docx"),
        "notion_status": "pending" if notion_enabled() else "disabled",
    })

def notion_enabled() -> bool:
    return bool(NOTION_TOKEN and NOTION_DATABASE_ID)

def notion_order_summary(order: dict) -> str:
    return "\n".join([
        f"Order ID: {order.get('order_id', '')}",
        f"User ID: {order.get('user_id', '')}",
        f"Username: @{order.get('username', '')}" if order.get("username") else "Username: ",
        f"Name: {order.get('name', '')}",
        f"Group: {order.get('group', '')}",
        f"Subject: {order.get('subject', '')}",
        f"Status: {order.get('status', '')}",
        f"Price: {order.get('price', 0)} RUB / {order.get('stars', 0)} Stars",
        f"Urgent: {order.get('urgent', False)}",
        f"Free: {order.get('free', False)}",
        f"Created: {order.get('created_at', '')}",
        f"Completed: {order.get('completed_at', '')}",
        f"Academic Level: {order.get('academic_level', '')}",
        f"Tone: {order.get('tone', '')}",
        f"Preferred Format: {order.get('preferred_format', '')}",
        "",
        "Task:",
        order.get("task", ""),
    ])

async def sync_order_to_notion(order: dict) -> None:
    if not notion_enabled() or order.get("notion_status") == "synced":
        return

    title = f"{order.get('subject', 'Order')} - {order.get('order_id', '')}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            properties_meta = await get_notion_database_properties(client, headers)
            title_property = get_notion_title_property(properties_meta)
            payload = {
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": build_notion_order_properties(order, title_property, properties_meta, title),
                "children": [{
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": notion_order_summary(order)[:2000]}}]
                    },
                }],
            }
            response = await client.post("https://api.notion.com/v1/pages", headers=headers, json=payload)
            response.raise_for_status()
            order["notion_status"] = "synced"
            order["notion_page_id"] = response.json().get("id", "")
    except Exception as exc:
        order["notion_status"] = "error"
        order["notion_error"] = str(exc)[:300]
        logger.warning("Notion sync failed for %s: %s", order.get("order_id"), exc)

async def get_notion_database_properties(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, dict]:
    global _notion_properties_cache
    if _notion_properties_cache is not None:
        return _notion_properties_cache

    response = await client.get(f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}", headers=headers)
    response.raise_for_status()
    _notion_properties_cache = response.json().get("properties", {})
    return _notion_properties_cache

def get_notion_title_property(properties: dict[str, dict]) -> str:
    global _notion_title_property_cache
    if NOTION_TITLE_PROPERTY:
        return NOTION_TITLE_PROPERTY
    if _notion_title_property_cache:
        return _notion_title_property_cache

    for name, prop in properties.items():
        if prop.get("type") == "title":
            _notion_title_property_cache = name
            return name
    return "Name"

def build_notion_order_properties(order: dict, title_property: str, properties_meta: dict[str, dict], title: str) -> dict:
    payment_status = "free" if order.get("free") else "unpaid"
    if order.get("paid"):
        payment_status = "paid"
    elif order.get("status") == "payment_claimed_manual_review":
        payment_status = "manual_review"

    values = {
        title_property: ("title", title),
        "Order ID": ("rich_text", order.get("order_id", "")),
        "User ID": ("number", order.get("user_id")),
        "Username": ("rich_text", order.get("username", "")),
        "Student Name": ("rich_text", order.get("name", "")),
        "Group": ("rich_text", order.get("group", "")),
        "Subject": ("rich_text", order.get("subject", "")),
        "Bot Status": ("select", order.get("status", "")),
        "Payment Status": ("select", payment_status),
        "Price RUB": ("number", order.get("price", 0)),
        "Stars": ("number", order.get("stars", 0)),
        "Urgent": ("checkbox", bool(order.get("urgent"))),
        "Free": ("checkbox", bool(order.get("free"))),
        "Created": ("date", order.get("created_at", "")),
        "Completed": ("date", order.get("completed_at", "")),
        "GitHub": ("url", GITHUB_REPO_URL),
        "Notes": ("rich_text", notion_order_summary(order)),
        "Academic Level": ("select", order.get("academic_level", "")),
        "Tone": ("select", order.get("tone", "")),
        "Preferred Format": ("select", order.get("preferred_format", "")),
    }

    notion_properties = {}
    for name, (expected_type, value) in values.items():
        if properties_meta.get(name, {}).get("type") != expected_type:
            continue
        if expected_type == "title":
            notion_properties[name] = {"title": [{"text": {"content": str(value)[:2000]}}]}
        elif expected_type == "rich_text":
            notion_properties[name] = {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
        elif expected_type == "select" and value:
            notion_properties[name] = {"select": {"name": str(value)}}
        elif expected_type == "number" and value is not None:
            notion_properties[name] = {"number": value}
        elif expected_type == "checkbox":
            notion_properties[name] = {"checkbox": bool(value)}
        elif expected_type == "date" and value:
            notion_properties[name] = {"date": {"start": str(value)}}
        elif expected_type == "url" and value:
            notion_properties[name] = {"url": str(value)}
    return notion_properties

async def sync_completed_job_order(job: dict, user: dict) -> None:
    append_order_once(job, user)
    order = next((o for o in _orders if o.get("order_id") == job["order_id"]), None)
    if order:
        await sync_order_to_notion(order)

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

def calc_price(task: str, urgent: bool = False) -> tuple[str, int, int]:
    ptype = detect_type(task)
    rub   = PRICES[ptype]["rub"]
    stars = PRICES[ptype]["stars"]
    if urgent:
        rub   = int(rub   * 1.5)
        stars = int(stars * 1.5)
    return ptype, rub, stars

def validate_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = parsed.pop("hash", "")
        auth_date = int(parsed.get("auth_date", "0") or "0")
        if not received_hash or not auth_date or time.time() - auth_date > 86400:
            return None
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

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s", request.url, str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "detail": "Внутренняя ошибка сервера. Мы уже разбираемся!"}
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.warning("HTTP %d on %s: %s", exc.status_code, request.url, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "detail": exc.detail}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Worker-Secret"],
)

# ─── Pydantic ─────────────────────────────────────────────────────────────────

class ProfileIn(BaseModel):
    init_data: str
    name: str
    group: str
    course: str = ""
    default_format: str = "docx"
    contact: str = ""
    notes: str = ""
    academic_level: str = "bachelor"
    tone: str = "formal"
    preferred_format: str = "docx"

class TaskIn(BaseModel):
    init_data: str
    subject: str
    task: str
    urgent: bool = False
    work_type: str = "auto"
    deadline: str = ""
    target_grade: str = ""
    output_format: str = "docx"
    requirements: str = ""
    materials: str = ""
    use_lxp: bool = False
    lxp_login: str = ""
    lxp_password: str = ""

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
    return {
        "ok": True, 
        "version": "1.1", 
        "pending": pending, 
        "processing": processing,
        "status": "healthy" if BOT_TOKEN else "unconfigured",
        "integrations": {
            "notion": "configured" if notion_enabled() else "missing_env",
        },
    }

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
        "tg_id": uid,
        "tg_username": user.get("username", ""),
        "name": body.name,
        "group": body.group,
        "course": body.course,
        "default_format": body.default_format,
        "contact": body.contact,
        "notes": body.notes,
        "academic_level": body.academic_level,
        "tone": body.tone,
        "preferred_format": body.preferred_format,
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
    ptype, price, stars = calc_price(body.get("task",""), body.get("urgent", False))
    return {"type": ptype, "label": PRICES[ptype]["label"], "price": price, "stars": stars}

@app.post("/api/submit")
async def submit_task(body: TaskIn):
    user = validate_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")

    uid = user["id"]
    profile = get_user(uid) or {"name": user.get("first_name",""), "group": ""}
    free = uid in FREE_IDS
    task = f"{body.subject}\n{body.task}".strip()
    ptype, price, stars = calc_price(task, body.urgent)

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
        "price":     price,
        "stars":     stars,
        "status":    "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "result":    None,
        "error":     None,
        "output_format": body.output_format,
        "academic_level": profile.get("academic_level", "bachelor"),
        "tone": profile.get("tone", "formal"),
    }

    logger.info("Job %s created for user %s: %s", job_id, uid, task[:60])
    return build_job_response(_jobs[job_id], user)

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, init_data: str):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    job = _jobs.get(job_id)
    if not job or job.get("user_id") != user["id"]:
        raise HTTPException(404, "Job not found")
    if job["status"] == "done":
        await sync_completed_job_order(job, user)
    return build_job_response(job, user)

@app.post("/api/payment_confirm")
async def payment_confirm(body: PaymentIn):
    user = validate_init_data(body.init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    for o in _orders:
        if o.get("order_id") == body.order_id and o.get("user_id") == user["id"]:
            if o.get("free"):
                return {"ok": True, "status": "free"}
            o["payment_claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            o["status"] = "payment_claimed_manual_review"
            return {"ok": True, "status": "manual_review"}
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
        _jobs[body.job_id]["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
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
