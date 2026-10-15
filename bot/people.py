"""Сотрудники по ФИО: регистрация при /start и поиск человека по имени-отчеству в любом падеже.

«Ивану Петровичу», «Иван Петрович», «Сидорову», «@ivan» → один и тот же сотрудник.
Сопоставление по основам слов (первые буквы): падежные окончания отбрасываются сами.
Если подходят двое — считаем, что не нашли, и бот переспросит, а не назначит наугад.
"""

import re

from . import db

FIO_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\-]{2,}(\s+[A-Za-zА-Яа-яЁё\-]{2,}){1,2}$")


def clean_fio(text: str) -> str | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not FIO_RE.match(t):
        return None
    return " ".join(w[:1].upper() + w[1:].lower() for w in t.split())


def _stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    return w[:5] if len(w) >= 7 else w[:4]


def short(fio: str | None) -> str:
    """«Иванов Иван Петрович» → «Иван Петрович Иванов» — так привычнее читать в карточке."""
    parts = (fio or "").split()
    return " ".join(parts[1:] + parts[:1]) if len(parts) == 3 else (fio or "")


def label(user: dict) -> str:
    name = short(user.get("fio")) or user.get("name") or ""
    return f"{name} (@{user['username']})" if user.get("username") else name


async def display(user_id: int, fallback: str) -> str:
    """Как подписывать человека в записях и сообщениях: ФИО, если он его указал."""
    u = await db.get_user(user_id)
    return short(u["fio"]) if u and u.get("fio") else fallback


async def resolve(name: str | None) -> dict | None:
    """Сотрудник по @username или по ФИО/имени-отчеству в любом падеже. None — не нашли или неоднозначно."""
    if not name:
        return None
    name = name.strip()
    if name.startswith("@") or re.fullmatch(r"[A-Za-z0-9_]{4,}", name):
        return await db.user_by_username(name)
    query = {_stem(w) for w in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", name)}
    if not query:
        return None
    best, best_score, tie = None, 0, False
    for u in await db.employees():
        parts = {_stem(w) for w in u["fio"].split()}
        score = len(query & parts)
        if score > best_score:
            best, best_score, tie = u, score, False
        elif score == best_score and score:
            tie = True
    need = 1 if len(query) == 1 else 2  # «Ивану Петровичу» — нужны и имя, и отчество
    if best_score < need or tie:
        return None
    return best


async def roster_for_llm() -> str:
    rows = await db.employees()
    return "; ".join(f"{u['fio']}" + (f" (@{u['username']})" if u.get("username") else "") for u in rows) or "нет"
