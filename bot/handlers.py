import logging
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from . import clarify, db, digest, files, llm, pipeline
from .formatting import TYPE_NAME, esc, linkify_ids, object_for_llm, record_for_llm, record_line

log = logging.getLogger(__name__)
router = Router()

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
PRIVATE = F.chat.type == ChatType.PRIVATE

HELP = (
    "Я читаю рабочие чаты объектов и собираю договорённости, задачи, проблемы и вопросы.\n\n"
    "<b>Руководителю (в личке):</b> просто задайте вопрос, например «какую плитку согласовал клиент на Ленина 5?»\n"
    "/digest — сводка сейчас · /objects — подключённые объекты\n\n"
    "<b>В чате объекта:</b>\n"
    "/setup Название — привязать чат к объекту (только руководитель)\n"
    "/brief — что решено по объекту · /my — мои задачи · /object — профиль объекта\n"
    "@бот вопрос — ответ по этому объекту\n"
    "/src_12 — показать исходное сообщение записи №12"
)


# ---------- личка ----------

@router.message(CommandStart(), PRIVATE)
async def start(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.full_name, started=True)
    became = await db.make_manager_if_none(m.from_user.id)
    extra = "\n\n👑 Вы назначены руководителем." if became else ""
    await m.answer(HELP + extra)


@router.message(Command("digest"), PRIVATE)
async def cmd_digest(m: Message):
    if not await db.is_manager(m.from_user.id):
        return await m.answer("Сводка доступна только руководителю.")
    await m.answer(await digest.build_digest())


@router.message(Command("objects"), PRIVATE)
async def cmd_objects(m: Message):
    if not await db.is_manager(m.from_user.id):
        return
    objs = await db.all_objects()
    await m.answer("\n".join(f"• {esc(o['name'])}" for o in objs) or "Пока нет подключённых чатов.")


@router.message(Command("my"))
async def cmd_my(m: Message):
    # TODO: сопоставление «Серёга» ↔ профиль Telegram; пока поиск по имени
    tasks = await db.tasks_for_assignee(m.from_user.first_name)
    if m.chat.type != ChatType.PRIVATE:
        obj = await db.object_by_chat(m.chat.id)
        tasks = [t for t in tasks if obj and t["object_id"] == obj["id"]]
    lines = [record_line(t, with_object=t["object_name"]) for t in tasks]
    await m.answer("<b>Ваши задачи:</b>\n" + "\n".join(lines) if lines else "Открытых задач нет.")


@router.message(PRIVATE, F.text, ~F.text.startswith("/"))
async def private_question(m: Message):
    """Вопрос в личку. Руководитель — по всем объектам, сотрудник — только по своим чатам."""
    if await db.is_manager(m.from_user.id):
        objects = await db.all_objects()
    else:
        objects = await db.fetchall(
            "SELECT DISTINCT o.* FROM objects o JOIN messages ms ON ms.chat_id = o.chat_id WHERE ms.user_id = ?",
            m.from_user.id,
        )
    if not objects:
        return await m.answer("Нет объектов, по которым я могу ответить.")
    await m.bot.send_chat_action(m.chat.id, "typing")
    text, kb = await _answer(m.text, objects)
    await m.answer(text, reply_markup=kb)


def sources_kb(ids: list[int]) -> InlineKeyboardMarkup | None:
    if not ids:
        return None
    buttons = [InlineKeyboardButton(text=f"📎 #{i}", callback_data=f"src:{i}") for i in ids[:9]]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)])


async def _can_see(user_id: int, r: dict) -> bool:
    if await db.is_manager(user_id):
        return True
    row = await db.fetchone(
        "SELECT 1 FROM messages WHERE chat_id = ? AND user_id = ? LIMIT 1", r["chat_id"], user_id
    )
    return row is not None


async def send_source(bot: Bot, user_id: int, target_chat_id: int, record_id: int) -> str | None:
    """Показывает исходное сообщение записи. Работает и в обычных группах, где ссылок нет:
    в том же чате — ответ на оригинал, в другом чате — пересылка оригинала. Возвращает ошибку или None."""
    r = await db.get_record(record_id)
    if not r or not await _can_see(user_id, r):
        return "Запись не найдена"
    try:
        if target_chat_id == r["chat_id"]:
            await bot.send_message(target_chat_id, "👆 Исходное сообщение",
                                   reply_parameters=ReplyParameters(message_id=r["message_id"]))
        else:
            await bot.forward_message(target_chat_id, r["chat_id"], r["message_id"])
    except Exception as e:
        log.warning("не смог показать источник #%s: %s", record_id, e)
        return "Исходное сообщение удалено или недоступно боту"
    return None


@router.message(F.text.regexp(r"^/src(?:_|\s+)(\d+)"))
async def cmd_src(m: Message, bot: Bot):
    record_id = int(m.text.split("@")[0].replace("_", " ").split()[1])
    err = await send_source(bot, m.from_user.id, m.chat.id, record_id)
    if err:
        await m.reply(err)


@router.callback_query(F.data.regexp(r"^src:\d+$"))
async def cb_src(c: CallbackQuery, bot: Bot):
    err = await send_source(bot, c.from_user.id, c.message.chat.id, int(c.data.split(":")[1]))
    await c.answer(err or "")


async def _answer(question: str, objects: list[dict], attached: str = "") -> tuple[str, InlineKeyboardMarkup | None]:
    records: dict[int, dict] = {}
    lines = []
    for o in objects:
        lines.append(f"[{o['name']}] ОБЪЕКТ: {object_for_llm(o)}")
        # с историей версий, чтобы бот мог показать «было → стало»
        for r in await db.records_for_object(o["id"], only_open=False):
            records[r["id"]] = r
            lines.append(f"[{o['name']}] {record_for_llm(r)}")
    # TODO: при росте базы — предварительный отбор записей поиском, а не все подряд
    reply = await llm.answer(question, "\n".join(lines[-400:]) or "записей нет", attached)
    text, used = linkify_ids(reply, records)
    return text, sources_kb(used)


# ---------- кнопки руководителя ----------

@router.callback_query(F.data.regexp(r"^(confirm|reject):\d+$"))
async def decision(c: CallbackQuery):
    if not await db.is_manager(c.from_user.id):
        return await c.answer("Только руководитель", show_alert=True)
    action, rid = c.data.split(":")
    r = await db.get_record(int(rid))
    if not r:
        return await c.answer("Запись не найдена")
    if action == "confirm":
        await db.update_record(r["id"], confidence="confirmed", status="active",
                               note=f"подтвердил {c.from_user.full_name}")
        if r["supersedes"]:
            await db.update_record(r["supersedes"], status="superseded")
        await c.answer("Подтверждено")
    else:
        await db.update_record(r["id"], status="rejected", note=f"отклонил {c.from_user.full_name}")
        if r["supersedes"]:
            await db.update_record(r["supersedes"], status="active")
        await c.answer("Отклонено, прежняя версия восстановлена" if r["supersedes"] else "Отклонено")
    await c.message.edit_reply_markup(reply_markup=None)


# ---------- группы ----------

@router.message(GROUPS, F.new_chat_members)
async def added_to_group(m: Message, bot: Bot):
    me = await bot.me()
    if any(u.id == me.id for u in m.new_chat_members):
        await m.answer("Привет! Руководитель, привяжите чат к объекту: <code>/setup Название объекта</code>.\n"
                       "Для работы мне нужны права администратора.")


@router.message(GROUPS, F.migrate_to_chat_id)
async def migrated(m: Message):
    # обычная группа превратилась в супергруппу: у неё новый id, и с этого момента работают ссылки
    await db.execute("UPDATE objects SET chat_id = ? WHERE chat_id = ?", m.migrate_to_chat_id, m.chat.id)
    log.info("чат %s мигрировал в супергруппу %s", m.chat.id, m.migrate_to_chat_id)


@router.message(Command("setup"), GROUPS)
async def cmd_setup(m: Message, command: CommandObject):
    if not await db.is_manager(m.from_user.id):
        return await m.reply("Привязать чат может только руководитель (сначала /start мне в личку).")
    if not command.args:
        return await m.reply("Укажите название: <code>/setup Ленина 5</code>")
    await db.bind_chat(m.chat.id, command.args.strip())
    await m.answer(
        f"Чат привязан к объекту <b>{esc(command.args.strip())}</b>.\n"
        "Я фиксирую договорённости и задачи и почти не пишу в чат — отмечаю реакциями.\n"
        "Скрины и пересылки переписки с клиентом присылайте сюда — так решения будут подтверждёнными.\n"
        "Чтобы я мог писать вам лично — нажмите /start у меня в личке."
    )
    obj = await db.object_by_chat(m.chat.id)
    if not obj["address"] or not obj["description"]:
        await _ask_object(m, obj)


async def _ask_object(m: Message, obj: dict) -> None:
    await clarify.ask(
        m.bot, chat_id=m.chat.id, object_id=obj["id"], source_message_id=m.message_id,
        author_id=m.from_user.id, kind="object",
        question="Какой адрес у объекта и что это за объект? (квартира / дом / офис, какие работы, клиент)",
    )


@router.message(Command("object"), GROUPS)
async def cmd_object(m: Message):
    obj = await db.object_by_chat(m.chat.id)
    if not obj:
        return
    await m.reply(f"<b>{esc(obj['name'])}</b>\n📍 {esc(obj['address']) or 'адрес не указан'}\n"
                  f"🏠 {esc(obj['description']) or 'описание не указано'}")
    if not obj["address"] or not obj["description"]:
        await _ask_object(m, obj)


@router.callback_query(F.data.regexp(r"^clar_skip:\d+$"))
async def cb_clar_skip(c: CallbackQuery, bot: Bot):
    await clarify.dismiss(bot, int(c.data.split(":")[1]))
    await c.answer("Ок, пропускаю")


@router.message(Command("brief"), GROUPS)
async def cmd_brief(m: Message):
    obj = await db.object_by_chat(m.chat.id)
    if obj:
        await m.answer(await digest.build_brief(obj))


async def _is_addressed(m: Message, bot: Bot) -> bool:
    me = await bot.me()
    text = m.text or m.caption or ""
    replied_to_me = m.reply_to_message and m.reply_to_message.from_user \
        and m.reply_to_message.from_user.id == me.id
    return f"@{me.username}".lower() in text.lower() or bool(replied_to_me)


@router.message(GROUPS)
async def group_message(m: Message, bot: Bot):
    obj = await db.object_by_chat(m.chat.id)
    if not obj or not m.from_user or m.from_user.is_bot:
        return
    await db.upsert_user(m.from_user.id, m.from_user.full_name)

    me = await bot.me()
    addressed = await _is_addressed(m, bot)
    # любое сообщение, в т.ч. обращение к боту, идёт через конвейер извлечения
    result = await pipeline.process(bot, m, obj, strip=f"@{me.username}", addressed=addressed)
    # ответ на уточняющий вопрос: запись дополнена, вопрос удалён, реакция ✍ — больше ничего не пишем
    if not addressed or result.get("clarified"):
        return

    # вопрос к самому боту — не «открытый вопрос» чата, его не храним, а отвечаем
    for r in result["records"]:
        if r["type"] == "question":
            await db.execute("DELETE FROM records WHERE id = ?", r["id"])
    saved = [r for r in result["records"] if r["type"] != "question"]

    lines = [f"{TYPE_NAME[r['type']]} — {record_line(r)}" for r in saved]
    lines += [f"✅ Закрыто: {esc(r['title'])}" for r in result["closed"]]
    recorded = ("Записал:\n" + "\n".join(lines)) if lines else ""

    # документ в этом сообщении или в сообщении, на которое ответили, — вопрос про него
    attached = result.get("document") or ""
    rm = m.reply_to_message
    if not attached and rm and not (rm.from_user and rm.from_user.id == me.id):
        attached = await _replied_content(bot, m.chat.id, rm)

    if recorded and not attached:
        await m.reply(recorded)
        return
    question = re.sub(re.escape(f"@{me.username}"), "", m.text or m.caption or "", flags=re.IGNORECASE).strip()
    # только данные этого объекта — другие объекты в контекст не попадают вообще
    text, kb = await _answer(question, [obj], attached)
    if recorded:
        text += "\n\n" + recorded
    await _reply_long(m, text, kb)


async def _replied_content(bot: Bot, chat_id: int, rm: Message) -> str:
    """Текст сообщения, на которое ответили. Если это файл, а бот его не сохранил
    (например, был выключен, когда файл прислали) — скачиваем и читаем сейчас."""
    saved = await db.get_message(chat_id, rm.message_id)
    if saved and (not rm.document or "[Файл «" in (saved["text"] or "")):
        return saved["text"] or ""
    text = rm.text or rm.caption or ""
    if rm.document:
        doc = await files.read_document(bot, rm.document)
        if doc:
            text = f"{text}\n[Файл «{rm.document.file_name}»]:\n{doc}".strip()
            if saved:
                await db.execute("UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
                                 text, chat_id, rm.message_id)
        elif not files.supported(rm.document):
            text += f"\n[Файл «{rm.document.file_name}» — формат не поддерживается]"
    return text


async def _reply_long(m: Message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    """Telegram режет сообщения на 4096 символов — делим по строкам, кнопки к последнему куску."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000 and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, chunk in enumerate(chunks):
        await m.reply(chunk, reply_markup=kb if i == len(chunks) - 1 else None)


@router.edited_message(GROUPS)
async def group_edit(m: Message, bot: Bot):
    obj = await db.object_by_chat(m.chat.id)
    if obj:
        await pipeline.process_edit(bot, m, obj)
