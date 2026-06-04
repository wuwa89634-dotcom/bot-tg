from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    club_url: str
    club_slug: str
    timezone: ZoneInfo
    db_path: str
    database_url: str | None
    menu_text: str
    menu_photo_path: str | None
    guide_text: str
    group_chat_id: int | None
    schedule_message_id: int | None
    schedule_thread_id: int | None


def _optional_int(value: str | None) -> int | None:
    if not value:
        return None
    return int(value)


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env")

    return Config(
        bot_token=token,
        club_url=os.getenv("CLUB_URL", "https://mangabuff.ru/clubs/fu-razvrat"),
        club_slug=os.getenv("CLUB_SLUG", "fu-razvrat"),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        db_path=os.getenv("DB_PATH", "bot.db"),
        database_url=os.getenv("DATABASE_URL") or None,
        menu_text=os.getenv("MENU_TEXT", "Главное меню клуба."),
        menu_photo_path=os.getenv("MENU_PHOTO_PATH") or None,
        guide_text=os.getenv(
            "GUIDE_TEXT",
            "Нажмите «Запись на вклады» и выберите свободное время на сегодня или завтра.",
        ),
        group_chat_id=_optional_int(os.getenv("GROUP_CHAT_ID")),
        schedule_message_id=_optional_int(os.getenv("SCHEDULE_MESSAGE_ID")),
        schedule_thread_id=_optional_int(os.getenv("SCHEDULE_THREAD_ID")),
    )
