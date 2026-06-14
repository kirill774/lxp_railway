# -*- coding: utf-8 -*-
"""
Локальный воркер — тянет задания с Railway, при необходимости
парсит ТЗ с LXP (newlxp.ru), генерирует ответ через AI, постит результат.

Запуск:
    python worker.py

Переменные окружения (.env.local или системные):
    RAILWAY_URL      — URL Railway backend (напр. https://your-app.railway.app)
    WORKER_SECRET    — секрет для авторизации воркера
    OPENAI_API_KEY   — ключ OpenAI (или OPENAI_BASE_URL для локальной LLM)
    OPENAI_MODEL     — модель (default: gpt-4o-mini)
    OPENAI_BASE_URL  — (опционально) базовый URL для локальной LLM
    POLL_INTERVAL    — интервал опроса в секундах (default: 5)
    LXP_HEADLESS     — true/false показывать браузер (default: true)
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx

# ─── Загрузка .env.local ──────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env.local"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()

# ─── Config ───────────────────────────────────────────────────────────────────
RAILWAY_URL    = os.getenv("RAILWAY_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_SECRET  = os.getenv("WORKER_SECRET", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", "5"))
LXP_HEADLESS   = os.getenv("LXP_HEADLESS", "true").lower() != "false"
LXP_URL        = "https://newlxp.ru"

_processing: set[str] = set()
_processing_lock = asyncio.Lock()
_job_started_at: dict[str, float] = {}
_lxp_sem = asyncio.Semaphore(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("worker")

if not WORKER_SECRET:
    logger.error("WORKER_SECRET not set")
    sys.exit(1)
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set — AI generation will fail")

# ─── LXP Parser ───────────────────────────────────────────────────────────────

async def lxp_find_task(login: str, password: str, subject: str, task_name: str) -> str:
    """
    Логинится на newlxp.ru, ищет задание по предмету и названию,
    возвращает текст ТЗ.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed: pip install playwright && playwright install chromium")
        return ""

    logger.info("LXP: searching for '%s' / '%s'", subject, task_name)
    tz_text = ""

    async with _lxp_sem:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=LXP_HEADLESS)
            context = await browser.new_context(
                locale="ru-RU",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                storage_state=None,
            )
            page = await context.new_page()

            try:
                # 1. Логин
                await page.goto(f"{LXP_URL}/sign-in", wait_until="networkidle", timeout=30000)
                await page.fill("input[type='email'], input[type='text']", login)
                await page.fill("input[type='password']", password)
                await page.click("button[type='submit'], button:has-text('Войти'), button:has-text('войти')")
                await page.wait_for_url(f"{LXP_URL}/dashboard", timeout=15000)
                logger.info("LXP: logged in successfully")

                # 2. Переходим к заданиям
                await page.goto(f"{LXP_URL}/tasks", wait_until="networkidle", timeout=20000)
                await asyncio.sleep(2)

                # 3. Ищем задание по названию предмета и задания
                # Пробуем найти ссылку содержащую название предмета или задания
                task_link = None

                # Сначала пробуем точное совпадение по тексту задания
                for selector in [
                    f"a:has-text('{task_name[:30]}')",
                    f"a:has-text('{subject[:20]}')",
                    "[class*='task'] a",
                    "[class*='assignment'] a",
                ]:
                    try:
                        elements = await page.query_selector_all(selector)
                        if elements:
                            # Берём первый подходящий
                            for el in elements:
                                text = (await el.text_content() or "").strip()
                                if (task_name[:15].lower() in text.lower() or
                                    subject[:10].lower() in text.lower()):
                                    task_link = el
                                    break
                            if task_link:
                                break
                    except Exception:
                        continue

                # Если не нашли — берём первый элемент с заданием
                if not task_link:
                    logger.warning("LXP: exact match not found, trying first available task link")
                    try:
                        task_link = await page.query_selector("[class*='task'] a, [class*='assignment'] a, main a[href*='task']")
                    except Exception:
                        pass

                if task_link:
                    await task_link.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(1)

                    # 4. Парсим содержимое ТЗ
                    tz_text = await page.evaluate("""
                        () => {
                            // Убираем навигацию, хедер, футер
                            const skip = ['header','footer','nav','[class*="sidebar"]','[class*="cookie"]'];
                            skip.forEach(s => document.querySelectorAll(s).forEach(el => el.remove()));
                            const main = document.querySelector('main') || document.body;
                            return main.innerText.slice(0, 4000);
                        }
                    """)
                    logger.info("LXP: parsed task text (%d chars)", len(tz_text))
                else:
                    logger.warning("LXP: no task link found on page")

            except Exception as e:
                logger.error("LXP error: %s", e)
            finally:
                await browser.close()

    return tz_text.strip()

# ─── AI Generation ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — опытный студент, пишущий учебные работы.
Пиши естественно, как живой человек. Избегай:
- Шаблонных фраз ("В данной работе рассматривается...")
- Формальных клише ("Следует отметить, что...")
- Повторения задания дословно
- Слишком правильной структуры (не каждый абзац с заголовком)

Пиши конкретно, с примерами, иногда чуть неформально. Длина — ровно столько, сколько нужно."""

def build_prompt(job: dict, lxp_tz: str = "") -> str:
    subject = job.get("subject", "")
    task = job.get("task", "")
    tone = job.get("tone", "formal")
    level = job.get("academic_level", "bachelor")
    fmt = job.get("output_format", "docx")

    tone_map = {"formal": "академический", "neutral": "нейтральный", "casual": "разговорный"}
    level_map = {"college": "колледж/СПО", "bachelor": "бакалавриат", "master": "магистратура"}

    parts = [
        f"Предмет: {subject}",
        f"Уровень: {level_map.get(level, level)}",
        f"Тональность: {tone_map.get(tone, tone)}",
        f"Формат вывода: {fmt}",
        "",
    ]

    if lxp_tz:
        parts += [
            "=== ПОЛНОЕ ТЗ С LXP ===",
            lxp_tz,
            "=== КОНЕЦ ТЗ ===",
            "",
            f"Задание от студента: {task}",
        ]
    else:
        parts += [f"Задание: {task}"]

    return "\n".join(parts)

async def generate_answer(job: dict, lxp_tz: str = "") -> str:
    prompt = build_prompt(job, lxp_tz)
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 3000,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

# ─── Worker Loop ──────────────────────────────────────────────────────────────

async def post_result(job_id: str, answer: str = "", error: str = "") -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{RAILWAY_URL}/worker/result",
            headers={"x-worker-secret": WORKER_SECRET},
            json={"job_id": job_id, "answer": answer, "error": error},
        )

async def process_job(job: dict) -> None:
    job_id = job["job_id"]
    async with _processing_lock:
        if job_id in _processing:
            logger.warning("Job %s already being processed, skipping", job_id)
            return
        _processing.add(job_id)
        _job_started_at[job_id] = time.time()

    logger.info("Processing job %s | subject=%s | use_lxp=%s",
                job_id, job.get("subject"), job.get("use_lxp"))
    try:
        lxp_tz = ""
        if job.get("use_lxp") and job.get("lxp_login") and job.get("lxp_password"):
            lxp_tz = await lxp_find_task(
                login=job["lxp_login"],
                password=job["lxp_password"],
                subject=job.get("subject", ""),
                task_name=job.get("task", ""),
            )
            if lxp_tz:
                logger.info("Job %s: LXP ТЗ получено (%d chars)", job_id, len(lxp_tz))
            else:
                logger.warning("Job %s: LXP ТЗ не найдено, продолжаем без него", job_id)

        answer = await generate_answer(job, lxp_tz)
        logger.info("Job %s: answer generated (%d chars)", job_id, len(answer))
        await post_result(job_id, answer=answer)

    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e)
        await post_result(job_id, error=str(e))
    finally:
        async with _processing_lock:
            _processing.discard(job_id)
            _job_started_at.pop(job_id, None)

async def poll_loop() -> None:
    logger.info("Worker started. Railway: %s | Poll: %ds", RAILWAY_URL, POLL_INTERVAL)
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(
                    f"{RAILWAY_URL}/worker/jobs",
                    headers={"x-worker-secret": WORKER_SECRET},
                )
                resp.raise_for_status()
                jobs = resp.json().get("jobs", [])
                if jobs:
                    logger.info("Got %d job(s)", len(jobs))
                    lxp_jobs = [job for job in jobs if job.get("use_lxp")]
                    fast_jobs = [job for job in jobs if not job.get("use_lxp")]
                    if fast_jobs:
                        await asyncio.gather(*[process_job(job) for job in fast_jobs])
                    for job in lxp_jobs:
                        await process_job(job)
                else:
                    logger.debug("No pending jobs")
            except Exception as e:
                logger.warning("Poll error: %s", e)

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(poll_loop())
