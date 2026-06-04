from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    club_url: str
    club_slug: str
    chat_url: str | None
    timezone: ZoneInfo
    db_path: str
    database_url: str | None
    menu_text: str
    menu_photo_path: str | None
    guide_text: str
    group_chat_id: int | None
    schedule_message_id: int | None
    schedule_thread_id: int | None
    admin_ids: set[int]


def _optional_int(value: str | None) -> int | None:
    if not value:
        return None
    return int(value)


def _int_set(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def _club_slug_from_url(club_url: str) -> str:
    path_parts = [part for part in urlparse(club_url).path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] == "clubs":
        return path_parts[1]
    raise RuntimeError("CLUB_URL must look like https://mangabuff.ru/clubs/fu-razvrat")


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN in .env")

    club_url = os.getenv("CLUB_URL", "https://mangabuff.ru/clubs/fu-razvrat").strip()
    default_menu_text = (
        "[ Гений, миллиардер и просто красивый мужчина: @reeigans]\n\n"
        "Вас приветствует бот клуба \"Keepers of Oneiroi\" Он создан для вашего "
        "удобства и слаженной работы над клубом."
    )
    placeholder_menu_text = "Текст главного меню. Его можно заменить в .env."
    menu_text = os.getenv("MENU_TEXT") or default_menu_text
    if menu_text.strip() == placeholder_menu_text:
        menu_text = default_menu_text
    menu_text = menu_text.replace("\\n", "\n")

    return Config(
        bot_token=token,
        club_url=club_url,
        club_slug=os.getenv("CLUB_SLUG") or _club_slug_from_url(club_url),
        chat_url=os.getenv("CHAT_URL") or None,
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        db_path=os.getenv("DB_PATH", "bot.db"),
        database_url=os.getenv("DATABASE_URL") or None,
        menu_text=menu_text,
        menu_photo_path=os.getenv("MENU_PHOTO_PATH") or str(Path("assets") / "menu.jpg"),
        guide_text=os.getenv(
            "GUIDE_TEXT",
            "Нажмите «Запись на вклады» и выберите свободное время на сегодня или завтра.",
        ),
        group_chat_id=_optional_int(os.getenv("GROUP_CHAT_ID")),
        schedule_message_id=_optional_int(os.getenv("SCHEDULE_MESSAGE_ID")),
        schedule_thread_id=_optional_int(os.getenv("SCHEDULE_THREAD_ID")),
        admin_ids=_int_set(os.getenv("ADMIN_IDS")),
    )
