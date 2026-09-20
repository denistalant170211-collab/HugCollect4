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

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    ""
).strip()

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
# ADMIN
# =========================================================

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get(
        "ADMIN_IDS",
        ""
    ).split(",")
    if x.strip().isdigit()
}


REFERRAL_REWARD_DAYS = int(
    os.environ.get(
        "REFERRAL_REWARD_DAYS",
        "1"
    )
)


def is_admin(
    user_id: int
) -> bool:
    return user_id in ADMIN_IDS


# =========================================================
# PATHS
# =========================================================

BASE_DIR = Path(
    __file__
).resolve().parent


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


PROFILE_BANNER = (
    ASSETS_DIR / "profile_banner.png"
)

MENU_BANNER = (
    ASSETS_DIR / "menu_banner.png"
)


# =========================================================
# DATABASE
# =========================================================

# Если на Render будет Persistent Disk:
#
# /var/data
#
# база автоматически будет:
#
# /var/data/hugcollect.db
#
# Можно также задать вручную:
#
# DB_PATH=/var/data/hugcollect.db


RENDER_DATA_DIR = Path(
    "/var/data"
)


if (
    RENDER_DATA_DIR.exists()
    and os.access(
        RENDER_DATA_DIR,
        os.W_OK
    )
):
    DEFAULT_DB_PATH = (
        RENDER_DATA_DIR
        / "hugcollect.db"
    )
else:
    DEFAULT_DB_PATH = (
        BASE_DIR
        / "hugcollect.db"
    )


DB_PATH = Path(
    os.environ.get(
        "DB_PATH",
        str(DEFAULT_DB_PATH)
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


CRYPTO_FALLBACK_YEAR = os.environ.get(
    "CRYPTO_FALLBACK_YEAR",
    ""
).strip()


YEAR_PRICE_USD = os.environ.get(
    "YEAR_PRICE_USD",
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


YEARLY_CHANNEL_ID = os.environ.get(
    "YEARLY_CHANNEL_ID",
    ""
).strip()


SUBSCRIPTION_CHANNEL_ID = os.environ.get(
    "SUBSCRIPTION_CHANNEL_ID",
    ""
).strip()


REQUIRED_CHANNEL_ID = (
    os.environ.get(
        "REQUIRED_CHANNEL_ID"
    )
    or os.environ.get(
        "REQUIRED_CHANNEL"
    )
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

        # CryptoBot
        "usd": "5",

        # Telegram Stars
        "stars": 200,

        "channel": (
            WEEKLY_CHANNEL_ID
            or SUBSCRIPTION_CHANNEL_ID
        ),
    },

    "month": {
        "title": "Месячная подписка",
        "days": 30,

        # CryptoBot
        "usd": "9",

        # Telegram Stars
        "stars": 350,

        "channel": (
            MONTHLY_CHANNEL_ID
            or SUBSCRIPTION_CHANNEL_ID
        ),
    },

    "year": {
        "title": "Годовая подписка",
        "days": 365,

        # Цена CryptoBot берётся из ENV
        # YEAR_PRICE_USD.
        "usd": YEAR_PRICE_USD,

        # Telegram Stars
        "stars": 500,

        "channel": (
            YEARLY_CHANNEL_ID
            or MONTHLY_CHANNEL_ID
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
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def ensure_column(
    conn,
    table_name: str,
    column_name: str,
    definition: str
):
    columns = {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }

    if column_name not in columns:

        conn.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name} {definition}
            """
        )


def init_db():

    with db() as conn:

        conn.execute(
            """
            PRAGMA journal_mode=WAL
            """
        )

        # ---------------------------------------------
        # PROFILES
        # ---------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 0,
                warmth INTEGER NOT NULL DEFAULT 1000,
                ref_code TEXT NOT NULL DEFAULT 'HUGGER',
                checks INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT,
                referral_bonus_days INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        ensure_column(
            conn,
            "profiles",
            "first_seen_at",
            "TEXT"
        )

        ensure_column(
            conn,
            "profiles",
            "referral_bonus_days",
            "INTEGER NOT NULL DEFAULT 0"
        )

        # ---------------------------------------------
        # HUGS
        # ---------------------------------------------

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

        # ---------------------------------------------
        # SUBSCRIPTIONS
        # ---------------------------------------------

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

        # ---------------------------------------------
        # PAYMENTS
        # ---------------------------------------------

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

        # ---------------------------------------------
        # DAILY USAGE
        # ---------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (
                    user_id,
                    usage_date
                )
            )
            """
        )

        # ---------------------------------------------
        # META
        # ---------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        # ---------------------------------------------
        # PROMO CODES
        # ---------------------------------------------

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

        # ---------------------------------------------
        # PROMO REDEMPTIONS
        # ---------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                PRIMARY KEY (
                    code,
                    user_id
                )
            )
            """
        )

        # ---------------------------------------------
        # REFERRALS
        # ---------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                rewarded INTEGER NOT NULL DEFAULT 0,
                rewarded_at TEXT
            )
            """
        )

        # ---------------------------------------------
        # INDEXES
        # ---------------------------------------------

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

        # ---------------------------------------------
        # MIGRATION
        # ---------------------------------------------

        now_iso = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        conn.execute(
            """
            UPDATE profiles
            SET first_seen_at=?
            WHERE first_seen_at IS NULL
            """,
            (now_iso,)
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

        # ---------------------------------------------
        # DEFAULT PROMO
        # ---------------------------------------------

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
    now_iso = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    with db() as conn:

        conn.execute(
            """
            INSERT OR IGNORE INTO profiles(
                user_id,
                level,
                warmth,
                ref_code,
                checks,
                first_seen_at,
                referral_bonus_days
            )
            VALUES(
                ?,
                0,
                1000,
                'HUGGER',
                0,
                ?,
                0
            )
            """,
            (
                user_id,
                now_iso
            )
        )

        conn.commit()


# =========================================================
# SUBSCRIPTION
# =========================================================

def get_active_subscription(
    user_id: int
):
    """
    Все подписки пользователя достаются из SQLite,
    после чего expires_at сравнивается Python.

    Это позволяет избежать проблем с timezone
    и SQLite string comparison.
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

            logger.warning(
                "Invalid subscription date: "
                "user=%s value=%s",
                user_id,
                row["expires_at"]
            )

    return None


def has_subscription(
    user_id: int
) -> bool:
    return (
        get_active_subscription(
            user_id
        )
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
            SELECT COALESCE(
                SUM(count),
                0
            )
            FROM hugs
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()[0]

    profile = dict(row)

    profile["sent"] = int(
        sent or 0
    )

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
                    seconds_left / 86400
                )
            )

        except (
            ValueError,
            TypeError
        ):
            pass

    return profile


# =========================================================
# PAYMENTS
# =========================================================

def create_payment(
    user_id: int,
    plan: str,
    method: str,
    external_id: str | None,
    status: str = "pending"
) -> int:

    now = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

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
    now = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

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

    return (
        dict(row)
        if row
        else None
    )


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

    return (
        dict(row)
        if row
        else None
    )


# =========================================================
# ACTIVATE SUBSCRIPTION
# =========================================================

def activate_subscription(
    user_id: int,
    plan: str,
    method: str,
    payment_id: str | None
):
    if plan not in PLANS:
        raise ValueError(
            f"Unknown plan: {plan}"
        )

    # ---------------------------------------------
    # Защита от повторной оплаты
    # ---------------------------------------------

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

    # ---------------------------------------------
    # Текущая подписка
    # ---------------------------------------------

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

    # ---------------------------------------------
    # Бонусные дни рефералов
    # ---------------------------------------------

    with db() as conn:

        row = conn.execute(
            """
            SELECT referral_bonus_days
            FROM profiles
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    referral_bonus_days = (
        int(row["referral_bonus_days"])
        if row
        else 0
    )

    # ---------------------------------------------
    # Срок
    # ---------------------------------------------

    expires_at = (
        starts_at
        + timedelta(
            days=PLANS[plan]["days"]
        )
    )

    if referral_bonus_days > 0:

        expires_at += timedelta(
            days=referral_bonus_days
        )

    now_iso = now.isoformat()

    # ---------------------------------------------
    # INSERT
    # ---------------------------------------------

    with db() as conn:

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

        # Бонусные дни использованы
        if referral_bonus_days > 0:

            conn.execute(
                """
                UPDATE profiles
                SET referral_bonus_days=0
                WHERE user_id=?
                """,
                (user_id,)
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
# REFERRALS
# =========================================================

def add_referral(
    referrer_id: int,
    referred_id: int
) -> bool:

    if referrer_id == referred_id:
        return False

    ensure_profile(
        referrer_id
    )

    ensure_profile(
        referred_id
    )

    with db() as conn:

        existing = conn.execute(
            """
            SELECT 1
            FROM referrals
            WHERE referred_id=?
            LIMIT 1
            """,
            (referred_id,)
        ).fetchone()

        if existing:
            return False

        try:

            conn.execute(
                """
                INSERT INTO referrals(
                    referrer_id,
                    referred_id,
                    created_at,
                    rewarded
                )
                VALUES(
                    ?,
                    ?,
                    ?,
                    0
                )
                """,
                (
                    referrer_id,
                    referred_id,
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                )
            )

            conn.commit()

            logger.info(
                "REFERRAL ADDED: "
                "referrer=%s referred=%s",
                referrer_id,
                referred_id
            )

            return True

        except sqlite3.IntegrityError:
            return False


def get_referral_count(
    user_id: int
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM referrals
            WHERE referrer_id=?
            """,
            (user_id,)
        ).fetchone()

    return int(
        row[0]
    )


def get_referral_rewarded_count(
    user_id: int
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM referrals
            WHERE referrer_id=?
            AND rewarded=1
            """,
            (user_id,)
        ).fetchone()

    return int(
        row[0]
    )


def get_referral_bonus_days(
    user_id: int
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT referral_bonus_days
            FROM profiles
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

    if not row:
        return 0

    return int(
        row["referral_bonus_days"] or 0
    )


async def get_referral_link(
    bot,
    user_id: int
) -> str:

    me = await bot.get_me()

    return (
        f"https://t.me/"
        f"{me.username}"
        f"?start=ref_{user_id}"
    )


async def reward_referrer_for_purchase(
    bot,
    referred_user_id: int
):
    """
    Реферальный бонус начисляется только
    после первой реальной оплаты приглашённого.

    За каждую пару referrer -> referred
    бонус выдаётся только один раз.
    """

    with db() as conn:

        referral = conn.execute(
            """
            SELECT *
            FROM referrals
            WHERE referred_id=?
            AND rewarded=0
            LIMIT 1
            """,
            (referred_user_id,)
        ).fetchone()

    if not referral:
        return False

    referrer_id = (
        referral["referrer_id"]
    )

    if referrer_id == referred_user_id:
        return False

    reward_days = max(
        1,
        REFERRAL_REWARD_DAYS
    )

    subscription = get_active_subscription(
        referrer_id
    )

    # ---------------------------------------------
    # У реферера уже есть подписка
    # ---------------------------------------------

    if subscription:

        try:

            expires_at = datetime.fromisoformat(
                subscription["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            new_expires_at = (
                expires_at
                + timedelta(
                    days=reward_days
                )
            )

            with db() as conn:

                cur = conn.execute(
                    """
                    UPDATE referrals
                    SET rewarded=1,
                        rewarded_at=?
                    WHERE id=?
                    AND rewarded=0
                    """,
                    (
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                        referral["id"]
                    )
                )

                if cur.rowcount == 0:
                    return False

                conn.execute(
                    """
                    UPDATE subscriptions
                    SET expires_at=?
                    WHERE id=?
                    """,
                    (
                        new_expires_at.isoformat(),
                        subscription["id"]
                    )
                )

                conn.commit()

        except (
            ValueError,
            TypeError
        ):
            return False

    # ---------------------------------------------
    # У реферера подписки нет
    # ---------------------------------------------

    else:

        with db() as conn:

            cur = conn.execute(
                """
                UPDATE referrals
                SET rewarded=1,
                    rewarded_at=?
                WHERE id=?
                AND rewarded=0
                """,
                (
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                    referral["id"]
                )
            )

            if cur.rowcount == 0:
                return False

            conn.execute(
                """
                UPDATE profiles
                SET referral_bonus_days=
                    referral_bonus_days+?
                WHERE user_id=?
                """,
                (
                    reward_days,
                    referrer_id
                )
            )

            conn.commit()

    # ---------------------------------------------
    # Сообщение рефереру
    # ---------------------------------------------

    try:

        await bot.send_message(
            referrer_id,
            (
                "🎁 Реферальный бонус!\n\n"
                "Ваш реферал впервые оплатил "
                "подписку.\n\n"
                f"Вам начислено +{reward_days} "
                "день подписки ✅"
            )
        )

    except TelegramError:
        pass

    logger.info(
        "REFERRAL REWARDED: "
        "referrer=%s referred=%s days=%s",
        referrer_id,
        referred_user_id,
        reward_days
    )

    return True


# =========================================================
# DAILY LIMIT
# =========================================================

def request_usage(
    user_id: int
) -> tuple[bool, int]:

    today = (
        datetime.now(
            timezone.utc
        )
        .date()
        .isoformat()
    )

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

    today = (
        datetime.now(
            timezone.utc
        )
        .date()
        .isoformat()
    )

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

        now = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

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

def kb_home(
    user_id: int | None = None
):
    rows = [
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
                "👥 Реферальная система",
                callback_data="nav:referrals"
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Техподдержка",
                callback_data="nav:support"
            )
        ],
    ]

    if (
        user_id is not None
        and is_admin(user_id)
    ):
        rows.append([
            InlineKeyboardButton(
                "🛠 Админ-панель",
                callback_data="admin:panel"
            )
        ])

    return InlineKeyboardMarkup(
        rows
    )


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
                "Неделя — 200⭐",
                callback_data="sub:week"
            )
        ],
        [
            InlineKeyboardButton(
                "Месяц — 350⭐",
                callback_data="sub:month"
            )
        ],
        [
            InlineKeyboardButton(
                "Год — 500⭐",
                callback_data="sub:year"
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
                callback_data=(
                    f"pay:crypto:{plan}"
                )
            )
        ],
        [
            InlineKeyboardButton(
                "⭐️ Telegram Stars",
                callback_data=(
                    f"pay:stars:{plan}"
                )
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

    return InlineKeyboardMarkup(
        rows
    )


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

    return InlineKeyboardMarkup(
        rows
    )


def kb_referrals():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home"
            )
        ]
    ])


def kb_admin():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data="admin:stats"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Пользователи",
                callback_data="admin:users"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Покупки",
                callback_data="admin:payments"
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Рефералы",
                callback_data="admin:referrals"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home"
            )
        ],
    ])


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
        return (
            f"https://{channel}"
        )

    if channel.startswith("@"):

        return (
            f"https://t.me/"
            f"{channel[1:]}"
        )

    if channel.lstrip("-").isdigit():
        return ""

    return (
        f"https://t.me/"
        f"{channel}"
    )


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
            "Required channel check failed: "
            "channel=%s user=%s error=%s",
            REQUIRED_CHANNEL_ID,
            user_id,
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
    Отдельная обязательная подписка на канал.

    Она НЕ влияет на таблицу subscriptions.
    """

    if not REQUIRED_CHANNEL_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    # Админ не блокируется обязательной подпиской
    if is_admin(user.id):
        return

    q = update.callback_query

    # -----------------------------------------------------
    # Проверка кнопкой
    # -----------------------------------------------------

    if (
        q
        and q.data == "gate:check"
    ):

        subscribed = await is_required_channel_member(
            context.bot,
            user.id
        )

        if subscribed:

            try:
                await q.answer(
                    "Подписка найдена ✅"
                )
            except TelegramError:
                pass

            await show_home(
                update,
                context
            )

        else:

            try:
                await q.answer(
                    "Вы ещё не подписались.",
                    show_alert=True
                )
            except TelegramError:
                pass

            await show_channel_gate(
                update,
                context
            )

        raise ApplicationHandlerStop

    # -----------------------------------------------------
    # Не блокируем оплату Stars
    # -----------------------------------------------------

    if update.pre_checkout_query:
        return

    # -----------------------------------------------------
    # Не блокируем успешную оплату
    # -----------------------------------------------------

    if (
        update.message
        and update.message.successful_payment
    ):
        return

    # -----------------------------------------------------
    # Проверка
    # -----------------------------------------------------

    if await is_required_channel_member(
        context.bot,
        user.id
    ):
        return

    if q:

        try:
            await q.answer(
                "Сначала подпишитесь на канал.",
                show_alert=True
            )
        except TelegramError:
            pass

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

                try:

                    # Фото и текст в ОДНОМ сообщении.
                    # Caption находится СВЕРХУ фото.
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=fh,
                        caption=text,
                        reply_markup=markup,
                        show_caption_above_media=True
                    )

                except TypeError:

                    # Совместимость со старой
                    # версией python-telegram-bot.
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=fh,
                        caption=text,
                        reply_markup=markup
                    )

                return

        except Exception:

            logger.exception(
                "Failed to send photo UI"
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
    context.user_data[
        "state"
    ] = None

    user_id = (
        update.effective_user.id
    )

    await send_ui(
        update,
        context,
        GREETING,
        kb_home(user_id)
    )


async def show_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = (
        update.effective_user.id
    )

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
        "⭐️ Неделя — 200 Stars\n"
        "⭐️ Месяц — 350 Stars\n"
        "⭐️ Год — 500 Stars\n\n"
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
            "❗️ Оформите подписку "
            "в личном кабинете."
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

    bonus_days = get_referral_bonus_days(
        user_id
    )

    bonus_text = ""

    if bonus_days > 0:

        bonus_text = (
            f"\n🎁 Бонус рефералов — "
            f"+{bonus_days} д."
        )

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
        f"{expires_text}"
        f"{bonus_text}\n"
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
    Первое сообщение уже отправлено
    confirm_and_send_hug().

    Поэтому повторно его не отправляем.
    """

    count = 356

    try:

        # -------------------------
        # Стартовая пауза
        # -------------------------

        await asyncio.sleep(1)

        # -------------------------
        # Проценты
        # -------------------------

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

        # -------------------------
        # 100%
        # -------------------------

        await bot.send_message(
            chat_id=chat_id,
            text="💤100%💤"
        )

        await asyncio.sleep(
            0.5
        )

        # -------------------------
        # Записываем результат
        # -------------------------

        add_hug(
            user_id,
            target,
            count
        )

        # -------------------------
        # Финал
        # -------------------------

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

    chat_id = (
        update.effective_chat.id
    )

    q = update.callback_query

    # -------------------------
    # Кнопка подтверждения
    # -------------------------

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

            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=initial_text
            )

            msg_id = (
                msg.message_id
            )

    # -------------------------
    # Обычное сообщение
    # -------------------------

    elif update.message:

        msg = await update.message.reply_text(
            initial_text
        )

        msg_id = (
            msg.message_id
        )

    # -------------------------
    # Запасной вариант
    # -------------------------

    else:

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=initial_text
        )

        msg_id = (
            msg.message_id
        )

    # -------------------------
    # Запуск анимации
    # -------------------------

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
        (
            f"🔎 Результат проверки {target}\n\n"
            f"🤗 Обнимашковость — "
            f"{random.randint(60, 100)}%\n\n"
            "✅ Проверка завершена."
        ),
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
# REFERRAL PAGE
# =========================================================

async def show_referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = (
        update.effective_user.id
    )

    link = await get_referral_link(
        context.bot,
        user_id
    )

    total = get_referral_count(
        user_id
    )

    rewarded = get_referral_rewarded_count(
        user_id
    )

    bonus_days = get_referral_bonus_days(
        user_id
    )

    text = (
        "👥 Реферальная система\n\n"
        f"👤 Приглашено — {total}\n"
        f"💳 Оплатили — {rewarded}\n"
        f"🎁 Бонусных дней — {bonus_days}\n\n"
        "🔗 Ваша ссылка:\n"
        f"{link}\n\n"
        f"🎁 За каждого приглашённого, "
        f"который впервые оплатит подписку, "
        f"вы получаете +{REFERRAL_REWARD_DAYS} "
        "день подписки."
    )

    await send_ui(
        update,
        context,
        text,
        kb_referrals()
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

    # HOME
    if data == "nav:home":

        await show_home(
            update,
            context
        )

        return

    # PROFILE
    if data == "nav:profile":

        await show_profile(
            update,
            context
        )

        return

    # MENU
    if data == "nav:menu":

        await show_menu(
            update,
            context
        )

        return

    # SUPPORT
    if data == "nav:support":

        await show_support(
            update,
            context
        )

        return

    # SUBSCRIPTION
    if data == "nav:sub":

        await show_subscription(
            update,
            context
        )

        return

    # PROMO
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

    # REFERRALS
    if data == "nav:referrals":

        await show_referrals(
            update,
            context
        )

        return

    # HUG
    if data == "menu:hug":

        await start_hug(
            update,
            context,
            user_id
        )

        return

    # SEARCH
    if data == "menu:search":

        await do_search(
            update,
            context,
            user_id
        )

        return

    # CHECK
    if data == "menu:check":

        await start_check(
            update,
            context,
            user_id
        )

        return

    # HISTORY
    if data == "menu:history":

        await show_history(
            update,
            context,
            user_id
        )

        return

    # HUG YES
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

    # HUG NO
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
# ADMIN STATS
# =========================================================

def admin_stats() -> str:

    today = (
        datetime.now(
            timezone.utc
        )
        .date()
        .isoformat()
    )

    with db() as conn:

        # Всего пользователей
        total_users = conn.execute(
            """
            SELECT COUNT(*)
            FROM profiles
            """
        ).fetchone()[0]

        # Новые сегодня
        new_today = conn.execute(
            """
            SELECT COUNT(*)
            FROM profiles
            WHERE substr(
                first_seen_at,
                1,
                10
            )=?
            """,
            (today,)
        ).fetchone()[0]

        # Всего успешных оплат
        total_paid = conn.execute(
            """
            SELECT COUNT(*)
            FROM payments
            WHERE status='paid'
            """
        ).fetchone()[0]

        # Уникальные покупатели
        unique_buyers = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id)
            FROM payments
            WHERE status='paid'
            """
        ).fetchone()[0]

        # Stars
        stars_payments = conn.execute(
            """
            SELECT plan
            FROM payments
            WHERE status='paid'
            AND method='stars'
            """
        ).fetchall()

        # Crypto
        crypto_payments = conn.execute(
            """
            SELECT plan
            FROM payments
            WHERE status='paid'
            AND method='cryptobot'
            """
        ).fetchall()

        # Рефералы
        total_referrals = conn.execute(
            """
            SELECT COUNT(*)
            FROM referrals
            """
        ).fetchone()[0]

        converted_referrals = conn.execute(
            """
            SELECT COUNT(*)
            FROM referrals
            WHERE rewarded=1
            """
        ).fetchone()[0]

        # Запросы сегодня
        requests_today = conn.execute(
            """
            SELECT COALESCE(
                SUM(requests),
                0
            )
            FROM daily_usage
            WHERE usage_date=?
            """,
            (today,)
        ).fetchone()[0]

        # Всего запусков sn1c
        total_hugs = conn.execute(
            """
            SELECT COALESCE(
                SUM(count),
                0
            )
            FROM hugs
            """
        ).fetchone()[0]

        # Проверки
        total_checks = conn.execute(
            """
            SELECT COALESCE(
                SUM(checks),
                0
            )
            FROM profiles
            """
        ).fetchone()[0]

        # Все подписки
        subscription_rows = conn.execute(
            """
            SELECT user_id, expires_at
            FROM subscriptions
            """
        ).fetchall()

    # -------------------------
    # Активные подписчики
    # -------------------------

    now = datetime.now(
        timezone.utc
    )

    active_users = set()

    for row in subscription_rows:

        try:

            expires_at = datetime.fromisoformat(
                row["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            if expires_at > now:

                active_users.add(
                    row["user_id"]
                )

        except (
            ValueError,
            TypeError
        ):
            continue

    active_subscriptions = len(
        active_users
    )

    # -------------------------
    # Stars revenue
    # -------------------------

    total_stars = 0

    for row in stars_payments:

        plan = row["plan"]

        if plan in PLANS:
            total_stars += int(
                PLANS[plan]["stars"]
            )

    # -------------------------
    # Crypto revenue
    # -------------------------

    total_crypto_usd = 0.0

    for row in crypto_payments:

        plan = row["plan"]

        if plan not in PLANS:
            continue

        try:

            total_crypto_usd += float(
                PLANS[plan]["usd"] or 0
            )

        except (
            ValueError,
            TypeError
        ):
            pass

    return (
        "🛠 АДМИН-ПАНЕЛЬ\n\n"

        "👥 Пользователи\n"
        f"• Всего — {total_users}\n"
        f"• Сегодня — {new_today}\n\n"

        "💎 Подписки\n"
        f"• Активных — {active_subscriptions}\n"
        f"• Всего оплат — {total_paid}\n"
        f"• Уникальных покупателей — "
        f"{unique_buyers}\n\n"

        "💰 ОПЛАТЫ\n"
        f"• Telegram Stars — "
        f"{total_stars}⭐\n"
        f"• CryptoBot — "
        f"{total_crypto_usd:.2f}$\n\n"

        "🔗 Рефералы\n"
        f"• Всего — {total_referrals}\n"
        f"• Конвертировано — "
        f"{converted_referrals}\n\n"

        "📊 Активность\n"
        f"• Запросов сегодня — "
        f"{requests_today}\n"
        f"• Всего sn1cов — "
        f"{total_hugs}\n"
        f"• Всего проверок — "
        f"{total_checks}"
    )


async def show_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = (
        update.effective_user.id
    )

    if not is_admin(
        user_id
    ):

        await send_ui(
            update,
            context,
            "❌ Доступ запрещён.",
            kb_home(user_id)
        )

        return

    await send_ui(
        update,
        context,
        admin_stats(),
        kb_admin()
    )


# =========================================================
# ADMIN USERS
# =========================================================

async def show_admin_users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                user_id,
                first_seen_at
            FROM profiles
            ORDER BY first_seen_at DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:

        text = (
            "👥 Пользователей пока нет."
        )

    else:

        lines = [
            "👥 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ\n"
        ]

        for row in rows:

            lines.append(
                f"• {row['user_id']}\n"
                f"  {row['first_seen_at']}"
            )

        text = "\n".join(
            lines
        )

    await send_ui(
        update,
        context,
        text,
        kb_admin()
    )


# =========================================================
# ADMIN PAYMENTS
# =========================================================

async def show_admin_payments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                user_id,
                plan,
                method,
                status,
                created_at
            FROM payments
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:

        text = (
            "💳 Покупок пока нет."
        )

    else:

        lines = [
            "💳 ПОСЛЕДНИЕ ПОКУПКИ\n"
        ]

        for row in rows:

            if row["status"] == "paid":
                status = "✅"
            elif row["status"] == "pending":
                status = "⏳"
            else:
                status = "❌"

            lines.append(
                f"{status} "
                f"{row['user_id']} | "
                f"{row['plan']} | "
                f"{row['method']}\n"
                f"   {row['created_at']}"
            )

        text = "\n".join(
            lines
        )

    await send_ui(
        update,
        context,
        text,
        kb_admin()
    )


# =========================================================
# ADMIN REFERRALS
# =========================================================

async def show_admin_referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                referrer_id,
                referred_id,
                created_at,
                rewarded
            FROM referrals
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:

        text = (
            "🔗 Рефералов пока нет."
        )

    else:

        lines = [
            "🔗 ПОСЛЕДНИЕ РЕФЕРАЛЫ\n"
        ]

        for row in rows:

            reward = (
                "✅"
                if row["rewarded"]
                else "⏳"
            )

            lines.append(
                f"{reward} "
                f"{row['referrer_id']} → "
                f"{row['referred_id']}\n"
                f"   {row['created_at']}"
            )

        text = "\n".join(
            lines
        )

    await send_ui(
        update,
        context,
        text,
        kb_admin()
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    q = update.callback_query

    user_id = (
        q.from_user.id
    )

    if not is_admin(
        user_id
    ):

        try:
            await q.answer(
                "Доступ запрещён.",
                show_alert=True
            )
        except TelegramError:
            pass

        return

    try:
        await q.answer()
    except TelegramError:
        pass

    data = q.data

    if data == "admin:panel":

        await show_admin_panel(
            update,
            context
        )

        return

    if data == "admin:stats":

        await send_ui(
            update,
            context,
            admin_stats(),
            kb_admin()
        )

        return

    if data == "admin:users":

        await show_admin_users(
            update,
            context
        )

        return

    if data == "admin:payments":

        await show_admin_payments(
            update,
            context
        )

        return

    if data == "admin:referrals":

        await show_admin_referrals(
            update,
            context
        )

        return


# =========================================================
# ACCESS / INVITE
# =========================================================

async def issue_channel_invite(
    bot,
    user_id: int,
    plan: str
):
    if plan not in PLANS:
        return None, "no_channel"

    channel = (
        PLANS[plan]["channel"]
    )

    if not channel:

        logger.error(
            "CHANNEL NOT CONFIGURED: "
            "plan=%s user=%s",
            plan,
            user_id
        )

        return None, "no_channel"

    logger.info(
        "Channel invite: "
        "plan=%s channel=%s user=%s",
        plan,
        channel,
        user_id
    )

    # -------------------------
    # Проверка участника
    # -------------------------

    try:

        member = await bot.get_chat_member(
            chat_id=channel,
            user_id=user_id
        )

        if member.status in {
            "member",
            "administrator",
            "creator"
        }:

            return (
                None,
                "already_member"
            )

    except TelegramError as exc:

        logger.warning(
            "Could not check channel member: "
            "plan=%s channel=%s user=%s "
            "error=%s",
            plan,
            channel,
            user_id,
            exc
        )

    # -------------------------
    # Дата окончания ссылки
    # -------------------------

    subscription = get_active_subscription(
        user_id
    )

    expire_date = None

    if subscription:

        try:

            expires_at = datetime.fromisoformat(
                subscription["expires_at"]
            )

            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(
                    tzinfo=timezone.utc
                )

            expire_date = expires_at

        except (
            ValueError,
            TypeError
        ):
            pass

    # -------------------------
    # Создание ссылки
    # -------------------------

    try:

        invite = await bot.create_chat_invite_link(
            chat_id=channel,
            name=(
                f"user {user_id} "
                f"{plan}"
            ),
            member_limit=1,
            expire_date=expire_date
        )

        logger.info(
            "Invite link created: "
            "plan=%s channel=%s user=%s",
            plan,
            channel,
            user_id
        )

        return (
            invite.invite_link,
            "ok"
        )

    except BadRequest as exc:

        logger.error(
            "CHANNEL INVITE FAILED: "
            "plan=%s channel=%s user=%s "
            "error=%s",
            plan,
            channel,
            user_id,
            exc
        )

        return None, "error"

    except TelegramError as exc:

        logger.error(
            "CHANNEL INVITE TELEGRAM ERROR: "
            "plan=%s channel=%s user=%s "
            "error=%s",
            plan,
            channel,
            user_id,
            exc
        )

        return None, "error"


def access_granted_text(
    plan: str,
    expires: datetime,
    invite_status: str
):
    if expires.tzinfo is None:
        expires = expires.replace(
            tzinfo=timezone.utc
        )

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
            "\n\n⚠️ Не удалось выдать "
            "ссылку в канал.\n"
            f"Напишите {SUPPORT_USERNAME}."
        )

    return text


# Защита от повторной отправки
_notified_payments: set[str] = set()


async def grant_paid_access(
    bot,
    user_id: int,
    plan: str,
    method: str,
    payment_key: str,
    payment_db_id: int | None = None
):
    # -------------------------
    # Если уже есть такая подписка
    # -------------------------

    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:

        try:

            expires = datetime.fromisoformat(
                existing["expires_at"]
            )

            if expires.tzinfo is None:
                expires = expires.replace(
                    tzinfo=timezone.utc
                )

            return expires

        except (
            ValueError,
            TypeError
        ):
            return None

    # -------------------------
    # Защита от дубля события
    # -------------------------

    if payment_key in _notified_payments:

        return activate_subscription(
            user_id,
            plan,
            method,
            payment_key
        )

    _notified_payments.add(
        payment_key
    )

    # -------------------------
    # Обновляем payment
    # -------------------------

    if payment_db_id:

        update_payment(
            payment_db_id,
            "paid",
            payment_key
        )

    # -------------------------
    # Создаём подписку
    # -------------------------

    expires = activate_subscription(
        user_id,
        plan,
        method,
        payment_key
    )

    # -------------------------
    # Реферальный бонус
    # -------------------------

    await reward_referrer_for_purchase(
        bot,
        user_id
    )

    # -------------------------
    # Канал
    # -------------------------

    link, invite_status = (
        await issue_channel_invite(
            bot,
            user_id,
            plan
        )
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
        reply_markup=kb_after_pay(
            link
        )
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

    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:

        expires = datetime.fromisoformat(
            existing["expires_at"]
        )

        if expires.tzinfo is None:
            expires = expires.replace(
                tzinfo=timezone.utc
            )

    else:

        expires = activate_subscription(
            user_id,
            plan,
            "promo",
            payment_key
        )

    link, invite_status = (
        await issue_channel_invite(
            bot,
            user_id,
            plan
        )
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

    elif invite_status == "error":

        text += (
            "\n\n⚠️ Ссылка в канал "
            "не была создана.\n"
            f"Напишите {SUPPORT_USERNAME}."
        )

    await bot.send_message(
        user_id,
        text,
        reply_markup=kb_after_pay(
            link
        )
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
            "❌ Вы уже использовали "
            "этот промокод.",
            reply_markup=kb_profile()
        )

        return True

    if status == "exhausted":

        await update.message.reply_text(
            "❌ Этот промокод больше "
            "недоступен.",
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
# CRYPTO API
# =========================================================

async def crypto_api(
    method: str,
    payload: dict
):
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError(
            "CRYPTO_PAY_API_TOKEN "
            "is not configured"
        )

    headers = {
        "Crypto-Pay-API-Token":
            CRYPTO_PAY_API_TOKEN
    }

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.post(
            (
                "https://pay.crypt.bot/"
                f"api/{method}"
            ),
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
    if plan not in PLANS:
        raise ValueError(
            f"Unknown plan: {plan}"
        )

    amount = PLANS[plan]["usd"]

    if not amount:
        raise RuntimeError(
            "USD price is not configured "
            f"for plan={plan}"
        )

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
            "amount": amount,
            "description": (
                PLANS[plan]["title"]
            ),
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
                    items[0].get(
                        "status"
                    )
                )

                return

        except Exception:

            logger.exception(
                "Crypto watcher error"
            )

        await asyncio.sleep(
            10
        )

    update_payment(
        payment_db_id,
        "timeout"
    )


# =========================================================
# SUBSCRIPTION CALLBACK
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

    # =====================================================
    # PLAN
    # =====================================================

    if (
        data.startswith("sub:")
        and data.count(":") == 1
    ):

        plan = data.split(":")[1]

        if plan not in PLANS:
            return

        p = PLANS[plan]

        if p["usd"]:

            price_text = (
                f"{p['usd']}$ / "
                f"{p['stars']}⭐"
            )

        else:

            price_text = (
                f"{p['stars']}⭐"
            )

        text = (
            f"💎 {p['title']}\n\n"
            f"⏳ Срок — {p['days']} дней\n"
            f"💵 Цена — {price_text}\n\n"
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

    # =====================================================
    # BACK
    # =====================================================

    if data == "pay:back":

        await show_subscription(
            update,
            context
        )

        return

    # =====================================================
    # CRYPTOBOT
    # =====================================================

    if data.startswith(
        "pay:crypto:"
    ):

        plan = data.split(":")[-1]

        if plan not in PLANS:
            return

        # -------------------------
        # Fallback links
        # -------------------------

        fallback = None

        if plan == "week":
            fallback = CRYPTO_FALLBACK_WEEK

        elif plan == "month":
            fallback = CRYPTO_FALLBACK_MONTH

        elif plan == "year":
            fallback = CRYPTO_FALLBACK_YEAR

        # -------------------------
        # API token отсутствует
        # -------------------------

        if not CRYPTO_PAY_API_TOKEN:

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
                        f"{PLANS[plan]['title']}\n\n"
                        "После оплаты нажмите "
                        "«Я оплатил»."
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
                    "в Render."
                ),
                kb_pay_methods(plan)
            )

            return

        # -------------------------
        # API
        # -------------------------

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
                invoice.get(
                    "bot_invoice_url"
                )
                or invoice.get(
                    "mini_app_invoice_url"
                )
                or invoice.get(
                    "web_app_invoice_url"
                )
            )

            if not url:
                raise RuntimeError(
                    "CryptoBot did not return "
                    "invoice URL"
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
                    f"{PLANS[plan]['title']}\n\n"
                    "Нажмите «Оплатить».\n"
                    "После оплаты подписка "
                    "активируется автоматически."
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
                    "Попробуйте Telegram Stars."
                ),
                kb_pay_methods(plan)
            )

        return

    # =====================================================
    # TELEGRAM STARS
    # =====================================================

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
                    "Доступ к функциям бота "
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
                    "подписка активируется "
                    "автоматически."
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
                    "❌ Не удалось создать "
                    "счёт Stars.\n\n"
                    "Попробуйте CryptoBot."
                ),
                reply_markup=kb_pay_methods(plan)
            )

        return

    # =====================================================
    # CANCEL
    # =====================================================

    if data == "pay:cancel":

        await send_ui(
            update,
            context,
            "❌ Оплата отменена.",
            kb_home(user_id)
        )

        return

    # =====================================================
    # CRYPTO CHECK
    # =====================================================

    if data.startswith(
        "check_crypto:"
    ):

        parts = data.split(":")

        if len(parts) < 4:
            return

        try:

            invoice_id = parts[1]

            plan = parts[2]

            payment_db_id = int(
                parts[3]
            )

        except (
            ValueError,
            IndexError
        ):

            try:
                await q.answer(
                    "Некорректные данные оплаты.",
                    show_alert=True
                )
            except TelegramError:
                pass

            return

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

            try:
                await q.answer(
                    "Автопроверка не настроена.",
                    show_alert=True
                )
            except TelegramError:
                pass

            return

        try:

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
                    context.bot,
                    user_id,
                    plan,
                    "cryptobot",
                    str(invoice_id),
                    payment_db_id
                )

                await safe_delete(
                    q.message
                )

                return

            try:
                await q.answer(
                    (
                        "Оплата ещё не найдена.\n"
                        "Попробуйте ещё раз."
                    ),
                    show_alert=True
                )
            except TelegramError:
                pass

        except Exception:

            logger.exception(
                "Crypto manual check failed"
            )

            try:
                await q.answer(
                    "Не удалось проверить оплату.",
                    show_alert=True
                )
            except TelegramError:
                pass

        return

    # =====================================================
    # MANUAL CRYPTO
    # =====================================================

    if data.startswith(
        "manual_crypto:"
    ):

        plan = data.split(":")[-1]

        await send_ui(
            update,
            context,
            (
                "⏳ Автоматическая проверка "
                "доступна после настройки "
                "CRYPTO_PAY_API_TOKEN."
            ),
            kb_back_home()
        )


# =========================================================
# STARS PRECHECKOUT
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


# =========================================================
# SUCCESSFUL STARS PAYMENT
# =========================================================

async def successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    payment = (
        update.message
        .successful_payment
    )

    payload = (
        payment.invoice_payload
    )

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

    (
        _,
        payload_user,
        plan,
        payment_db_id
    ) = parts[:4]

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
    user_id = (
        update.effective_user.id
    )

    ensure_profile(
        user_id
    )

    # ---------------------------------------------
    # Реферальная ссылка
    # ---------------------------------------------

    if context.args:

        start_param = (
            context.args[0]
        )

        if start_param.startswith(
            "ref_"
        ):

            try:

                referrer_id = int(
                    start_param[4:]
                )

            except ValueError:

                referrer_id = None

            if (
                referrer_id
                and referrer_id != user_id
            ):

                add_referral(
                    referrer_id,
                    user_id
                )

    # ---------------------------------------------
    # Проверяем существующую подписку,
    # но НЕ меняем её
    # ---------------------------------------------

    subscription = (
        get_active_subscription(
            user_id
        )
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
        reply_markup=kb_home(
            user_id
        )
    )


# =========================================================
# /ADMIN
# =========================================================

async def cmd_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = (
        update.effective_user.id
    )

    if not is_admin(
        user_id
    ):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    await update.message.reply_text(
        admin_stats(),
        reply_markup=kb_admin()
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

    # =====================================================
    # HUG TARGET
    # =====================================================

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

    # =====================================================
    # CONFIRM
    # =====================================================

    if state == "awaiting_confirm":

        await update.message.reply_text(
            "Нажмите кнопку выше 👆",
            reply_markup=kb_confirm_hug()
        )

        return

    # =====================================================
    # PROMO
    # =====================================================

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

    # =====================================================
    # CHECK
    # =====================================================

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

    # =====================================================
    # SEARCH
    # =====================================================

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

    # =====================================================
    # PROMO WITHOUT STATE
    # =====================================================

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
        reply_markup=kb_home(
            user_id
        )
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

            with db() as conn:

                rows = conn.execute(
                    """
                    SELECT DISTINCT user_id
                    FROM subscriptions
                    """
                ).fetchall()

            for row in rows:

                user_id = row["user_id"]

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

        await asyncio.sleep(
            60
        )


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

    # =====================================================
    # REQUIRED CHANNEL
    # =====================================================

    app.add_handler(
        TypeHandler(
            Update,
            channel_gate
        ),
        group=-1
    )

    # =====================================================
    # START
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    # =====================================================
    # ADMIN COMMAND
    # =====================================================

    app.add_handler(
        CommandHandler(
            "admin",
            cmd_admin
        )
    )

    # =====================================================
    # ADMIN CALLBACKS
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:"
        )
    )

    # =====================================================
    # NAVIGATION
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            nav_callback,
            pattern=(
                r"^(nav:|menu:|hug:)"
            )
        )
    )

    # =====================================================
    # SUBSCRIPTIONS
    # =====================================================

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

    # =====================================================
    # STARS PRECHECKOUT
    # =====================================================

    app.add_handler(
        PreCheckoutQueryHandler(
            pre_checkout
        )
    )

    # =====================================================
    # SUCCESSFUL PAYMENT
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text
        )
    )

    # =====================================================
    # RENDER WEBHOOK
    # =====================================================

    if WEBHOOK_BASE:

        webhook_path = BOT_TOKEN

        webhook_url = (
            f"{WEBHOOK_BASE.rstrip('/')}/"
            f"{webhook_path}"
        )

        logger.info(
            "Starting webhook"
        )

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True
        )

    # =====================================================
    # LOCAL POLLING
    # =====================================================

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