import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import db

log = logging.getLogger(__name__)


def decision_kb(record_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm:{record_id}"),
        InlineKeyboardButton(text="❌ Ошибка", callback_data=f"reject:{record_id}"),
    ]])


async def to_managers(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    for uid in await db.manager_ids():
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:  # руководитель ещё не нажал /start у бота
            log.warning("не смог написать руководителю %s: %s", uid, e)


def thread_kw(obj: dict) -> dict:
    """Параметры отправки в топик объекта (у обычного чата топика нет)."""
    return {"message_thread_id": obj["thread_id"]} if obj.get("thread_id") else {}


async def to_object(bot: Bot, obj: dict, text: str, **kw):
    """Сообщение в чат объекта — в его топик, если чат компании разбит на топики."""
    return await bot.send_message(obj["chat_id"], text, **thread_kw(obj), **kw)


async def to_status_topic(bot: Bot, obj: dict, text: str) -> None:
    """Топик «Статусы объектов» в чате компании (/statuses) — лента смен этапов по всем объектам."""
    thread = await db.status_thread(obj["chat_id"])
    if thread is None or thread == obj.get("thread_id"):
        return
    try:
        await bot.send_message(obj["chat_id"], text, **({"message_thread_id": thread} if thread else {}))
    except Exception as e:
        log.warning("не смог написать в топик статусов: %s", e)
