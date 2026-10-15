"""Этап объекта: «Демонтаж», «Заливаем полы», «Штукатурка», «Сдан».

Меняют участники чата:
- командой `/stage Заливаем полы` (без текста — текущий этап и история);
- тегом `@бот этап: заливаем полы` / `@бот перешли на штукатурку`;
- бот сам замечает в переписке «начали заливать полы» и ПРЕДЛАГАЕТ сменить этап кнопкой — молча не меняет.
Каждая смена пишется в историю и уходит руководителю.
"""

import json
import re
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from . import db, notify
from .formatting import esc, link, short_date

STAGE_PREFIX = re.compile(
    r"^(этап|стадия|статус объекта|сменить этап|смени этап|поменяй этап|сейчас идёт|сейчас идет|"
    r"перешли на|начали этап|новый этап)\s*[:\-—]?\s*", re.IGNORECASE,
)


def parse_tag(text: str) -> str | None:
    """«этап: заливаем полы» → «Заливаем полы». None — если это не про этап."""
    m = STAGE_PREFIX.match(text.strip())
    if not m:
        return None
    stage = text.strip()[m.end():].strip(" .!")
    return stage[:1].upper() + stage[1:] if stage else ""


def line(st: dict | None) -> str:
    if not st:
        return "🏗 Этап: не указан"
    return (f"🏗 Этап: <b>{esc(st['stage'])}</b> · с {short_date(st['created_at'])}, "
            f"{esc(st['user_name'])} · {link(st, '↗')}")


async def change(bot: Bot, obj: dict, stage: str, user_id: int, user_name: str,
                 chat_id: int, message_id: int) -> str:
    prev = await db.set_stage(obj["id"], stage, user_id, user_name, chat_id, message_id,
                              datetime.now(timezone.utc).isoformat())
    was = f"«{esc(prev['stage'])}» → " if prev else ""
    text = f"🏗 <b>{esc(obj['name'])}</b>: этап {was}«<b>{esc(stage)}</b>» ({esc(user_name)})"
    await notify.to_managers(bot, text)
    await notify.to_status_topic(bot, obj, text)
    return f"🏗 Этап объекта <b>{esc(obj['name'])}</b>: {was}«<b>{esc(stage)}</b>»"


async def history_text(obj: dict) -> str:
    hist = await db.stage_history(obj["id"])
    if not hist:
        return (f"🏗 У объекта <b>{esc(obj['name'])}</b> этап ещё не указан.\n"
                "Укажите: <code>/stage Заливаем полы</code>")
    lines = [f"<b>{esc(obj['name'])}</b>", line(hist[0])]
    if len(hist) > 1:
        lines.append("\n<b>История:</b>")
        lines += [f"• {short_date(h['created_at'])} — {esc(h['stage'])} ({esc(h['user_name'])})" for h in hist[1:]]
    return "\n".join(lines)


async def propose(bot: Bot, obj: dict, msg: dict, stage: str) -> None:
    """Бот заметил в переписке смену этапа — спрашивает кнопкой, а не меняет сам."""
    cur = await db.current_stage(obj["id"])
    if cur and cur["stage"].strip().lower() == stage.strip().lower():
        return
    pending = await db.fetchone(
        "SELECT 1 FROM clarifications WHERE object_id = ? AND kind = 'stage' AND status = 'open'", obj["id"]
    )
    if pending:
        return  # одно предложение за раз
    cid = await db.execute(
        "INSERT INTO clarifications (chat_id, object_id, kind, question, source_message_id, author_id, created_at, payload) "
        "VALUES (?, ?, 'stage', ?, ?, ?, ?, ?)",
        msg["chat_id"], obj["id"], f"Сменить этап на «{stage}»?", msg["message_id"], msg["user_id"],
        datetime.now(timezone.utc).isoformat(), json.dumps({"stage": stage}, ensure_ascii=False),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"stage:{cid}:yes"),
        InlineKeyboardButton(text="Нет", callback_data=f"stage:{cid}:no"),
    ]])
    was = f" (сейчас: {esc(cur['stage'])})" if cur else ""
    sent = await bot.send_message(
        msg["chat_id"], f"🏗 Сменить этап объекта на «<b>{esc(stage)}</b>»?{was}",
        reply_parameters=ReplyParameters(message_id=msg["message_id"], allow_sending_without_reply=True),
        reply_markup=kb, **notify.thread_kw(obj),
    )
    await db.execute("UPDATE clarifications SET ask_message_id = ? WHERE id = ?", sent.message_id, cid)
