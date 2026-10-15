import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonWebApp, WebAppInfo

from . import clarify, db, digest, media, webapp
from .config import config
from .handlers import router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.connect()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="brief", description="Что решено по объекту"),
        BotCommand(command="my", description="Мои задачи"),
        BotCommand(command="digest", description="Сводка (руководитель)"),
        BotCommand(command="setup", description="Привязать чат к объекту"),
        BotCommand(command="object", description="Профиль объекта"),
        BotCommand(command="stage", description="Этап объекта (сменить или посмотреть)"),
        BotCommand(command="statuses", description="Этот топик — для статусов всех объектов"),
    ])
    asyncio.create_task(digest.scheduler(bot))
    asyncio.create_task(clarify.expire_loop(bot))
    asyncio.create_task(media.warmup())
    runner = await webapp.start(bot)
    if config.webapp_url:
        # кнопка «Объекты» слева от поля ввода в личке с ботом
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Объекты", web_app=WebAppInfo(url=config.webapp_url)))
        await bot.set_my_commands([
            BotCommand(command="app", description="Открыть объекты (Mini App)"),
            BotCommand(command="fio", description="Указать или изменить ФИО"),
            BotCommand(command="my", description="Мои задачи"),
            BotCommand(command="digest", description="Сводка (руководитель)"),
        ], scope=BotCommandScopeAllPrivateChats())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await runner.cleanup()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
