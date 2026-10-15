import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from . import db, digest, llm, pipeline
from .formatting import esc, linkify_ids, record_for_llm, record_line

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
    "/brief — что решено по объекту · /my — мои задачи\n"
    "@бот вопрос — ответ по этому объекту"
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
    await m.answer(await _answer(m.text, objects))


async def _answer(question: str, objects: list[dict]) -> str:
    records: dict[int, dict] = {}
    lines = []
    for o in objects:
        # с историей версий, чтобы бот мог показать «было → стало»
        for r in await db.records_for_object(o["id"], only_open=False):
            records[r["id"]] = r
            lines.append(f"[{o['name']}] {record_for_llm(r)}")
    # TODO: при росте базы — предварительный отбор записей поиском, а не все подряд
    reply = await llm.answer(question, "\n".join(lines[-400:]) or "записей нет")
    return linkify_ids(reply, records)


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

    if await _is_addressed(m, bot):
        me = await bot.me()
        question = (m.text or "").replace(f"@{me.username}", "").strip()
        # только данные этого объекта — другие объекты в контекст не попадают вообще
        await m.reply(await _answer(question, [obj]))
        return
    await pipeline.process(bot, m, obj)


@router.edited_message(GROUPS)
async def group_edit(m: Message, bot: Bot):
    obj = await db.object_by_chat(m.chat.id)
    if obj:
        await pipeline.process_edit(bot, m, obj)
