from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config, load_config
from mangabuff import check_profile_in_club, parse_profile_url
from storage import Booking, ChatMember, ClubUser, Storage


logging.basicConfig(level=logging.INFO)
router = Router()
config: Config
storage: Storage


def now_moscow() -> datetime:
    return datetime.now(config.timezone)


def visible_dates() -> list[date]:
    today = now_moscow().date()
    return [today, today + timedelta(days=1)]


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
    bookings = storage.list_bookings(visible_dates())
    grouped: dict[str, list[Booking]] = {}
    for booking in bookings:
        grouped.setdefault(booking.booking_date, []).append(booking)

    parts = ["Расписание вкладов:"]
    for booking_date in visible_dates():
        date_key = booking_date.isoformat()
        parts.append(f"~ {date_key}")
        day_bookings = grouped.get(date_key, [])
        if day_bookings:
            for booking in day_bookings:
                parts.append(f"- {slot_label(booking.hour)} - {user_label(booking)}")
        else:
            parts.append("- свободно")
        parts.append("")

    return "\n".join(parts).strip()


def bookings_keyboard(current_user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    current = now_moscow()
    current_hour = current.hour
    bookings = {
        (item.booking_date, item.hour): item
        for item in storage.list_bookings(visible_dates())
    }

    for booking_date in visible_dates():
        start_hour = current_hour + 1 if booking_date == current.date() else 0
        for hour in range(start_hour, 24):
            booking = bookings.get((booking_date.isoformat(), hour))
            if booking:
                prefix = "✅" if booking.telegram_id == current_user_id else "❌"
                text = f"{prefix} {hour:02d}:00"
            else:
                text = slot_label(hour)
            builder.button(text=text, callback_data=f"book:{booking_date.isoformat()}:{hour}")

    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="Назад", callback_data="menu"))
    return builder.as_markup()


async def send_bookings(message: Message, telegram_id: int) -> None:
    await replace_with_text(
        message,
        bookings_text(),
        reply_markup=bookings_keyboard(telegram_id),
    )


async def refresh_schedule_message(bot: Bot) -> None:
    chat_id, message_id, thread_id = schedule_target()
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_text(bookings_text(), chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=bookings_text(),
                message_thread_id=thread_id,
            )
            storage.set_setting("schedule_message_id", str(sent.message_id))
        except Exception:
            storage.set_setting("schedule_message_id", "")
            logging.exception("Schedule message is unavailable")
    except Exception:
        logging.exception("Failed to refresh schedule message")


def _setting_int(key: str) -> int | None:
    value = storage.get_setting(key)
    return int(value) if value else None


def schedule_target() -> tuple[int | None, int | None, int | None]:
    return (
        config.group_chat_id or _setting_int("group_chat_id"),
        config.schedule_message_id or _setting_int("schedule_message_id"),
        config.schedule_thread_id or _setting_int("schedule_thread_id"),
    )


async def ensure_schedule_message(bot: Bot, message: Message) -> None:
    chat_id, message_id, thread_id = schedule_target()
    current_thread_id = message.message_thread_id
    if chat_id == message.chat.id and message_id:
        if thread_id != current_thread_id:
            await _delete_message_silent(bot, chat_id, message_id)
        else:
            try:
                await bot.edit_message_text(bookings_text(), chat_id=chat_id, message_id=message_id)
                await _delete_silent(message)
                return
            except TelegramBadRequest:
                storage.set_setting("schedule_message_id", "")

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=bookings_text(),
        message_thread_id=current_thread_id,
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
async def start(message: Message) -> None:
    if message.chat.type != "private":
        return
    user = storage.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "Вы не зарегистрированы в клубе.\n"
            "Отправьте ссылку на ваш профиль в MangaBuff."
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
    await message.answer(bookings_text())


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


@router.callback_query(F.data.startswith("book:"))
async def on_book_slot(call: CallbackQuery, bot: Bot) -> None:
    user = storage.get_user(call.from_user.id)
    if not user:
        await call.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    _, date_text, hour_text = call.data.split(":")
    booking_date = date.fromisoformat(date_text)
    hour = int(hour_text)
    current = now_moscow()
    if booking_date not in visible_dates() or (
        booking_date == current.date() and hour <= current.hour
    ):
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
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
