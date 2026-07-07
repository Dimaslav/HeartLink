import asyncio
import time
from contextlib import suppress
from datetime import datetime, timedelta
from html import escape as html_escape
from typing import Optional

import aiohttp
from cachetools import TTLCache
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    PreCheckoutQuery,
    LabeledPrice,
)

from config import (
    ADMIN_ID,
    BOT_TOKEN,
    DEFAULT_MAX_AGE,
    DEFAULT_MIN_AGE,
    DEFAULT_SEARCH_RADIUS,
    OWN_GENDER_BUTTONS,
    PROXY_URL,
    RATE_LIMIT_LIKES,
    RATE_LIMIT_SEARCH,
    RATE_LIMIT_SUPERLIKES,
    SEARCH_GENDER_BUTTONS,
    SUPERLIKE_PACK_COUNT,
    SUPERLIKE_PACK_PRICE_STARS,
    PREMIUM_PRICE_STARS,
    logger,
    rate_limiter,
    validate_name,
    validate_age,
    validate_bio,
    validate_photo_meta,
    format_time,
)
from database import Database

# ===================== BOT / SESSION =====================
session = AiohttpSession(proxy=PROXY_URL or None)
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=session
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===================== БАЗА ДАННЫХ =====================
db = Database()

# ===================== КЭШ ГЕОКОДИНГА =====================
city_cache = TTLCache(maxsize=2048, ttl=7 * 24 * 3600)

# ===================== СОСТОЯНИЯ FSM =====================
class ProfileStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_age = State()
    waiting_for_gender = State()
    waiting_for_search_gender = State()
    waiting_for_location = State()
    waiting_for_photo = State()
    waiting_for_bio = State()

class EditStates(StatesGroup):
    waiting_for_new_name = State()
    waiting_for_new_age = State()
    waiting_for_new_bio = State()
    waiting_for_new_photo = State()
    waiting_for_new_location = State()
    waiting_for_new_gender = State()
    waiting_for_new_search_gender = State()
    waiting_for_new_search_min_age = State()
    waiting_for_new_search_max_age = State()
    waiting_for_new_search_radius = State()

class AdminStates(StatesGroup):
    waiting_for_user_id_to_ban = State()
    waiting_for_user_id_to_unban = State()
    waiting_for_broadcast = State()
    waiting_for_premium_id = State()

class ReportStates(StatesGroup):
    waiting_for_reason = State()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

def as_int(value, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default

def format_radius(radius: int) -> str:
    return "без ограничений" if radius == 0 else f"{radius} км"

def parse_gender_choice(text: str, include_any: bool = False) -> Optional[str]:
    text = (text or "").strip()
    mapping = SEARCH_GENDER_BUTTONS if include_any else OWN_GENDER_BUTTONS
    return mapping.get(text)

def create_gender_keyboard(include_any: bool = False):
    mapping = SEARCH_GENDER_BUTTONS if include_any else OWN_GENDER_BUTTONS
    buttons = [[KeyboardButton(text=t) for t in mapping.keys()]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def create_location_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
        resize_keyboard=True
    )

def create_main_keyboard(user_id: int, is_premium: bool = False):
    premium_star = "⭐ " if is_premium else ""
    buttons = [
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🔍 Искать")],
        [KeyboardButton(text="❤️ Мои лайки"), KeyboardButton(text=f"{premium_star}Мои симпатии")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="⭐ Премиум")],
    ]
    if is_admin(user_id):
        buttons.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def create_profile_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_profile"),
            InlineKeyboardButton(text="⭐ Премиум", callback_data="premium_menu")
        ]
    ])

def create_viewing_keyboard(profile_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❤️ Лайк", callback_data=f"like_{profile_id}"),
            InlineKeyboardButton(text="⭐ Суперлайк", callback_data=f"superlike_{profile_id}")
        ],
        [InlineKeyboardButton(text="➡️ Дальше", callback_data="next")],
        [
            InlineKeyboardButton(text="🚫 Пожаловаться", callback_data=f"report_{profile_id}"),
            InlineKeyboardButton(text="⛔ Заблокировать", callback_data=f"block_profile_{profile_id}")
        ]
    ])

def create_like_keyboard(like_id: int, from_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_like_{like_id}_{from_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_like_{like_id}_{from_id}")
        ],
        [InlineKeyboardButton(text="⛔ Заблокировать", callback_data=f"block_like_{from_id}")]
    ])

def create_premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить премиум (30 дней)", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🎁 Купить 10 суперлайков", callback_data="buy_superlikes")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
    ])

def create_edit_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Имя", callback_data="edit_name"),
            InlineKeyboardButton(text="Возраст", callback_data="edit_age")
        ],
        [
            InlineKeyboardButton(text="О себе", callback_data="edit_bio"),
            InlineKeyboardButton(text="Фото", callback_data="edit_photo")
        ],
        [
            InlineKeyboardButton(text="Пол", callback_data="edit_gender"),
            InlineKeyboardButton(text="Город", callback_data="edit_location")
        ],
        [
            InlineKeyboardButton(text="Кого ищу", callback_data="edit_search_gender"),
            InlineKeyboardButton(text="Мин. возраст", callback_data="edit_search_min_age")
        ],
        [
            InlineKeyboardButton(text="Макс. возраст", callback_data="edit_search_max_age"),
            InlineKeyboardButton(text="Радиус поиска", callback_data="edit_search_radius")
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_profile")]
    ])

def create_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_list"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⭐ Выдать премиум", callback_data="admin_premium")
        ],
        [
            InlineKeyboardButton(text="⛔ Бан", callback_data="admin_ban"),
            InlineKeyboardButton(text="✅ Разбан", callback_data="admin_unban")
        ]
    ])

async def build_main_keyboard(user_id: int):
    return create_main_keyboard(user_id, await db.check_premium(user_id))

def is_invalid_file_id_error(error: Exception) -> bool:
    msg = str(error).lower()
    return any(
        phrase in msg for phrase in [
            "wrong file identifier",
            "wrong remote file identifier",
            "file identifier",
            "file_id",
            "can't find file",
            "file reference expired",
        ]
    )

async def safe_execute(func, *args, **kwargs):
    op_name = getattr(func, "__name__", func.__class__.__name__)
    for attempt in range(3):
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramNetworkError as e:
            if attempt == 2:
                logger.error(f"Network error in {op_name}: {e}")
                return None
            await asyncio.sleep(1.0 * (attempt + 1))
        except TelegramAPIError as e:
            logger.warning(f"Telegram API error in {op_name}: {e}")
            return None
        except Exception:
            logger.exception(f"Unexpected error in {op_name}")
            return None

async def get_city_from_coords(lat, lon):
    try:
        key = (round(float(lat), 3), round(float(lon), 3))
        if key in city_cache:
            return city_cache[key]

        url = "https://nominatim.openstreetmap.org/reverse"
        params = {"lat": lat, "lon": lon, "format": "json", "accept-language": "ru"}
        headers = {"User-Agent": "DatingBot/1.0"}
        timeout = aiohttp.ClientTimeout(total=7)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    address = data.get("address", {})
                    for key_name in ["city", "town", "village", "hamlet"]:
                        if key_name in address:
                            city_cache[key] = address[key_name]
                            return address[key_name]

        city_cache[key] = "Неизвестно"
        return "Неизвестно"
    except Exception:
        return "Неизвестно"

# ===================== ЭКРАНЫ =====================
async def show_profile(message: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")

    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    await db.update_user(user_id, last_active=int(time.time()))
    is_premium = await db.check_premium(user_id)
    user = await db.get_user(user_id)
    superlikes = await db.get_available_superlikes(user_id)

    text = (
        f"👤 <b>{html_escape(user['name'])}</b>, {user.get('age', '?')}\n"
        f"📍 {html_escape(user.get('city') or 'Не указан')}\n"
        f"👫 Пол: {html_escape(user.get('gender', 'Не указано'))} | "
        f"Ищет: {html_escape(user.get('search_gender', 'Любой'))}\n"
        f"🎯 Радиус: {format_radius(as_int(user.get('search_radius'), DEFAULT_SEARCH_RADIUS))}\n"
        f"🎂 Возраст поиска: {as_int(user.get('min_age_search'), DEFAULT_MIN_AGE)}–{as_int(user.get('max_age_search'), DEFAULT_MAX_AGE)}\n"
        f"{'⭐ Премиум' if is_premium else '🆓 Бесплатный'} | 🎁 Суперлайков: {superlikes}\n"
        f"🕒 Активность: {format_time(user.get('last_active'))}\n\n"
        f"📝 {html_escape(user.get('bio') or 'Нет описания')}"
    )

    if user.get("photo"):
        try:
            return await message.answer_photo(
                user["photo"],
                caption=text,
                reply_markup=create_profile_keyboard()
            )
        except TelegramBadRequest as e:
            if is_invalid_file_id_error(e):
                await db.update_user(user_id, photo="", photo_updated=0)
            else:
                logger.warning(f"Profile photo send failed for user {user_id}: {e}")

    await message.answer(text, reply_markup=create_profile_keyboard())

async def show_settings(message: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")
    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    await db.update_user(user_id, last_active=int(time.time()))
    await db.check_premium(user_id)
    user = await db.get_user(user_id)

    search_radius = as_int(user.get("search_radius"), DEFAULT_SEARCH_RADIUS)
    min_age = as_int(user.get("min_age_search"), DEFAULT_MIN_AGE)
    max_age = as_int(user.get("max_age_search"), DEFAULT_MAX_AGE)

    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"👤 Имя: <b>{html_escape(user.get('name') or '')}</b>\n"
        f"👫 Пол: <b>{html_escape(user.get('gender', 'Не указано'))}</b>\n"
        f"🔎 Ищет: <b>{html_escape(user.get('search_gender', 'Любой'))}</b>\n"
        f"📍 Город: <b>{html_escape(user.get('city') or 'Не указан')}</b>\n"
        f"🎯 Радиус поиска: <b>{format_radius(search_radius)}</b>\n"
        f"🎂 Возраст поиска: <b>{min_age}–{max_age}</b>\n\n"
        "Выбери, что хочешь изменить:"
    )
    await message.answer(text, reply_markup=create_edit_keyboard())

async def show_premium(message: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")
    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    await db.update_user(user_id, last_active=int(time.time()))
    is_premium = await db.check_premium(user_id)
    user = await db.get_user(user_id)
    superlikes = await db.get_available_superlikes(user_id)

    if is_premium:
        days_left = max(0, (int(user.get("premium_until", 0) or 0) - int(time.time()) + 86399) // 86400)
        text = (
            f"⭐ <b>Премиум активен!</b>\n"
            f"Осталось: <b>{days_left}</b> дн.\n"
            f"🎁 Суперлайков: <b>{superlikes}</b>\n"
        )
    else:
        text = (
            f"⭐ <b>Премиум возможности:</b>\n"
            f"• 5 суперлайков/день\n"
            f"• Больше внимания в поиске\n\n"
            f"Цена: <b>{PREMIUM_PRICE_STARS} ⭐</b>\n"
            f"У вас суперлайков: <b>{superlikes}</b>"
        )

    await message.answer(text, reply_markup=create_premium_keyboard())

async def show_matches(message: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")
    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    await db.update_user(user_id, last_active=int(time.time()))
    matches = await db.get_mutual_likes(user_id)

    if not matches:
        return await message.answer("😔 Пока нет взаимных симпатий.")

    for m in matches[:5]:
        text = (
            f"👤 <b>{html_escape(m['name'])}</b>, {m.get('age', '?')}\n"
            f"📍 {html_escape(m.get('city') or 'Город не указан')}\n"
        )
        kb = None
        if m.get("username"):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Написать", url=f"https://t.me/{m['username']}")]
            ])

        if m.get("photo"):
            try:
                await message.answer_photo(m["photo"], caption=text, reply_markup=kb)
                continue
            except Exception:
                pass

        await message.answer(text, reply_markup=kb)

async def send_next_profile(message: Message, user_id: int, max_attempts: int = 20):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")
    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    if not user.get("photo"):
        return await message.answer("⚠️ Добавь фото в профиль, чтобы начинать поиск.")

    await db.update_user(user_id, last_active=int(time.time()))
    await db.check_premium(user_id)

    for _ in range(max_attempts):
        candidate = await db.get_next_profile(user_id)
        if not candidate:
            return await message.answer("😔 Пока нет новых анкет для просмотра.")

        await db.add_view(user_id, candidate["id"])

        text = (
            f"👤 <b>{html_escape(candidate['name'])}</b>, {candidate.get('age', '?')}\n"
            f"📍 {html_escape(candidate.get('city') or 'Город не указан')}\n"
            f"📝 {html_escape(candidate.get('bio') or 'Нет описания')}"
        )

        try:
            if candidate.get("photo"):
                await message.answer_photo(
                    candidate["photo"],
                    caption=text,
                    reply_markup=create_viewing_keyboard(candidate["id"])
                )
            else:
                await message.answer(text, reply_markup=create_viewing_keyboard(candidate["id"]))
            return
        except TelegramBadRequest as e:
            if is_invalid_file_id_error(e):
                await db.update_user(candidate["id"], photo="", photo_updated=0)
                try:
                    await message.answer(text, reply_markup=create_viewing_keyboard(candidate["id"]))
                    return
                except Exception:
                    continue
            else:
                logger.warning(f"Cannot send candidate {candidate['id']} to {user_id}: {e}")
                try:
                    await message.answer(text, reply_markup=create_viewing_keyboard(candidate["id"]))
                    return
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Failed to send candidate {candidate['id']} to {user_id}: {e}")
            continue

    await message.answer("😔 Не удалось показать анкеты. Попробуйте позже.")

async def show_received_likes(message: Message, user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return await message.answer("Сначала создай профиль через /start")
    if user.get("is_banned"):
        return await message.answer("⛔ Ваш аккаунт заблокирован.")

    await db.update_user(user_id, last_active=int(time.time()))
    likes = await db.get_likes_received(user_id)

    if not likes:
        return await message.answer("😔 Пока тебя никто не лайкнул.")

    for like in likes[:10]:
        like_type = "⭐ Суперлайк" if like["is_super"] else "❤️ Лайк"
        text = (
            f"👤 <b>{html_escape(like['name'])}</b>, {like.get('age', '?')}\n"
            f"📍 {html_escape(like.get('city') or 'Город не указан')}\n"
            f"✨ {like_type}"
        )
        sent = False

        if like.get("photo"):
            try:
                await message.answer_photo(
                    like["photo"],
                    caption=text,
                    reply_markup=create_like_keyboard(like["like_id"], like["id"])
                )
                sent = True
            except Exception:
                sent = False

        if not sent:
            try:
                await message.answer(
                    text,
                    reply_markup=create_like_keyboard(like["like_id"], like["id"])
                )
                sent = True
            except Exception:
                sent = False

        if sent:
            await db.mark_like_seen(like["like_id"])

async def show_admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Доступ запрещён.")

    await message.answer("👨‍💼 <b>Админ-панель</b>\n\nВыберите действие:", reply_markup=create_admin_keyboard())

# ===================== РЕГИСТРАЦИЯ =====================
async def start_flow(
    message: Message,
    state: FSMContext,
    user_id: int,
    first_name: str,
    username: Optional[str],
    inviter_id: Optional[int] = None
):
    user = await db.get_user(user_id)

    if user:
        if user.get("is_banned"):
            await state.clear()
            return await message.answer("⛔ Ваш аккаунт заблокирован.")

        await state.clear()
        await db.update_user(user_id, last_active=int(time.time()))
        is_premium = await db.check_premium(user_id)
        await message.answer(
            f"👋 <b>С возвращением, {html_escape(first_name or 'друг')}</b>!",
            reply_markup=create_main_keyboard(user_id, is_premium)
        )
        return

    if inviter_id:
        await state.update_data(referral_inviter_id=inviter_id)

    await message.answer(
        "👋 Привет! Давай создадим анкету.\n\nКак тебя зовут?",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(ProfileStates.waiting_for_name)

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)

    inviter_id = None
    if len(parts) > 1 and parts[1].isdigit():
        code = int(parts[1])
        if code != user_id:
            inviter_id = code

    await start_flow(
        message=message,
        state=state,
        user_id=user_id,
        first_name=message.from_user.first_name or "друг",
        username=message.from_user.username,
        inviter_id=inviter_id
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    user = await db.get_user(message.from_user.id)

    if current_state is None:
        if user:
            is_premium = await db.check_premium(message.from_user.id)
            await message.reply(
                "🤷 Нет активных действий.",
                reply_markup=create_main_keyboard(message.from_user.id, is_premium)
            )
        else:
            await message.reply("🤷 Нет активных действий.")
        return

    await state.clear()
    if user:
        is_premium = await db.check_premium(message.from_user.id)
        await message.reply(
            "✅ Действие отменено.",
            reply_markup=create_main_keyboard(message.from_user.id, is_premium)
        )
    else:
        await message.reply("✅ Действие отменено. Используй /start или кнопки меню.")

# ===================== РЕГИСТРАЦИЯ: ВВОД ДАННЫХ =====================
@dp.message(ProfileStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    valid, err = validate_name(message.text)
    if not valid:
        return await message.answer(f"❌ {err}")

    await state.update_data(name=message.text.strip())
    await message.answer("Сколько тебе лет?")
    await state.set_state(ProfileStates.waiting_for_age)

@dp.message(ProfileStates.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    valid, age = validate_age(message.text)
    if not valid:
        return await message.answer("❌ Введи возраст от 18 до 100 цифрами")

    await state.update_data(age=age)
    await message.answer("Укажи свой пол:", reply_markup=create_gender_keyboard())
    await state.set_state(ProfileStates.waiting_for_gender)

@dp.message(ProfileStates.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    gender = parse_gender_choice(message.text, include_any=False)
    if not gender:
        return await message.answer("❌ Выбери из кнопок", reply_markup=create_gender_keyboard())

    await state.update_data(gender=gender)
    await message.answer("Кого ты ищешь?", reply_markup=create_gender_keyboard(include_any=True))
    await state.set_state(ProfileStates.waiting_for_search_gender)

@dp.message(ProfileStates.waiting_for_search_gender)
async def process_search_gender(message: Message, state: FSMContext):
    gender = parse_gender_choice(message.text, include_any=True)
    if not gender:
        return await message.answer("❌ Выбери из кнопок", reply_markup=create_gender_keyboard(include_any=True))

    await state.update_data(search_gender=gender)
    await message.answer("Отправь свою геолокацию для поиска рядом:", reply_markup=create_location_keyboard())
    await state.set_state(ProfileStates.waiting_for_location)

@dp.message(ProfileStates.waiting_for_location, F.location)
async def process_location(message: Message, state: FSMContext):
    city = await get_city_from_coords(message.location.latitude, message.location.longitude)
    await state.update_data(
        lat=message.location.latitude,
        lon=message.location.longitude,
        city=city
    )
    await message.answer("Добавь свое фото:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(ProfileStates.waiting_for_photo)

@dp.message(ProfileStates.waiting_for_location)
async def process_location_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, отправь геолокацию кнопкой ниже.", reply_markup=create_location_keyboard())

@dp.message(ProfileStates.waiting_for_photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    valid, err = validate_photo_meta(photo.file_size, photo.width, photo.height)
    if not valid:
        return await message.answer(f"❌ {err}")

    await state.update_data(photo=photo.file_id)
    await message.answer("Расскажи о себе (минимум 10 символов):")
    await state.set_state(ProfileStates.waiting_for_bio)

@dp.message(ProfileStates.waiting_for_photo)
async def process_photo_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, отправь фото одним сообщением.")

@dp.message(ProfileStates.waiting_for_bio)
async def process_bio(message: Message, state: FSMContext):
    valid, err = validate_bio(message.text)
    if not valid:
        return await message.answer(f"❌ {err}")

    data = await state.get_data()
    data["bio"] = message.text.strip()
    data["id"] = message.from_user.id
    data["username"] = message.from_user.username or ""

    inviter_id = data.get("referral_inviter_id")
    inviter_id = int(inviter_id) if inviter_id else None

    ok = await db.create_user(data, inviter_id=inviter_id)
    if not ok:
        await state.clear()
        return await message.answer("❌ Не удалось создать анкету. Попробуйте позже.")

    await state.clear()
    is_premium = await db.check_premium(message.from_user.id)
    await message.answer(
        "✅ Анкета создана! Добро пожаловать.",
        reply_markup=create_main_keyboard(message.from_user.id, is_premium)
    )

# ===================== ПРОФИЛЬ / НАСТРОЙКИ / ПРЕМИУМ =====================
async def handle_profile_entry(message: Message):
    await show_profile(message, message.from_user.id)

async def handle_settings_entry(message: Message):
    await show_settings(message, message.from_user.id)

async def handle_premium_entry(message: Message):
    await show_premium(message, message.from_user.id)

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await handle_profile_entry(message)

@dp.message(F.text == "👤 Мой профиль")
async def profile_button(message: Message):
    await handle_profile_entry(message)

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    await handle_settings_entry(message)

@dp.message(F.text == "⚙️ Настройки")
async def settings_button(message: Message):
    await handle_settings_entry(message)

@dp.callback_query(F.data == "edit_profile")
async def edit_profile_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("⚙️ <b>Настройки профиля</b>\n\nВыбери, что хочешь изменить:", reply_markup=create_edit_keyboard())

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    with suppress(Exception):
        await callback.message.delete()
    await show_profile(callback.message, callback.from_user.id)

@dp.callback_query(F.data == "premium_menu")
async def premium_menu_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await show_premium(callback.message, callback.from_user.id)

@dp.message(Command("premium"))
async def cmd_premium(message: Message):
    await handle_premium_entry(message)

@dp.message(F.text == "⭐ Премиум")
async def premium_button(message: Message):
    await handle_premium_entry(message)

@dp.message(Command("matches"))
async def cmd_matches(message: Message):
    await show_matches(message, message.from_user.id)

@dp.message(F.text.contains("Мои симпатии"))
async def matches_button(message: Message):
    await show_matches(message, message.from_user.id)

# ===================== РЕДАКТИРОВАНИЕ ПРОФИЛЯ =====================
@dp.callback_query(F.data.startswith("edit_"))
async def start_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    field = callback.data.removeprefix("edit_")
    await state.clear()

    if field == "name":
        await callback.message.answer("Введи новое имя:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_name)

    elif field == "age":
        await callback.message.answer("Введи новый возраст:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_age)

    elif field == "bio":
        await callback.message.answer("Введи новое описание:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_bio)

    elif field == "photo":
        await callback.message.answer("Отправь новое фото:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_photo)

    elif field == "location":
        await callback.message.answer("Отправь новую геолокацию:", reply_markup=create_location_keyboard())
        await state.set_state(EditStates.waiting_for_new_location)

    elif field == "gender":
        await callback.message.answer("Укажи свой пол:", reply_markup=create_gender_keyboard())
        await state.set_state(EditStates.waiting_for_new_gender)

    elif field == "search_gender":
        await callback.message.answer("Кого ты ищешь?", reply_markup=create_gender_keyboard(include_any=True))
        await state.set_state(EditStates.waiting_for_new_search_gender)

    elif field == "search_min_age":
        await callback.message.answer("Введи новый минимальный возраст поиска:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_search_min_age)

    elif field == "search_max_age":
        await callback.message.answer("Введи новый максимальный возраст поиска:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(EditStates.waiting_for_new_search_max_age)

    elif field == "search_radius":
        await callback.message.answer(
            "Введи новый радиус поиска в км (0 = без ограничений):",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(EditStates.waiting_for_new_search_radius)

@dp.message(EditStates.waiting_for_new_name)
async def save_edit_name(message: Message, state: FSMContext):
    valid, err = validate_name(message.text)
    if not valid:
        return await message.answer(f"❌ {err}")

    await db.update_user(message.from_user.id, name=message.text.strip(), last_active=int(time.time()))
    await state.clear()
    await message.answer("✅ Имя обновлено!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_age)
async def save_edit_age(message: Message, state: FSMContext):
    valid, age = validate_age(message.text)
    if not valid:
        return await message.answer("❌ Введи возраст от 18 до 100")

    await db.update_user(message.from_user.id, age=age, last_active=int(time.time()))
    await state.clear()
    await message.answer("✅ Возраст обновлён!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_bio)
async def save_edit_bio(message: Message, state: FSMContext):
    valid, err = validate_bio(message.text)
    if not valid:
        return await message.answer(f"❌ {err}")

    await db.update_user(message.from_user.id, bio=message.text.strip(), last_active=int(time.time()))
    await state.clear()
    await message.answer("✅ Описание обновлено!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_photo, F.photo)
async def save_edit_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    valid, err = validate_photo_meta(photo.file_size, photo.width, photo.height)
    if not valid:
        return await message.answer(f"❌ {err}")

    await db.update_user(
        message.from_user.id,
        photo=photo.file_id,
        photo_updated=1,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer("✅ Фото обновлено!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_photo)
async def save_edit_photo_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, отправь фото одним сообщением.")

@dp.message(EditStates.waiting_for_new_location, F.location)
async def save_edit_location(message: Message, state: FSMContext):
    city = await get_city_from_coords(message.location.latitude, message.location.longitude)
    await db.update_user(
        message.from_user.id,
        lat=message.location.latitude,
        lon=message.location.longitude,
        city=city,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer(
        f"✅ Город обновлён на: {html_escape(city)}",
        reply_markup=await build_main_keyboard(message.from_user.id)
    )

@dp.message(EditStates.waiting_for_new_location)
async def save_edit_location_invalid(message: Message):
    await message.answer("⚠️ Пожалуйста, отправь геолокацию кнопкой ниже.", reply_markup=create_location_keyboard())

@dp.message(EditStates.waiting_for_new_gender)
async def save_edit_gender(message: Message, state: FSMContext):
    gender = parse_gender_choice(message.text, include_any=False)
    if not gender:
        return await message.answer("❌ Выбери из кнопок", reply_markup=create_gender_keyboard())

    await db.update_user(
        message.from_user.id,
        gender=gender,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer("✅ Пол обновлён!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_search_gender)
async def save_edit_search_gender(message: Message, state: FSMContext):
    gender = parse_gender_choice(message.text, include_any=True)
    if not gender:
        return await message.answer("❌ Выбери из кнопок", reply_markup=create_gender_keyboard(include_any=True))

    await db.update_user(
        message.from_user.id,
        search_gender=gender,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer("✅ Настройки поиска обновлены!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_search_min_age)
async def save_edit_search_min_age(message: Message, state: FSMContext):
    valid, age = validate_age(message.text)
    if not valid:
        return await message.answer("❌ Введи возраст от 18 до 100 цифрами")

    user = await db.get_user(message.from_user.id)
    max_age = as_int(user.get("max_age_search"), DEFAULT_MAX_AGE)
    if age > max_age:
        return await message.answer(f"❌ Минимальный возраст не может быть больше максимального ({max_age})")

    await db.update_user(
        message.from_user.id,
        min_age_search=age,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer("✅ Минимальный возраст поиска обновлён!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_search_max_age)
async def save_edit_search_max_age(message: Message, state: FSMContext):
    valid, age = validate_age(message.text)
    if not valid:
        return await message.answer("❌ Введи возраст от 18 до 100 цифрами")

    user = await db.get_user(message.from_user.id)
    min_age = as_int(user.get("min_age_search"), DEFAULT_MIN_AGE)
    if age < min_age:
        return await message.answer(f"❌ Максимальный возраст не может быть меньше минимального ({min_age})")

    await db.update_user(
        message.from_user.id,
        max_age_search=age,
        last_active=int(time.time())
    )
    await state.clear()
    await message.answer("✅ Максимальный возраст поиска обновлён!", reply_markup=await build_main_keyboard(message.from_user.id))

@dp.message(EditStates.waiting_for_new_search_radius)
async def save_edit_search_radius(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        return await message.answer("❌ Введи число от 0 до 5000")

    radius = int(text)
    if not (0 <= radius <= 5000):
        return await message.answer("❌ Введи число от 0 до 5000")

    await db.update_user(
        message.from_user.id,
        search_radius=radius,
        last_active=int(time.time())
    )
    await state.clear()
    if radius == 0:
        text_reply = "✅ Радиус поиска обновлён: без ограничений"
    else:
        text_reply = f"✅ Радиус поиска обновлён: {radius} км"
    await message.answer(text_reply, reply_markup=await build_main_keyboard(message.from_user.id))

# ===================== ПОИСК / ЛАЙКИ / СИМПАТИИ =====================
async def handle_search_entry(message: Message):
    await send_next_profile(message, message.from_user.id)

@dp.message(Command("search"))
async def cmd_search(message: Message):
    if not rate_limiter.check(message.from_user.id, "search", RATE_LIMIT_SEARCH):
        return await message.answer("⚠️ Слишком много запросов, подожди минуту")
    await handle_search_entry(message)

@dp.message(F.text == "🔍 Искать")
async def search_button(message: Message):
    if not rate_limiter.check(message.from_user.id, "search", RATE_LIMIT_SEARCH):
        return await message.answer("⚠️ Слишком много запросов, подожди минуту")
    await handle_search_entry(message)

@dp.message(Command("likes"))
async def cmd_likes(message: Message):
    await show_received_likes(message, message.from_user.id)

@dp.message(F.text == "❤️ Мои лайки")
async def likes_button(message: Message):
    await show_received_likes(message, message.from_user.id)

@dp.callback_query(F.data.startswith("like_"))
async def handle_like(callback: CallbackQuery):
    profile_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    if not rate_limiter.check(user_id, "like", RATE_LIMIT_LIKES):
        await callback.answer("⚠️ Слишком много лайков", show_alert=True)
        return

    result = await db.add_like(user_id, profile_id, False)
    if result is True:
        await callback.answer("❤️ Лайк отправлен!")
    elif result == "self_like":
        await callback.answer("❌ Нельзя лайкать себя", show_alert=True)
        return
    elif result == "blocked":
        await callback.answer("⛔ Пользователь заблокирован", show_alert=True)
        return
    elif result == "no_profile":
        await callback.answer("❌ Профиль не найден", show_alert=True)
        return
    else:
        await callback.answer("❌ Ошибка или вы уже лайкали", show_alert=True)
        return

    with suppress(Exception):
        await callback.message.delete()
    await send_next_profile(callback.message, user_id)

@dp.callback_query(F.data.startswith("superlike_"))
async def handle_superlike(callback: CallbackQuery):
    profile_id = int(callback.data.split("_", 1)[1])
    user_id = callback.from_user.id

    if not rate_limiter.check(user_id, "superlike", RATE_LIMIT_SUPERLIKES):
        await callback.answer("⚠️ Слишком много суперлайков", show_alert=True)
        return

    result = await db.add_like(user_id, profile_id, True)
    if result is True:
        await callback.answer("⭐ Суперлайк отправлен!")
    elif result == "self_like":
        await callback.answer("❌ Нельзя лайкать себя", show_alert=True)
        return
    elif result == "no_superlikes":
        await callback.answer("❌ Нет суперлайков", show_alert=True)
        return
    elif result == "blocked":
        await callback.answer("⛔ Пользователь заблокирован", show_alert=True)
        return
    elif result == "no_profile":
        await callback.answer("❌ Профиль не найден", show_alert=True)
        return
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    with suppress(Exception):
        await callback.message.delete()
    await send_next_profile(callback.message, user_id)

@dp.callback_query(F.data == "next")
async def handle_next(callback: CallbackQuery):
    await callback.answer()
    with suppress(Exception):
        await callback.message.delete()
    await send_next_profile(callback.message, callback.from_user.id)

@dp.callback_query(F.data.startswith("accept_like_"))
async def handle_accept_like(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    from_id = int(parts[3])
    user_id = callback.from_user.id

    ok = await db.accept_like(from_id, user_id)
    if not ok:
        await callback.answer("❌ Лайк уже обработан или не найден", show_alert=True)
        return

    user1 = await db.get_user(user_id)
    user2 = await db.get_user(from_id)

    if user2 and user2.get("username"):
        await callback.message.answer(f"🎉 Взаимная симпатия!\nКонтакт: @{html_escape(user2['username'])}")
    elif user2:
        await callback.message.answer(f"🎉 Взаимная симпатия с {html_escape(user2['name'])}!\nUsername скрыт.")

    if user1 and user1.get("username"):
        await safe_execute(bot.send_message, from_id, f"🎉 Взаимная симпатия!\nКонтакт: @{html_escape(user1['username'])}")
    elif user1:
        await safe_execute(bot.send_message, from_id, f"🎉 Взаимная симпатия с {html_escape(user1['name'])}!\nUsername скрыт.")

    await callback.answer("✅ Принято!")
    with suppress(Exception):
        await callback.message.delete()

@dp.callback_query(F.data.startswith("reject_like_"))
async def handle_reject_like(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    from_id = int(parts[3])
    ok = await db.reject_like(from_id, callback.from_user.id)
    if ok:
        await callback.answer("❌ Отклонено")
    else:
        await callback.answer("❌ Уже обработано", show_alert=True)

    with suppress(Exception):
        await callback.message.delete()

@dp.callback_query(F.data.startswith("block_like_"))
async def handle_block_from_like(callback: CallbackQuery):
    block_id = int(callback.data.removeprefix("block_like_"))
    await db.block_user(callback.from_user.id, block_id)
    await callback.answer("⛔ Заблокировано")
    with suppress(Exception):
        await callback.message.delete()

@dp.callback_query(F.data.startswith("block_profile_"))
async def handle_block_profile(callback: CallbackQuery):
    block_id = int(callback.data.removeprefix("block_profile_"))
    await db.block_user(callback.from_user.id, block_id)
    await callback.answer("⛔ Заблокировано")
    with suppress(Exception):
        await callback.message.delete()

# ===================== ЖАЛОБЫ =====================
@dp.callback_query(F.data.startswith("report_"))
async def handle_report(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    target_id = int(callback.data.removeprefix("report_"))
    await state.clear()
    await state.update_data(report_target_id=target_id)
    await callback.message.answer(
        "🚫 Напиши причину жалобы одним сообщением:",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(ReportStates.waiting_for_reason)

@dp.message(ReportStates.waiting_for_reason)
async def save_report_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("report_target_id")
    if not target_id:
        await state.clear()
        return await message.answer("❌ Не удалось определить профиль для жалобы.")

    reason = (message.text or "").strip()
    if len(reason) < 3:
        return await message.answer("❌ Слишком короткая причина.")

    ok = await db.add_report(message.from_user.id, int(target_id), reason)
    await state.clear()

    if ok:
        await message.answer(
            "✅ Жалоба отправлена. Спасибо за помощь!",
            reply_markup=await build_main_keyboard(message.from_user.id)
        )
        if is_admin(ADMIN_ID):
            await safe_execute(
                bot.send_message,
                ADMIN_ID,
                f"🚨 <b>Новая жалоба</b>\n\n"
                f"От: <code>{message.from_user.id}</code>\n"
                f"На: <code>{target_id}</code>\n"
                f"Причина: {html_escape(reason)}"
            )
    else:
        await message.answer(
            "❌ Не удалось отправить жалобу.",
            reply_markup=await build_main_keyboard(message.from_user.id)
        )

# ===================== ПЛАТЕЖИ =====================
VALID_PAYLOADS = {"premium_pack", "superlikes_pack"}

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await callback.answer()
    await safe_execute(
        bot.send_invoice,
        chat_id=callback.from_user.id,
        title="Премиум доступ",
        description="30 дней премиум-статуса",
        payload="premium_pack",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Премиум 30 дней", amount=PREMIUM_PRICE_STARS)]
    )

@dp.callback_query(F.data == "buy_superlikes")
async def buy_superlikes(callback: CallbackQuery):
    await callback.answer()
    await safe_execute(
        bot.send_invoice,
        chat_id=callback.from_user.id,
        title="Пакет суперлайков",
        description="10 суперлайков",
        payload="superlikes_pack",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="10 суперлайков", amount=SUPERLIKE_PACK_PRICE_STARS)]
    )

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    ok = pre_checkout_query.invoice_payload in VALID_PAYLOADS
    await bot.answer_pre_checkout_query(
        pre_checkout_query.id,
        ok=ok,
        error_message=None if ok else "Неизвестный платёж"
    )

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id

    ok = await db.add_payment(
        user_id,
        payment.total_amount,
        payment.invoice_payload,
        payment.telegram_payment_charge_id,
        payment.provider_payment_charge_id
    )
    if not ok:
        logger.warning("Платёж успешно прошёл, но не сохранился в БД.")

    if payment.invoice_payload == "premium_pack":
        await db.give_premium(user_id, 30)
        await message.answer(
            "✅ Премиум активирован на 30 дней!",
            reply_markup=await build_main_keyboard(user_id)
        )
    elif payment.invoice_payload == "superlikes_pack":
        await db.give_superlikes(user_id, SUPERLIKE_PACK_COUNT)
        await message.answer(
            f"✅ Начислено {SUPERLIKE_PACK_COUNT} суперлайков!",
            reply_markup=await build_main_keyboard(user_id)
        )
    else:
        await message.answer(
            "✅ Платёж принят!",
            reply_markup=await build_main_keyboard(user_id)
        )

# ===================== АДМИН-ПАНЕЛЬ =====================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    await show_admin_panel(message)

@dp.message(F.text == "👑 Админ-панель")
async def admin_button(message: Message):
    await show_admin_panel(message)

@dp.callback_query(F.data.startswith("admin_"))
async def handle_admin(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await callback.answer()
    action = callback.data.removeprefix("admin_")

    if action == "stats":
        users = await db.get_users_count()
        today = await db.get_today_users()
        active = await db.get_active_today()
        total_likes = await db.get_total_likes()
        matches = await db.get_matches()
        revenue = await db.get_total_revenue()
        reports = await db.get_reports_count()

        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей: <b>{users}</b>\n"
            f"🆕 Сегодня: <b>{today}</b>\n"
            f"✅ Активных сегодня: <b>{active}</b>\n"
            f"❤️ Лайков: <b>{total_likes}</b>\n"
            f"💞 Матчей: <b>{matches}</b>\n"
            f"🚨 Жалоб: <b>{reports}</b>\n"
            f"💰 Доход: <b>{revenue}</b> ⭐"
        )
        await callback.message.edit_text(text, reply_markup=create_admin_keyboard())

    elif action == "broadcast":
        await callback.message.answer("Введите текст рассылки:")
        await state.set_state(AdminStates.waiting_for_broadcast)

    elif action == "premium":
        await callback.message.answer("Введите ID пользователя:")
        await state.set_state(AdminStates.waiting_for_premium_id)

    elif action == "ban":
        await callback.message.answer("Введите ID пользователя для бана:")
        await state.set_state(AdminStates.waiting_for_user_id_to_ban)

    elif action == "unban":
        await callback.message.answer("Введите ID пользователя для разбана:")
        await state.set_state(AdminStates.waiting_for_user_id_to_unban)

    elif action == "list":
        users = await db.get_all_users(limit=20)
        if not users:
            await callback.message.edit_text("Нет зарегистрированных пользователей.", reply_markup=create_admin_keyboard())
        else:
            lines = []
            for u in users:
                status = "⛔ Забанен" if u["is_banned"] else "✅ Активен"
                premium = "⭐" if u["is_premium"] else "—"
                username = f"@{u['username']}" if u.get("username") else "—"
                lines.append(
                    f"• <code>{u['id']}</code> | {html_escape(u['name'])} | "
                    f"{u.get('age', '?')} | {html_escape(u.get('city') or 'Не указан')} | "
                    f"{html_escape(username)} | {premium} | {status}"
                )
            text = "📋 <b>Последние 20 пользователей:</b>\n\n" + "\n".join(lines)
            await callback.message.edit_text(text, reply_markup=create_admin_keyboard())

@dp.message(AdminStates.waiting_for_broadcast)
async def admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Доступ запрещён.")
        await state.clear()
        return

    text = message.text or ""
    sent = 0
    failed = 0
    offset = 0
    batch_size = 1000

    status_msg = await message.reply("📨 Начинаю рассылку...")

    while True:
        users = await db.get_all_users(limit=batch_size, offset=offset)
        if not users:
            break

        for user in users:
            if user.get("is_banned"):
                continue
            result = await safe_execute(
                bot.send_message,
                user["id"],
                f"📢 <b>Сообщение от администратора:</b>\n\n{html_escape(text)}"
            )
            if result:
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

        offset += batch_size

    await safe_execute(status_msg.edit_text, f"✅ Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")
    await state.clear()
    await show_admin_panel(message)

@dp.message(AdminStates.waiting_for_premium_id)
async def admin_give_premium(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Доступ запрещён.")
        await state.clear()
        return

    try:
        uid = int(message.text.strip())
        user = await db.get_user(uid)
        if not user:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return

        await db.give_premium(uid, 30)
        await message.answer("✅ Премиум выдан на 30 дней")
        await safe_execute(bot.send_message, uid, "⭐ Вам выдан премиум администратором!")
    except Exception:
        await message.answer("❌ Ошибка")
    await state.clear()
    await show_admin_panel(message)

@dp.message(AdminStates.waiting_for_user_id_to_ban)
async def admin_ban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Доступ запрещён.")
        await state.clear()
        return

    try:
        uid = int(message.text.strip())
        ok = await db.ban_user(uid)
        if not ok:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        await message.answer("✅ Пользователь заблокирован")
        await safe_execute(bot.send_message, uid, "⛔ Ваш аккаунт заблокирован администратором.")
    except Exception:
        await message.answer("❌ Ошибка")
    await state.clear()
    await show_admin_panel(message)

@dp.message(AdminStates.waiting_for_user_id_to_unban)
async def admin_unban_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Доступ запрещён.")
        await state.clear()
        return

    try:
        uid = int(message.text.strip())
        ok = await db.unban_user(uid)
        if not ok:
            await message.answer("❌ Пользователь не найден")
            await state.clear()
            return
        await message.answer("✅ Пользователь разблокирован")
        await safe_execute(bot.send_message, uid, "✅ Ваш аккаунт разблокирован администратором.")
    except Exception:
        await message.answer("❌ Ошибка")
    await state.clear()
    await show_admin_panel(message)

# ===================== ФОНА И СТАРТ =====================
async def daily_reset_loop():
    await db.reset_daily_superlikes()

    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        await db.reset_daily_superlikes()
        logger.info("✅ Ежедневный сброс суперлайков выполнен")

async def rate_limiter_cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        rate_limiter.cleanup()

async def main():
    await db.init_db()

    cleanup_task = asyncio.create_task(rate_limiter_cleanup_loop())
    reset_task = asyncio.create_task(daily_reset_loop())

    logger.info("🤖 Бот запускается...")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramNetworkError as e:
        logger.error(f"Нет доступа к Telegram API: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_task.cancel()
        reset_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await reset_task
        await db.close()
        with suppress(Exception):
            await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
