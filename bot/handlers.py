import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from . import clarify, db, digest, files, llm, modes, notify, people, pipeline, stages, tasks
from .pipeline import thread_of
from .config import config
from .formatting import TYPE_NAME, esc, link, linkify_ids, object_for_llm, record_for_llm, record_line

log = logging.getLogger(__name__)
router = Router()

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
PRIVATE = F.chat.type == ChatType.PRIVATE

HELP = (
    "Я читаю рабочие чаты объектов и собираю договорённости, задачи, проблемы и вопросы.\n\n"
    "<b>Руководителю (в личке):</b> просто задайте вопрос, например «какую плитку согласовал клиент на Ленина 5?»\n"
    "/app — объекты в Mini App · "
    "/digest — сводка сейчас · /objects — подключённые объекты\n\n"
    "<b>В чате объекта:</b>\n"
    "/setup Название — привязать чат к объекту (только руководитель)\n"
    "/brief — что решено по объекту · /my — мои задачи · /object — профиль объекта\n"
    "/stage Заливаем полы — сменить этап объекта (без текста — этап и история)\n"
    "/statuses — в топике «Статусы объектов»: сюда пойдут смены этапов\n"
    "/fio — изменить своё ФИО\n"
    "@бот вопрос — ответ по этому объекту\n"
    "/src_12 — показать исходное сообщение записи №12"
)


# ---------- личка ----------

@router.message(CommandStart(), PRIVATE)
async def start(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.full_name, started=True, username=m.from_user.username)
    became = await db.make_manager_if_none(m.from_user.id)
    extra = "\n\n👑 Вы назначены руководителем." if became else ""
    user = await db.get_user(m.from_user.id)
    if not user.get("fio"):
        await db.execute("UPDATE users SET awaiting_fio = 1 WHERE user_id = ?", m.from_user.id)
        return await m.answer("Здравствуйте! Напишите, пожалуйста, ваше <b>ФИО полностью</b> — "
                              "например: <i>Иванов Иван Петрович</i>.\n"
                              "По нему коллеги смогут ставить вам задачи, а я — присылать их сюда." + extra)
    await m.answer(f"С возвращением, {esc(people.short(user['fio']))}!\n\n" + HELP + extra)


@router.message(Command("fio"), PRIVATE)
async def cmd_fio(m: Message):
    await db.upsert_user(m.from_user.id, m.from_user.full_name, started=True, username=m.from_user.username)
    await db.execute("UPDATE users SET awaiting_fio = 1 WHERE user_id = ?", m.from_user.id)
    user = await db.get_user(m.from_user.id)
    now = f"Сейчас: <b>{esc(user['fio'])}</b>.\n" if user.get("fio") else ""
    await m.answer(f"{now}Напишите ФИО полностью — например: <i>Иванов Иван Петрович</i>.")


async def _save_fio(m: Message) -> bool:
    """Если ждём от человека ФИО — сохраняем. True — сообщение обработано."""
    user = await db.get_user(m.from_user.id)
    if not user or not user.get("awaiting_fio"):
        return False
    fio = people.clean_fio(m.text)
    if not fio:
        await m.answer("Не похоже на ФИО 🙂 Напишите фамилию, имя и отчество через пробел — "
                       "например: <i>Иванов Иван Петрович</i>.")
        return True
    await db.execute("UPDATE users SET fio = ?, awaiting_fio = 0 WHERE user_id = ?", fio, m.from_user.id)
    await m.answer(f"Записал: <b>{esc(fio)}</b> ✅\nТеперь задачу вам можно поставить по имени-отчеству — "
                   f"например: «@бот задачу {esc(people.short(fio).rsplit(' ', 1)[0])} заказать бетон».\n"
                   "Изменить ФИО — /fio\n\n" + HELP)
    return True


@router.message(Command("app"), PRIVATE)
async def cmd_app(m: Message):
    if not config.webapp_url:
        return await m.answer("Mini App ещё не настроен (WEBAPP_URL).")
    from aiogram.types import WebAppInfo
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Открыть объекты", web_app=WebAppInfo(url=config.webapp_url)),
    ]])
    await m.answer("Объекты, этапы, задачи и проблемы — в одном окне:", reply_markup=kb)


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
    tasks = await db.tasks_for_assignee(m.from_user.first_name, m.from_user.id, m.from_user.username)
    if m.chat.type != ChatType.PRIVATE:
        obj = await db.object_by_chat(m.chat.id, thread_of(m))
        tasks = [t for t in tasks if obj and t["object_id"] == obj["id"]]
    lines = [record_line(t, with_object=t["object_name"]) for t in tasks]
    await m.answer("<b>Ваши задачи:</b>\n" + "\n".join(lines) if lines else "Открытых задач нет.")


@router.message(PRIVATE, F.text, ~F.text.startswith("/"))
async def private_question(m: Message):
    """Вопрос в личку. Руководитель — по всем объектам, сотрудник — только по своим чатам."""
    if await _save_fio(m):
        return
    if await db.is_manager(m.from_user.id):
        objects = await db.all_objects()
    else:
        objects = await db.fetchall(
            "SELECT DISTINCT o.* FROM objects o JOIN messages ms ON ms.chat_id = o.chat_id "
            "AND COALESCE(ms.thread_id, 0) = o.thread_id WHERE ms.user_id = ?",
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
            obj = await db.get_object(r["object_id"])
            await bot.send_message(target_chat_id, "👆 Исходное сообщение", **notify.thread_kw(obj),
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
        await m.answer("Привет! Руководитель, привяжите объект: <code>/setup Название объекта</code>.\n"
                       "Если в чате компании есть топики — пишите /setup внутри топика объекта, "
                       "а в топике «Статусы объектов» — /statuses.\n"
                       "Для работы мне нужны права администратора.\n"
                       "Сотрудникам: напишите мне /start в личку и укажите ФИО — так вам смогут ставить задачи.")


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
    await db.bind_chat(m.chat.id, command.args.strip(), thread_of(m))  # в чате компании — топик = объект
    await m.answer(
        f"{'Топик' if thread_of(m) else 'Чат'} привязан к объекту <b>{esc(command.args.strip())}</b>.\n"
        "Я фиксирую договорённости и задачи и почти не пишу в чат — отмечаю реакциями.\n"
        "Скрины и пересылки переписки с клиентом присылайте сюда — так решения будут подтверждёнными.\n"
        "Чтобы я мог писать вам лично — нажмите /start у меня в личке."
    )
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
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
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
    if not obj:
        return
    await m.reply(f"<b>{esc(obj['name'])}</b>\n📍 {esc(obj['address']) or 'адрес не указан'}\n"
                  f"🏠 {esc(obj['description']) or 'описание не указано'}\n"
                  + stages.line(await db.current_stage(obj["id"])))
    if not obj["address"] or not obj["description"]:
        await _ask_object(m, obj)


@router.callback_query(F.data.regexp(r"^clar_skip:\d+$"))
async def cb_clar_skip(c: CallbackQuery, bot: Bot):
    await clarify.dismiss(bot, int(c.data.split(":")[1]))
    await c.answer("Ок, пропускаю")


@router.message(Command("statuses"), GROUPS)
async def cmd_statuses(m: Message):
    """В топике «Статусы объектов»: сюда бот публикует смены этапов всех объектов; без топика — сводка этапов."""
    if not await db.is_manager(m.from_user.id):
        return await m.reply("Настроить топик статусов может только руководитель.")
    await db.set_status_thread(m.chat.id, thread_of(m))
    objs = await db.fetchall("SELECT * FROM objects WHERE chat_id = ? ORDER BY name", m.chat.id)
    lines = [f"• <b>{esc(o['name'])}</b>: {esc(o['stage']) if o.get('stage') else 'этап не указан'}" for o in objs]
    await m.reply("🏗 Сюда буду публиковать смены этапов по всем объектам.\n\n<b>Сейчас:</b>\n"
                  + ("\n".join(lines) or "объекты ещё не привязаны — /setup Название в топике объекта"))


@router.message(Command("stage"), GROUPS)
async def cmd_stage(m: Message, command: CommandObject, bot: Bot):
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
    if not obj:
        return
    if not command.args:
        return await m.reply(await stages.history_text(obj))
    stage = command.args.strip()
    await m.reply(await stages.change(bot, obj, stage[:1].upper() + stage[1:], m.from_user.id,
                                      await people.display(m.from_user.id, m.from_user.full_name), m.chat.id, m.message_id))


@router.callback_query(F.data.regexp(r"^stage:\d+:(yes|no)$"))
async def cb_stage(c: CallbackQuery, bot: Bot):
    _, cid, choice = c.data.split(":")
    prop = await db.fetchone("SELECT * FROM clarifications WHERE id = ? AND kind = 'stage'", int(cid))
    if not prop or prop["status"] != "open":
        return await c.answer("Уже решено")
    await db.execute("UPDATE clarifications SET status = ? WHERE id = ?",
                     "answered" if choice == "yes" else "dismissed", prop["id"])
    if choice == "no":
        await c.message.delete()
        return await c.answer("Ок, этап не меняю")
    obj = await db.get_object(prop["object_id"])
    stage = json.loads(prop["payload"])["stage"]
    text = await stages.change(bot, obj, stage, c.from_user.id, await people.display(c.from_user.id, c.from_user.full_name),
                               prop["chat_id"], prop["source_message_id"])
    await c.message.edit_text(text, reply_markup=None)
    await c.answer("Этап обновлён")


@router.message(Command("brief"), GROUPS)
async def cmd_brief(m: Message):
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
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
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
    if not obj or not m.from_user or m.from_user.is_bot:
        return
    await db.upsert_user(m.from_user.id, m.from_user.full_name, username=m.from_user.username)

    if m.media_group_id:
        # альбом приходит пачкой отдельных сообщений — копим и разбираем одним эпизодом
        key = f"{m.chat.id}:{m.media_group_id}"
        _albums.setdefault(key, []).append(m)
        if key in _album_tasks:
            _album_tasks[key].cancel()
        _album_tasks[key] = asyncio.create_task(_flush_album(key, bot, obj))
        return

    me = await bot.me()
    if not await _is_addressed(m, bot):
        # «уши»: поток читаем молча — записи «👁 замечено ботом», реакции, без сообщений в чат
        await pipeline.process(bot, m, obj)
        return

    msg = await pipeline.ingest(bot, m, strip=f"@{me.username}")
    await _handle_addressed(m, bot, obj, msg)


ALBUM_WAIT = 1.5  # секунд тишины, после которых альбом считается полученным
_albums: dict[str, list[Message]] = {}
_album_tasks: dict[str, asyncio.Task] = {}


async def _flush_album(key: str, bot: Bot, obj: dict) -> None:
    try:
        await asyncio.sleep(ALBUM_WAIT)
    except asyncio.CancelledError:
        return  # пришло ещё фото из этого альбома — ждём дальше
    _album_tasks.pop(key, None)  # дальше не отменяем: разбор уже начался
    msgs = sorted(_albums.pop(key, []), key=lambda x: x.message_id)
    if not msgs:
        return
    try:
        me = await bot.me()
        addressed = any([await _is_addressed(x, bot) for x in msgs])
        msg = await pipeline.ingest_album(bot, msgs, strip=f"@{me.username}" if addressed else None)
        if addressed:
            # отвечаем на сообщение с подписью — оно и есть обращение
            head = next((x for x in msgs if x.caption), msgs[0])
            await _handle_addressed(head, bot, obj, msg)
        else:
            await pipeline.analyze(bot, msg, obj)
    except Exception:
        log.exception("не смог разобрать альбом %s", key)


async def _handle_addressed(m: Message, bot: Bot, obj: dict, msg: dict) -> None:
    me = await bot.me()
    rm = m.reply_to_message
    to_bot = bool(rm and rm.from_user and rm.from_user.id == me.id)

    # ответ на черновик «зафиксировать как…» или на уточняющий вопрос бота
    if to_bot:
        draft = await db.fetchone(
            "SELECT * FROM clarifications WHERE chat_id = ? AND ask_message_id = ? AND status = 'open' AND kind = 'draft'",
            m.chat.id, rm.message_id,
        )
        if draft:
            await modes.answer_draft(bot, m, msg, obj, draft)
            return
        # «сделал» / «отбой» реплаем на карточку задачи — закрываем без кнопки и без руководителя
        card_tasks = await tasks.tasks_by_message(m.chat.id, rm.message_id)
        if card_tasks:
            action = tasks.quick_word(msg["text"])
            if action:
                changed, denied = await tasks.apply(action, card_tasks, m.from_user.id, m.from_user.full_name)
                await m.reply(tasks.summary(action, changed, denied))
            else:
                await modes.close_flow(bot, m, msg, obj, candidates=card_tasks)
            return
        clar = await clarify.find_for_answer(msg)
        if clar and await clarify.resolve(bot, clar, msg):
            await pipeline._react(bot, msg, "✍")
            return

    # тег реплаем на файл/фото/голосовое: «зафиксируй» — фиксируем его содержимое, иначе — отвечаем про него
    attached = msg.get("document") or ""
    if not attached and rm and not to_bot:
        attached = await _replied_content(bot, m.chat.id, rm)
        if attached and modes.detect(msg["text"]) in ("fix", "task"):
            msg["text"] = f"{msg['text']}\n[Сообщение, на которое ответили]: {attached}"
            attached = ""

    stage = stages.parse_tag(msg["text"]) if not attached else None
    if stage is not None:  # «@бот этап: заливаем полы» — до режимов, иначе «статус…» уйдёт в запрос контекста
        if not stage:
            return await m.reply("Какой этап? Например: <code>@бот этап: заливаем полы</code>")
        return await m.reply(await stages.change(bot, obj, stage, m.from_user.id, await people.display(m.from_user.id, m.from_user.full_name),
                                                 m.chat.id, m.message_id))

    if not attached and await modes.handle(bot, m, msg, obj):
        return

    question = re.sub(re.escape(f"@{me.username}"), "", m.text or m.caption or "", flags=re.IGNORECASE).strip()
    # только данные этого объекта — другие объекты в контекст не попадают вообще
    text, kb = await _answer(question, [obj], attached)
    await _reply_long(m, text, kb)


@router.callback_query(F.data.regexp(r"^done:\d+$"))
async def cb_done(c: CallbackQuery, bot: Bot):
    r = await db.get_record(int(c.data.split(":")[1]))
    if not r or r["type"] != "task":
        return await c.answer("Задача не найдена")
    uname = (c.from_user.username or "").lower()
    is_assignee = str(r.get("assignee_user_id") or "") == str(c.from_user.id) or \
        (uname and (r["assignee"] or "").lower().lstrip("@") == uname)
    # отметить выполнение может любой участник чата объекта — руководитель не нужен
    in_chat = await db.fetchone("SELECT 1 FROM messages WHERE chat_id = ? AND user_id = ? LIMIT 1",
                                r["chat_id"], c.from_user.id)
    if not (is_assignee or in_chat or await db.is_manager(c.from_user.id)):
        return await c.answer("Отметить может участник чата объекта", show_alert=True)
    if r["status"] in ("done", "cancelled"):
        return await c.answer("Уже выполнено")
    await db.update_record(r["id"], status="done", note=f"выполнено: {c.from_user.full_name}")
    stamp = f"\n\n✅ <b>Выполнено</b> — {esc(c.from_user.full_name)}, {datetime.now(config.tz):%d.%m %H:%M}"
    try:
        await c.message.edit_text(c.message.html_text + stamp, reply_markup=None)
    except Exception:
        pass
    if c.message.chat.id != r["chat_id"]:  # подтвердили из лички — сообщим в чат объекта
        await notify.to_object(bot, await db.get_object(r["object_id"]),
                               f"✅ {esc(r['title'] or r['value'])} — выполнено ({esc(c.from_user.full_name)})")
    await c.answer("Отмечено как выполнено")


@router.callback_query(F.data.regexp(r"^draft:\d+:(problem|task|agreement|manager|cancel)$"))
async def cb_draft(c: CallbackQuery, bot: Bot):
    _, draft_id, choice = c.data.split(":")
    draft = await db.fetchone("SELECT * FROM clarifications WHERE id = ? AND kind = 'draft'", int(draft_id))
    if not draft or draft["status"] != "open":
        return await c.answer("Уже разобрано")
    if c.from_user.id != draft["author_id"] and not await db.is_manager(c.from_user.id):
        return await c.answer("Выбрать может автор сообщения или руководитель", show_alert=True)
    if choice == "cancel":
        await clarify.dismiss(bot, draft["id"])
        return await c.answer("Отменено")
    if choice == "manager":
        obj = await db.get_object(draft["object_id"])
        src = await db.get_message(draft["chat_id"], draft["source_message_id"])
        await notify.to_managers(bot, f"❓ <b>{esc(obj['name'])}: вопрос руководителю</b>\n"
                                      f"{esc(src['user_name'])}: {esc(src['text'][:800])}\n{link(src)}")
        await db.execute("UPDATE clarifications SET status = 'answered' WHERE id = ?", draft["id"])
        await c.message.edit_text("❓ Передал вопрос руководителю.", reply_markup=None)
        return await c.answer("Передано")
    await modes.finish_draft(bot, c.message, draft, choice)
    await c.answer("Зафиксировано")


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
    obj = await db.object_by_chat(m.chat.id, thread_of(m))
    if obj:
        await pipeline.process_edit(bot, m, obj)
