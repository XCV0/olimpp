import html
import re
from datetime import datetime

from .config import config

CONF_ICON = {"confirmed": "🟢", "reported": "🟡", "discussion": "⚪", "conflict": "🔴"}
TYPE_NAME = {"agreement": "Договорённость", "task": "Задача", "problem": "Проблема", "question": "Вопрос"}
# у задач, проблем и вопросов значок показывает статус, у договорённостей — достоверность
STATUS_ICON = {
    "task": {"done": "✅", "accepted": "🔵", "open": "⚪", "cancelled": "✖️"},
    "problem": {"open": "⚠️", "closed": "✅"},
    "question": {"open": "❓", "closed": "✅"},
}
STATUS_NAME = {
    "done": "выполнена", "accepted": "в работе", "cancelled": "отменена", "open": "открыта", "closed": "закрыта",
    "active": "актуальна", "superseded": "заменена", "rejected": "отклонена", "disputed": "спорная",
}
# откуда запись: человек явно зафиксировал через @бота или бот сам заметил в переписке
SOURCE_MARK = {"tag": "✍️", "stream": "👁"}
SOURCE_NAME = {"tag": "зафиксировано человеком", "stream": "замечено ботом в переписке"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def msg_link(chat_id: int, message_id: int) -> str | None:
    # ссылки t.me/c/... существуют только у супергрупп (id вида -100XXXXXXXXXX);
    # у обычной группы ссылки на сообщение нет вообще — тогда показываем /src
    s = str(chat_id)
    if not s.startswith("-100"):
        return None
    return f"https://t.me/c/{s.removeprefix('-100')}/{message_id}"


def link(r: dict, text: str = "сообщение") -> str:
    """Ссылка на исходное сообщение. r — запись, сообщение или этап: у всех есть chat_id и message_id.
    В обычной группе ссылок нет: для записи даём /src_N (бот покажет оригинал), для остального — номер сообщения."""
    url = msg_link(r["chat_id"], r["message_id"])
    if url:
        return f'<a href="{url}">{text}</a>'
    if "type" in r and "id" in r:  # запись из records
        return f"📎 /src_{r['id']}"
    return f"📎 сообщение №{r['message_id']}"


def esc(s: str | None) -> str:
    return html.escape(s or "")


def local_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(config.tz)


def short_date(iso: str) -> str:
    return local_dt(iso).strftime("%d.%m %H:%M")


def is_overdue(r: dict) -> bool:
    today = datetime.now(config.tz).date().isoformat()
    return r["type"] == "task" and r["status"] in ("open", "accepted") \
        and bool(r.get("due_date")) and r["due_date"] < today


def icon(r: dict) -> str:
    if is_overdue(r):
        return "🔴"
    if r["type"] in STATUS_ICON:
        return STATUS_ICON[r["type"]].get(r["status"], "")
    return CONF_ICON.get(r["confidence"], "")


def status_text(r: dict) -> str:
    return "просрочена" if is_overdue(r) else STATUS_NAME.get(r["status"], r["status"])


def record_line(r: dict, with_object: str | None = None) -> str:
    icon_ = icon(r)
    parts = [f"{icon_} <b>{esc(r['title'])}</b>: {esc(r['value'])}"]
    if r.get("assignee"):
        parts.append(f"👤 {esc(r['assignee'])}")
    if r.get("due_date"):
        parts.append(f"⏰ {r['due_date']}")
    src = SOURCE_MARK.get(r.get("source") or "stream", "")
    meta = f"{src} {esc(r['author_name'])}, {short_date(r['created_at'])} · {link(r)}"
    prefix = f"[{esc(with_object)}] " if with_object else ""
    return f"{prefix}{' · '.join(parts)}\n   <i>{meta}</i>"


def record_for_llm(r: dict) -> str:
    """Плоская строка для контекста LLM."""
    bits = [
        f"#{r['id']}", f"значок={icon(r)}", r["type"], f"тема={r['topic_key'] or '-'}",
        f"статус={status_text(r)}",
        f"достоверность={r['confidence']}", f"источник={SOURCE_NAME.get(r.get('source') or 'stream')}",
        f"«{r['title']}: {r['value']}»",
        f"автор={r['author_name']}", f"дата={r['created_at'][:16]}",
    ]
    if r.get("assignee"):
        bits.append(f"исполнитель={r['assignee']}")
    if r.get("due_date"):
        bits.append(f"срок={r['due_date']}")
    if r.get("details"):
        bits.append(f"детали={r['details']}")
    if r.get("note"):
        bits.append(f"пометка={r['note']}")
    return " | ".join(bits)


def message_for_llm(m: dict) -> str:
    dt = local_dt(m["date"])
    head = f"[{dt:%Y-%m-%d %H:%M}, {WEEKDAYS[dt.weekday()]}] {m['user_name']}"
    if m["forward_from"]:
        head += f" (переслано от: {m['forward_from']})"
    if m["kind"] != "text":
        head += f" [{m['kind']}]"
    return f"{head}: {m['text'] or '(без текста)'}"


def linkify_ids(text: str, records: dict[int, dict]) -> tuple[str, list[int]]:
    """Заменяет [#12] в ответе LLM на ссылку на исходное сообщение. Текст LLM экранируется.
    Возвращает текст и id упомянутых записей (для кнопок «📎 исходное»)."""
    text = esc(text)
    used: list[int] = []

    def repl(match: re.Match) -> str:
        r = records.get(int(match.group(1)))
        if not r:
            return ""
        if r["id"] not in used:
            used.append(r["id"])
        return link(r, "↗ источник")

    return re.sub(r"\[#(\d+)\]", repl, text), used


def object_for_llm(obj: dict) -> str:
    return (f"название={obj['name']} | адрес={obj.get('address') or 'не указан'} | "
            f"описание={obj.get('description') or 'не указано'} | текущий этап={obj.get('stage') or 'не указан'}")
