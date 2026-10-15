"""SQLite-хранилище. Все выборки записей идут с фильтром по object_id —
изоляция объектов обеспечивается здесь, а не в промпте."""

import aiosqlite

from .config import config

SCHEMA = """
-- объект = чат компании + топик (thread_id); 0 — обычный чат без топиков
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL DEFAULT 0,
    address TEXT,
    description TEXT,         -- что за объект: квартира/дом/офис, какие работы, клиент
    stage TEXT,
    UNIQUE (chat_id, thread_id)
);
-- чат компании: в какой топик бот публикует статусы объектов
CREATE TABLE IF NOT EXISTS company_chats (
    chat_id INTEGER PRIMARY KEY,
    status_thread_id INTEGER
);
-- история этапов объекта («заливаем полы», «штукатурка», «сдан»)
CREATE TABLE IF NOT EXISTS object_stages (
    id INTEGER PRIMARY KEY,
    object_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    user_id INTEGER, user_name TEXT,
    chat_id INTEGER, message_id INTEGER,
    created_at TEXT
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
    # objects до топиков: chat_id был UNIQUE — пересобираем таблицу с ключом (chat_id, thread_id)
    cols = [r[1] for r in await (await _db.execute("PRAGMA table_info(objects)")).fetchall()]
    if "thread_id" not in cols:
        for col in ("address", "description", "stage"):
            if col not in cols:
                await _db.execute(f"ALTER TABLE objects ADD COLUMN {col} TEXT")
        await _db.executescript("""
            CREATE TABLE objects_new (id INTEGER PRIMARY KEY, name TEXT NOT NULL, chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0, address TEXT, description TEXT, stage TEXT,
                UNIQUE (chat_id, thread_id));
            INSERT INTO objects_new (id, name, chat_id, thread_id, address, description, stage)
                SELECT id, name, chat_id, 0, address, description, stage FROM objects;
            DROP TABLE objects;
            ALTER TABLE objects_new RENAME TO objects;
        """)
    for table, col, kind in (("messages", "thread_id", "INTEGER DEFAULT 0"), ("users", "fio", "TEXT"),
                             ("users", "awaiting_fio", "INTEGER DEFAULT 0")):
        try:
            await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        except aiosqlite.OperationalError:
            pass
    # миграции баз, созданных до появления новых полей
    for table, col in (("objects", "address"), ("objects", "description"), ("objects", "stage"), ("users", "username"),
                       ("records", "source"), ("records", "details"), ("records", "assignee_user_id"),
                       ("records", "card_message_id"),
                       ("clarifications", "payload")):
        try:
            await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
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

async def bind_chat(chat_id: int, name: str, thread_id: int = 0) -> None:
    await execute(
        "INSERT INTO objects (name, chat_id, thread_id) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id, thread_id) DO UPDATE SET name = excluded.name",
        name, chat_id, thread_id,
    )


async def object_by_chat(chat_id: int, thread_id: int = 0) -> dict | None:
    return await fetchone("SELECT * FROM objects WHERE chat_id = ? AND thread_id = ?", chat_id, thread_id)


async def set_status_thread(chat_id: int, thread_id: int) -> None:
    await execute(
        "INSERT INTO company_chats (chat_id, status_thread_id) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET status_thread_id = excluded.status_thread_id", chat_id, thread_id,
    )


async def status_thread(chat_id: int) -> int | None:
    row = await fetchone("SELECT status_thread_id FROM company_chats WHERE chat_id = ?", chat_id)
    return row["status_thread_id"] if row else None


async def get_user(user_id: int) -> dict | None:
    return await fetchone("SELECT * FROM users WHERE user_id = ?", user_id)


async def employees() -> list[dict]:
    """Сотрудники с ФИО — по ним ставят задачи «по имени-отчеству»."""
    return await fetchall("SELECT * FROM users WHERE fio IS NOT NULL AND fio != '' ORDER BY fio")


async def update_object(object_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    await execute(f"UPDATE objects SET {sets} WHERE id = ?", *fields.values(), object_id)


async def get_object(object_id: int) -> dict | None:
    return await fetchone("SELECT * FROM objects WHERE id = ?", object_id)


async def set_stage(object_id: int, stage: str, user_id: int, user_name: str,
                    chat_id: int, message_id: int, at: str) -> dict | None:
    """Меняет этап объекта и пишет историю. Возвращает предыдущий этап (или None)."""
    prev = await current_stage(object_id)
    await execute(
        "INSERT INTO object_stages (object_id, stage, user_id, user_name, chat_id, message_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", object_id, stage, user_id, user_name, chat_id, message_id, at,
    )
    await execute("UPDATE objects SET stage = ? WHERE id = ?", stage, object_id)  # для контекста LLM
    return prev


async def current_stage(object_id: int) -> dict | None:
    return await fetchone("SELECT * FROM object_stages WHERE object_id = ? ORDER BY id DESC LIMIT 1", object_id)


async def stage_history(object_id: int, limit: int = 10) -> list[dict]:
    return await fetchall(
        "SELECT * FROM object_stages WHERE object_id = ? ORDER BY id DESC LIMIT ?", object_id, limit
    )


async def all_objects() -> list[dict]:
    return await fetchall("SELECT * FROM objects ORDER BY name")


async def upsert_user(user_id: int, name: str, started: bool = False, username: str | None = None) -> None:
    await execute(
        "INSERT INTO users (user_id, name, started, username) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, "
        "started = MAX(started, excluded.started), username = COALESCE(excluded.username, username)",
        user_id, name, int(started), (username or "").lower() or None,
    )


async def user_by_username(username: str) -> dict | None:
    return await fetchone("SELECT * FROM users WHERE username = ?", username.lstrip("@").lower())


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
        "file_id, forward_from, reply_to, thread_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        m["chat_id"], m["message_id"], m["user_id"], m["user_name"], m["date"], m["text"],
        m["kind"], m["file_id"], m["forward_from"], m["reply_to"], m.get("thread_id") or 0,
    )


async def get_message(chat_id: int, message_id: int) -> dict | None:
    return await fetchone(
        "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", chat_id, message_id
    )


async def recent_messages(chat_id: int, limit: int = 20, thread_id: int = 0) -> list[dict]:
    # только свой топик: контекст одного объекта не должен утекать в разбор другого
    rows = await fetchall(
        "SELECT * FROM messages WHERE chat_id = ? AND COALESCE(thread_id, 0) = ? ORDER BY message_id DESC LIMIT ?",
        chat_id, thread_id, limit,
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
    cond = "AND status NOT IN ('superseded', 'rejected', 'done', 'closed', 'cancelled')" if only_open else ""
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


async def tasks_for_assignee(name: str, user_id: int | None = None, username: str | None = None) -> list[dict]:
    return await fetchall(
        "SELECT r.*, o.name AS object_name FROM records r JOIN objects o ON o.id = r.object_id "
        "WHERE r.type = 'task' AND r.status IN ('open', 'accepted') "
        "AND (r.assignee LIKE ? OR r.assignee_user_id = ? OR (? != '' AND lower(r.assignee) = ?)) ORDER BY r.due_date",
        f"%{name}%", str(user_id or ""), (username or "").lower(), f"@{(username or '').lower()}",
    )
