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
import sqlite3
import time
import uuid
import base64
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import io

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/lxp_bot.sqlite3").strip()
FERNET_KEY = os.getenv("FERNET_KEY", "").strip()
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

# ─── In-memory log buffer (last 200 entries) ──────────────────────────────────
import collections
_log_buffer: collections.deque = collections.deque(maxlen=200)

class _DequeHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append({
            "ts": self.formatter.formatTime(record) if self.formatter else record.asctime,
            "level": record.levelname,
            "msg": record.getMessage(),
        })

_dh = _DequeHandler()
_dh.setFormatter(logging.Formatter("%(asctime)s"))
logging.getLogger("railway").addHandler(_dh)
logging.getLogger("uvicorn.access").addHandler(_dh)

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

WORKER_JOB_TIMEOUT_SECS = 300  # 5 minutes — reset stale processing jobs

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


def _db_path() -> Path:
    path = Path(DATABASE_PATH)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS users (tg_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return conn


def _fernet_key() -> bytes:
    if FERNET_KEY:
        return FERNET_KEY.encode()
    seed = f"{BOT_TOKEN}:{WORKER_SECRET}".encode()
    return base64.urlsafe_b64encode(hashlib.sha256(seed).digest())


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    from cryptography.fernet import Fernet
    return Fernet(_fernet_key()).encrypt(value.encode()).decode()


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(_fernet_key()).decrypt(value.encode()).decode()
    except InvalidToken:
        logger.warning("Stored encrypted secret cannot be decrypted")
        return ""


def _serialize_user(data: dict) -> dict:
    return _serialize_secret_fields(data)


def _deserialize_user(data: dict) -> dict:
    return _deserialize_secret_fields(data)


def _serialize_secret_fields(data: dict) -> dict:
    stored = dict(data)
    password = stored.pop("lxp_password", "")
    if password:
        stored["lxp_password_enc"] = _encrypt_secret(password)
    return stored


def _deserialize_secret_fields(data: dict) -> dict:
    restored = dict(data)
    if restored.get("lxp_password_enc"):
        restored["lxp_password"] = _decrypt_secret(restored.pop("lxp_password_enc"))
    return restored


def public_profile(profile: dict | None) -> dict | None:
    if not profile:
        return profile
    public = dict(profile)
    public.pop("lxp_password", None)
    public.pop("lxp_password_enc", None)
    return public


def persist_user(uid: int, data: dict) -> None:
    with _db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (tg_id, data) VALUES (?, ?)",
            (str(uid), json.dumps(_serialize_user(data), ensure_ascii=False)),
        )


def persist_job(job: dict) -> None:
    with _db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, data) VALUES (?, ?)",
            (job["job_id"], json.dumps(_serialize_secret_fields(job), ensure_ascii=False)),
        )


def persist_order(order: dict) -> None:
    with _db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders (order_id, data) VALUES (?, ?)",
            (order["order_id"], json.dumps(order, ensure_ascii=False)),
        )


def load_state() -> None:
    try:
        with _db_conn() as conn:
            _users.update({tg_id: _deserialize_user(json.loads(data)) for tg_id, data in conn.execute("SELECT tg_id, data FROM users")})
            _jobs.update({job_id: _deserialize_secret_fields(json.loads(data)) for job_id, data in conn.execute("SELECT job_id, data FROM jobs")})
            _orders.extend(json.loads(data) for (data,) in conn.execute("SELECT data FROM orders"))
    except Exception as exc:
        logger.warning("SQLite state load failed: %s", exc)


def get_user(uid: int) -> dict | None:
    return _users.get(str(uid))

def save_user(uid: int, data: dict) -> None:
    _users[str(uid)] = data
    persist_user(uid, data)

def get_user_orders(uid: int) -> list:
    return [o for o in _orders if o.get("user_id") == uid]


load_state()

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
        "use_lxp": job.get("use_lxp", False),
    }

def append_order_once(job: dict, user: dict) -> None:
    if any(o.get("order_id") == job["order_id"] for o in _orders):
        return
    free = job["user_id"] in FREE_IDS
    order = {
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
    }
    _orders.append(order)
    persist_order(order)

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
            persist_order(order)
    except Exception as exc:
        order["notion_status"] = "error"
        order["notion_error"] = str(exc)[:300]
        persist_order(order)
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

# ─── File Generation ──────────────────────────────────────────────────────────

def _md_to_paragraphs(text: str) -> list[dict]:
    """Parse markdown text into list of {type, text, level} dicts."""
    result = []
    for line in text.split('\n'):
        line = line.rstrip()
        if not line:
            continue
        m = re.match(r'^(#{1,3})\s+(.*)', line)
        if m:
            result.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2)})
        elif line.startswith('- ') or line.startswith('• '):
            result.append({"type": "bullet", "text": line[2:]})
        elif re.match(r'^\d+\.\s', line):
            result.append({"type": "numbered", "text": re.sub(r'^\d+\.\s', '', line)})
        else:
            result.append({"type": "paragraph", "text": line})
    return result

def generate_docx(title: str, subject: str, text: str, author: str = "", tone: str = "formal") -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.2)
        section.right_margin = Inches(1.2)

    t = doc.add_heading(title, level=0)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if t.runs:
        t.runs[0].font.color.rgb = RGBColor(0x1a, 0x1a, 0x2e)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(f"Предмет: {subject}")
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    if author:
        run2 = meta.add_run(f"  |  Автор: {author}")
        run2.font.size = Pt(10)
        run2.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    doc.add_paragraph()

    paragraphs = _md_to_paragraphs(text)
    for p in paragraphs:
        if p["type"] == "heading":
            doc.add_heading(p["text"], level=p["level"])
        elif p["type"] == "bullet":
            doc.add_paragraph(p["text"], style='List Bullet')
        elif p["type"] == "numbered":
            doc.add_paragraph(p["text"], style='List Number')
        else:
            para = doc.add_paragraph(p["text"])
            para.paragraph_format.space_after = Pt(6)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def generate_txt(title: str, subject: str, text: str, author: str = "") -> bytes:
    lines = [title, "=" * len(title), f"Предмет: {subject}"]
    if author:
        lines.append(f"Автор: {author}")
    lines.extend(["", text])
    return "\n".join(lines).encode("utf-8")


def generate_pptx(title: str, subject: str, text: str, author: str = "") -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN

    prs = Presentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)

    DARK = RGBColor(0x1a, 0x1a, 0x2e)
    ACCENT = RGBColor(0x4f, 0x8a, 0xff)

    def add_slide(layout_idx=6):
        layout = prs.slide_layouts[layout_idx] if layout_idx < len(prs.slide_layouts) else prs.slide_layouts[6]
        return prs.slides.add_slide(layout)

    def add_textbox(slide, text, left, top, width, height, size=18, bold=False, color=None, align=PP_ALIGN.LEFT):
        txBox = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
        tf = txBox.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = align
        run = p.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        if color:
            run.font.color.rgb = color
        return txBox

    slide0 = add_slide(0) if len(prs.slide_layouts) > 0 else add_slide(6)
    try:
        slide0.shapes.title.text = title
        slide0.placeholders[1].text = f"{subject}" + (f"\n{author}" if author else "")
    except Exception:
        add_textbox(slide0, title, 1, 2.5, 11, 1.5, size=36, bold=True, color=DARK, align=PP_ALIGN.CENTER)
        add_textbox(slide0, subject, 1, 4.2, 11, 0.8, size=20, color=ACCENT, align=PP_ALIGN.CENTER)

    paragraphs = _md_to_paragraphs(text)
    chunks = []
    current_chunk = []
    current_heading = title

    for p in paragraphs:
        if p["type"] == "heading" and p["level"] <= 2:
            if current_chunk:
                chunks.append((current_heading, current_chunk))
            current_heading = p["text"]
            current_chunk = []
        else:
            current_chunk.append(p)
            if len(current_chunk) >= 6:
                chunks.append((current_heading, current_chunk))
                current_chunk = []
                current_heading = current_heading + " (продолжение)"

    if current_chunk:
        chunks.append((current_heading, current_chunk))

    for slide_title, items in chunks:
        slide = add_slide(6)
        add_textbox(slide, slide_title, 0.5, 0.3, 12, 1, size=28, bold=True, color=DARK)
        y = 1.5
        for item in items:
            prefix = "• " if item["type"] == "bullet" else ""
            add_textbox(slide, prefix + item["text"], 0.7, y, 11.5, 0.6, size=16, color=DARK)
            y += 0.65
            if y > 6.5:
                break

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def generate_xlsx(title: str, subject: str, text: str, author: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = subject[:31]

    HEADER_FILL = PatternFill("solid", fgColor="1a1a2e")
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=12)
    TITLE_FONT  = Font(bold=True, size=14, color="1a1a2e")
    ACCENT_FILL = PatternFill("solid", fgColor="e8f0fe")
    thin = Side(style="thin", color="cccccc")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 80

    ws.merge_cells('A1:B1')
    ws['A1'] = title
    ws['A1'].font = TITLE_FONT
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 30

    ws['A2'] = 'Предмет'
    ws['B2'] = subject
    ws['A2'].font = Font(bold=True, color="4f8aff")
    if author:
        ws['A3'] = 'Автор'
        ws['B3'] = author
        ws['A3'].font = Font(bold=True, color="4f8aff")

    row = 4
    ws.cell(row=row, column=1, value='Тип').font = HEADER_FONT
    ws.cell(row=row, column=1).fill = HEADER_FILL
    ws.cell(row=row, column=2, value='Содержание').font = HEADER_FONT
    ws.cell(row=row, column=2).fill = HEADER_FILL
    ws.row_dimensions[row].height = 22

    paragraphs = _md_to_paragraphs(text)
    for i, p in enumerate(paragraphs):
        row += 1
        type_label = {"heading": "Заголовок", "bullet": "• Пункт", "numbered": "№ Пункт", "paragraph": "Текст"}.get(p["type"], "")
        c1 = ws.cell(row=row, column=1, value=type_label)
        c2 = ws.cell(row=row, column=2, value=p["text"])
        c1.border = border
        c2.border = border
        c2.alignment = Alignment(wrap_text=True)
        if p["type"] == "heading":
            c1.font = Font(bold=True, color="4f8aff")
            c2.font = Font(bold=True)
            c2.fill = ACCENT_FILL
        if i % 2 == 0 and p["type"] != "heading":
            c2.fill = PatternFill("solid", fgColor="f8f9ff")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


FORMAT_GENERATORS = {
    "docx": (generate_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "pptx": (generate_pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "xlsx": (generate_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "txt":  (generate_txt,  "text/plain; charset=utf-8", ".txt"),
}

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
    lxp_login: str = ""
    lxp_password: str = ""

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
        "lxp_login": body.lxp_login,
        "lxp_password": body.lxp_password,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_user(uid, profile)
    return {"ok": True, "profile": public_profile(profile)}

@app.get("/api/profile")
async def get_profile(init_data: str):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    uid = user["id"]
    return {
        "ok": True,
        "profile": public_profile(get_user(uid)),
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
        "use_lxp": body.use_lxp,
        "lxp_login": profile.get("lxp_login", "") if body.use_lxp else "",
        "lxp_password": profile.get("lxp_password", "") if body.use_lxp else "",
    }
    persist_job(_jobs[job_id])

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
            persist_order(o)
            return {"ok": True, "status": "manual_review"}
    raise HTTPException(404, "Order not found")

@app.get("/api/orders")
async def orders(init_data: str):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    return {"ok": True, "orders": get_user_orders(user["id"])}

@app.get("/api/jobs/{job_id}/download")
async def download_job_file(job_id: str, init_data: str, fmt: str = ""):
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid init_data")
    job = _jobs.get(job_id)
    if not job or job.get("user_id") != user["id"]:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done" or not job.get("result"):
        raise HTTPException(400, "Job not ready")

    output_fmt = fmt or job.get("output_format", "docx")
    if output_fmt not in FORMAT_GENERATORS:
        output_fmt = "docx"

    profile = get_user(user["id"]) or {}
    gen_fn, mime, ext = FORMAT_GENERATORS[output_fmt]

    title = job.get("subject", "Задание")
    author = profile.get("name", "")
    tone = job.get("tone", "formal")

    try:
        if output_fmt == "docx":
            file_bytes = gen_fn(title, job["subject"], job["result"], author, tone)
        else:
            file_bytes = gen_fn(title, job["subject"], job["result"], author)
    except Exception as exc:
        logger.error("File generation failed for job %s fmt %s: %s", job_id, output_fmt, exc)
        raise HTTPException(500, f"Ошибка генерации файла: {exc}")

    safe_title = re.sub(r'[^\w\s-]', '', title)[:40].strip().replace(' ', '_')
    filename = f"{safe_title}{ext}"

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ─── Worker API (защищён WORKER_SECRET) ──────────────────────────────────────

@app.get("/worker/jobs")
async def worker_get_jobs(x_worker_secret: str = Header(...)):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")

    now = time.time()
    for job in list(_jobs.values()):
        if job["status"] != "processing":
            continue
        started_at = job.get("processing_started_at", 0)
        if started_at and now - float(started_at) > WORKER_JOB_TIMEOUT_SECS:
            job["status"] = "pending"
            job.pop("processing_started_at", None)
            persist_job(job)
            logger.warning("Job %s reset from stale processing", job["job_id"])

    pending = [j for j in _jobs.values() if j["status"] == "pending"]
    # Берём первые 3 задания и помечаем как processing
    result = []
    for job in pending[:3]:
        _jobs[job["job_id"]]["status"] = "processing"
        _jobs[job["job_id"]]["processing_started_at"] = time.time()
        persist_job(_jobs[job["job_id"]])
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
        persist_job(_jobs[body.job_id])
        logger.error("Job %s failed: %s", body.job_id, body.error)
    else:
        _jobs[body.job_id]["status"] = "done"
        _jobs[body.job_id]["result"] = body.answer
        _jobs[body.job_id]["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        persist_job(_jobs[body.job_id])
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

@app.get("/worker/logs")
async def worker_logs(x_worker_secret: str = Header(...), n: int = 50):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")
    entries = list(_log_buffer)[-n:]
    return {"ok": True, "count": len(entries), "logs": entries}

@app.get("/worker/queue")
async def worker_queue(x_worker_secret: str = Header(...)):
    if not worker_auth(x_worker_secret):
        raise HTTPException(403, "Invalid worker secret")
    return {
        "ok": True,
        "pending": [
            {"job_id": job["job_id"], "subject": job.get("subject"), "created_at": job.get("created_at")}
            for job in _jobs.values()
            if job["status"] == "pending"
        ],
        "processing": [
            {
                "job_id": job["job_id"],
                "subject": job.get("subject"),
                "started_at": job.get("processing_started_at"),
                "use_lxp": job.get("use_lxp"),
            }
            for job in _jobs.values()
            if job["status"] == "processing"
        ],
        "done": sum(1 for job in _jobs.values() if job["status"] == "done"),
        "error": sum(1 for job in _jobs.values() if job["status"] == "error"),
    }

# ─── Static (Mini App) ────────────────────────────────────────────────────────

_static = Path(__file__).parent / "miniapp"
if _static.exists():
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
else:
    @app.get("/")
    async def root():
        return HTMLResponse("<h1>LXP API</h1><p>Mini App files not found.</p>")
