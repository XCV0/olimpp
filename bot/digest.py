"""Сводки: утренняя руководителю и /brief по объекту. Собираются кодом, без LLM."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from . import db, notify
from .config import config
from .formatting import esc, record_line

log = logging.getLogger(__name__)

QUESTION_STALE_HOURS = 4
QUIET_DAYS = 3


async def build_digest() -> str:
    now = datetime.now(timezone.utc)
    today = now.astimezone(config.tz).date().isoformat()
    blocks = {"⚡ Спорное": [], "🟡 Ждут подтверждения клиента": [], "⏳ Просроченные задачи": [],
              "⚠️ Открытые проблемы": [], "❓ Вопросы без ответа": [], "🤷 Не уточнено": [], "💤 Тихие объекты": []}

    for obj in await db.all_objects():
        for r in await db.records_for_object(obj["id"]):
            line = record_line(r, with_object=obj["name"])
            if r["type"] == "agreement" and r["status"] == "disputed":
                blocks["⚡ Спорное"].append(line)
            elif r["type"] == "agreement" and r["confidence"] == "reported":
                blocks["🟡 Ждут подтверждения клиента"].append(line)
            elif r["type"] == "task" and r["due_date"] and r["due_date"] < today:
                blocks["⏳ Просроченные задачи"].append(line)
            elif r["type"] == "problem":
                blocks["⚠️ Открытые проблемы"].append(line)
            elif "не уточнено" in (r["note"] or ""):
                blocks["🤷 Не уточнено"].append(line + f"\n   <i>{esc(r['note'])}</i>")
            elif r["type"] == "question" and \
                    now - datetime.fromisoformat(r["created_at"]) > timedelta(hours=QUESTION_STALE_HOURS):
                blocks["❓ Вопросы без ответа"].append(line)

        last = await db.fetchone("SELECT MAX(date) AS d FROM messages WHERE chat_id = ?", obj["chat_id"])
        if not last["d"] or now - datetime.fromisoformat(last["d"]) > timedelta(days=QUIET_DAYS):
            blocks["💤 Тихие объекты"].append(f"[{esc(obj['name'])}] нет сообщений {QUIET_DAYS}+ дня")

    parts = [f"📊 <b>Сводка на {datetime.now(config.tz):%d.%m}</b>"]
    for title, lines in blocks.items():
        if lines:
            parts.append(f"\n<b>{title} ({len(lines)})</b>\n" + "\n".join(lines[:15]))
    if len(parts) == 1:
        parts.append("\nВсё спокойно 👌")
    return "\n".join(parts)


async def build_brief(obj: dict) -> str:
    recs = await db.records_for_object(obj["id"])
    by_type: dict[str, list[str]] = {}
    for r in recs:
        by_type.setdefault(r["type"], []).append(record_line(r))
    titles = {"agreement": "📌 Договорённости", "task": "🛠 Задачи",
              "problem": "⚠️ Проблемы", "question": "❓ Открытые вопросы"}
    parts = [f"<b>{esc(obj['name'])} — кратко</b>"]
    for t, title in titles.items():
        if by_type.get(t):
            parts.append(f"\n<b>{title}</b>\n" + "\n".join(by_type[t]))
    if len(parts) == 1:
        parts.append("Пока ничего не зафиксировано.")
    return "\n".join(parts)


async def scheduler(bot: Bot) -> None:
    while True:
        now = datetime.now(config.tz)
        run = now.replace(hour=config.digest_hour, minute=0, second=0, microsecond=0)
        if run <= now:
            run += timedelta(days=1)
        await asyncio.sleep((run - now).total_seconds())
        try:
            await notify.to_managers(bot, await build_digest())
        except Exception:
            log.exception("digest failed")
