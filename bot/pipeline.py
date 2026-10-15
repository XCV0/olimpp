"""Конвейер: сообщение → нормализация → (быстрые правила | LLM) → записи → реакция и сигналы."""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import (
    Message, MessageOriginChannel, MessageOriginChat, MessageOriginHiddenUser,
    MessageOriginUser, ReactionTypeEmoji, ReplyParameters,
)

from . import db, llm, notify
from .formatting import esc, link, message_for_llm, record_for_llm, record_line

log = logging.getLogger(__name__)

ACK_WORDS = {"+", "ок", "ok", "окей", "принял", "понял", "хорошо", "сделаю", "добро", "👍"}
DONE_WORDS = {"сделал", "сделано", "готово", "забрал", "привёз", "привез", "выполнил", "done", "✅"}

# приоритет реакций: показываем самую важную
REACTION_PRIORITY = ["⚡", "🤔", "✍", "👀"]


def _origin_name(m: Message) -> str | None:
    o = m.forward_origin
    if o is None:
        return None
    if isinstance(o, MessageOriginUser):
        return o.sender_user.full_name
    if isinstance(o, MessageOriginHiddenUser):
        return o.sender_user_name
    if isinstance(o, MessageOriginChat):
        return o.sender_chat.title
    if isinstance(o, MessageOriginChannel):
        return o.chat.title
    return "неизвестно"


def normalize(m: Message) -> dict:
    kind, file_id = "text", None
    if m.photo:
        kind, file_id = "photo", m.photo[-1].file_id
    elif m.voice:
        kind, file_id = "voice", m.voice.file_id
    elif m.video_note:
        kind, file_id = "voice", m.video_note.file_id
    elif m.video:
        kind, file_id = "video", m.video.file_id
    elif m.document:
        kind, file_id = "document", m.document.file_id
    text = m.text or m.caption or ""
    # TODO STT: голосовые → текст (нужен отдельный STT-сервис)
    # TODO Vision/OCR: фото и скрины → описание и текст (можно через vision-модель на OpenRouter)
    return {
        "chat_id": m.chat.id,
        "message_id": m.message_id,
        "user_id": m.from_user.id if m.from_user else 0,
        "user_name": m.from_user.full_name if m.from_user else "?",
        "date": m.date.astimezone(timezone.utc).isoformat(),
        "text": text,
        "kind": kind,
        "file_id": file_id,
        "forward_from": _origin_name(m),
        "reply_to": m.reply_to_message.message_id if m.reply_to_message else None,
    }


async def _is_employee_name(chat_id: int, name: str) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM messages WHERE chat_id = ? AND user_name = ? LIMIT 1", chat_id, name
    )
    return row is not None


async def _trusted_confidence(msg: dict, llm_conf: str) -> tuple[str, str | None]:
    """LLM предлагает уровень, но 🟢 выдаём только по проверяемому признаку."""
    if llm_conf != "confirmed":
        return llm_conf, None
    if msg["forward_from"] and not await _is_employee_name(msg["chat_id"], msg["forward_from"]):
        return "confirmed", f"пересылка от {msg['forward_from']}"
    if msg["kind"] == "photo":
        return "reported", "скрин не проверен (vision не подключён)"
    return "reported", "нет доказательства от клиента"


async def _quick_rules(msg: dict, obj: dict) -> str | None:
    """Ответ «+»/«сделал» реплаем на сообщение с задачей — без LLM."""
    if not msg["reply_to"]:
        return None
    word = msg["text"].strip().lower().rstrip("!.")
    if word not in ACK_WORDS | DONE_WORDS:
        return None
    tasks = [r for r in await db.records_by_message(msg["chat_id"], msg["reply_to"])
             if r["type"] == "task" and r["object_id"] == obj["id"]]
    if not tasks:
        return None
    new_status = "done" if word in DONE_WORDS else "accepted"
    for t in tasks:
        await db.update_record(t["id"], status=new_status)
    return "👀"


def _new_record(msg: dict, obj: dict, item: dict, confidence: str, note: str | None) -> dict:
    status = {"agreement": "active", "task": "open"}.get(item["type"], "open")
    return dict(
        object_id=obj["id"], type=item["type"], topic_key=(item.get("topic_key") or "").lower() or None,
        title=item.get("title") or "", value=item.get("value") or "", confidence=confidence,
        status=status, assignee=item.get("assignee"), due_date=item.get("due_date"),
        author_id=msg["user_id"], author_name=msg["user_name"], chat_id=msg["chat_id"],
        message_id=msg["message_id"], created_at=msg["date"], note=note,
    )


async def _apply_agreement(bot: Bot, obj: dict, rec: dict) -> str:
    old = await db.active_agreement(obj["id"], rec["topic_key"]) if rec["topic_key"] else None

    if old and old["value"].strip().lower() == rec["value"].strip().lower():
        # то же самое решение: возможно, пришло подтверждение
        if rec["confidence"] == "confirmed" and old["confidence"] != "confirmed":
            await db.update_record(old["id"], confidence="confirmed", note=rec["note"])
            return "✍"
        return "✍" if old["confidence"] == "confirmed" else "🤔"

    if old and old["confidence"] == "confirmed" and rec["confidence"] != "confirmed":
        # пересказ не может перебить подтверждённое — это конфликт
        rec.update(confidence="conflict", status="disputed")
        rid = await db.add_record(**rec, supersedes=old["id"])
        await notify.to_managers(
            bot,
            f"🔴 <b>{esc(obj['name'])}: конфликт договорённости</b>\n"
            f"Подтверждено: {record_line(old)}\n"
            f"Новое без подтверждения: {record_line(await db.get_record(rid))}\n\n"
            f"Подтвердить новую версию?",
            notify.decision_kb(rid),
        )
        return "⚡"

    rid = await db.add_record(**rec, supersedes=old["id"] if old else None)
    if old:
        await db.update_record(old["id"], status="superseded")
        await notify.to_managers(
            bot,
            f"⚡ <b>{esc(obj['name'])}: изменение договорённости</b>\n"
            f"Было: {record_line(old)}\n"
            f"Стало: {record_line(await db.get_record(rid))}",
            # TODO: искать в чате действия по старой версии («привезли», «начали укладку»)
            notify.decision_kb(rid),
        )
        return "⚡"
    return "✍" if rec["confidence"] == "confirmed" else "🤔"


async def _clarify_date(bot: Bot, msg: dict, problem: str) -> None:
    sent = await bot.send_message(
        msg["chat_id"], f"🤔 Уточните дату: {esc(problem)}", reply_parameters=ReplyParameters(message_id=msg["message_id"])
    )
    await asyncio.sleep(600)  # уточнение не висит в чате
    try:
        await bot.delete_message(msg["chat_id"], sent.message_id)
    except Exception:
        pass


async def process(bot: Bot, m: Message, obj: dict) -> None:
    msg = normalize(m)
    await db.save_message(msg)

    reactions: list[str] = []
    quick = await _quick_rules(msg, obj)
    if quick:
        reactions.append(quick)
    elif msg["text"].strip() and len(msg["text"].strip()) > 2:
        context = [message_for_llm(x) for x in await db.recent_messages(msg["chat_id"], 21)
                   if x["message_id"] != msg["message_id"]][-20:]
        records = await db.records_for_object(obj["id"])
        result = await llm.extract(
            message_for_llm(msg), "\n".join(context), "\n".join(record_for_llm(r) for r in records)
        )
        reactions += await _apply(bot, msg, obj, result)

    if reactions:
        best = min(reactions, key=REACTION_PRIORITY.index)
        try:
            await bot.set_message_reaction(msg["chat_id"], msg["message_id"], [ReactionTypeEmoji(emoji=best)])
        except Exception as e:
            log.warning("реакция не поставлена: %s", e)


async def _apply(bot: Bot, msg: dict, obj: dict, result: dict) -> list[str]:
    reactions = []
    for item in result.get("items", []):
        if item.get("type") not in ("agreement", "task", "problem", "question"):
            continue
        if item.get("confidence") == "discussion":
            continue  # предположения в базу не пишем, они остаются в истории сообщений
        conf, note = await _trusted_confidence(msg, item.get("confidence", "reported"))
        if item.get("date_problem"):
            note = f"{note + '; ' if note else ''}дата: {item['date_problem']}"
            asyncio.create_task(_clarify_date(bot, msg, item["date_problem"]))
            reactions.append("🤔")
        rec = _new_record(msg, obj, item, conf, note)

        if item["type"] == "agreement":
            reactions.append(await _apply_agreement(bot, obj, rec))
        else:
            rid = await db.add_record(**rec)
            reactions.append("👀")
            if item["type"] == "problem":
                await notify.to_managers(
                    bot, f"⚠️ <b>{esc(obj['name'])}: проблема</b>\n{record_line(await db.get_record(rid))}"
                )

    for c in result.get("closes", []):
        r = await db.get_record(int(c.get("record_id", 0)))
        # закрывать можно только записи этого же объекта
        if not r or r["object_id"] != obj["id"] or r["type"] not in ("task", "question"):
            continue
        await db.update_record(r["id"], status="done" if r["type"] == "task" else "closed")
        reactions.append("👀")
    return reactions


async def process_edit(bot: Bot, m: Message, obj: dict) -> None:
    old = await db.get_message(m.chat.id, m.message_id)
    new_text = m.text or m.caption or ""
    if not old or old["text"] == new_text:
        return
    edited_at = (m.edit_date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    await db.save_edit(m.chat.id, m.message_id, old["text"], new_text, edited_at)

    recs = await db.records_by_message(m.chat.id, m.message_id)
    age_h = (datetime.fromisoformat(edited_at) - datetime.fromisoformat(old["date"])).total_seconds() / 3600
    if not recs and age_h < 1:
        return  # свежая правка опечатки без записей — не шумим
    for r in recs:
        await db.update_record(r["id"], note=f"{r['note'] + '; ' if r['note'] else ''}исходное сообщение отредактировано")
    await notify.to_managers(
        bot,
        f"✏️ <b>{esc(obj['name'])}: отредактировано сообщение</b> ({esc(old['user_name'])}, "
        f"через {age_h:.0f} ч)\nБыло: <i>{esc(old['text'])}</i>\nСтало: <i>{esc(new_text)}</i>\n"
        f"Связанных записей: {len(recs)} · {link(old)}",
    )
    # TODO: переизвлекать записи из нового текста и показывать расхождение
