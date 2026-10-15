import html
import re
from datetime import datetime

from .config import config

CONF_ICON = {"confirmed": "🟢", "reported": "🟡", "discussion": "⚪", "conflict": "🔴"}
TYPE_NAME = {"agreement": "Договорённость", "task": "Задача", "problem": "Проблема", "question": "Вопрос"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def msg_link(chat_id: int, message_id: int) -> str:
    # ссылки t.me/c/... работают только для супергрупп (id вида -100XXXXXXXXXX)
    internal = str(chat_id).removeprefix("-100")
    return f"https://t.me/c/{internal}/{message_id}"


def link(r: dict, text: str = "сообщение") -> str:
    return f'<a href="{msg_link(r["chat_id"], r["message_id"])}">{text}</a>'


def esc(s: str | None) -> str:
    return html.escape(s or "")


def local_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(config.tz)


def short_date(iso: str) -> str:
    return local_dt(iso).strftime("%d.%m %H:%M")


def record_line(r: dict, with_object: str | None = None) -> str:
    icon = CONF_ICON.get(r["confidence"], "")
    parts = [f"{icon} <b>{esc(r['title'])}</b>: {esc(r['value'])}"]
    if r.get("assignee"):
        parts.append(f"👤 {esc(r['assignee'])}")
    if r.get("due_date"):
        parts.append(f"⏰ {r['due_date']}")
    meta = f"{esc(r['author_name'])}, {short_date(r['created_at'])} · {link(r)}"
    prefix = f"[{esc(with_object)}] " if with_object else ""
    return f"{prefix}{' · '.join(parts)}\n   <i>{meta}</i>"


def record_for_llm(r: dict) -> str:
    """Плоская строка для контекста LLM."""
    bits = [
        f"#{r['id']}", r["type"], f"тема={r['topic_key'] or '-'}", f"статус={r['status']}",
        f"достоверность={r['confidence']}", f"«{r['title']}: {r['value']}»",
        f"автор={r['author_name']}", f"дата={r['created_at'][:16]}",
    ]
    if r.get("assignee"):
        bits.append(f"исполнитель={r['assignee']}")
    if r.get("due_date"):
        bits.append(f"срок={r['due_date']}")
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


def linkify_ids(text: str, records: dict[int, dict]) -> str:
    """Заменяет [#12] в ответе LLM на ссылку на исходное сообщение. Текст LLM экранируется."""
    text = esc(text)

    def repl(match: re.Match) -> str:
        r = records.get(int(match.group(1)))
        return link(r, "↗ источник") if r else ""

    return re.sub(r"\[#(\d+)\]", repl, text)
