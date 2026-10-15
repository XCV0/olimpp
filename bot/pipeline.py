"""Конвейер: сообщение → нормализация → (быстрые правила | LLM) → записи → реакция и сигналы."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import (
    Message, MessageOriginChannel, MessageOriginChat, MessageOriginHiddenUser,
    MessageOriginUser, ReactionTypeEmoji,
)

from . import approvals, clarify, db, files, llm, media, notify, people, stages, tasks
from .formatting import esc, link, message_for_llm, object_for_llm, record_for_llm, record_line

log = logging.getLogger(__name__)


# приоритет реакций: показываем самую важную
REACTION_PRIORITY = ["⚡", "🤔", "✍", "👍", "👀"]


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
        "thread_id": thread_of(m),
    }


def thread_of(m: Message) -> int:
    """Топик сообщения в чате компании; 0 — обычный чат или «General»."""
    return m.message_thread_id if m.is_topic_message and m.message_thread_id else 0


async def _is_employee_name(chat_id: int, name: str) -> bool:
    row = await db.fetchone(
        "SELECT 1 FROM messages WHERE chat_id = ? AND user_name = ? LIMIT 1", chat_id, name
    )
    if row:
        return True
    # сотрудник мог ещё не писать в этот чат — сверяем с ФИО и именами из профилей
    if await db.fetchone("SELECT 1 FROM users WHERE fio = ? OR name = ? LIMIT 1", name, name):
        return True
    return await people.resolve(name) is not None and len(name.split()) >= 2


async def _trusted_confidence(msg: dict, llm_conf: str) -> tuple[str, str | None]:
    """LLM предлагает уровень, но 🟢 выдаём только по проверяемому признаку."""
    if llm_conf != "confirmed":
        return llm_conf, None
    if msg["forward_from"] and not await _is_employee_name(msg["chat_id"], msg["forward_from"]):
        return "confirmed", f"пересылка от {msg['forward_from']}"
    shot = msg.get("photo")
    if shot and shot.get("is_chat_screenshot"):
        who = shot.get("counterpart") or ""
        date = shot.get("screenshot_date") or ""
        if not who:
            return "reported", "скрин: не видно, с кем переписка"
        if await _is_employee_name(msg["chat_id"], who):
            return "reported", f"скрин переписки с сотрудником ({who}), а не с клиентом"
        if date and date > msg["date"][:10]:
            return "reported", f"⚠️ дата на скрине ({date}) позже отправки — возможна подделка"
        return "confirmed", f"скрин переписки с {who}" + (f" от {date}" if date else "")
    if msg["kind"] == "photo":
        return "reported", "фото без переписки с клиентом"
    return "reported", "нет доказательства от клиента"


async def _quick_rules(msg: dict, obj: dict) -> str | None:
    """«+»/«сделал» реплаем на сообщение с задачей или на её карточку — без LLM и без руководителя."""
    if not msg["reply_to"]:
        return None
    action = tasks.quick_word(msg["text"])
    if not action:
        return None
    found = [t for t in await tasks.tasks_by_message(msg["chat_id"], msg["reply_to"]) if t["object_id"] == obj["id"]]
    if not found:
        return None
    await tasks.apply(action, found, msg["user_id"], msg["user_name"])
    return "👍" if action == "done" else "👀"


def _new_record(msg: dict, obj: dict, item: dict, confidence: str, note: str | None,
                source: str = "stream") -> dict:
    status = {"agreement": "active", "task": "open"}.get(item["type"], "open")
    return dict(
        object_id=obj["id"], type=item["type"], topic_key=(item.get("topic_key") or "").lower() or None,
        title=item.get("title") or "", value=item.get("value") or "", confidence=confidence,
        status=status, assignee=item.get("assignee"), due_date=item.get("due_date"),
        author_id=msg["user_id"], author_name=msg["user_name"], chat_id=msg["chat_id"],
        message_id=msg["message_id"], created_at=msg["date"], note=note, source=source,
    )


async def _apply_agreement(bot: Bot, obj: dict, rec: dict) -> tuple[str, int]:
    old = await db.active_agreement(obj["id"], rec["topic_key"]) if rec["topic_key"] else None

    if old and old["value"].strip().lower() == rec["value"].strip().lower():
        # то же самое решение: возможно, пришло подтверждение
        if rec["confidence"] == "confirmed" and old["confidence"] != "confirmed":
            await db.update_record(old["id"], confidence="confirmed", note=rec["note"])
            return "✍", old["id"]
        return ("✍" if old["confidence"] == "confirmed" else "🤔"), old["id"]

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
        return "⚡", rid

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
        return "⚡", rid
    return ("✍" if rec["confidence"] == "confirmed" else "🤔"), rid


async def process(bot: Bot, m: Message, obj: dict, strip: str | None = None, addressed: bool = False) -> dict:
    """Возвращает {"records": записи из этого сообщения, "closed": закрытые им записи,
    "clarified": запись или объект, если сообщение оказалось ответом на вопрос бота}."""
    msg = await ingest(bot, m, strip)
    return await analyze(bot, msg, obj, addressed)


async def analyze(bot: Bot, msg: dict, obj: dict, addressed: bool = False) -> dict:
    """Разбор уже принятого сообщения или эпизода (альбома)."""
    doc_text = msg.get("document")
    own_ids = set(msg.get("album_ids") or [msg["message_id"]])

    # сначала — не ответ ли это на уточняющий вопрос бота
    if msg["text"].strip():
        clar = await clarify.find_for_answer(msg, obj["id"])
        if clar:
            updated = await clarify.resolve(bot, clar, msg)
            if updated:
                await _react(bot, msg, "✍")
                return {"records": [], "closed": [], "clarified": updated, "clar_kind": clar["kind"],
                        "document": doc_text}

    reactions: list[str] = []
    closed: list[dict] = []
    quick = await _quick_rules(msg, obj)
    client = await _client_voice(msg)
    if quick:
        reactions.append(quick)
    elif client and (ap := await approvals.try_apply(bot, obj, msg, client)):
        reactions.append(ap)  # ответ клиента на лист согласования — 🟡 стали 🟢
    elif (hint := tasks.intent(msg["text"])) and (status_change := await _status_change(msg, obj, hint)):
        # «отмени задачу…», «сделал…» — меняем статус существующей задачи, новую не создаём
        reactions.append(status_change)
    elif msg["text"].strip() and len(msg["text"].strip()) > 2:
        # в контексте старые сообщения обрезаем: там могут быть целые документы
        context = [message_for_llm(x)[:600] for x in await db.recent_messages(
                       msg["chat_id"], 20 + len(own_ids), msg.get("thread_id") or 0)
                   if x["message_id"] not in own_ids][-20:]
        records = await db.records_for_object(obj["id"])
        result = await llm.extract(
            message_for_llm(msg), "\n".join(context), "\n".join(record_for_llm(r) for r in records),
            object_for_llm(obj), addressed=addressed, roster=await people.roster_for_llm(),
        )
        new_reactions, closed = await _apply(bot, msg, obj, result)
        reactions += new_reactions
        if result.get("stage") and not addressed:
            await stages.propose(bot, obj, msg, result["stage"])

    if reactions:
        await _react(bot, msg, min(reactions, key=REACTION_PRIORITY.index))
    return {"records": await db.records_by_message(msg["chat_id"], msg["message_id"]), "closed": closed,
            "document": doc_text}


async def ingest(bot: Bot, m: Message, strip: str | None = None) -> dict:
    """Нормализует сообщение, превращает файл/голос/фото в текст и сохраняет копию.
    Медиа-текст хранится вместе с сообщением — по нему потом работают поиск и вопросы."""
    msg = normalize(m)
    user = await db.get_user(msg["user_id"])
    if user and user.get("fio"):
        msg["user_name"] = people.short(user["fio"])  # в записях и ответах — ФИО, а не ник в Telegram
    if strip:
        msg["text"] = re.sub(re.escape(strip), "", msg["text"], flags=re.IGNORECASE).strip()
    caption = msg["text"]
    msg["caption"] = caption
    if m.document and not (m.document.mime_type or "").startswith(("image/", "audio/")):
        doc = await files.read_document(bot, m.document)
        if doc:
            msg["document"] = doc
            msg["text"] = f"{caption}\n[Файл «{m.document.file_name}»]:\n{doc}".strip()
    elif m.voice or m.video_note or m.audio:
        f = m.voice or m.video_note or m.audio
        speech = await media.transcribe(bot, f.file_id)
        label = "Голосовое" if m.voice else "Кружок" if m.video_note else "Аудио"
        msg["text"] = f"{caption}\n[{label}]: {speech}".strip() if speech \
            else f"{caption}\n[{label}: не удалось расшифровать]".strip()
        msg["voice_ok"] = bool(speech)
    elif m.photo or (m.document and (m.document.mime_type or "").startswith("image/")):
        file_id = m.photo[-1].file_id if m.photo else m.document.file_id
        mime = "image/jpeg" if m.photo else m.document.mime_type
        p = await media.read_photo(bot, file_id, mime)
        if p:
            msg["photo"] = p
            msg["text"] = f"{caption}\n{media.photo_to_text(p)}".strip()
    msg["media_text"] = msg["text"][len(caption):].strip() if msg["text"].startswith(caption) else msg["text"]
    await db.save_message(msg)
    return msg


async def ingest_album(bot: Bot, msgs: list[Message], strip: str | None = None) -> dict:
    """Альбом (одна media_group) → один эпизод. Каждое фото/файл сохраняется своим сообщением,
    а разбирается всё вместе: общая подпись + описания всех снимков, одна запись, одна реакция."""
    items = await asyncio.gather(*(ingest(bot, m, strip) for m in msgs))
    first = dict(items[0])
    caption = next((i["caption"] for i in items if i["caption"]), "")
    parts = [f"{n}. {i['media_text'] or '(не распознано)'}" for n, i in enumerate(items, 1)]
    first["text"] = f"{caption}\n[Альбом: {len(items)} шт.]\n" + "\n".join(parts)
    first["text"] = first["text"].strip()
    first["album_ids"] = [i["message_id"] for i in items]
    # для 🟢 важен скрин переписки — берём его, если он есть в альбоме
    shots = [i["photo"] for i in items if i.get("photo")]
    first["photo"] = next((p for p in shots if p.get("is_chat_screenshot")), shots[0] if shots else None)
    docs = [i["document"] for i in items if i.get("document")]
    first["document"] = "\n\n".join(docs) or None
    return first


async def _client_voice(msg: dict) -> str | None:
    """Имя клиента, если это его слова: пересылка не от сотрудника или скрин переписки не с сотрудником."""
    if msg.get("forward_from") and not await _is_employee_name(msg["chat_id"], msg["forward_from"]):
        return msg["forward_from"]
    shot = msg.get("photo")
    if shot and shot.get("is_chat_screenshot") and shot.get("counterpart") \
            and not await _is_employee_name(msg["chat_id"], shot["counterpart"]):
        return shot["counterpart"]
    return None


async def _status_change(msg: dict, obj: dict, hint: str) -> str | None:
    found = await tasks.open_tasks(obj["id"])
    refs = set(tasks.referenced_ids(msg["text"]))
    candidates = [t for t in found if t["id"] in refs] or found
    if not candidates:
        return None
    res = await tasks.detect_update(msg["text"], candidates)
    action = res.get("action") if res.get("action") in ("done", "cancel", "accept") else None
    ids = {int(i) for i in res.get("record_ids") or []}
    chosen = [t for t in candidates if t["id"] in ids] or ([candidates[0]] if refs and len(candidates) == 1 else [])
    if not action or not chosen:
        return None  # не поняли, о какой задаче — пусть разберёт обычное извлечение
    changed, _ = await tasks.apply(action, chosen, msg["user_id"], msg["user_name"])
    return ("👍" if action == "done" else "👀") if changed else None


async def _react(bot: Bot, msg: dict, emoji: str) -> None:
    try:
        await bot.set_message_reaction(msg["chat_id"], msg["message_id"], [ReactionTypeEmoji(emoji=emoji)])
    except Exception as e:
        log.warning("реакция не поставлена: %s", e)


async def _apply(bot: Bot, msg: dict, obj: dict, result: dict) -> tuple[list[str], list[dict]]:
    reactions, closed = [], []
    asked = False  # не больше одного вопроса на сообщение
    for item in result.get("items", []):
        if item.get("type") not in ("agreement", "task", "problem", "question"):
            continue
        if item["type"] == "task" and tasks.intent(f"{item.get('title')} {item.get('value')}") == "cancel":
            continue  # «отменить задачу» — это не задача
        if item["type"] == "agreement" and item.get("confidence") == "discussion":
            continue  # предположения в договорённости не пишем, они остаются в истории сообщений
        conf, note = await _trusted_confidence(msg, item.get("confidence", "reported"))
        if item.get("date_problem"):
            note = f"{note + '; ' if note else ''}дата: {item['date_problem']}"
        rec = _new_record(msg, obj, item, conf, note)
        if item["type"] == "task" and item.get("assignee"):
            if u := await people.resolve(item["assignee"]):  # «Ивану Петровичу» → сотрудник из списка
                rec["assignee"], rec["assignee_user_id"] = people.short(u["fio"]) or f"@{u['username']}", u["user_id"]

        if item["type"] == "agreement":
            reaction, rid = await _apply_agreement(bot, obj, rec)
            reactions.append(reaction)
        else:
            dup = await tasks.find_duplicate(obj["id"], rec) if item["type"] == "task" else None
            if dup:
                rid = (await tasks.merge_into(dup, rec))["id"]  # такая задача уже есть — дополняем, не плодим
            else:
                rid = await db.add_record(**rec)
            reactions.append("👀")

        question = item.get("ask")
        if question and not asked and item["type"] != "question" and (item.get("missing") or item.get("date_problem")):
            asked = await clarify.ask(
                bot, chat_id=msg["chat_id"], object_id=obj["id"], source_message_id=msg["message_id"],
                author_id=msg["user_id"], question=question, record_id=rid,
            )
            if asked:
                reactions.append("🤔")
            if item["type"] == "problem":
                await notify.to_managers(
                    bot, f"⚠️ <b>{esc(obj['name'])}: проблема</b>\n{record_line(await db.get_record(rid))}"
                )

    for c in result.get("closes", []):
        r = await db.get_record(int(c.get("record_id", 0)))
        # закрывать можно только записи этого же объекта
        if not r or r["object_id"] != obj["id"] or r["type"] not in ("task", "question"):
            continue
        if r["status"] in ("done", "closed", "cancelled"):
            continue
        if r["type"] == "task":
            await tasks.apply("done", [r], msg["user_id"], msg["user_name"])
        else:
            await db.update_record(r["id"], status="closed")
        closed.append(r)
        reactions.append("👍")
    for c in result.get("cancels", []):
        r = await db.get_record(int(c.get("record_id", 0) or 0))
        if r and r["object_id"] == obj["id"] and r["type"] == "task" and r["status"] in ("open", "accepted"):
            changed, _ = await tasks.apply("cancel", [r], msg["user_id"], msg["user_name"])
            if changed:
                closed.append(r)
                reactions.append("👀")
    return reactions, closed


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
