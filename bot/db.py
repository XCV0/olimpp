"""SQLite-хранилище. Все выборки записей идут с фильтром по object_id —
изоляция объектов обеспечивается здесь, а не в промпте."""

import aiosqlite

from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    chat_id INTEGER UNIQUE NOT NULL,
    address TEXT,
    description TEXT          -- что за объект: квартира/дом/офис, какие работы, клиент
);
-- вопросы бота в чате, когда человеку не хватило данных
CREATE TABLE IF NOT EXISTS clarifications (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    object_id INTEGER NOT NULL,
    record_id INTEGER,         -- NULL для вопроса о профиле объекта
    kind TEXT NOT NULL,        -- record / object
    question TEXT NOT NULL,
    ask_message_id INTEGER,    -- сообщение бота с вопросом
    source_message_id INTEGER, -- сообщение, к которому вопрос
    author_id INTEGER,         -- у кого спрашиваем
    status TEXT DEFAULT 'open',-- open / answered / dismissed / expired
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    is_manager INTEGER DEFAULT 0,
    started INTEGER DEFAULT 0
);
-- копия каждого сообщения: удаления бот не видит, правки сравниваем с оригиналом
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER, message_id INTEGER,
    user_id INTEGER, user_name TEXT,
    date TEXT, text TEXT,
    kind TEXT,             -- text / photo / voice / document / video
    file_id TEXT,
    forward_from TEXT,     -- кто автор оригинала, если это пересылка
    reply_to INTEGER,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS message_edits (
    chat_id INTEGER, message_id INTEGER,
    old_text TEXT, new_text TEXT, edited_at TEXT
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    object_id INTEGER NOT NULL,
    type TEXT NOT NULL,        -- agreement / task / problem / question
    topic_key TEXT,            -- "санузел/плитка пол" — для версий договорённостей
    title TEXT,
    value TEXT,
    confidence TEXT,           -- confirmed / reported / conflict
    status TEXT,               -- agreement: active/superseded/rejected; task: open/accepted/done; problem/question: open/closed
    assignee TEXT,
    due_date TEXT,
    author_id INTEGER, author_name TEXT,
    chat_id INTEGER, message_id INTEGER,
    created_at TEXT,
    supersedes INTEGER,
    note TEXT
);
"""

_db: aiosqlite.Connection | None = None


async def connect() -> None:
    global _db
    _db = await aiosqlite.connect(config.db_path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    # миграция баз, созданных до появления профиля объекта
    for col in ("address", "description"):
        try:
            await _db.execute(f"ALTER TABLE objects ADD COLUMN {col} TEXT")
        except aiosqlite.OperationalError:
            pass
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


def db() -> aiosqlite.Connection:
    assert _db is not None, "db.connect() не вызван"
    return _db


async def fetchall(sql: str, *args) -> list[dict]:
    async with db().execute(sql, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def fetchone(sql: str, *args) -> dict | None:
    async with db().execute(sql, args) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def execute(sql: str, *args) -> int:
    cur = await db().execute(sql, args)
    await db().commit()
    return cur.lastrowid


# --- объекты и пользователи ---

async def bind_chat(chat_id: int, name: str) -> None:
    await execute(
        "INSERT INTO objects (name, chat_id) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name",
        name, chat_id,
    )


async def object_by_chat(chat_id: int) -> dict | None:
    return await fetchone("SELECT * FROM objects WHERE chat_id = ?", chat_id)


async def update_object(object_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    await execute(f"UPDATE objects SET {sets} WHERE id = ?", *fields.values(), object_id)


async def get_object(object_id: int) -> dict | None:
    return await fetchone("SELECT * FROM objects WHERE id = ?", object_id)


async def all_objects() -> list[dict]:
    return await fetchall("SELECT * FROM objects ORDER BY name")


async def upsert_user(user_id: int, name: str, started: bool = False) -> None:
    await execute(
        "INSERT INTO users (user_id, name, started) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
        "started = MAX(started, excluded.started)",
        user_id, name, int(started),
    )


async def is_manager(user_id: int) -> bool:
    if user_id in config.manager_ids:
        return True
    row = await fetchone("SELECT is_manager FROM users WHERE user_id = ?", user_id)
    return bool(row and row["is_manager"])


async def manager_ids() -> list[int]:
    rows = await fetchall("SELECT user_id FROM users WHERE is_manager = 1")
    return sorted(config.manager_ids | {r["user_id"] for r in rows})


async def make_manager_if_none(user_id: int) -> bool:
    """Первый, кто написал /start, становится руководителем, если MANAGER_IDS пуст."""
    if await manager_ids():
        return False
    await execute("UPDATE users SET is_manager = 1 WHERE user_id = ?", user_id)
    return True


# --- сообщения ---

async def save_message(m: dict) -> None:
    await execute(
        "INSERT OR REPLACE INTO messages (chat_id, message_id, user_id, user_name, date, text, kind, "
        "file_id, forward_from, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        m["chat_id"], m["message_id"], m["user_id"], m["user_name"], m["date"], m["text"],
        m["kind"], m["file_id"], m["forward_from"], m["reply_to"],
    )


async def get_message(chat_id: int, message_id: int) -> dict | None:
    return await fetchone(
        "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", chat_id, message_id
    )


async def recent_messages(chat_id: int, limit: int = 20) -> list[dict]:
    rows = await fetchall(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?", chat_id, limit
    )
    return rows[::-1]


async def save_edit(chat_id: int, message_id: int, old: str, new: str, at: str) -> None:
    await execute(
        "INSERT INTO message_edits VALUES (?, ?, ?, ?, ?)", chat_id, message_id, old, new, at
    )
    await execute(
        "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?", new, chat_id, message_id
    )


# --- записи ---

async def add_record(**r) -> int:
    cols = ", ".join(r)
    marks = ", ".join("?" * len(r))
    return await execute(f"INSERT INTO records ({cols}) VALUES ({marks})", *r.values())


async def update_record(record_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    await execute(f"UPDATE records SET {sets} WHERE id = ?", *fields.values(), record_id)


async def get_record(record_id: int) -> dict | None:
    return await fetchone("SELECT * FROM records WHERE id = ?", record_id)


async def active_agreement(object_id: int, topic_key: str) -> dict | None:
    return await fetchone(
        "SELECT * FROM records WHERE object_id = ? AND type = 'agreement' AND topic_key = ? "
        "AND status = 'active' ORDER BY id DESC LIMIT 1",
        object_id, topic_key,
    )


async def records_for_object(object_id: int, only_open: bool = True) -> list[dict]:
    cond = "AND status NOT IN ('superseded', 'rejected', 'done', 'closed')" if only_open else ""
    return await fetchall(
        f"SELECT * FROM records WHERE object_id = ? {cond} ORDER BY id", object_id
    )


async def records_by_message(chat_id: int, message_id: int) -> list[dict]:
    return await fetchall(
        "SELECT * FROM records WHERE chat_id = ? AND message_id = ?", chat_id, message_id
    )


async def agreement_history(object_id: int, topic_key: str) -> list[dict]:
    return await fetchall(
        "SELECT * FROM records WHERE object_id = ? AND type = 'agreement' AND topic_key = ? ORDER BY id",
        object_id, topic_key,
    )


async def tasks_for_assignee(name: str) -> list[dict]:
    return await fetchall(
        "SELECT r.*, o.name AS object_name FROM records r JOIN objects o ON o.id = r.object_id "
        "WHERE r.type = 'task' AND r.status IN ('open', 'accepted') AND r.assignee LIKE ? ORDER BY r.due_date",
        f"%{name}%",
    )
