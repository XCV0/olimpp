import os
from dataclasses import dataclass
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    llm_key: str
    llm_model: str
    llm_fallback_model: str
    llm_base_url: str
    manager_ids: frozenset[int]
    db_path: str
    tz: timezone
    digest_hour: int


def load() -> Config:
    ids = os.getenv("MANAGER_IDS", "").replace(" ", "")
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        llm_key=os.getenv("OPENROUTER_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "deepseek/deepseek-v4.1-flash"),
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "x-ai/grok-4.3"),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        manager_ids=frozenset(int(x) for x in ids.split(",") if x),
        db_path=os.getenv("DB_PATH", "bot.db"),
        tz=timezone(timedelta(hours=int(os.getenv("TIMEZONE_OFFSET", "3")))),
        digest_hour=int(os.getenv("DIGEST_HOUR", "9")),
    )


config = load()
