from __future__ import annotations

import asyncio
import html
import logging
import secrets
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config, load_config
from mangabuff import check_profile_in_club, parse_profile_url
from storage import Booking, ChatMember, ClubUser, Giveaway, Storage


logging.basicConfig(level=logging.INFO)
router = Router()
config: Config
storage: Storage


class GiveawayCreation(StatesGroup):
    title = State()
    media = State()
    description = State()
    winner_count = State()
    ends_at = State()
    preview = State()
    edit_menu = State()


def now_moscow() -> datetime:
    return datetime.now(config.timezone)


def current_slot_start() -> datetime:
    return now_moscow().replace(minute=0, second=0, microsecond=0)


def booking_window_bounds() -> tuple[datetime, datetime]:
    start = current_slot_start() + timedelta(hours=1)
    return start, start + timedelta(hours=48)


def booking_window_slots() -> list[datetime]:
    start, end = booking_window_bounds()
    return [
        start + timedelta(hours=offset)
        for offset in range(int((end - start).total_seconds() // 3600))
    ]


def booking_window_dates() -> list[date]:
    return list(dict.fromkeys(slot.date() for slot in booking_window_slots()))


def booking_slot_datetime(booking_date: date, hour: int) -> datetime:
    return datetime(
        booking_date.year,
        booking_date.month,
        booking_date.day,
        hour,
        tzinfo=config.timezone,
    )


def is_bookable_slot(booking_date: date, hour: int) -> bool:
    slot = booking_slot_datetime(booking_date, hour)
    start, end = booking_window_bounds()
    return start <= slot < end


def slot_label(hour: int) -> str:
    return f"{hour:02d}:00 - {(hour + 1) % 24:02d}:00"


def user_label(user: ClubUser | Booking) -> str:
    username = f"@{user.username}" if user.username else f"id{user.telegram_id}"
    return f"{username} ({user.display_name})"


def html_mention(user: ChatMember | ClubUser) -> str:
    if user.username:
        label = f"@{user.username}"
    elif isinstance(user, ClubUser):
        label = user.display_name
    else:
        label = user.full_name
    return f'<a href="tg://user?id={user.telegram_id}">{html.escape(label)}</a>'


def remember_chat_user(message: Message) -> None:
    if message.chat.type == "private" or not message.from_user or message.from_user.is_bot:
        return
    storage.upsert_chat_member(
        chat_id=message.chat.id,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Запись на вклады", callback_data="bookings")],
            [InlineKeyboardButton(text="Розыгрыши", callback_data="giveaways")],
            [InlineKeyboardButton(text="Гайд по использованию", callback_data="guide")],
            [InlineKeyboardButton(text="Список участников", callback_data="users")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu")]]
    )


async def booking_link_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    username = storage.get_setting("bot_username")
    if not username:
        me = await bot.get_me()
        username = me.username or ""
        if username:
            storage.set_setting("bot_username", username)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Записаться на вклады",
                    url=f"https://t.me/{username}?start=bookings",
                )
            ]
        ]
    )


def giveaways_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Активные розыгрыши", callback_data="giveaway_active")],
            [InlineKeyboardButton(text="Создать розыгрыш", callback_data="giveaway_create")],
            [InlineKeyboardButton(text="История розыгрышей", callback_data="giveaway_history")],
            [InlineKeyboardButton(text="Назад", callback_data="menu")],
        ]
    )


def giveaway_step_keyboard(
    back_callback: str | None = None,
    allow_skip: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if allow_skip:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="giveaway_media_skip")])
    if back_callback:
        rows.append([InlineKeyboardButton(text="Назад", callback_data=back_callback)])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="giveaway_create_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def giveaway_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать", callback_data="giveaway_publish")],
            [InlineKeyboardButton(text="Изменить", callback_data="giveaway_edit")],
            [InlineKeyboardButton(text="Отменить", callback_data="giveaway_create_cancel")],
        ]
    )


def giveaway_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Название", callback_data="giveaway_edit_title"),
                InlineKeyboardButton(text="Медиа", callback_data="giveaway_edit_media"),
            ],
            [
                InlineKeyboardButton(text="Описание", callback_data="giveaway_edit_description"),
                InlineKeyboardButton(text="Победители", callback_data="giveaway_edit_winners"),
            ],
            [InlineKeyboardButton(text="Дата окончания", callback_data="giveaway_edit_ends")],
            [InlineKeyboardButton(text="Назад", callback_data="giveaway_preview")],
            [InlineKeyboardButton(text="Отмена", callback_data="giveaway_create_cancel")],
        ]
    )


def giveaway_target() -> tuple[int | None, int | None]:
    return (
        _setting_int("giveaway_chat_id"),
        _setting_int("giveaway_thread_id"),
    )


def parse_giveaway_end(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value.strip(), "%d.%m.%y %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=config.timezone)


def giveaway_end_datetime(giveaway: Giveaway) -> datetime:
    parsed = datetime.fromisoformat(giveaway.ends_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(config.timezone)


def giveaway_status_label(status: str) -> str:
    return {
        "active": "Активен",
        "drawing": "Выбор победителей",
        "completed": "Завершён",
        "cancelled": "Отменён",
    }.get(status, status)


def giveaway_card_text(
    *,
    title: str,
    description: str,
    winner_count: int,
    ends_at: datetime,
    author_telegram_id: int,
    author_username: str | None,
    author_display_name: str,
    participant_count: int,
    status: str,
    winners: list[ClubUser] | None = None,
    preview: bool = False,
) -> str:
    author_label = f"@{author_username}" if author_username else author_display_name
    author = (
        f'<a href="tg://user?id={author_telegram_id}">'
        f"{html.escape(author_label)}</a>"
    )
    rows = []
    if preview:
        rows.append("<b>Предпросмотр розыгрыша</b>")
    rows.extend(
        [
            f"🎁 <b>{html.escape(title)}</b>",
            "",
            html.escape(description),
            "",
            f"Победителей: <b>{winner_count}</b>",
            f"Окончание: <b>{ends_at:%d.%m.%y %H:%M} МСК</b>",
            f"Организатор: {author}",
            f"Участников: <b>{participant_count}</b>",
            f"Статус: <b>{giveaway_status_label(status)}</b>",
        ]
    )
    if winners:
        rows.append("")
        rows.append("<b>Победители:</b>")
        for index, winner in enumerate(winners, start=1):
            winner_row = f"{index}. {html_mention(winner)}"
            if len("\n".join([*rows, winner_row])) > 900:
                rows.append(f"…и ещё {len(winners) - index + 1}")
                break
            rows.append(winner_row)
    elif status == "completed":
        rows.extend(["", "Победителей нет: никто не участвовал."])
    return "\n".join(rows)


def giveaway_text(
    giveaway: Giveaway,
    winners: list[ClubUser] | None = None,
) -> str:
    return giveaway_card_text(
        title=giveaway.title,
        description=giveaway.description,
        winner_count=giveaway.winner_count,
        ends_at=giveaway_end_datetime(giveaway),
        author_telegram_id=giveaway.author_telegram_id,
        author_username=giveaway.author_username,
        author_display_name=giveaway.author_display_name,
        participant_count=giveaway.participant_count,
        status=giveaway.status,
        winners=winners,
    )


def giveaway_join_keyboard(giveaway: Giveaway) -> InlineKeyboardMarkup | None:
    if giveaway.status != "active":
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Участвовать — {giveaway.participant_count}",
                    callback_data=f"giveaway_join:{giveaway.id}",
                )
            ]
        ]
    )


async def send_giveaway_card(
    bot: Bot,
    giveaway: Giveaway,
    *,
    chat_id: int,
    thread_id: int | None = None,
    preview: bool = False,
    preview_keyboard: InlineKeyboardMarkup | None = None,
) -> Message:
    if preview:
        text = giveaway_card_text(
            title=giveaway.title,
            description=giveaway.description,
            winner_count=giveaway.winner_count,
            ends_at=giveaway_end_datetime(giveaway),
            author_telegram_id=giveaway.author_telegram_id,
            author_username=giveaway.author_username,
            author_display_name=giveaway.author_display_name,
            participant_count=0,
            status=giveaway.status,
            preview=True,
        )
    else:
        text = giveaway_text(giveaway)
    markup = preview_keyboard if preview else giveaway_join_keyboard(giveaway)
    if giveaway.media_type == "photo" and giveaway.media_file_id:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=giveaway.media_file_id,
            caption=text,
            reply_markup=markup,
            message_thread_id=thread_id,
        )
    if giveaway.media_type == "animation" and giveaway.media_file_id:
        return await bot.send_animation(
            chat_id=chat_id,
            animation=giveaway.media_file_id,
            caption=text,
            reply_markup=markup,
            message_thread_id=thread_id,
        )
    if giveaway.media_type == "document" and giveaway.media_file_id:
        return await bot.send_document(
            chat_id=chat_id,
            document=giveaway.media_file_id,
            caption=text,
            reply_markup=markup,
            message_thread_id=thread_id,
        )
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        message_thread_id=thread_id,
    )


async def edit_giveaway_card(
    bot: Bot,
    giveaway: Giveaway,
    winners: list[ClubUser] | None = None,
) -> None:
    if giveaway.chat_id is None or giveaway.message_id is None:
        return
    text = giveaway_text(giveaway, winners)
    markup = giveaway_join_keyboard(giveaway)
    if giveaway.media_type in {"photo", "animation", "document"}:
        await bot.edit_message_caption(
            chat_id=giveaway.chat_id,
            message_id=giveaway.message_id,
            caption=text,
            reply_markup=markup,
        )
    else:
        await bot.edit_message_text(
            text,
            chat_id=giveaway.chat_id,
            message_id=giveaway.message_id,
            reply_markup=markup,
        )


async def pin_giveaway_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
        return True
    except TelegramAPIError as error:
        logging.warning(
            "Failed to pin giveaway message: chat_id=%s message_id=%s error=%s",
            chat_id,
            message_id,
            error,
        )
        return False


async def unpin_giveaway_message(bot: Bot, giveaway: Giveaway) -> bool:
    if giveaway.chat_id is None or giveaway.message_id is None:
        return False
    try:
        await bot.unpin_chat_message(
            chat_id=giveaway.chat_id,
            message_id=giveaway.message_id,
        )
        return True
    except TelegramAPIError as error:
        logging.warning(
            "Failed to unpin giveaway message: giveaway_id=%s chat_id=%s "
            "message_id=%s error=%s",
            giveaway.id,
            giveaway.chat_id,
            giveaway.message_id,
            error,
        )
        return False


def giveaway_list_keyboard(
    giveaways: list[Giveaway],
    back_callback: str = "giveaways",
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item.title[:32]} · {giveaway_end_datetime(item):%d.%m %H:%M}",
                callback_data=f"giveaway_view:{item.id}",
            )
        ]
        for item in giveaways
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def giveaway_view_keyboard(
    giveaway: Giveaway,
    viewer_id: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if giveaway.status == "active" and (
        giveaway.author_telegram_id == viewer_id or viewer_id in config.admin_ids
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отменить розыгрыш",
                    callback_data=f"giveaway_cancel:{giveaway.id}",
                )
            ]
        )
    back_callback = (
        "giveaway_active"
        if giveaway.status in {"active", "drawing"}
        else "giveaway_history"
    )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def can_set_giveaway_topic(message: Message) -> bool:
    if message.from_user.id in config.admin_ids:
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    except Exception:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


def menu_photo_path() -> Path | None:
    if not config.menu_photo_path:
        return None
    path = Path(config.menu_photo_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path if path.exists() else None


def message_has_media(message: Message) -> bool:
    return bool(message.photo or message.animation or message.document)


async def send_main_menu(message: Message) -> None:
    markup = main_menu_keyboard()
    photo_path = menu_photo_path()
    if photo_path:
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=config.menu_text,
            reply_markup=markup,
        )
    else:
        await message.answer(config.menu_text, reply_markup=markup)


async def edit_or_send_menu(call: CallbackQuery) -> None:
    if call.message:
        await replace_with_menu(call.message)
    await call.answer()


async def replace_with_menu(message: Message) -> None:
    markup = main_menu_keyboard()
    photo_path = menu_photo_path()
    if not photo_path:
        await replace_with_text(message, config.menu_text, reply_markup=markup)
        return

    if message_has_media(message):
        try:
            await message.edit_caption(caption=config.menu_text, reply_markup=markup)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return

    await _delete_silent(message)
    await message.answer_photo(
        photo=FSInputFile(photo_path),
        caption=config.menu_text,
        reply_markup=markup,
    )


async def replace_with_text(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    if not message_has_media(message):
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return

    await _delete_silent(message)
    await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


def bookings_text() -> str:
    slots = booking_window_slots()
    bookings = {
        (item.booking_date, item.hour): item
        for item in storage.list_bookings(booking_window_dates())
    }

    parts = ["Расписание вкладов:"]
    for booking_date in booking_window_dates():
        parts.append(f"~ {booking_date.isoformat()}")
        day_slots = [slot for slot in slots if slot.date() == booking_date]
        index = 0
        while index < len(day_slots):
            slot = day_slots[index]
            booking = bookings.get((slot.date().isoformat(), slot.hour))
            if not booking:
                parts.append(f"- {slot_label(slot.hour)} - свободно")
                index += 1
                continue

            end_index = index + 1
            while end_index < len(day_slots):
                next_slot = day_slots[end_index]
                next_booking = bookings.get(
                    (next_slot.date().isoformat(), next_slot.hour)
                )
                if not next_booking or next_booking.telegram_id != booking.telegram_id:
                    break
                end_index += 1

            if end_index - index >= 2:
                end_time = day_slots[end_index - 1] + timedelta(hours=1)
                time_range = f"{slot:%H:%M} - {end_time:%H:%M}"
            else:
                time_range = slot_label(slot.hour)
            parts.append(f"- {time_range} - {user_label(booking)}")
            index = end_index
        parts.append("")

    current_slot = current_slot_start()
    current_booking = storage.get_booking(current_slot.date(), current_slot.hour)
    parts.append("Сейчас на очереди:")
    if current_booking:
        parts.append(f"- {user_label(current_booking)}")
    else:
        parts.append("- Сейчас свободно.")

    return "\n".join(parts).strip()


def bookings_keyboard(current_user_id: int) -> InlineKeyboardMarkup:
    slots = booking_window_slots()
    bookings = {
        (item.booking_date, item.hour): item
        for item in storage.list_bookings(booking_window_dates())
    }
    rows: list[list[InlineKeyboardButton]] = []

    for booking_date in booking_window_dates():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"~ {booking_date.isoformat()}",
                    callback_data="noop",
                )
            ]
        )
        day_buttons: list[InlineKeyboardButton] = []
        for slot in (item for item in slots if item.date() == booking_date):
            booking = bookings.get((slot.date().isoformat(), slot.hour))
            if booking:
                prefix = "✅" if booking.telegram_id == current_user_id else "❌"
                text = f"{prefix} {slot.hour:02d}:00"
            else:
                text = slot_label(slot.hour)
            day_buttons.append(
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"book:{slot.date().isoformat()}:{slot.hour}",
                )
            )

        rows.extend(
            day_buttons[index : index + 4]
            for index in range(0, len(day_buttons), 4)
        )

    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_bookings(message: Message, telegram_id: int) -> None:
    await replace_with_text(
        message,
        bookings_text(),
        reply_markup=bookings_keyboard(telegram_id),
    )


async def refresh_schedule_message(bot: Bot) -> None:
    chat_id, message_id, thread_id = schedule_target()
    if not chat_id or not message_id:
        logging.info(
            "Schedule refresh skipped: chat_id=%s message_id=%s thread_id=%s",
            chat_id,
            message_id,
            thread_id,
        )
        return
    try:
        await bot.edit_message_text(
            bookings_text(),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=await booking_link_keyboard(bot),
        )
        logging.info("Schedule message refreshed: chat_id=%s message_id=%s", chat_id, message_id)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            logging.info("Schedule message unchanged: chat_id=%s message_id=%s", chat_id, message_id)
            return
        logging.warning(
            "Schedule edit failed, sending a replacement: chat_id=%s message_id=%s thread_id=%s error=%s",
            chat_id,
            message_id,
            thread_id,
            error,
        )
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=bookings_text(),
                message_thread_id=thread_id,
                reply_markup=await booking_link_keyboard(bot),
            )
            storage.set_setting("schedule_message_id", str(sent.message_id))
            logging.info(
                "Replacement schedule message sent: chat_id=%s message_id=%s thread_id=%s",
                chat_id,
                sent.message_id,
                thread_id,
            )
        except Exception:
            storage.set_setting("schedule_message_id", "")
            logging.exception("Schedule message is unavailable")
    except Exception:
        logging.exception("Failed to refresh schedule message")


async def schedule_refresh_loop(bot: Bot) -> None:
    while True:
        try:
            await refresh_schedule_message(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Unexpected schedule refresh error")

        current = now_moscow()
        next_hour = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        await asyncio.sleep(max((next_hour - current).total_seconds() + 2, 1))


async def finish_giveaway(bot: Bot, giveaway_id: int) -> None:
    giveaway = storage.get_giveaway(giveaway_id)
    if not giveaway or giveaway.status != "active":
        return
    if not storage.claim_giveaway(giveaway_id):
        return

    try:
        participants = storage.list_giveaway_participants(giveaway_id)
        winner_total = min(giveaway.winner_count, len(participants))
        winners = (
            secrets.SystemRandom().sample(participants, winner_total)
            if winner_total
            else []
        )
        storage.complete_giveaway(
            giveaway_id,
            [winner.telegram_id for winner in winners],
            datetime.now(timezone.utc).isoformat(),
        )
        completed = storage.get_giveaway(giveaway_id)
        if not completed:
            return
        try:
            await edit_giveaway_card(bot, completed, winners)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                logging.warning("Failed to edit completed giveaway %s: %s", giveaway_id, error)

        if completed.chat_id and completed.message_id:
            await unpin_giveaway_message(bot, completed)
            if winners:
                prefix = f"🎉 Победители розыгрыша «{html.escape(completed.title)}»:\n"
                chunks: list[str] = []
                current = prefix
                for winner in winners:
                    mention = html_mention(winner)
                    candidate = f"{current}{mention}\n"
                    if len(candidate) > 3500 and current != prefix:
                        chunks.append(current.rstrip())
                        current = f"{mention}\n"
                    else:
                        current = candidate
                chunks.append(current.rstrip())
            else:
                chunks = [
                    f"Розыгрыш «{html.escape(completed.title)}» завершён. "
                    "Участников не было."
                ]
            for result_text in chunks:
                await bot.send_message(
                    chat_id=completed.chat_id,
                    text=result_text,
                    message_thread_id=completed.thread_id,
                    reply_to_message_id=completed.message_id,
                )
    except Exception:
        storage.restore_active_giveaway(giveaway_id)
        logging.exception("Failed to finish giveaway %s", giveaway_id)


async def giveaway_completion_loop(bot: Bot) -> None:
    while True:
        try:
            due = storage.list_due_giveaways(datetime.now(timezone.utc).isoformat())
            for giveaway in due:
                await finish_giveaway(bot, giveaway.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Unexpected giveaway completion error")
        await asyncio.sleep(15)


def _setting_int(key: str) -> int | None:
    value = storage.get_setting(key)
    return int(value) if value else None


def schedule_target() -> tuple[int | None, int | None, int | None]:
    return (
        _setting_int("group_chat_id") or config.group_chat_id,
        _setting_int("schedule_message_id") or config.schedule_message_id,
        _setting_int("schedule_thread_id") or config.schedule_thread_id,
    )


async def ensure_schedule_message(bot: Bot, message: Message) -> None:
    chat_id, message_id, thread_id = schedule_target()
    current_thread_id = message.message_thread_id
    if chat_id == message.chat.id and message_id:
        if thread_id != current_thread_id:
            await _delete_message_silent(bot, chat_id, message_id)
        else:
            try:
                await bot.edit_message_text(
                    bookings_text(),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=await booking_link_keyboard(bot),
                )
                await _delete_silent(message)
                return
            except TelegramBadRequest:
                storage.set_setting("schedule_message_id", "")

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=bookings_text(),
        message_thread_id=current_thread_id,
        reply_markup=await booking_link_keyboard(bot),
    )
    storage.set_setting("group_chat_id", str(message.chat.id))
    storage.set_setting("schedule_message_id", str(sent.message_id))
    storage.set_setting("schedule_thread_id", str(current_thread_id or ""))
    await _delete_silent(message)


async def _delete_message_silent(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _delete_silent(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def show_creation_prompt(
    message: Message,
    state: FSMContext,
    next_state: State,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    sent: Message | None = None
    if prompt_message_id:
        try:
            await message.bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            await _delete_message_silent(message.bot, message.chat.id, prompt_message_id)
            sent = await message.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                reply_markup=reply_markup,
            )
    elif message.from_user and message.from_user.is_bot:
        await replace_with_text(message, text, reply_markup=reply_markup)
        prompt_message_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=reply_markup)

    if sent:
        prompt_message_id = sent.message_id
    if message.from_user and not message.from_user.is_bot:
        await _delete_silent(message)
    await state.update_data(prompt_message_id=prompt_message_id)
    await state.set_state(next_state)


def draft_giveaway(data: dict, author: ClubUser) -> Giveaway:
    return Giveaway(
        id=0,
        author_telegram_id=author.telegram_id,
        author_username=author.username,
        author_display_name=author.display_name,
        title=data["title"],
        description=data["description"],
        media_type=data.get("media_type"),
        media_file_id=data.get("media_file_id"),
        winner_count=int(data["winner_count"]),
        ends_at=data["ends_at"],
        status="active",
        chat_id=None,
        thread_id=None,
        message_id=None,
        participant_count=0,
        created_at="",
        completed_at=None,
    )


async def show_giveaway_preview(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    required = {"title", "description", "winner_count", "ends_at"}
    if not required.issubset(data):
        await state.clear()
        await message.answer("Черновик розыгрыша устарел. Начните создание заново.")
        return
    author = storage.get_user(message.chat.id)
    if not author:
        await state.clear()
        await message.answer("Регистрация не найдена. Нажмите /start.")
        return
    draft = draft_giveaway(data, author)
    prompt_message_id = data.get("prompt_message_id")
    if prompt_message_id:
        await _delete_message_silent(message.bot, message.chat.id, prompt_message_id)
    if message.from_user and not message.from_user.is_bot:
        await _delete_silent(message)
    sent = await send_giveaway_card(
        message.bot,
        draft,
        chat_id=message.chat.id,
        preview=True,
        preview_keyboard=giveaway_preview_keyboard(),
    )
    await state.update_data(prompt_message_id=sent.message_id)
    await state.set_state(GiveawayCreation.preview)


async def finish_edited_field_or_continue(
    message: Message,
    state: FSMContext,
    next_state: State,
    next_text: str,
    next_keyboard: InlineKeyboardMarkup,
) -> None:
    data = await state.get_data()
    if data.get("editing_field"):
        await state.update_data(editing_field=None)
        await show_giveaway_preview(message, state)
        return
    await show_creation_prompt(
        message,
        state,
        next_state,
        next_text,
        next_keyboard,
    )


def require_registered(message: Message) -> ClubUser | None:
    user = storage.get_user(message.from_user.id)
    return user


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if message.chat.type != "private":
        return
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Вы не зарегистрированы в клубе.\n"
            "Отправьте ссылку на ваш профиль в MangaBuff."
        )
        return
    if command.args == "bookings":
        await message.answer(
            bookings_text(),
            reply_markup=bookings_keyboard(message.from_user.id),
        )
        return
    await send_main_menu(message)


@router.message(Command("post_schedule"))
async def post_schedule(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("Эту команду нужно отправить в чате клуба.")
        return
    remember_chat_user(message)
    await ensure_schedule_message(message.bot, message)


@router.message(Command("refresh_schedule"))
async def force_refresh_schedule(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("Эту команду нужно отправить в чате клуба.")
        return
    remember_chat_user(message)
    await refresh_schedule_message(message.bot)
    await _delete_silent(message)


@router.message(Command("set_giveaway_topic"))
async def set_giveaway_topic(message: Message) -> None:
    if message.chat.type == "private":
        await message.answer("Эту команду нужно отправить в нужной теме чата.")
        return
    if not await can_set_giveaway_topic(message):
        await message.answer("Настраивать тему розыгрышей может только администратор чата.")
        return
    storage.set_setting("giveaway_chat_id", str(message.chat.id))
    storage.set_setting("giveaway_thread_id", str(message.message_thread_id or ""))
    await message.answer("Эта тема назначена для публикации розыгрышей.")
    await _delete_silent(message)


@router.message(F.new_chat_members)
async def remember_new_chat_members(message: Message) -> None:
    if message.chat.type == "private":
        return
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        storage.upsert_chat_member(
            chat_id=message.chat.id,
            telegram_id=member.id,
            username=member.username,
            full_name=member.full_name,
        )


@router.message(F.left_chat_member)
async def forget_left_chat_member(message: Message) -> None:
    if message.chat.type == "private" or not message.left_chat_member:
        return
    storage.delete_chat_member(message.chat.id, message.left_chat_member.id)


@router.message(F.text == ".ник")
async def chat_profile_link(message: Message) -> None:
    if message.chat.type == "private":
        return
    remember_chat_user(message)
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Ответьте командой .ник на сообщение участника.")
        return

    target = message.reply_to_message.from_user
    if not target.is_bot:
        storage.upsert_chat_member(
            chat_id=message.chat.id,
            telegram_id=target.id,
            username=target.username,
            full_name=target.full_name,
        )
    user = storage.get_user(target.id)
    if not user:
        await message.answer("Этот участник ещё не зарегистрирован в боте.")
        return

    username = f"@{target.username}" if target.username else target.full_name
    await message.answer(
        f"{html.escape(username)}: {html.escape(user.profile_url)}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(F.text == ".распес")
async def chat_schedule(message: Message) -> None:
    if message.chat.type == "private":
        return
    remember_chat_user(message)
    await message.answer(
        bookings_text(),
        reply_markup=await booking_link_keyboard(message.bot),
    )


@router.message(F.text.startswith(".всем"))
async def chat_mention_all(message: Message) -> None:
    if message.chat.type == "private":
        return
    remember_chat_user(message)

    text = message.text[len(".всем") :].strip()
    if not text:
        await message.answer("Напишите текст после команды. Например: .всем Будьте добры занять вклады")
        return

    users = storage.list_chat_members(message.chat.id)
    if not users:
        await message.answer("Я пока не видел участников этого чата.")
        return

    mentions = " ".join(html_mention(user) for user in users)
    await message.answer(
        f"{html.escape(text)}\n\n{mentions}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@router.message(GiveawayCreation.title)
async def giveaway_title_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Отправьте название розыгрыша текстом.")
        return
    title = message.text.strip()
    if not 1 <= len(title) <= 100:
        await message.answer("Название должно содержать от 1 до 100 символов.")
        return
    await state.update_data(title=title)
    await finish_edited_field_or_continue(
        message,
        state,
        GiveawayCreation.media,
        "Отправьте GIF или фотографию для розыгрыша.\n"
        "Медиа можно пропустить.",
        giveaway_step_keyboard("giveaway_back_title", allow_skip=True),
    )


@router.message(GiveawayCreation.media)
async def giveaway_media_input(message: Message, state: FSMContext) -> None:
    media_type: str | None = None
    media_file_id: str | None = None
    if message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.animation:
        media_type = "animation"
        media_file_id = message.animation.file_id
    elif message.document and message.document.mime_type == "image/gif":
        media_type = "document"
        media_file_id = message.document.file_id
    else:
        await message.answer("Отправьте фотографию или GIF либо нажмите «Пропустить».")
        return
    await state.update_data(media_type=media_type, media_file_id=media_file_id)
    await finish_edited_field_or_continue(
        message,
        state,
        GiveawayCreation.description,
        "Отправьте описание розыгрыша текстом.",
        giveaway_step_keyboard("giveaway_back_media"),
    )


@router.message(GiveawayCreation.description)
async def giveaway_description_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Отправьте описание розыгрыша текстом.")
        return
    description = message.text.strip()
    if not 1 <= len(description) <= 500:
        await message.answer("Описание должно содержать от 1 до 500 символов.")
        return
    await state.update_data(description=description)
    await finish_edited_field_or_continue(
        message,
        state,
        GiveawayCreation.winner_count,
        "Укажите количество победителей числом от 1 до 100.",
        giveaway_step_keyboard("giveaway_back_description"),
    )


@router.message(GiveawayCreation.winner_count)
async def giveaway_winner_count_input(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Укажите количество победителей целым числом.")
        return
    winner_count = int(message.text.strip())
    if not 1 <= winner_count <= 100:
        await message.answer("Количество победителей должно быть от 1 до 100.")
        return
    await state.update_data(winner_count=winner_count)
    await finish_edited_field_or_continue(
        message,
        state,
        GiveawayCreation.ends_at,
        "Укажите дату окончания по московскому времени.\n"
        "Формат: <code>10.10.26 10:10</code>",
        giveaway_step_keyboard("giveaway_back_winners"),
    )


@router.message(GiveawayCreation.ends_at)
async def giveaway_ends_at_input(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Отправьте дату окончания текстом.")
        return
    ends_at = parse_giveaway_end(message.text)
    if not ends_at:
        await message.answer("Неверный формат. Пример: <code>10.10.26 10:10</code>")
        return
    if ends_at <= now_moscow():
        await message.answer("Дата окончания должна быть в будущем.")
        return
    await state.update_data(
        ends_at=ends_at.astimezone(timezone.utc).isoformat(),
        editing_field=None,
    )
    await show_giveaway_preview(message, state)


@router.message(F.text)
async def register_profile(message: Message) -> None:
    if message.chat.type != "private":
        remember_chat_user(message)
        return
    if storage.get_user(message.from_user.id):
        await message.answer("Вы уже зарегистрированы. Нажмите /start.")
        return

    profile_url = message.text.strip()
    profile_id = parse_profile_url(profile_url)
    if profile_id is None:
        await message.answer("Отправьте ссылку вида https://mangabuff.ru/users/854887")
        return
    if storage.profile_exists(profile_id):
        await message.answer(
            "Этот профиль MangaBuff уже зарегистрирован в боте.\n"
            "Отправьте ссылку на ваш профиль."
        )
        return

    await message.answer("Проверяю профиль и участие в клубе...")
    check = check_profile_in_club(profile_url, config.club_slug, config.club_url)
    if not check.ok:
        logging.warning("MangaBuff profile check failed: reason=%s detail=%s", check.reason, check.detail)
        if check.reason == "network":
            await message.answer(
                "Не удалось открыть страницу клуба MangaBuff.\n"
                "Попробуйте ещё раз позже. Если ошибка повторится, проверьте логи Railway."
            )
        elif check.reason in {"auth_required", "profile_auth_required"}:
            await message.answer(
                "MangaBuff не дал открыть страницу без авторизации.\n"
                "Обратитесь за помощью к @reeigans"
            )
        elif check.reason == "login_failed":
            await message.answer(
                "Не удалось войти в аккаунт MangaBuff.\n"
                "Обратитесь за помощью к @reeigans"
            )
        elif check.reason == "club_not_found":
            await message.answer(
                "Не найдена страница клуба MangaBuff.\n"
                "Проверьте переменную CLUB_URL в Railway."
            )
        elif check.reason == "members_unavailable":
            await message.answer(
                "Не удалось прочитать список участников клуба.\n"
                "Проверьте доступ к странице клуба или добавьте MANGABUFF_COOKIE в Railway."
            )
        else:
            await message.answer(
                "Вы не состоите в клубе.\n"
                "Отправьте ссылку на профиль MangaBuff еще раз."
            )
        return

    storage.add_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        display_name=check.display_name or f"MangaBuff #{profile_id}",
        profile_id=profile_id,
        profile_url=profile_url,
    )
    await message.answer("Вы успешно добавлены, перезапустите бота: /start")


@router.callback_query(F.data == "menu")
async def on_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_send_menu(call)


@router.callback_query(F.data == "giveaways")
async def on_giveaways(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await replace_with_text(
        call.message,
        "Розыгрыши клуба",
        reply_markup=giveaways_menu_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_active")
async def on_active_giveaways(call: CallbackQuery) -> None:
    giveaways = storage.list_giveaways(["active", "drawing"])
    text = "Активные розыгрыши:" if giveaways else "Активных розыгрышей пока нет."
    await replace_with_text(
        call.message,
        text,
        reply_markup=giveaway_list_keyboard(giveaways),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_history")
async def on_giveaway_history(call: CallbackQuery) -> None:
    giveaways = storage.list_giveaways(["completed", "cancelled"])
    text = "История розыгрышей:" if giveaways else "История розыгрышей пока пуста."
    await replace_with_text(
        call.message,
        text,
        reply_markup=giveaway_list_keyboard(giveaways),
    )
    await call.answer()


@router.callback_query(F.data.startswith("giveaway_view:"))
async def on_giveaway_view(call: CallbackQuery) -> None:
    giveaway_id = int(call.data.split(":", 1)[1])
    giveaway = storage.get_giveaway(giveaway_id)
    if not giveaway:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    winners = (
        storage.list_giveaway_winners(giveaway_id)
        if giveaway.status == "completed"
        else None
    )
    await replace_with_text(
        call.message,
        giveaway_text(giveaway, winners),
        reply_markup=giveaway_view_keyboard(giveaway, call.from_user.id),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_create")
async def on_giveaway_create(call: CallbackQuery, state: FSMContext) -> None:
    if not storage.get_user(call.from_user.id):
        await call.answer("Создавать розыгрыши могут только зарегистрированные участники.", show_alert=True)
        return
    await state.clear()
    await state.update_data(prompt_message_id=call.message.message_id)
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.title,
        "Введите название розыгрыша.",
        giveaway_step_keyboard("giveaways"),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_create_cancel")
async def on_giveaway_create_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await replace_with_text(
        call.message,
        "Создание розыгрыша отменено.",
        reply_markup=giveaways_menu_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_media_skip")
async def on_giveaway_media_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(media_type=None, media_file_id=None)
    data = await state.get_data()
    if data.get("editing_field"):
        await state.update_data(editing_field=None)
        await show_giveaway_preview(call.message, state)
    else:
        await show_creation_prompt(
            call.message,
            state,
            GiveawayCreation.description,
            "Отправьте описание розыгрыша текстом.",
            giveaway_step_keyboard("giveaway_back_media"),
        )
    await call.answer()


@router.callback_query(F.data == "giveaway_back_title")
async def on_giveaway_back_title(call: CallbackQuery, state: FSMContext) -> None:
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.title,
        "Введите название розыгрыша.",
        giveaway_step_keyboard("giveaways"),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_back_media")
async def on_giveaway_back_media(call: CallbackQuery, state: FSMContext) -> None:
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.media,
        "Отправьте GIF или фотографию для розыгрыша.\nМедиа можно пропустить.",
        giveaway_step_keyboard("giveaway_back_title", allow_skip=True),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_back_description")
async def on_giveaway_back_description(call: CallbackQuery, state: FSMContext) -> None:
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.description,
        "Отправьте описание розыгрыша текстом.",
        giveaway_step_keyboard("giveaway_back_media"),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_back_winners")
async def on_giveaway_back_winners(call: CallbackQuery, state: FSMContext) -> None:
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.winner_count,
        "Укажите количество победителей числом от 1 до 100.",
        giveaway_step_keyboard("giveaway_back_description"),
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_preview")
async def on_giveaway_preview(call: CallbackQuery, state: FSMContext) -> None:
    await show_giveaway_preview(call.message, state)
    await call.answer()


@router.callback_query(F.data == "giveaway_edit")
async def on_giveaway_edit(call: CallbackQuery, state: FSMContext) -> None:
    await show_creation_prompt(
        call.message,
        state,
        GiveawayCreation.edit_menu,
        "Что изменить?",
        giveaway_edit_keyboard(),
    )
    await call.answer()


async def begin_giveaway_field_edit(
    call: CallbackQuery,
    state: FSMContext,
    *,
    field: str,
    target_state: State,
    prompt: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    await state.update_data(editing_field=field)
    await show_creation_prompt(
        call.message,
        state,
        target_state,
        prompt,
        keyboard,
    )
    await call.answer()


@router.callback_query(F.data == "giveaway_edit_title")
async def on_giveaway_edit_title(call: CallbackQuery, state: FSMContext) -> None:
    await begin_giveaway_field_edit(
        call,
        state,
        field="title",
        target_state=GiveawayCreation.title,
        prompt="Введите новое название розыгрыша.",
        keyboard=giveaway_step_keyboard("giveaway_preview"),
    )


@router.callback_query(F.data == "giveaway_edit_media")
async def on_giveaway_edit_media(call: CallbackQuery, state: FSMContext) -> None:
    await begin_giveaway_field_edit(
        call,
        state,
        field="media",
        target_state=GiveawayCreation.media,
        prompt="Отправьте новую фотографию или GIF либо нажмите «Пропустить».",
        keyboard=giveaway_step_keyboard("giveaway_preview", allow_skip=True),
    )


@router.callback_query(F.data == "giveaway_edit_description")
async def on_giveaway_edit_description(call: CallbackQuery, state: FSMContext) -> None:
    await begin_giveaway_field_edit(
        call,
        state,
        field="description",
        target_state=GiveawayCreation.description,
        prompt="Введите новое описание розыгрыша.",
        keyboard=giveaway_step_keyboard("giveaway_preview"),
    )


@router.callback_query(F.data == "giveaway_edit_winners")
async def on_giveaway_edit_winners(call: CallbackQuery, state: FSMContext) -> None:
    await begin_giveaway_field_edit(
        call,
        state,
        field="winner_count",
        target_state=GiveawayCreation.winner_count,
        prompt="Укажите новое количество победителей от 1 до 100.",
        keyboard=giveaway_step_keyboard("giveaway_preview"),
    )


@router.callback_query(F.data == "giveaway_edit_ends")
async def on_giveaway_edit_ends(call: CallbackQuery, state: FSMContext) -> None:
    await begin_giveaway_field_edit(
        call,
        state,
        field="ends_at",
        target_state=GiveawayCreation.ends_at,
        prompt="Введите новую дату окончания: <code>10.10.26 10:10</code>",
        keyboard=giveaway_step_keyboard("giveaway_preview"),
    )


@router.callback_query(F.data == "giveaway_publish")
async def on_giveaway_publish(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    required = {"title", "description", "winner_count", "ends_at"}
    if not required.issubset(data):
        await call.answer("Данные розыгрыша заполнены не полностью.", show_alert=True)
        return
    chat_id, thread_id = giveaway_target()
    if not chat_id:
        await call.answer(
            "Сначала отправьте /set_giveaway_topic в нужной теме чата.",
            show_alert=True,
        )
        return
    author = storage.get_user(call.from_user.id)
    if not author:
        await call.answer("Регистрация не найдена.", show_alert=True)
        return
    ends_at = datetime.fromisoformat(data["ends_at"])
    if ends_at <= datetime.now(timezone.utc):
        await call.answer("Дата окончания уже прошла. Измените её.", show_alert=True)
        return

    giveaway_id = storage.create_giveaway(
        author_telegram_id=author.telegram_id,
        title=data["title"],
        description=data["description"],
        media_type=data.get("media_type"),
        media_file_id=data.get("media_file_id"),
        winner_count=int(data["winner_count"]),
        ends_at=data["ends_at"],
        chat_id=chat_id,
        thread_id=thread_id,
    )
    giveaway = storage.get_giveaway(giveaway_id)
    if not giveaway:
        await call.answer("Не удалось сохранить розыгрыш.", show_alert=True)
        return
    try:
        sent = await send_giveaway_card(
            call.bot,
            giveaway,
            chat_id=chat_id,
            thread_id=thread_id,
        )
    except Exception:
        storage.cancel_giveaway(giveaway_id, datetime.now(timezone.utc).isoformat())
        logging.exception("Failed to publish giveaway %s", giveaway_id)
        await call.answer("Не удалось опубликовать розыгрыш в выбранной теме.", show_alert=True)
        return

    storage.set_giveaway_message(giveaway_id, sent.message_id)
    pinned = await pin_giveaway_message(call.bot, chat_id, sent.message_id)
    await state.clear()
    await replace_with_text(
        call.message,
        (
            "Розыгрыш опубликован и закреплён."
            if pinned
            else (
                "Розыгрыш опубликован, но закрепить сообщение не удалось.\n"
                "Проверьте право бота «Закрепление сообщений»."
            )
        ),
        reply_markup=giveaways_menu_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("giveaway_join:"))
async def on_giveaway_join(call: CallbackQuery) -> None:
    user = storage.get_user(call.from_user.id)
    if not user:
        await call.answer(
            "Участвовать могут только зарегистрированные пользователи. Откройте бота и нажмите /start.",
            show_alert=True,
        )
        return
    giveaway_id = int(call.data.split(":", 1)[1])
    giveaway = storage.get_giveaway(giveaway_id)
    if not giveaway or giveaway.status != "active":
        await call.answer("Этот розыгрыш уже завершён.", show_alert=True)
        return
    if giveaway_end_datetime(giveaway) <= now_moscow():
        await finish_giveaway(call.bot, giveaway_id)
        await call.answer("Этот розыгрыш уже завершён.", show_alert=True)
        return
    if not storage.add_giveaway_participant(giveaway_id, call.from_user.id):
        await call.answer("Вы уже участвуете.", show_alert=True)
        return
    updated = storage.get_giveaway(giveaway_id)
    if not updated:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    try:
        await edit_giveaway_card(call.bot, updated)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            logging.warning("Failed to update giveaway participant count: %s", error)
    await call.answer("Вы участвуете!")


@router.callback_query(F.data.startswith("giveaway_cancel:"))
async def on_giveaway_cancel(call: CallbackQuery) -> None:
    giveaway_id = int(call.data.split(":", 1)[1])
    giveaway = storage.get_giveaway(giveaway_id)
    if not giveaway:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    if (
        giveaway.author_telegram_id != call.from_user.id
        and call.from_user.id not in config.admin_ids
    ):
        await call.answer("Отменить розыгрыш может автор или администратор.", show_alert=True)
        return
    if not storage.cancel_giveaway(giveaway_id, datetime.now(timezone.utc).isoformat()):
        await call.answer("Этот розыгрыш уже завершён.", show_alert=True)
        return
    cancelled = storage.get_giveaway(giveaway_id)
    if not cancelled:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    try:
        await edit_giveaway_card(call.bot, cancelled)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            logging.warning("Failed to edit cancelled giveaway: %s", error)
    await unpin_giveaway_message(call.bot, cancelled)
    await replace_with_text(
        call.message,
        giveaway_text(cancelled),
        reply_markup=giveaway_view_keyboard(cancelled, call.from_user.id),
    )
    await call.answer("Розыгрыш отменён.")


@router.callback_query(F.data == "guide")
async def on_guide(call: CallbackQuery) -> None:
    await replace_with_text(call.message, config.guide_text, reply_markup=back_keyboard())
    await call.answer()


@router.callback_query(F.data == "users")
async def on_users(call: CallbackQuery) -> None:
    users = storage.list_users()
    if not users:
        text = "Список пользователей пуст."
    else:
        rows = ["Список пользователей:"]
        for index, user in enumerate(users, start=1):
            username = f"@{user.username}" if user.username else f"id{user.telegram_id}"
            rows.append(f"{index}. {html.escape(username)} - {html.escape(user.display_name)}")
        text = "\n".join(rows)
    await replace_with_text(
        call.message,
        text,
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()


@router.callback_query(F.data == "bookings")
async def on_bookings(call: CallbackQuery) -> None:
    user = storage.get_user(call.from_user.id)
    if not user:
        await call.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return
    await send_bookings(call.message, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "noop")
async def on_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("book:"))
async def on_book_slot(call: CallbackQuery, bot: Bot) -> None:
    user = storage.get_user(call.from_user.id)
    if not user:
        await call.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    _, date_text, hour_text = call.data.split(":")
    booking_date = date.fromisoformat(date_text)
    hour = int(hour_text)
    if not is_bookable_slot(booking_date, hour):
        await call.answer("Это время уже недоступно.", show_alert=True)
        return

    existing = storage.get_booking(booking_date, hour)
    if existing and existing.telegram_id != call.from_user.id:
        await call.answer("Это время уже занято.", show_alert=True)
        return
    if existing and existing.telegram_id == call.from_user.id:
        storage.delete_booking(booking_date, hour, call.from_user.id)
        await call.answer("Бронь отменена.")
    else:
        if not storage.add_booking(booking_date, hour, call.from_user.id):
            await call.answer("Это время только что заняли.", show_alert=True)
            return
        await call.answer("Вы успешно записаны.")

    await replace_with_text(
        call.message,
        bookings_text(),
        reply_markup=bookings_keyboard(call.from_user.id),
    )
    await refresh_schedule_message(bot)


async def main() -> None:
    global config, storage
    config = load_config()
    storage = Storage(config.database_url or config.db_path)
    storage.init()
    storage.recover_drawing_giveaways()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    refresh_task = asyncio.create_task(schedule_refresh_loop(bot))
    giveaway_task = asyncio.create_task(giveaway_completion_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        refresh_task.cancel()
        giveaway_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        with suppress(asyncio.CancelledError):
            await giveaway_task


if __name__ == "__main__":
    asyncio.run(main())
