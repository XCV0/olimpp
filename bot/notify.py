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
