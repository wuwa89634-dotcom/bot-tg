from __future__ import annotations

import asyncio
import html
import logging
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Config, load_config
from mangabuff import check_profile_in_club, parse_profile_url
from storage import Booking, ChatMember, ClubUser, Storage


logging.basicConfig(level=logging.INFO)
router = Router()
config: Config
storage: Storage


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


def menu_photo_path() -> Path | None:
    if not config.menu_photo_path:
        return None
    path = Path(config.menu_photo_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path if path.exists() else None


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

    if message.photo:
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
    if not message.photo:
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
                "Проверьте MANGABUFF_COOKIE или используйте MANGABUFF_EMAIL/MANGABUFF_PASSWORD."
            )
        elif check.reason == "login_failed":
            await message.answer(
                "Не удалось войти в аккаунт MangaBuff.\n"
                "Проверьте MANGABUFF_EMAIL, MANGABUFF_PASSWORD и MANGABUFF_LOGIN_FIELD в Railway."
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
async def on_menu(call: CallbackQuery) -> None:
    await edit_or_send_menu(call)


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

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    refresh_task = asyncio.create_task(schedule_refresh_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task


if __name__ == "__main__":
    asyncio.run(main())
