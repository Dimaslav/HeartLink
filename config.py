import logging
import math
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Tuple

from dotenv import load_dotenv

load_dotenv()

# ===================== ТОКЕНЫ И ID =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env файле")

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# ===================== НАСТРОЙКИ БЕЗОПАСНОСТИ =====================
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB
MIN_PHOTO_RATIO = 0.3
MAX_PHOTO_RATIO = 3.0

RATE_LIMIT_LIKES = 30
RATE_LIMIT_SEARCH = 20
RATE_LIMIT_SUPERLIKES = 5

# ===================== ПЛАТЁЖНЫЕ НАСТРОЙКИ =====================
PREMIUM_PRICE_STARS = 50
SUPERLIKE_PACK_PRICE_STARS = 20
SUPERLIKE_PACK_COUNT = 10
BOOST_PRICE_STARS = 15
BOOST_DURATION_MIN = 30
PREMIUM_DAILY_SUPERLIKES = 5
FREE_DAILY_SUPERLIKES = 1

# ===================== ФИЛЬТРЫ ПО УМОЛЧАНИЮ =====================
DEFAULT_SEARCH_RADIUS = 100
DEFAULT_MIN_AGE = 18
DEFAULT_MAX_AGE = 100

# ===================== АНТИ-СПАМ =====================
PROFANITY_LIST = [
    "сука", "блядь", "хуй", "пизда", "еблан", "пиздец", "мудак",
    "fuck", "shit", "bitch", "asshole"
]

LINK_REGEX = re.compile(r"(?:https?://|www\.|t\.me|telegram\.me|@)", re.IGNORECASE)
PROFANITY_PATTERNS = [
    re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)
    for word in PROFANITY_LIST
]

# ===================== ЛОГИРОВАНИЕ =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

if PAYMENT_PROVIDER_TOKEN:
    logger.info("PAYMENT_PROVIDER_TOKEN загружен.")
else:
    logger.info("PAYMENT_PROVIDER_TOKEN не указан. Для Telegram Stars он не нужен.")

if PROXY_URL:
    logger.info(f"PROXY_URL задан: {PROXY_URL}")

# ===================== RATE LIMITER =====================
class RateLimiter:
    def __init__(self):
        self.actions = defaultdict(list)

    def check(self, uid: int, action: str, limit: int, period: int = 60) -> bool:
        key = f"{uid}:{action}"
        now = time.monotonic()
        self.actions[key] = [t for t in self.actions[key] if now - t < period]
        if len(self.actions[key]) >= limit:
            return False
        self.actions[key].append(now)
        return True

    def cleanup(self, max_age: int = 3600):
        now = time.monotonic()
        stale_keys = []
        for key, values in self.actions.items():
            values = [t for t in values if now - t < max_age]
            if values:
                self.actions[key] = values
            else:
                stale_keys.append(key)
        for key in stale_keys:
            self.actions.pop(key, None)


rate_limiter = RateLimiter()

# ===================== ВАЛИДАЦИЯ =====================
def validate_name(name: str) -> Tuple[bool, str]:
    name = (name or "").strip()
    if len(name) < 2:
        return False, "Минимум 2 символа"
    if len(name) > 50:
        return False, "Максимум 50 символов"
    if not re.fullmatch(r"[A-Za-zА-Яа-яЁё\s\-]+", name):
        return False, "Только буквы, пробелы и дефис"
    if LINK_REGEX.search(name):
        return False, "В имени нельзя использовать ссылки и @"
    if contains_profanity(name)[0]:
        return False, "Имя содержит недопустимые слова"
    return True, ""

def validate_age(age_str: str) -> Tuple[bool, int]:
    age_str = (age_str or "").strip()
    if not age_str.isdigit():
        return False, 0
    age = int(age_str)
    if not (18 <= age <= 100):
        return False, 0
    return True, age

def validate_bio(bio: str) -> Tuple[bool, str]:
    bio = (bio or "").strip()
    if len(bio) < 10:
        return False, "Минимум 10 символов"
    if len(bio) > 500:
        return False, "Максимум 500 символов"
    if LINK_REGEX.search(bio):
        return False, "В описании запрещены ссылки и @username"
    if contains_profanity(bio)[0]:
        return False, "Описание содержит недопустимые слова"
    return True, ""

def validate_photo_meta(file_size: int | None, width: int | None, height: int | None) -> Tuple[bool, str]:
    if file_size is not None and file_size > MAX_PHOTO_SIZE:
        return False, "Фото слишком большое (макс. 10 МБ)"
    if width and height:
        ratio = width / height
        if ratio < MIN_PHOTO_RATIO or ratio > MAX_PHOTO_RATIO:
            return False, "Неверное соотношение сторон фото"
    return True, ""

def contains_profanity(text: str) -> Tuple[bool, str]:
    if not text:
        return False, ""
    text = text.lower()
    for pat, word in zip(PROFANITY_PATTERNS, PROFANITY_LIST):
        if pat.search(text):
            return True, word
    if re.search(r"(.)\1{4,}", text):
        return True, "spam"
    return False, ""

def format_time(ts):
    if not ts:
        return "никогда"
    dt = datetime.fromtimestamp(int(ts))
    now = datetime.now()
    if dt.date() == now.date():
        return f"сегодня в {dt.strftime('%H:%M')}"
    if dt.date() == (now - timedelta(days=1)).date():
        return f"вчера в {dt.strftime('%H:%M')}"
    return dt.strftime("%d.%m.%Y %H:%M")

def start_of_today_ts() -> int:
    dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))
