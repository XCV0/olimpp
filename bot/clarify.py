"""Уточняющие вопросы бота в чате, когда человек не написал важное.

Как работает:
- конвейер видит, что у записи нет исполнителя/срока/конкретики/места или объект неясен,
  и бот задаёт ОДИН короткий вопрос ответом на исходное сообщение (с кнопкой «Пропустить»);
- ответом считается реплай на вопрос бота или следующее сообщение того же автора в течение
  ANSWER_WINDOW (LLM дополнительно проверяет, что это действительно ответ);
- после ответа запись дополняется, а вопрос удаляется — в чате не остаётся мусора;
- без ответа за QUESTION_TTL вопрос тоже удаляется, а запись получает пометку «не уточнено»
  и попадает в сводку руководителю.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from . import db, llm
from .formatting import esc, message_for_llm, record_for_llm

log = logging.getLogger(__name__)

ANSWER_WINDOW = timedelta(minutes=10)
QUESTION_TTL = timedelta(hours=2)
MAX_OPEN_PER_CHAT = 3  # больше вопросов разом — уже спам

UPDATABLE = {"title", "value", "assignee", "due_date", "topic_key"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _skip_kb(clar_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пропустить", callback_data=f"clar_skip:{clar_id}"),
    ]])


def _add_note(note: str | None, extra: str) -> str:
    return f"{note}; {extra}" if note else extra


async def ask(bot: Bot, *, chat_id: int, object_id: int, source_message_id: int, author_id: int,
              question: str, record_id: int | None = None, kind: str = "record") -> bool:
    """Задаёт вопрос в чате. False — если лимит открытых вопросов исчерпан."""
    open_count = await db.fetchone(
        "SELECT COUNT(*) AS n FROM clarifications WHERE chat_id = ? AND status = 'open'", chat_id
    )
    if open_count["n"] >= MAX_OPEN_PER_CHAT:
        if record_id:
            r = await db.get_record(record_id)
            await db.update_record(record_id, note=_add_note(r["note"], f"не уточнено: {question}"))
        return False

    clar_id = await db.execute(
        "INSERT INTO clarifications (chat_id, object_id, record_id, kind, question, source_message_id, "
        "author_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        chat_id, object_id, record_id, kind, question, source_message_id, author_id, _now(),
    )
    try:
        sent = await bot.send_message(
            chat_id, f"❓ {esc(question)}",
            reply_parameters=ReplyParameters(message_id=source_message_id, allow_sending_without_reply=True),
            reply_markup=_skip_kb(clar_id),
        )
    except Exception as e:
        log.warning("не смог задать вопрос: %s", e)
        await db.execute("UPDATE clarifications SET status = 'expired' WHERE id = ?", clar_id)
        return False
    await db.execute("UPDATE clarifications SET ask_message_id = ? WHERE id = ?", sent.message_id, clar_id)
    if record_id:
        r = await db.get_record(record_id)
        await db.update_record(record_id, note=_add_note(r["note"], "ждёт уточнения"))
    return True


async def find_for_answer(msg: dict) -> dict | None:
    """Открытый вопрос, на который похоже отвечает это сообщение."""
    if msg["reply_to"]:
        return await db.fetchone(
            "SELECT * FROM clarifications WHERE chat_id = ? AND ask_message_id = ? AND status = 'open'",
            msg["chat_id"], msg["reply_to"],
        )
    since = (datetime.fromisoformat(msg["date"]) - ANSWER_WINDOW).isoformat()
    return await db.fetchone(
        "SELECT * FROM clarifications WHERE chat_id = ? AND author_id = ? AND status = 'open' "
        "AND created_at >= ? ORDER BY id DESC LIMIT 1",
        msg["chat_id"], msg["user_id"], since,
    )


async def _close(bot: Bot, clar: dict, status: str) -> None:
    await db.execute("UPDATE clarifications SET status = ? WHERE id = ?", status, clar["id"])
    if clar["ask_message_id"]:
        try:
            await bot.delete_message(clar["chat_id"], clar["ask_message_id"])
        except Exception:
            pass


def _clean_note(note: str | None) -> str | None:
    if not note:
        return None
    parts = [p for p in note.split("; ") if p != "ждёт уточнения"]
    return "; ".join(parts) or None


async def resolve(bot: Bot, clar: dict, msg: dict) -> dict | None:
    """Пробует принять сообщение как ответ. Возвращает обновлённую запись/объект или None,
    если это не ответ (тогда сообщение обрабатывается как обычное)."""
    answer = message_for_llm(msg)

    if clar["kind"] == "object":
        res = await llm.parse_object(f"ВОПРОС: {clar['question']}\nОТВЕТ: {answer}")
        if not res.get("is_answer"):
            return None
        fields = {k: res[k] for k in ("address", "description") if res.get(k)}
        if fields:
            await db.update_object(clar["object_id"], **fields)
        await _close(bot, clar, "answered")
        return await db.get_object(clar["object_id"])

    r = await db.get_record(clar["record_id"]) if clar["record_id"] else None
    if not r:
        await _close(bot, clar, "expired")
        return None
    res = await llm.merge_answer(record_for_llm(r), clar["question"], answer)
    # сообщение пришло реплаем на вопрос — точно ответ, даже если LLM сомневается
    if not res.get("is_answer") and msg["reply_to"] != clar["ask_message_id"]:
        return None
    updates = {k: v for k, v in (res.get("updates") or {}).items() if k in UPDATABLE and v}
    if "topic_key" in updates:
        updates["topic_key"] = updates["topic_key"].lower()
    updates["note"] = _add_note(_clean_note(r["note"]), f"уточнено: {msg['user_name']}")
    await db.update_record(r["id"], **updates)
    await _close(bot, clar, "answered")
    return await db.get_record(r["id"])


async def dismiss(bot: Bot, clar_id: int) -> None:
    clar = await db.fetchone("SELECT * FROM clarifications WHERE id = ?", clar_id)
    if not clar or clar["status"] != "open":
        return
    await _close(bot, clar, "dismissed")
    if clar["record_id"]:
        r = await db.get_record(clar["record_id"])
        if r:
            await db.update_record(r["id"], note=_clean_note(r["note"]))


async def expire_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            cutoff = (datetime.now(timezone.utc) - QUESTION_TTL).isoformat()
            for clar in await db.fetchall(
                "SELECT * FROM clarifications WHERE status = 'open' AND created_at < ?", cutoff
            ):
                await _close(bot, clar, "expired")
                if clar["record_id"]:
                    r = await db.get_record(clar["record_id"])
                    if r:
                        await db.update_record(
                            r["id"], note=_add_note(_clean_note(r["note"]), f"не уточнено: {clar['question']}")
                        )
        except Exception:
            log.exception("expire_loop failed")
