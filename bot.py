import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from telegram import (
    Update,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("hugbot")


# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")


SUPPORT_USERNAME = os.environ.get(
    "SUPPORT_USERNAME",
    "@support"
).strip()


PORT = int(
    os.environ.get(
        "PORT",
        "10000"
    )
)


WEBHOOK_BASE = (
    os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("PUBLIC_URL")
    or ""
).strip()


# =========================================================
# PATHS
# =========================================================

BASE_DIR = Path(__file__).resolve().parent


ASSET_CANDIDATES = [
    BASE_DIR / "assets",
    BASE_DIR / "HugCollectBot" / "assets",
    Path.cwd() / "assets",
    Path.cwd() / "HugCollectBot" / "assets",
]


ASSETS_DIR = next(
    (
        p
        for p in ASSET_CANDIDATES
        if p.is_dir()
    ),
    BASE_DIR / "assets"
)


PROFILE_BANNER = ASSETS_DIR / "profile_banner.png"
MENU_BANNER = ASSETS_DIR / "menu_banner.png"


# =========================================================
# DATABASE
# =========================================================

# На Render лучше указывать:
# DB_PATH=/var/data/hugcollect.db
#
# Локально, если переменная не задана,
# база будет рядом с этим файлом.

DB_PATH = Path(
    os.environ.get(
        "DB_PATH",
        str(BASE_DIR / "hugcollect.db")
    )
)


DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True
)


logger.info(
    "DB_PATH=%s",
    DB_PATH
)


# =========================================================
# CRYPTOBOT
# =========================================================

CRYPTO_PAY_API_TOKEN = os.environ.get(
    "CRYPTO_PAY_API_TOKEN",
    ""
).strip()


CRYPTO_ASSET = os.environ.get(
    "CRYPTO_ASSET",
    "USDT"
).strip().upper()


CRYPTO_FALLBACK_WEEK = os.environ.get(
    "CRYPTO_FALLBACK_WEEK",
    ""
).strip()


CRYPTO_FALLBACK_MONTH = os.environ.get(
    "CRYPTO_FALLBACK_MONTH",
    ""
).strip()


# =========================================================
# CHANNELS
# =========================================================

WEEKLY_CHANNEL_ID = os.environ.get(
    "WEEKLY_CHANNEL_ID",
    ""
).strip()


MONTHLY_CHANNEL_ID = os.environ.get(
    "MONTHLY_CHANNEL_ID",
    ""
).strip()


SUBSCRIPTION_CHANNEL_ID = os.environ.get(
    "SUBSCRIPTION_CHANNEL_ID",
    ""
).strip()


REQUIRED_CHANNEL_ID = (
    os.environ.get("REQUIRED_CHANNEL_ID")
    or os.environ.get("REQUIRED_CHANNEL")
    or ""
).strip()


REQUIRED_CHANNEL_URL = os.environ.get(
    "REQUIRED_CHANNEL_URL",
    ""
).strip()


# =========================================================
# PLANS
# =========================================================

PLANS = {
    "week": {
        "title": "Недельная подписка",
        "days": 7,
        "usd": "5",
        "stars": 400,
        "channel": (
            WEEKLY_CHANNEL_ID
            or SUBSCRIPTION_CHANNEL_ID
        ),
    },
    "month": {
        "title": "Месячная подписка",
        "days": 30,
        "usd": "9",
        "stars": 700,
        "channel": (
            MONTHLY_CHANNEL_ID
            or SUBSCRIPTION_CHANNEL_ID
        ),
    },
}


# =========================================================
# PROMO
# =========================================================

PROMO_CODE = os.environ.get(
    "PROMO_CODE",
    "HUGVIP"
).strip().upper()


PROMO_PLAN = os.environ.get(
    "PROMO_PLAN",
    "month"
).strip().lower()


PROMO_MAX_USES = os.environ.get(
    "PROMO_MAX_USES",
    ""
).strip()


# =========================================================
# TEXTS
# =========================================================

GREETING = (
    "Привет, пользователь!\n"
    "Чем я могу вам помочь?"
)


SUPPORT_TEXT = (
    f"Если вы столкнулись с проблемой — "
    f"напишите: {SUPPORT_USERNAME}"
)


CHANNEL_GATE_TEXT = (
    "🔒 Сначала подпишитесь на канал\n\n"
    "Без подписки бот закрыт: меню, кабинет и "
    "основные функции недоступны.\n\n"
    "1️⃣ Нажмите «Подписаться»\n"
    "2️⃣ Подпишитесь на канал\n"
    "3️⃣ Вернитесь сюда и нажмите "
    "«Проверить подписку»"
)


# =========================================================
# DATABASE INIT
# =========================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    with db() as conn:

        conn.execute(
            """
            PRAGMA journal_mode=WAL
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 0,
                warmth INTEGER NOT NULL DEFAULT 1000,
                ref_code TEXT NOT NULL DEFAULT 'HUGGER',
                checks INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target TEXT NOT NULL,
                count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                method TEXT NOT NULL,
                payment_id TEXT,
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                method TEXT NOT NULL,
                external_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                plan TEXT NOT NULL,
                max_uses INTEGER,
                used INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                PRIMARY KEY (code, user_id)
            )
            """
        )

        # Уникальная защита от повторного использования
        # одной и той же оплаты
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_subscriptions_payment_id
            ON subscriptions(payment_id)
            WHERE payment_id IS NOT NULL
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_payments_external_id
            ON payments(external_id)
            WHERE external_id IS NOT NULL
            """
        )

        migrated = conn.execute(
            """
            SELECT value
            FROM meta
            WHERE key='level_zero_v1'
            """
        ).fetchone()

        if not migrated:
            conn.execute(
                """
                UPDATE profiles
                SET level=0
                """
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO meta(
                    key,
                    value
                )
                VALUES(
                    'level_zero_v1',
                    '1'
                )
                """
            )

        if PROMO_CODE:

            plan = (
                PROMO_PLAN
                if PROMO_PLAN in PLANS
                else "month"
            )

            max_uses = (
                int(PROMO_MAX_USES)
                if PROMO_MAX_USES.isdigit()
                else None
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO promo_codes(
                    code,
                    plan,
                    max_uses,
                    used,
                    active
                )
                VALUES(
                    ?,
                    ?,
                    ?,
                    0,
                    1
                )
                """,
                (
                    PROMO_CODE,
                    plan,
                    max_uses
                )
            )

        conn.commit()


# =========================================================
# PROFILE
# =========================================================

def ensure_profile(
    user_id: int
):
    with db() as conn:

        conn.execute(
            """
            INSERT OR IGNORE INTO profiles(
                user_id,
                level,
                warmth,
                ref_code,
                checks
            )
            VALUES(
                ?,
                0,
                1000,
                'HUGGER',
                0
            )
            """,
            (user_id,)
        )

        conn.commit()


# =========================================================
# SUBSCRIPTION
# =========================================================

def get_active_subscription(
    user_id: int
):
    """
    ВАЖНО:
    SQLite больше не сравнивает даты напрямую.

    Мы забираем подписки пользователя,
    а сравнение даты делаем в Python.
    """

    now = datetime.now(
        timezone.utc
    )

    with db() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=?
            ORDER BY expires_at DESC
            """,
            (user_id,)
        ).fetchall()

    for row in rows:

        try:
            expires_at = datetime.fromisoformat(
                row["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            if expires_at > now:
                return dict(row)

        except (
            ValueError,
            TypeError
        ):
            continue

    return None


def has_subscription(
    user_id: int
) -> bool:
    return (
        get_active_subscription(user_id)
        is not None
    )


# =========================================================
# PROFILE INFO
# =========================================================

def get_profile(
    user_id: int
) -> dict:

    ensure_profile(
        user_id
    )

    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM profiles
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        sent = conn.execute(
            """
            SELECT COUNT(*)
            FROM hugs
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()[0]

    profile = dict(row)

    profile["sent"] = sent

    subscription = get_active_subscription(
        user_id
    )

    profile["sub"] = subscription
    profile["sub_active"] = (
        subscription is not None
    )

    profile["sub_days_left"] = 0

    if subscription:

        try:
            expires_at = datetime.fromisoformat(
                subscription["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            seconds_left = (
                expires_at
                - datetime.now(timezone.utc)
            ).total_seconds()

            profile["sub_days_left"] = max(
                0,
                int(
                    seconds_left
                    / 86400
                )
            )

        except (
            ValueError,
            TypeError
        ):
            pass

    return profile


# =========================================================
# PAYMENT DB
# =========================================================

def create_payment(
    user_id: int,
    plan: str,
    method: str,
    external_id: str | None,
    status: str = "pending"
) -> int:

    now = datetime.now(
        timezone.utc
    ).isoformat()

    with db() as conn:

        cur = conn.execute(
            """
            INSERT INTO payments(
                user_id,
                plan,
                method,
                external_id,
                status,
                created_at,
                updated_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                user_id,
                plan,
                method,
                external_id,
                status,
                now,
                now
            )
        )

        conn.commit()

        return cur.lastrowid


def update_payment(
    payment_id: int,
    status: str,
    external_id: str | None = None
):
    now = datetime.now(
        timezone.utc
    ).isoformat()

    with db() as conn:

        if external_id:

            conn.execute(
                """
                UPDATE payments
                SET status=?,
                    external_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    external_id,
                    now,
                    payment_id
                )
            )

        else:

            conn.execute(
                """
                UPDATE payments
                SET status=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    now,
                    payment_id
                )
            )

        conn.commit()


def get_payment(
    payment_id: int
):
    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE id=?
            """,
            (payment_id,)
        ).fetchone()

    return dict(row) if row else None


def find_subscription_by_payment(
    payment_id: str
):
    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE payment_id=?
            LIMIT 1
            """,
            (payment_id,)
        ).fetchone()

    return dict(row) if row else None


# =========================================================
# ACTIVATE SUBSCRIPTION
# =========================================================

def activate_subscription(
    user_id: int,
    plan: str,
    method: str,
    payment_id: str | None
):
    """
    Выдаёт подписку и НЕ стирает старую.

    Если у пользователя уже есть активная подписка,
    новая подписка начинается после её окончания.
    """

    if plan not in PLANS:
        raise ValueError(
            f"Unknown plan: {plan}"
        )

    # Не начисляем одну и ту же оплату второй раз
    if payment_id:

        existing = find_subscription_by_payment(
            payment_id
        )

        if existing:

            expires_at = datetime.fromisoformat(
                existing["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            return expires_at

    now = datetime.now(
        timezone.utc
    )

    current = get_active_subscription(
        user_id
    )

    if current:

        current_exp = datetime.fromisoformat(
            current["expires_at"]
        )

        if current_exp.tzinfo is None:
            current_exp = current_exp.replace(
                tzinfo=timezone.utc
            )

        starts_at = max(
            now,
            current_exp
        )

    else:

        starts_at = now

    expires_at = (
        starts_at
        + timedelta(
            days=PLANS[plan]["days"]
        )
    )

    now_iso = now.isoformat()

    with db() as conn:

        # Дополнительная защита от дубля оплаты
        if payment_id:

            already = conn.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE payment_id=?
                LIMIT 1
                """,
                (payment_id,)
            ).fetchone()

            if already:

                result = datetime.fromisoformat(
                    already["expires_at"]
                )

                if result.tzinfo is None:
                    result = result.replace(
                        tzinfo=timezone.utc
                    )

                return result

        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id,
                plan,
                method,
                payment_id,
                starts_at,
                expires_at,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                user_id,
                plan,
                method,
                payment_id,
                starts_at.isoformat(),
                expires_at.isoformat(),
                now_iso
            )
        )

        conn.commit()

    logger.info(
        "SUBSCRIPTION ACTIVATED: "
        "user=%s plan=%s method=%s expires=%s",
        user_id,
        plan,
        method,
        expires_at.isoformat()
    )

    return expires_at


# =========================================================
# DAILY LIMIT
# =========================================================

def request_usage(
    user_id: int
) -> tuple[bool, int]:

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    with db() as conn:

        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=?
            AND usage_date=?
            """,
            (
                user_id,
                today
            )
        ).fetchone()

        used = (
            int(row["requests"])
            if row
            else 0
        )

        if used >= 50:
            return False, used

        if row:

            conn.execute(
                """
                UPDATE daily_usage
                SET requests=requests+1
                WHERE user_id=?
                AND usage_date=?
                """,
                (
                    user_id,
                    today
                )
            )

        else:

            conn.execute(
                """
                INSERT INTO daily_usage(
                    user_id,
                    usage_date,
                    requests
                )
                VALUES(
                    ?,
                    ?,
                    1
                )
                """,
                (
                    user_id,
                    today
                )
            )

        conn.commit()

    return True, used + 1


def usage_today(
    user_id: int
) -> int:

    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    with db() as conn:

        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=?
            AND usage_date=?
            """,
            (
                user_id,
                today
            )
        ).fetchone()

    if not row:
        return 0

    return int(
        row["requests"]
    )


# =========================================================
# HUGS / CHECKS
# =========================================================

def add_check(
    user_id: int
):
    with db() as conn:

        conn.execute(
            """
            UPDATE profiles
            SET checks=checks+1
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()


def add_hug(
    user_id: int,
    target: str,
    count: int
):
    with db() as conn:

        conn.execute(
            """
            INSERT INTO hugs(
                user_id,
                target,
                count,
                created_at
            )
            VALUES(
                ?,
                ?,
                ?,
                ?
            )
            """,
            (
                user_id,
                target,
                count,
                datetime.now().strftime(
                    "%d.%m.%Y %H:%M"
                )
            )
        )

        conn.execute(
            """
            UPDATE profiles
            SET warmth=MIN(
                1000,
                warmth+10
            )
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()


def get_history(
    user_id: int,
    limit: int = 10
):
    with db() as conn:

        return conn.execute(
            """
            SELECT
                target,
                count,
                created_at
            FROM hugs
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                user_id,
                limit
            )
        ).fetchall()


# =========================================================
# PROMO
# =========================================================

def redeem_promo(
    user_id: int,
    raw_code: str
) -> tuple[str, str | None]:

    code = (
        (raw_code or "")
        .strip()
        .upper()
    )

    if not code:
        return "empty", None

    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=?
            """,
            (code,)
        ).fetchone()

        if not row:
            return "invalid", None

        if not int(row["active"]):
            return "invalid", None

        plan = row["plan"]

        if plan not in PLANS:
            return "invalid", None

        already = conn.execute(
            """
            SELECT 1
            FROM promo_redemptions
            WHERE code=?
            AND user_id=?
            """,
            (
                code,
                user_id
            )
        ).fetchone()

        if already:
            return "already", None

        max_uses = row["max_uses"]

        if (
            max_uses is not None
            and int(row["used"]) >= int(max_uses)
        ):
            return "exhausted", None

        now = datetime.now(
            timezone.utc
        ).isoformat()

        try:

            conn.execute(
                """
                INSERT INTO promo_redemptions(
                    code,
                    user_id,
                    redeemed_at
                )
                VALUES(
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    code,
                    user_id,
                    now
                )
            )

            conn.execute(
                """
                UPDATE promo_codes
                SET used=used+1
                WHERE code=?
                """,
                (code,)
            )

            conn.commit()

        except sqlite3.IntegrityError:
            return "already", None

    return "ok", plan


# =========================================================
# KEYBOARDS
# =========================================================

def kb_home():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👤 Личный кабинет",
                callback_data="nav:profile"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Меню",
                callback_data="nav:menu"
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Техподдержка",
                callback_data="nav:support"
            )
        ],
    ])


def kb_profile():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💎 Оформить подписку",
                callback_data="nav:sub"
            )
        ],
        [
            InlineKeyboardButton(
                "🎟 Ввести промокод",
                callback_data="nav:promo"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home"
            )
        ],
    ])


def kb_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "sn1cти аккаунт❄️",
                callback_data="menu:hug"
            )
        ],
        [
            InlineKeyboardButton(
                "👀 П0иск",
                callback_data="menu:search"
            ),
            InlineKeyboardButton(
                "🔎 Проверить",
                callback_data="menu:check"
            ),
        ],
        [
            InlineKeyboardButton(
                "📜 История поиска",
                callback_data="menu:history"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home"
            )
        ],
    ])


def kb_back_home():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home"
            )
        ]
    ])


def kb_confirm_hug():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Да sn1cти аккаунт!",
                callback_data="hug:yes"
            )
        ],
        [
            InlineKeyboardButton(
                "Нет, отмена",
                callback_data="hug:no"
            )
        ],
    ])


def kb_hooray():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Ура! 🎉",
                callback_data="nav:home"
            )
        ]
    ])


def kb_sub_plans():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Неделя — 5$ / 400⭐",
                callback_data="sub:week"
            )
        ],
        [
            InlineKeyboardButton(
                "Месяц — 9$ / 700⭐",
                callback_data="sub:month"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Главное меню",
                callback_data="nav:home"
            )
        ],
    ])


def kb_pay_methods(
    plan: str
):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🪙 CryptoBot",
                callback_data=f"pay:crypto:{plan}"
            )
        ],
        [
            InlineKeyboardButton(
                "⭐️ Telegram Stars",
                callback_data=f"pay:stars:{plan}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="pay:back"
            )
        ],
    ])


def kb_after_pay(
    link: str | None
):
    rows = []

    if link:
        rows.append([
            InlineKeyboardButton(
                "🔐 Получить доступ к каналу",
                url=link
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "👤 Личный кабинет",
            callback_data="nav:profile"
        )
    ])

    rows.append([
        InlineKeyboardButton(
            "🏠 Главное меню",
            callback_data="nav:home"
        )
    ])

    return InlineKeyboardMarkup(rows)


def kb_channel_gate():
    rows = []

    url = required_channel_url()

    if url:
        rows.append([
            InlineKeyboardButton(
                "📢 Подписаться",
                url=url
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "✅ Проверить подписку",
            callback_data="gate:check"
        )
    ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# REQUIRED CHANNEL
# =========================================================

def required_channel_url() -> str:

    if REQUIRED_CHANNEL_URL:
        return REQUIRED_CHANNEL_URL

    channel = (
        REQUIRED_CHANNEL_ID
        .strip()
    )

    if not channel:
        return ""

    if channel.startswith(
        "https://"
    ):
        return channel

    if channel.startswith(
        "t.me/"
    ):
        return f"https://{channel}"

    if channel.startswith("@"):
        return (
            f"https://t.me/"
            f"{channel[1:]}"
        )

    if channel.lstrip("-").isdigit():
        return ""

    return f"https://t.me/{channel}"


async def is_required_channel_member(
    bot,
    user_id: int
) -> bool:

    if not REQUIRED_CHANNEL_ID:
        return True

    try:

        member = await bot.get_chat_member(
            REQUIRED_CHANNEL_ID,
            user_id
        )

        return member.status in {
            "creator",
            "administrator",
            "member",
            "restricted",
        }

    except TelegramError as exc:

        logger.error(
            "Channel subscription check error: %s",
            exc
        )

        return False


async def show_channel_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_ui(
        update,
        context,
        CHANNEL_GATE_TEXT,
        kb_channel_gate()
    )


async def channel_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """
    Проверяет обязательную подписку на канал.

    Не имеет никакого отношения к купленной
    подписке в таблице subscriptions.
    """

    if not REQUIRED_CHANNEL_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    q = update.callback_query

    # Кнопка проверки подписки
    if (
        q
        and q.data == "gate:check"
    ):

        await q.answer()

        if await is_required_channel_member(
            context.bot,
            user.id
        ):
            await show_home(
                update,
                context
            )

        else:

            await q.answer(
                "Вы ещё не подписались.",
                show_alert=True
            )

            await show_channel_gate(
                update,
                context
            )

        raise ApplicationHandlerStop

    # Не блокируем pre_checkout
    if update.pre_checkout_query:
        return

    # Не блокируем successful_payment
    if (
        update.message
        and update.message.successful_payment
    ):
        return

    if await is_required_channel_member(
        context.bot,
        user.id
    ):
        return

    await show_channel_gate(
        update,
        context
    )

    raise ApplicationHandlerStop


# =========================================================
# UI
# =========================================================

async def safe_delete(
    message: Message | None
):
    if not message:
        return

    try:

        await message.delete()

    except TelegramError:
        pass


async def send_ui(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    photo: Path | None = None,
    replace: bool = True
):
    chat_id = update.effective_chat.id
    q = update.callback_query

    if (
        replace
        and q
        and q.message
    ):
        await safe_delete(
            q.message
        )

    if (
        photo
        and photo.is_file()
    ):

        try:

            with photo.open("rb") as fh:

                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=text,
                    reply_markup=markup
                )

                return

        except Exception:
            logger.exception(
                "Failed to send photo"
            )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup
    )


async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["state"] = None

    await send_ui(
        update,
        context,
        GREETING,
        kb_home()
    )


async def show_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    profile = get_profile(
        user_id
    )

    await send_ui(
        update,
        context,
        profile_caption(
            profile,
            user_id
        ),
        kb_profile(),
        photo=PROFILE_BANNER
    )


async def show_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_ui(
        update,
        context,
        "Выберите действие 👇",
        kb_menu(),
        photo=MENU_BANNER
    )


async def show_support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_ui(
        update,
        context,
        SUPPORT_TEXT,
        kb_back_home()
    )


def subscription_shop_text() -> str:
    return (
        "‼️ Доступ к основным функциям бота ‼️\n\n"
        "5️⃣0️⃣ запросов в день\n"
        "✔️ Защита пользователя\n\n"
        "1️⃣ Цена на неделю — 5$ / 400⭐\n"
        "2️⃣ Цена на месяц — 9$ / 700⭐\n\n"
        "👇 Выберите срок подписки ниже 👇"
    )


async def show_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await send_ui(
        update,
        context,
        subscription_shop_text(),
        kb_sub_plans()
    )


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
) -> bool:

    if has_subscription(
        user_id
    ):
        return True

    await send_ui(
        update,
        context,
        (
            "❌ Упс\n\n"
            "⭕️ У вас не имеется подписка\n\n"
            "❗️ Оформите подписку в личном кабинете."
        ),
        kb_profile()
    )

    return False


# =========================================================
# PROFILE TEXT
# =========================================================

def profile_caption(
    profile: dict,
    user_id: int
) -> str:

    if profile["sub_active"]:
        sub_text = "✅ Активна"
    else:
        sub_text = "❌ Не оформлена"

    expires_text = ""

    if profile["sub"]:

        try:

            expires_at = datetime.fromisoformat(
                profile["sub"]["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            expires_text = (
                "\n⏳ До: "
                f"{expires_at.strftime('%d.%m.%Y %H:%M')}"
            )

        except (
            ValueError,
            TypeError
        ):
            pass

    return (
        "👤 Личный кабинет\n\n"
        f"🤗 Уровень — "
        f"{profile['level']}\n"
        f"💞 Приоритет — "
        f"{profile['warmth']}/1000\n"
        f"💬 Отправлено жалоб — "
        f"{profile['sent']}\n"
        f"👀 Всего поисков — "
        f"{profile['checks']}\n\n"
        f"💎 Подписка — "
        f"{sub_text}"
        f"{expires_text}\n"
        f"📊 Запросов сегодня — "
        f"{usage_today(user_id)}/50"
    )


# =========================================================
# HUG PROCESS
# =========================================================

async def run_hug_animation(
    bot,
    chat_id: int,
    message_id: int,
    user_id: int,
    target: str
):
    """
    Первое сообщение уже отправляет
    confirm_and_send_hug().

    Поэтому здесь НЕ пытаемся повторно
    редактировать его.
    """

    count = 356

    try:

        # Первая пауза
        await asyncio.sleep(1)

        progress_steps = [
            8,
            20,
            34,
            49,
            68,
            71,
            90,
        ]

        for percent in progress_steps:

            await bot.send_message(
                chat_id=chat_id,
                text=f"💤{percent}%💤"
            )

            await asyncio.sleep(1)

        # 100%
        await bot.send_message(
            chat_id=chat_id,
            text="💤100%💤"
        )

        await asyncio.sleep(0.5)

        # Сохраняем один результат
        add_hug(
            user_id,
            target,
            count
        )

        # Финал
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⭕️Отправлено жалоб — "
                f"{count}⭕️"
            ),
            reply_markup=kb_hooray()
        )

    except Exception:

        logger.exception(
            "Hug animation failed"
        )


async def start_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    if not await require_subscription(
        update,
        context,
        user_id
    ):
        return

    context.user_data[
        "state"
    ] = "awaiting_hug_target"

    await send_ui(
        update,
        context,
        (
            "Введите @username или ID аккаунта "
            "который будет sнесён"
        ),
        kb_back_home()
    )


async def confirm_and_send_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    if not await require_subscription(
        update,
        context,
        user_id
    ):
        context.user_data[
            "state"
        ] = None

        return

    ok, _used = request_usage(
        user_id
    )

    if not ok:

        context.user_data[
            "state"
        ] = None

        await send_ui(
            update,
            context,
            (
                "⛔️ Лимит на сегодня исчерпан.\n\n"
                "Доступно 50 запросов в день."
            ),
            kb_menu()
        )

        return

    context.user_data[
        "state"
    ] = None

    target = context.user_data.get(
        "hug_target",
        "другу"
    )

    initial_text = (
        "💤Идет процесс отправления жалоб 💤\n\n"
        "3%\n\n"
        "🔰Ожидание до 1 минуты🔰"
    )

    chat_id = update.effective_chat.id
    q = update.callback_query

    # Нажатие кнопки подтверждения
    if (
        q
        and q.message
    ):

        try:

            await q.message.edit_text(
                initial_text
            )

            msg_id = (
                q.message.message_id
            )

        except BadRequest:

            # Если сообщение нельзя изменить
            # отправляем новое
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=initial_text
            )

            msg_id = msg.message_id

    elif update.message:

        msg = await update.message.reply_text(
            initial_text
        )

        msg_id = msg.message_id

    else:

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=initial_text
        )

        msg_id = msg.message_id

    # ВАЖНО:
    # дальше процесс идёт отдельно
    context.application.create_task(
        run_hug_animation(
            context.bot,
            chat_id,
            msg_id,
            user_id,
            target
        ),
        update=update
    )


# =========================================================
# CHECK
# =========================================================

async def start_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    if not await require_subscription(
        update,
        context,
        user_id
    ):
        return

    context.user_data[
        "state"
    ] = "awaiting_check_target"

    await send_ui(
        update,
        context,
        "Введите @username для проверки:",
        kb_back_home()
    )


async def do_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target: str
):
    if not await require_subscription(
        update,
        context,
        user_id
    ):
        return

    ok, _used = request_usage(
        user_id
    )

    if not ok:

        await update.message.reply_text(
            (
                "⛔️ Лимит на сегодня исчерпан.\n\n"
                "Доступно 50 запросов в день."
            ),
            reply_markup=kb_menu()
        )

        return

    add_check(
        user_id
    )

    await update.message.reply_text(
        f"🔎 Результат проверки {target}\n\n"
        f"🤗 Обнимашковость — "
        f"{random.randint(60, 100)}%\n\n"
        "✅ Проверка завершена.",
        reply_markup=kb_menu()
    )


# =========================================================
# SEARCH
# =========================================================

async def do_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    if not await require_subscription(
        update,
        context,
        user_id
    ):
        return

    context.user_data[
        "state"
    ] = "awaiting_search"

    await send_ui(
        update,
        context,
        "Введите запрос для поиска:",
        kb_menu()
    )


# =========================================================
# HISTORY
# =========================================================

async def show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    history = get_history(
        user_id
    )

    if not history:

        await send_ui(
            update,
            context,
            (
                "📜 История пуста.\n\n"
                "Вы ещё ничего не искали."
            ),
            kb_menu()
        )

        return

    lines = []

    for row in history:

        lines.append(
            f"• {row['target']} — "
            f"{row['count']} "
            f"({row['created_at']})"
        )

    text = (
        "📜 История п0иска:\n\n"
        + "\n".join(lines)
    )

    await send_ui(
        update,
        context,
        text,
        kb_menu()
    )


# =========================================================
# NAV CALLBACK
# =========================================================

async def nav_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    q = update.callback_query

    user_id = q.from_user.id
    data = q.data

    ensure_profile(
        user_id
    )

    try:
        await q.answer()

    except TelegramError:
        pass

    if data == "nav:home":

        await show_home(
            update,
            context
        )

        return

    if data == "nav:profile":

        await show_profile(
            update,
            context
        )

        return

    if data == "nav:menu":

        await show_menu(
            update,
            context
        )

        return

    if data == "nav:support":

        await show_support(
            update,
            context
        )

        return

    if data == "nav:sub":

        await show_subscription(
            update,
            context
        )

        return

    if data == "nav:promo":

        context.user_data[
            "state"
        ] = "awaiting_promo"

        await send_ui(
            update,
            context,
            "🎟 Введите промокод:",
            kb_profile()
        )

        return

    if data == "menu:hug":

        await start_hug(
            update,
            context,
            user_id
        )

        return

    if data == "menu:search":

        await do_search(
            update,
            context,
            user_id
        )

        return

    if data == "menu:check":

        await start_check(
            update,
            context,
            user_id
        )

        return

    if data == "menu:history":

        await show_history(
            update,
            context,
            user_id
        )

        return

    if data == "hug:yes":

        if not context.user_data.get(
            "hug_target"
        ):

            await send_ui(
                update,
                context,
                (
                    "❌ Получатель не выбран.\n\n"
                    "Сначала введите аккаунт."
                ),
                kb_menu()
            )

            return

        await confirm_and_send_hug(
            update,
            context,
            user_id
        )

        return

    if data == "hug:no":

        context.user_data[
            "state"
        ] = None

        context.user_data.pop(
            "hug_target",
            None
        )

        await send_ui(
            update,
            context,
            "❌ Отменено.",
            kb_menu()
        )


# =========================================================
# SUB CALLBACK
# =========================================================

async def subscription_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    q = update.callback_query

    try:
        await q.answer()
    except TelegramError:
        pass

    user_id = q.from_user.id
    data = q.data

    ensure_profile(
        user_id
    )

    # -------------------------
    # PLAN
    # -------------------------

    if (
        data.startswith("sub:")
        and data.count(":") == 1
    ):

        plan = data.split(":")[1]

        if plan not in PLANS:
            return

        p = PLANS[plan]

        text = (
            f"💎 {p['title']}\n\n"
            f"⏳ Срок — {p['days']} дней\n"
            f"💵 Цена — {p['usd']}$\n"
            f"⭐️ Stars — {p['stars']}\n\n"
            "После оплаты подписка выдаётся "
            "автоматически.\n\n"
            "Выберите способ оплаты:"
        )

        await send_ui(
            update,
            context,
            text,
            kb_pay_methods(plan)
        )

        return

    # -------------------------
    # BACK
    # -------------------------

    if data == "pay:back":

        await show_subscription(
            update,
            context
        )

        return

    # -------------------------
    # CRYPTOBOT
    # -------------------------

    if data.startswith(
        "pay:crypto:"
    ):

        plan = data.split(":")[-1]

        if plan not in PLANS:
            return

        # Crypto API не настроен
        if not CRYPTO_PAY_API_TOKEN:

            fallback = (
                CRYPTO_FALLBACK_WEEK
                if plan == "week"
                else CRYPTO_FALLBACK_MONTH
            )

            if fallback:

                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💳 Оплатить в CryptoBot",
                            url=fallback
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Я оплатил",
                            callback_data=(
                                f"manual_crypto:"
                                f"{plan}"
                            )
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить",
                            callback_data="pay:cancel"
                        )
                    ],
                ])

                await send_ui(
                    update,
                    context,
                    (
                        "💳 Оплата через CryptoBot\n\n"
                        "После оплаты нажмите "
                        "«Я оплатил».\n\n"
                        "⚠️ Автоматическая проверка "
                        "будет работать после настройки "
                        "CRYPTO_PAY_API_TOKEN."
                    ),
                    markup
                )

                return

            await send_ui(
                update,
                context,
                (
                    "❌ CryptoBot не настроен.\n\n"
                    "Добавьте "
                    "CRYPTO_PAY_API_TOKEN "
                    "в Environment Variables."
                ),
                kb_pay_methods(plan)
            )

            return

        try:

            invoice, _payload = (
                await create_crypto_invoice(
                    user_id,
                    plan
                )
            )

            invoice_id = int(
                invoice["invoice_id"]
            )

            payment_db_id = create_payment(
                user_id,
                plan,
                "cryptobot",
                str(invoice_id)
            )

            url = (
                invoice.get("bot_invoice_url")
                or invoice.get(
                    "mini_app_invoice_url"
                )
                or invoice.get(
                    "web_app_invoice_url"
                )
            )

            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💳 Оплатить",
                        url=url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ Я оплатил",
                        callback_data=(
                            f"check_crypto:"
                            f"{invoice_id}:"
                            f"{plan}:"
                            f"{payment_db_id}"
                        )
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Отменить",
                        callback_data="pay:cancel"
                    )
                ],
            ])

            await send_ui(
                update,
                context,
                (
                    "💳 Оплата подписки\n\n"
                    f"{PLANS[plan]['title']} — "
                    f"{PLANS[plan]['usd']}$\n\n"
                    "Нажмите кнопку «Оплатить».\n"
                    "После оплаты бот автоматически "
                    "выдаст подписку."
                ),
                markup
            )

            context.application.create_task(
                crypto_watch(
                    context.application,
                    user_id,
                    plan,
                    payment_db_id,
                    invoice_id
                )
            )

        except Exception:

            logger.exception(
                "Crypto invoice creation failed"
            )

            await send_ui(
                update,
                context,
                (
                    "❌ Не удалось создать счёт.\n\n"
                    "Попробуйте ещё раз или "
                    "выберите Telegram Stars."
                ),
                kb_pay_methods(plan)
            )

        return

    # -------------------------
    # TELEGRAM STARS
    # -------------------------

    if data.startswith(
        "pay:stars:"
    ):

        plan = data.split(":")[-1]

        if plan not in PLANS:
            return

        p = PLANS[plan]

        payment_db_id = create_payment(
            user_id,
            plan,
            "stars",
            None
        )

        context.user_data[
            "pending_star_payment"
        ] = payment_db_id

        await safe_delete(
            q.message
        )

        try:

            await context.bot.send_invoice(
                chat_id=user_id,
                title=p["title"],
                description=(
                    f"Доступ к функциям бота "
                    f"на {p['days']} дней."
                ),
                payload=(
                    f"hug:"
                    f"{user_id}:"
                    f"{plan}:"
                    f"{payment_db_id}"
                ),
                provider_token="",
                currency="XTR",
                prices=[
                    LabeledPrice(
                        p["title"],
                        p["stars"]
                    )
                ],
            )

            await context.bot.send_message(
                user_id,
                (
                    "⭐️ Оплатите счёт выше.\n\n"
                    "После успешной оплаты "
                    "подписка активируется автоматически."
                ),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "❌ Отменить",
                            callback_data="pay:cancel"
                        )
                    ]
                ])
            )

        except TelegramError:

            logger.exception(
                "Stars invoice failed"
            )

            update_payment(
                payment_db_id,
                "failed"
            )

            await context.bot.send_message(
                user_id,
                (
                    "❌ Не удалось создать счёт Stars.\n\n"
                    "Попробуйте CryptoBot."
                ),
                reply_markup=kb_pay_methods(plan)
            )

        return

    # -------------------------
    # CANCEL
    # -------------------------

    if data == "pay:cancel":

        await send_ui(
            update,
            context,
            "❌ Оплата отменена.",
            kb_home()
        )

        return

    # -------------------------
    # CRYPTO CHECK
    # -------------------------

    if data.startswith(
        "check_crypto:"
    ):

        parts = data.split(":")

        if len(parts) < 4:
            return

        _, invoice_id, plan, payment_db_id = (
            parts[:4]
        )

        if plan not in PLANS:
            return

        existing = find_subscription_by_payment(
            str(invoice_id)
        )

        if existing:

            await send_ui(
                update,
                context,
                "✅ Оплата уже зачислена.",
                kb_after_pay(None)
            )

            return

        if not CRYPTO_PAY_API_TOKEN:

            await q.answer(
                "Автопроверка не настроена.",
                show_alert=True
            )

            return

        try:

            result = await crypto_api(
                "getInvoices",
                {
                    "invoice_ids": str(invoice_id)
                }
            )

            items = result.get(
                "items",
                []
            )

            if (
                items
                and items[0].get("status")
                == "paid"
            ):

                await grant_paid_access(
                    context.bot,
                    user_id,
                    plan,
                    "cryptobot",
                    str(invoice_id),
                    int(payment_db_id)
                )

                await safe_delete(
                    q.message
                )

                return

            await q.answer(
                (
                    "Оплата ещё не найдена.\n"
                    "Подождите немного и попробуйте снова."
                ),
                show_alert=True
            )

        except Exception:

            logger.exception(
                "Crypto manual check failed"
            )

            await q.answer(
                "Не удалось проверить оплату.",
                show_alert=True
            )

        return


# =========================================================
# CRYPTO API
# =========================================================

async def crypto_api(
    method: str,
    payload: dict
):
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError(
            "CRYPTO_PAY_API_TOKEN is not configured"
        )

    headers = {
        "Crypto-Pay-API-Token":
            CRYPTO_PAY_API_TOKEN
    }

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.post(
            f"https://pay.crypt.bot/api/{method}",
            json=payload,
            headers=headers
        )

        response.raise_for_status()

        data = response.json()

    if not data.get("ok"):

        raise RuntimeError(
            data.get(
                "error",
                {}
            ).get(
                "name",
                "Crypto Pay API error"
            )
        )

    return data["result"]


async def create_crypto_invoice(
    user_id: int,
    plan: str
):
    payload_id = (
        f"hug:"
        f"{user_id}:"
        f"{plan}:"
        f"{int(datetime.now(timezone.utc).timestamp())}"
    )

    result = await crypto_api(
        "createInvoice",
        {
            "asset": CRYPTO_ASSET,
            "amount": PLANS[plan]["usd"],
            "description": PLANS[plan]["title"],
            "payload": payload_id,
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 1800,
        }
    )

    return result, payload_id


async def crypto_watch(
    application: Application,
    user_id: int,
    plan: str,
    payment_db_id: int,
    invoice_id: int
):
    for _ in range(180):

        try:

            if find_subscription_by_payment(
                str(invoice_id)
            ):
                return

            result = await crypto_api(
                "getInvoices",
                {
                    "invoice_ids":
                        str(invoice_id)
                }
            )

            items = result.get(
                "items",
                []
            )

            if (
                items
                and items[0].get("status")
                == "paid"
            ):

                await grant_paid_access(
                    application.bot,
                    user_id,
                    plan,
                    "cryptobot",
                    str(invoice_id),
                    payment_db_id
                )

                return

            if (
                items
                and items[0].get("status")
                in {
                    "expired",
                    "invalid"
                }
            ):

                update_payment(
                    payment_db_id,
                    items[0].get("status")
                )

                return

        except Exception:

            logger.exception(
                "Crypto watcher error"
            )

        await asyncio.sleep(10)

    update_payment(
        payment_db_id,
        "timeout"
    )


# =========================================================
# ACCESS / INVITE
# =========================================================

async def issue_channel_invite(
    bot,
    user_id: int,
    plan: str
):
    channel = PLANS[plan]["channel"]

    if not channel:
        return None, "no_channel"

    try:

        member = await bot.get_chat_member(
            channel,
            user_id
        )

        if member.status in {
            "member",
            "administrator",
            "creator"
        }:
            return None, "already_member"

    except TelegramError:
        pass

    subscription = get_active_subscription(
        user_id
    )

    expire_ts = None

    if subscription:

        try:

            expires_at = datetime.fromisoformat(
                subscription["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            expire_ts = int(
                expires_at.timestamp()
            )

        except (
            ValueError,
            TypeError
        ):
            pass

    try:

        invite = await bot.create_chat_invite_link(
            chat_id=channel,
            name=(
                f"user {user_id} {plan}"
            ),
            member_limit=1,
            expire_date=expire_ts
        )

        return (
            invite.invite_link,
            "ok"
        )

    except TelegramError:

        logger.exception(
            "Could not create invite link"
        )

        return None, "error"


def access_granted_text(
    plan: str,
    expires: datetime,
    invite_status: str
):
    text = (
        "✅ Оплата получена!\n\n"
        f"💎 Подписка — "
        f"{PLANS[plan]['title']}\n"
        f"⏳ Действует до — "
        f"{expires.strftime('%d.%m.%Y %H:%M')}\n\n"
        "✔️ Функции бота открыты."
    )

    if invite_status == "already_member":

        text += (
            "\n\n🔐 Вы уже состоите "
            "в канале подписки."
        )

    elif invite_status == "no_channel":

        text += (
            "\n\nℹ️ Канал подписки "
            "не настроен."
        )

    elif invite_status == "error":

        text += (
            f"\n\n⚠️ Не удалось выдать "
            f"ссылку в канал.\n"
            f"Напишите {SUPPORT_USERNAME}."
        )

    return text


_notified_payments: set[str] = set()


async def grant_paid_access(
    bot,
    user_id: int,
    plan: str,
    method: str,
    payment_key: str,
    payment_db_id: int | None = None
):
    # Не выдаём одну оплату второй раз
    if payment_key in _notified_payments:

        return activate_subscription(
            user_id,
            plan,
            method,
            payment_key
        )

    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:

        _notified_payments.add(
            payment_key
        )

        return datetime.fromisoformat(
            existing["expires_at"]
        )

    _notified_payments.add(
        payment_key
    )

    if payment_db_id:

        update_payment(
            payment_db_id,
            "paid",
            payment_key
        )

    expires = activate_subscription(
        user_id,
        plan,
        method,
        payment_key
    )

    link, invite_status = await issue_channel_invite(
        bot,
        user_id,
        plan
    )

    text = access_granted_text(
        plan,
        expires,
        invite_status
    )

    if link:

        text += (
            "\n\n👇 Ваша ссылка в канал:"
        )

    await bot.send_message(
        user_id,
        text,
        reply_markup=kb_after_pay(link)
    )

    return expires


async def grant_promo_access(
    bot,
    user_id: int,
    plan: str,
    code: str
):
    payment_key = (
        f"promo:"
        f"{code}:"
        f"{user_id}"
    )

    # Промокод тоже не выдаём дважды
    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:

        expires = datetime.fromisoformat(
            existing["expires_at"]
        )

    else:

        expires = activate_subscription(
            user_id,
            plan,
            "promo",
            payment_key
        )

    link, invite_status = await issue_channel_invite(
        bot,
        user_id,
        plan
    )

    text = (
        f"✅ Промокод {code} активирован!\n\n"
        f"💎 Подписка — "
        f"{PLANS[plan]['title']}\n"
        f"⏳ Действует до — "
        f"{expires.strftime('%d.%m.%Y %H:%M')}"
    )

    if link:
        text += (
            "\n\n👇 Ссылка в канал:"
        )

    await bot.send_message(
        user_id,
        text,
        reply_markup=kb_after_pay(link)
    )

    return expires


# =========================================================
# PROMO TEXT
# =========================================================

async def apply_promo_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    raw_code: str
) -> bool:

    status, plan = redeem_promo(
        user_id,
        raw_code
    )

    if status == "empty":

        await update.message.reply_text(
            "Введите промокод.",
            reply_markup=kb_profile()
        )

        return True

    if status == "invalid":
        return False

    if status == "already":

        await update.message.reply_text(
            "❌ Вы уже использовали этот промокод.",
            reply_markup=kb_profile()
        )

        return True

    if status == "exhausted":

        await update.message.reply_text(
            "❌ Этот промокод больше недоступен.",
            reply_markup=kb_profile()
        )

        return True

    await grant_promo_access(
        context.bot,
        user_id,
        plan,
        raw_code.strip().upper()
    )

    return True


# =========================================================
# STARS
# =========================================================

async def pre_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.pre_checkout_query

    payload = (
        query.invoice_payload
        or ""
    )

    parts = payload.split(":")

    valid = (
        len(parts) >= 4
        and parts[0] == "hug"
        and parts[2] in PLANS
    )

    try:

        if valid:

            await query.answer(
                ok=True
            )

        else:

            await query.answer(
                ok=False,
                error_message=(
                    "Счёт недействителен. "
                    "Откройте оплату заново."
                )
            )

    except TelegramError:

        logger.exception(
            "Pre checkout error"
        )


async def successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    payment = (
        update.message
        .successful_payment
    )

    payload = payment.invoice_payload

    parts = payload.split(":")

    if (
        len(parts) < 4
        or parts[0] != "hug"
    ):

        await update.message.reply_text(
            (
                "❌ Не удалось распознать оплату.\n"
                f"Напишите {SUPPORT_USERNAME}."
            ),
            reply_markup=kb_back_home()
        )

        return

    _, payload_user, plan, payment_db_id = (
        parts[:4]
    )

    user_id = (
        update.effective_user.id
    )

    if (
        str(user_id) != payload_user
        or plan not in PLANS
    ):

        await update.message.reply_text(
            (
                "❌ Оплата не совпала "
                "с аккаунтом."
            ),
            reply_markup=kb_back_home()
        )

        return

    charge_id = (
        payment.telegram_payment_charge_id
    )

    try:

        db_id = int(
            payment_db_id
        )

    except ValueError:

        db_id = (
            context.user_data.get(
                "pending_star_payment"
            )
        )

    await grant_paid_access(
        context.bot,
        user_id,
        plan,
        "stars",
        charge_id,
        db_id
    )


# =========================================================
# /START
# =========================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    # Только создаём профиль, если его нет.
    # НИЧЕГО НЕ СБРАСЫВАЕМ.
    ensure_profile(
        user_id
    )

    # Просто читаем текущую подписку.
    # Она не изменяется.
    subscription = get_active_subscription(
        user_id
    )

    if subscription:

        logger.info(
            "START: active subscription "
            "user=%s plan=%s expires=%s",
            user_id,
            subscription["plan"],
            subscription["expires_at"]
        )

    else:

        logger.info(
            "START: no active subscription "
            "user=%s",
            user_id
        )

    context.user_data[
        "state"
    ] = None

    context.user_data.pop(
        "hug_target",
        None
    )

    await update.message.reply_text(
        "Меню перенесено в сообщение 👇",
        reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        GREETING,
        reply_markup=kb_home()
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = (
        update.message.text or ""
    ).strip()

    if not text:
        return

    user_id = (
        update.effective_user.id
    )

    ensure_profile(
        user_id
    )

    state = context.user_data.get(
        "state"
    )

    # -------------------------
    # HUG TARGET
    # -------------------------

    if state == "awaiting_hug_target":

        context.user_data[
            "hug_target"
        ] = text

        context.user_data[
            "state"
        ] = "awaiting_confirm"

        await update.message.reply_text(
            (
                f"Вы уверены, что хотите "
                f"отправить жалобы {text}?"
            ),
            reply_markup=kb_confirm_hug()
        )

        return

    # -------------------------
    # CONFIRM
    # -------------------------

    if state == "awaiting_confirm":

        await update.message.reply_text(
            "Нажмите кнопку выше 👆",
            reply_markup=kb_confirm_hug()
        )

        return

    # -------------------------
    # PROMO
    # -------------------------

    if state == "awaiting_promo":

        context.user_data[
            "state"
        ] = None

        applied = await apply_promo_text(
            update,
            context,
            user_id,
            text
        )

        if not applied:

            await update.message.reply_text(
                "❌ Промокод не найден.",
                reply_markup=kb_profile()
            )

        return

    # -------------------------
    # CHECK
    # -------------------------

    if state == "awaiting_check_target":

        context.user_data[
            "state"
        ] = None

        await do_check(
            update,
            context,
            user_id,
            text
        )

        return

    # -------------------------
    # SEARCH
    # -------------------------

    if state == "awaiting_search":

        context.user_data[
            "state"
        ] = None

        if not await require_subscription(
            update,
            context,
            user_id
        ):
            return

        ok, _used = request_usage(
            user_id
        )

        if not ok:

            await update.message.reply_text(
                (
                    "⛔️ Лимит на сегодня исчерпан.\n\n"
                    "Доступно 50 запросов в день."
                ),
                reply_markup=kb_menu()
            )

            return

        add_check(
            user_id
        )

        await update.message.reply_text(
            (
                f"🔎 Результат поиска: {text}\n\n"
                "✅ Поиск завершён."
            ),
            reply_markup=kb_menu()
        )

        return

    # -------------------------
    # ПРОМОКОД БЕЗ STATE
    # -------------------------

    applied = await apply_promo_text(
        update,
        context,
        user_id,
        text
    )

    if applied:
        return

    await update.message.reply_text(
        "Не понимаю 🙈 Воспользуйтесь меню.",
        reply_markup=kb_home()
    )


# =========================================================
# EXPIRATION
# =========================================================

async def remove_from_channels(
    bot,
    user_id: int
):
    channels = {
        p["channel"]
        for p in PLANS.values()
        if p["channel"]
    }

    for channel in channels:

        try:

            await bot.ban_chat_member(
                channel,
                user_id
            )

            await bot.unban_chat_member(
                channel,
                user_id,
                only_if_banned=True
            )

        except TelegramError as exc:

            logger.warning(
                "Could not remove expired "
                "user=%s from channel=%s: %s",
                user_id,
                channel,
                exc
            )


async def expiration_loop(
    application: Application
):
    while True:

        try:

            now = datetime.now(
                timezone.utc
            )

            with db() as conn:

                rows = conn.execute(
                    """
                    SELECT DISTINCT user_id
                    FROM subscriptions
                    """
                ).fetchall()

            for row in rows:

                user_id = row["user_id"]

                # Если активной подписки больше нет,
                # можно убрать пользователя из каналов.
                if not has_subscription(
                    user_id
                ):

                    await remove_from_channels(
                        application.bot,
                        user_id
                    )

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Expiration loop failed"
            )

        await asyncio.sleep(60)


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application
):
    application.create_task(
        expiration_loop(
            application
        )
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    logger.info(
        "Database initialized: %s",
        DB_PATH
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # -----------------------------------------------------
    # ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ
    # -----------------------------------------------------

    app.add_handler(
        TypeHandler(
            Update,
            channel_gate
        ),
        group=-1
    )

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    # -----------------------------------------------------
    # NAVIGATION
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            nav_callback,
            pattern=(
                r"^(nav:|menu:|hug:)"
            )
        )
    )

    # -----------------------------------------------------
    # SUBSCRIPTIONS
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            subscription_callback,
            pattern=(
                r"^(sub:|pay:|"
                r"manual_crypto:|"
                r"check_crypto:)"
            )
        )
    )

    # -----------------------------------------------------
    # STARS PRECHECKOUT
    # -----------------------------------------------------

    app.add_handler(
        PreCheckoutQueryHandler(
            pre_checkout
        )
    )

    # -----------------------------------------------------
    # SUCCESSFUL PAYMENT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment
        )
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )

    # -----------------------------------------------------
    # RENDER WEBHOOK
    # -----------------------------------------------------

    if WEBHOOK_BASE:

        webhook_path = BOT_TOKEN

        webhook_url = (
            f"{WEBHOOK_BASE.rstrip('/')}/"
            f"{webhook_path}"
        )

        logger.info(
            "Starting webhook: %s",
            webhook_url
        )

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )

    # -----------------------------------------------------
    # LOCAL POLLING
    # -----------------------------------------------------

    else:

        logger.info(
            "Starting polling"
        )

        app.run_polling(
            drop_pending_updates=True
        )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()