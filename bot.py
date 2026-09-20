import asyncio
import gzip
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any

import httpx
from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    ReplyKeyboardRemove,
    Update,
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
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hugbot")


# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set")

SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@support").strip()
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_BASE = (
    os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("PUBLIC_URL")
    or ""
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

REFERRAL_REWARD_DAYS = max(
    1,
    int(os.environ.get("REFERRAL_REWARD_DAYS", "1")),
)

DAILY_REQUEST_LIMIT = max(
    1,
    int(os.environ.get("DAILY_REQUEST_LIMIT", "50")),
)

LOG_RETENTION_DAYS = max(
    1,
    int(os.environ.get("LOG_RETENTION_DAYS", "30")),
)

USAGE_RETENTION_DAYS = max(
    1,
    int(os.environ.get("USAGE_RETENTION_DAYS", "90")),
)

STATS_RETENTION_DAYS = max(
    1,
    int(os.environ.get("STATS_RETENTION_DAYS", "365")),
)

AUTO_BACKUP = os.environ.get("AUTO_BACKUP", "0").strip() == "1"
AUTO_BACKUP_ADMIN_ID_RAW = os.environ.get("AUTO_BACKUP_ADMIN_ID", "").strip()
AUTO_BACKUP_ADMIN_ID = (
    int(AUTO_BACKUP_ADMIN_ID_RAW)
    if AUTO_BACKUP_ADMIN_ID_RAW.isdigit()
    else None
)
AUTO_BACKUP_HOUR_UTC = min(
    23,
    max(0, int(os.environ.get("AUTO_BACKUP_HOUR_UTC", "3"))),
)

PROMO_CODE = os.environ.get("PROMO_CODE", "HUGVIP").strip().upper()
PROMO_PLAN = os.environ.get("PROMO_PLAN", "month").strip().lower()
PROMO_MAX_USES_RAW = os.environ.get("PROMO_MAX_USES", "").strip()
PROMO_MAX_USES = (
    int(PROMO_MAX_USES_RAW)
    if PROMO_MAX_USES_RAW.isdigit()
    else None
)

if PROMO_MAX_USES == 0:
    PROMO_MAX_USES = None

CRYPTO_PAY_API_TOKEN = os.environ.get("CRYPTO_PAY_API_TOKEN", "").strip()
CRYPTO_ASSET = os.environ.get("CRYPTO_ASSET", "USDT").strip().upper()
CRYPTO_FALLBACK_WEEK = os.environ.get("CRYPTO_FALLBACK_WEEK", "").strip()
CRYPTO_FALLBACK_MONTH = os.environ.get("CRYPTO_FALLBACK_MONTH", "").strip()
CRYPTO_FALLBACK_YEAR = os.environ.get("CRYPTO_FALLBACK_YEAR", "").strip()
YEAR_PRICE_USD = os.environ.get("YEAR_PRICE_USD", "").strip()

WEEKLY_CHANNEL_ID = os.environ.get("WEEKLY_CHANNEL_ID", "").strip()
MONTHLY_CHANNEL_ID = os.environ.get("MONTHLY_CHANNEL_ID", "").strip()
YEARLY_CHANNEL_ID = os.environ.get("YEARLY_CHANNEL_ID", "").strip()
SUBSCRIPTION_CHANNEL_ID = os.environ.get("SUBSCRIPTION_CHANNEL_ID", "").strip()

REQUIRED_CHANNEL_ID = (
    os.environ.get("REQUIRED_CHANNEL_ID")
    or os.environ.get("REQUIRED_CHANNEL")
    or ""
).strip()

REQUIRED_CHANNEL_URL = os.environ.get("REQUIRED_CHANNEL_URL", "").strip()


# =========================================================
# MODERATION
# =========================================================

MODERATION_REASONS = {
    "spam": "🚫 Спам",
    "scam": "🎣 Мошенничество / фишинг",
    "copyright": "©️ Нарушение авторских прав",
    "impersonation": "👤 Выдача себя за другого",
    "illegal": "⚠️ Потенциально запрещённый контент",
    "other": "📝 Другое",
}


# =========================================================
# REFERRAL TIERS
# =========================================================

def parse_referral_tiers(raw: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []

    for chunk in (raw or "").split(","):
        chunk = chunk.strip()

        if not chunk or ":" not in chunk:
            continue

        left, right = chunk.split(":", 1)

        if not left.strip().isdigit() or not right.strip().isdigit():
            continue

        threshold = int(left.strip())
        days = int(right.strip())

        if threshold > 0 and days > 0:
            result.append((threshold, days))

    return sorted(set(result))


REFERRAL_TIERS = parse_referral_tiers(
    os.environ.get("REFERRAL_TIERS", "5:3,10:7,25:14")
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None

    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# =========================================================
# PLANS
# =========================================================

PLANS = {
    "week": {
        "title": "Недельная подписка",
        "days": 7,
        "usd": "5",
        "stars": 200,
        "channel": WEEKLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID,
    },
    "month": {
        "title": "Месячная подписка",
        "days": 30,
        "usd": "9",
        "stars": 350,
        "channel": MONTHLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID,
    },
    "year": {
        "title": "Годовая подписка",
        "days": 365,
        "usd": YEAR_PRICE_USD,
        "stars": 500,
        "channel": (
            YEARLY_CHANNEL_ID
            or MONTHLY_CHANNEL_ID
            or SUBSCRIPTION_CHANNEL_ID
        ),
    },
}


# =========================================================
# TEXT
# =========================================================

GREETING = "Привет, пользователь!\nЧем я могу вам помочь?"

SUPPORT_TEXT = (
    f"Если вы столкнулись с проблемой — напишите: {SUPPORT_USERNAME}"
)

CHANNEL_GATE_TEXT = (
    "🔒 Сначала подпишитесь на канал\n\n"
    "Без подписки бот закрыт: меню, кабинет и основные функции недоступны.\n\n"
    "1️⃣ Нажмите «Подписаться»\n"
    "2️⃣ Подпишитесь на канал\n"
    "3️⃣ Вернитесь сюда и нажмите «Проверить подписку»"
)


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
    (p for p in ASSET_CANDIDATES if p.is_dir()),
    BASE_DIR / "assets",
)

PROFILE_BANNER = ASSETS_DIR / "profile_banner.png"
MENU_BANNER = ASSETS_DIR / "menu_banner.png"


# =========================================================
# POSTGRESQL
# =========================================================

DB_POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row},
)

DB_OPENED = False


def db():
    return DB_POOL.connection()


def db_ping() -> float:
    started = utcnow()

    with db() as conn:
        conn.execute("SELECT 1").fetchone()

    return (utcnow() - started).total_seconds() * 1000


# =========================================================
# LOGS / DAILY STATS
# =========================================================

ALLOWED_STATS = {
    "new_users",
    "requests",
    "hugs",
    "checks",
    "searches",
    "paid_payments",
    "stars_revenue",
    "crypto_revenue",
    "referrals",
    "tier_rewards",
}


def log_event(
    level: str,
    event: str,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
):
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO app_logs(level, event, user_id, details, created_at)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    level,
                    event,
                    user_id,
                    json.dumps(
                        details or {},
                        ensure_ascii=False,
                        default=str,
                    ),
                    utcnow(),
                ),
            )

            conn.commit()

    except Exception:
        logger.exception(
            "Could not save DB log event=%s",
            event,
        )


def increment_stat(
    metric: str,
    amount: Decimal | int | float = 1,
):
    if metric not in ALLOWED_STATS:
        return

    today = utcnow().date()
    amount_value = Decimal(str(amount))

    columns = {
        "new_users": "new_users",
        "requests": "requests",
        "hugs": "hugs",
        "checks": "checks",
        "searches": "searches",
        "paid_payments": "paid_payments",
        "stars_revenue": "stars_revenue",
        "crypto_revenue": "crypto_revenue",
        "referrals": "referrals",
        "tier_rewards": "tier_rewards",
    }

    column = columns[metric]

    try:
        with db() as conn:
            conn.execute(
                f"""
                INSERT INTO daily_stats(stat_date, {column})
                VALUES (%s, %s)
                ON CONFLICT(stat_date)
                DO UPDATE SET {column}=daily_stats.{column}+EXCLUDED.{column}
                """,
                (
                    today,
                    amount_value,
                ),
            )

            conn.commit()

    except Exception:
        logger.exception(
            "Could not update daily stat=%s",
            metric,
        )


# =========================================================
# DATABASE INIT / MIGRATIONS
# =========================================================

def init_db():
    global DB_OPENED

    if not DB_OPENED:
        DB_POOL.open(wait=True)
        DB_OPENED = True

    with db() as conn:

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                level INTEGER NOT NULL DEFAULT 0,
                warmth INTEGER NOT NULL DEFAULT 1000,
                ref_code TEXT NOT NULL DEFAULT 'HUGGER',
                checks INTEGER NOT NULL DEFAULT 0,
                first_seen_at TIMESTAMPTZ,
                referral_bonus_days INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hugs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                target TEXT NOT NULL,
                count INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan TEXT NOT NULL,
                method TEXT NOT NULL,
                payment_id TEXT,
                starts_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan TEXT NOT NULL,
                method TEXT NOT NULL,
                external_id TEXT,
                amount NUMERIC(18, 4),
                currency TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                paid_at TIMESTAMPTZ,
                failure_reason TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id BIGINT NOT NULL,
                usage_date DATE NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, usage_date)
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
                active BOOLEAN NOT NULL DEFAULT TRUE,
                expires_at TIMESTAMPTZ
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                redeemed_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(code, user_id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id BIGSERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referred_id BIGINT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                rewarded BOOLEAN NOT NULL DEFAULT FALSE,
                rewarded_at TIMESTAMPTZ
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_tier_rewards (
                id BIGSERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                threshold INTEGER NOT NULL,
                days INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(referrer_id, threshold)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_logs (
                id BIGSERIAL PRIMARY KEY,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                user_id BIGINT,
                details JSONB,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                stat_date DATE PRIMARY KEY,
                new_users BIGINT NOT NULL DEFAULT 0,
                requests BIGINT NOT NULL DEFAULT 0,
                hugs BIGINT NOT NULL DEFAULT 0,
                checks BIGINT NOT NULL DEFAULT 0,
                searches BIGINT NOT NULL DEFAULT 0,
                paid_payments BIGINT NOT NULL DEFAULT 0,
                stars_revenue NUMERIC(18,4) NOT NULL DEFAULT 0,
                crypto_revenue NUMERIC(18,4) NOT NULL DEFAULT 0,
                referrals BIGINT NOT NULL DEFAULT 0,
                tier_rewards BIGINT NOT NULL DEFAULT 0
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_revocations (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                channel TEXT NOT NULL,
                revoked_for_expires_at TIMESTAMPTZ NOT NULL,
                revoked_at TIMESTAMPTZ NOT NULL,
                UNIQUE(user_id, channel, revoked_for_expires_at)
            )
            """
        )

        # -----------------------------------------------------
        # MODERATION
        # -----------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderation_cases (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'unknown',
                reason TEXT NOT NULL,
                description TEXT,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'prepared',
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderation_evidence (
                id BIGSERIAL PRIMARY KEY,
                case_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        migrations = [
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS username TEXT",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS first_name TEXT",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS last_name TEXT",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS referral_bonus_days INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount NUMERIC(18,4)",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency TEXT",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS failure_reason TEXT",
            "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
        ]

        for sql in migrations:
            conn.execute(sql)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_payment_id
            ON subscriptions(payment_id)
            WHERE payment_id IS NOT NULL
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_external_id
            ON payments(external_id)
            WHERE external_id IS NOT NULL
            """
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_username "
            "ON profiles(LOWER(username))"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id "
            "ON subscriptions(user_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hugs_user_id "
            "ON hugs(user_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_searches_user_id "
            "ON searches(user_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_referrer "
            "ON referrals(referrer_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_status "
            "ON payments(status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_created_at "
            "ON app_logs(created_at)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_stats_date "
            "ON daily_stats(stat_date)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_cases_user "
            "ON moderation_cases(user_id, id DESC)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_cases_status "
            "ON moderation_cases(status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_evidence_case "
            "ON moderation_evidence(case_id)"
        )

        now = utcnow()

        conn.execute(
            "UPDATE profiles SET first_seen_at=%s "
            "WHERE first_seen_at IS NULL",
            (now,),
        )

        if PROMO_CODE:
            promo_plan = (
                PROMO_PLAN
                if PROMO_PLAN in PLANS
                else "month"
            )

            conn.execute(
                """
                INSERT INTO promo_codes(
                    code,
                    plan,
                    max_uses,
                    used,
                    active,
                    expires_at
                )
                VALUES (%s, %s, %s, 0, TRUE, NULL)
                ON CONFLICT(code) DO NOTHING
                """,
                (
                    PROMO_CODE,
                    promo_plan,
                    PROMO_MAX_USES,
                ),
            )

            if PROMO_MAX_USES is None:
                conn.execute(
                    """
                    UPDATE promo_codes
                    SET max_uses=NULL
                    WHERE code=%s AND used=0 AND max_uses=0
                    """,
                    (PROMO_CODE,),
                )

        conn.commit()

    logger.info("PostgreSQL database initialized")


# =========================================================
# PROFILE
# =========================================================

def ensure_profile(
    user_id: int,
    telegram_user=None,
) -> bool:

    now = utcnow()

    username = (
        getattr(telegram_user, "username", None)
        if telegram_user
        else None
    )

    first_name = (
        getattr(telegram_user, "first_name", None)
        if telegram_user
        else None
    )

    last_name = (
        getattr(telegram_user, "last_name", None)
        if telegram_user
        else None
    )

    with db() as conn:

        inserted = conn.execute(
            """
            INSERT INTO profiles(
                user_id,
                username,
                first_name,
                last_name,
                level,
                warmth,
                ref_code,
                checks,
                first_seen_at,
                referral_bonus_days
            )
            VALUES(
                %s,
                %s,
                %s,
                %s,
                0,
                1000,
                'HUGGER',
                0,
                %s,
                0
            )
            ON CONFLICT(user_id) DO NOTHING
            RETURNING user_id
            """,
            (
                user_id,
                username,
                first_name,
                last_name,
                now,
            ),
        ).fetchone()

        conn.execute(
            """
            UPDATE profiles
            SET username=%s,
                first_name=%s,
                last_name=%s
            WHERE user_id=%s
            """,
            (
                username,
                first_name,
                last_name,
                user_id,
            ),
        )

        conn.commit()

    if inserted:
        increment_stat("new_users")
        log_event(
            "INFO",
            "user_registered",
            user_id,
        )
        return True

    return False


def get_profile(
    user_id: int,
    telegram_user=None,
) -> dict:

    ensure_profile(
        user_id,
        telegram_user,
    )

    with db() as conn:

        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id=%s",
            (user_id,),
        ).fetchone()

        sent = conn.execute(
            """
            SELECT COALESCE(SUM(count),0) AS total
            FROM hugs
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()

        searches = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM searches
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()

    profile = dict(row)

    profile["sent"] = int(
        sent["total"] or 0
    )

    profile["searches"] = int(
        searches["total"] or 0
    )

    subscription = get_active_subscription(
        user_id
    )

    profile["sub"] = subscription
    profile["sub_active"] = subscription is not None
    profile["sub_days_left"] = 0

    if subscription:
        expires = aware(
            subscription["expires_at"]
        )

        profile["sub_days_left"] = max(
            0,
            int(
                (
                    (
                        expires - utcnow()
                    ).total_seconds()
                ) // 86400
            ),
        )

    return profile


# =========================================================
# SUBSCRIPTIONS
# =========================================================

def get_active_subscription(user_id: int):

    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=%s
              AND expires_at > NOW()
            ORDER BY expires_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def has_subscription(user_id: int) -> bool:
    return get_active_subscription(
        user_id
    ) is not None


def find_subscription_by_payment(
    payment_id: str,
):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE payment_id=%s
            LIMIT 1
            """,
            (payment_id,),
        ).fetchone()


def activate_subscription(
    user_id: int,
    plan: str,
    method: str,
    payment_id: str | None,
    consume_referral_bonus: bool = True,
):

    if plan not in PLANS:
        raise ValueError(
            f"Unknown plan: {plan}"
        )

    if payment_id:
        existing = find_subscription_by_payment(
            payment_id
        )

        if existing:
            return (
                aware(existing["expires_at"]),
                False,
            )

    now = utcnow()

    with db() as conn:

        current = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=%s
              AND expires_at > %s
            ORDER BY expires_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (
                user_id,
                now,
            ),
        ).fetchone()

        bonus_row = conn.execute(
            """
            SELECT referral_bonus_days
            FROM profiles
            WHERE user_id=%s
            FOR UPDATE
            """,
            (user_id,),
        ).fetchone()

        referral_bonus_days = int(
            (
                bonus_row["referral_bonus_days"]
                if bonus_row
                else 0
            )
            or 0
        )

        starts_at = max(
            now,
            aware(current["expires_at"])
            if current
            else now,
        )

        expires_at = (
            starts_at
            + timedelta(
                days=PLANS[plan]["days"]
            )
        )

        if (
            consume_referral_bonus
            and referral_bonus_days > 0
        ):
            expires_at += timedelta(
                days=referral_bonus_days
            )

        try:

            row = conn.execute(
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
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    user_id,
                    plan,
                    method,
                    payment_id,
                    starts_at,
                    expires_at,
                    now,
                ),
            ).fetchone()

            if not row and payment_id:

                existing = conn.execute(
                    """
                    SELECT expires_at
                    FROM subscriptions
                    WHERE payment_id=%s
                    LIMIT 1
                    """,
                    (payment_id,),
                ).fetchone()

                conn.commit()

                return (
                    aware(
                        existing["expires_at"]
                    ),
                    False,
                )

            if (
                consume_referral_bonus
                and referral_bonus_days > 0
            ):
                conn.execute(
                    """
                    UPDATE profiles
                    SET referral_bonus_days=0
                    WHERE user_id=%s
                    """,
                    (user_id,),
                )

            conn.commit()

        except psycopg_errors.UniqueViolation:

            conn.rollback()

            existing = (
                find_subscription_by_payment(
                    payment_id
                )
                if payment_id
                else None
            )

            if existing:
                return (
                    aware(existing["expires_at"]),
                    False,
                )

            raise

    logger.info(
        "SUBSCRIPTION ACTIVATED user=%s plan=%s method=%s expires=%s",
        user_id,
        plan,
        method,
        expires_at.isoformat(),
    )

    log_event(
        "INFO",
        "subscription_activated",
        user_id,
        {
            "plan": plan,
            "method": method,
            "payment_id": payment_id,
        },
    )

    return (
        expires_at,
        True,
    )


# =========================================================
# PAYMENTS
# =========================================================

def create_payment(
    user_id: int,
    plan: str,
    method: str,
    external_id: str | None,
    amount: float | int | Decimal | None,
    currency: str | None,
    status: str = "pending",
) -> int:

    now = utcnow()

    with db() as conn:

        if external_id:

            existing = conn.execute(
                """
                SELECT id
                FROM payments
                WHERE external_id=%s
                LIMIT 1
                """,
                (external_id,),
            ).fetchone()

            if existing:
                return int(
                    existing["id"]
                )

        row = conn.execute(
            """
            INSERT INTO payments(
                user_id,
                plan,
                method,
                external_id,
                amount,
                currency,
                status,
                created_at,
                updated_at
            )
            VALUES(
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            RETURNING id
            """,
            (
                user_id,
                plan,
                method,
                external_id,
                amount,
                currency,
                status,
                now,
                now,
            ),
        ).fetchone()

        conn.commit()

    return int(row["id"])


def get_payment(payment_id: int):

    with db() as conn:

        return conn.execute(
            "SELECT * FROM payments WHERE id=%s",
            (payment_id,),
        ).fetchone()


def update_payment(
    payment_id: int,
    status: str,
    external_id: str | None = None,
    failure_reason: str | None = None,
):

    now = utcnow()

    paid_at = (
        now
        if status == "paid"
        else None
    )

    with db() as conn:

        conn.execute(
            """
            UPDATE payments
            SET status=%s,
                external_id=COALESCE(%s, external_id),
                failure_reason=COALESCE(%s, failure_reason),
                paid_at=CASE
                    WHEN %s='paid'
                    THEN COALESCE(paid_at,%s)
                    ELSE paid_at
                END,
                updated_at=%s
            WHERE id=%s
            """,
            (
                status,
                external_id,
                failure_reason,
                status,
                paid_at,
                now,
                payment_id,
            ),
        )

        conn.commit()


# =========================================================
# DAILY USAGE
# =========================================================

def request_usage(
    user_id: int,
) -> tuple[bool, int]:

    today = utcnow().date()

    with db() as conn:

        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=%s
              AND usage_date=%s
            FOR UPDATE
            """,
            (
                user_id,
                today,
            ),
        ).fetchone()

        if not row:

            used = 1

            conn.execute(
                """
                INSERT INTO daily_usage(
                    user_id,
                    usage_date,
                    requests
                )
                VALUES(%s,%s,1)
                """,
                (
                    user_id,
                    today,
                ),
            )

        else:

            current = int(
                row["requests"]
            )

            if current >= DAILY_REQUEST_LIMIT:

                conn.commit()

                return (
                    False,
                    current,
                )

            used = current + 1

            conn.execute(
                """
                UPDATE daily_usage
                SET requests=%s
                WHERE user_id=%s
                  AND usage_date=%s
                """,
                (
                    used,
                    user_id,
                    today,
                ),
            )

        conn.commit()

    increment_stat(
        "requests"
    )

    return (
        True,
        used,
    )


def usage_today(
    user_id: int,
) -> int:

    today = utcnow().date()

    with db() as conn:

        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=%s
              AND usage_date=%s
            """,
            (
                user_id,
                today,
            ),
        ).fetchone()

    return int(
        row["requests"]
    ) if row else 0


# =========================================================
# HUGS / SEARCH / CHECK HISTORY
# =========================================================

def add_hug(
    user_id: int,
    target: str,
    count: int,
):

    now = utcnow()

    with db() as conn:

        conn.execute(
            """
            INSERT INTO hugs(
                user_id,
                target,
                count,
                created_at
            )
            VALUES(%s,%s,%s,%s)
            """,
            (
                user_id,
                target,
                count,
                now,
            ),
        )

        conn.execute(
            """
            UPDATE profiles
            SET warmth=LEAST(1000,warmth+10),
                level=GREATEST(
                    level,
                    1 + (
                        (
                            SELECT COUNT(*)
                            FROM hugs
                            WHERE user_id=%s
                        )::INT / 25
                    )
                )
            WHERE user_id=%s
            """,
            (
                user_id,
                user_id,
            ),
        )

        conn.commit()

    increment_stat(
        "hugs",
        count,
    )

    log_event(
        "INFO",
        "hug_completed",
        user_id,
        {
            "target": target,
            "count": count,
        },
    )


def add_check(
    user_id: int,
):
    with db() as conn:

        conn.execute(
            """
            UPDATE profiles
            SET checks=checks+1
            WHERE user_id=%s
            """,
            (user_id,),
        )

        conn.commit()

    increment_stat(
        "checks"
    )


def add_search(
    user_id: int,
    query: str,
):

    with db() as conn:

        conn.execute(
            """
            INSERT INTO searches(
                user_id,
                query,
                created_at
            )
            VALUES(%s,%s,%s)
            """,
            (
                user_id,
                query,
                utcnow(),
            ),
        )

        conn.commit()

    increment_stat(
        "searches"
    )

    log_event(
        "INFO",
        "search_completed",
        user_id,
        {
            "query": query,
        },
    )


def get_history(
    user_id: int,
    limit: int = 10,
):

    with db() as conn:

        return conn.execute(
            """
            SELECT query, created_at
            FROM searches
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                user_id,
                limit,
            ),
        ).fetchall()


# =========================================================
# MODERATION / PUBLIC TARGET CHECK
# =========================================================

def normalize_moderation_target(
    raw: str,
) -> tuple[str, str | None]:

    value = (
        raw or ""
    ).strip()

    if not value:
        return (
            "",
            None,
        )

    match = re.match(
        r"^(?:https?://)?(?:t\.me|telegram\.me)/([^/?#]+)",
        value,
        flags=re.IGNORECASE,
    )

    if match:

        username = (
            match.group(1)
            .strip()
        )

        if username.startswith("+"):
            return (
                value,
                None,
            )

        if username.lower() == "joinchat":
            return (
                value,
                None,
            )

        if re.fullmatch(
            r"[A-Za-z0-9_]{5,32}",
            username,
        ):
            username = username.lstrip("@")

            return (
                f"@{username}",
                username,
            )

        return (
            value,
            None,
        )

    if value.startswith("@"):

        username = (
            value[1:]
            .strip()
        )

        if re.fullmatch(
            r"[A-Za-z0-9_]{5,32}",
            username,
        ):
            return (
                f"@{username}",
                username,
            )

    if re.fullmatch(
        r"[A-Za-z0-9_]{5,32}",
        value,
    ):
        return (
            f"@{value}",
            value,
        )

    return (
        value,
        None,
    )


def moderation_target_type_ru(
    value: str,
) -> str:

    mapping = {
        "channel": "📢 Канал",
        "supergroup": "👥 Супергруппа",
        "group": "👥 Группа",
        "private": "👤 Личный чат",
        "public_page": "🌐 Публичная страница",
        "unknown": "❓ Не определён",
    }

    return mapping.get(
        value,
        value,
    )


def _extract_meta(
    html: str,
    prop: str,
) -> str:

    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html,
            flags=re.IGNORECASE,
        )

        if match:
            return unescape(
                match.group(1)
            ).strip()

    return ""


async def inspect_moderation_target(
    bot,
    raw_target: str,
) -> dict[str, Any]:

    target, username = normalize_moderation_target(
        raw_target
    )

    result: dict[str, Any] = {
        "target": target,
        "username": username,
        "found": False,
        "type": "unknown",
        "title": "",
        "description": "",
        "url": "",
        "source": "",
        "error": "",
    }

    if not target:

        result["error"] = (
            "Пустая ссылка или username."
        )

        return result

    # -----------------------------------------------------
    # Telegram Bot API
    # -----------------------------------------------------

    if username:

        try:

            chat = await bot.get_chat(
                target
            )

            result.update(
                {
                    "found": True,
                    "type": chat.type or "unknown",
                    "title": (
                        getattr(chat, "title", None)
                        or getattr(chat, "first_name", None)
                        or ""
                    ),
                    "description": (
                        getattr(
                            chat,
                            "description",
                            None,
                        )
                        or ""
                    ),
                    "url": (
                        f"https://t.me/{username}"
                    ),
                    "source": "telegram_api",
                }
            )

            return result

        except TelegramError as exc:

            logger.info(
                "Telegram API target check failed "
                "target=%s error=%s",
                target,
                exc,
            )

    # -----------------------------------------------------
    # Public t.me page
    # -----------------------------------------------------

    if username:

        url = (
            f"https://t.me/{username}"
        )

        try:

            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "Chrome/120 Safari/537.36"
                    )
                },
            ) as client:

                response = await client.get(
                    url
                )

            if response.status_code == 200:

                html = response.text

                title = (
                    _extract_meta(
                        html,
                        "og:title",
                    )
                    or _extract_meta(
                        html,
                        "twitter:title",
                    )
                )

                description = (
                    _extract_meta(
                        html,
                        "og:description",
                    )
                    or _extract_meta(
                        html,
                        "twitter:description",
                    )
                )

                if title or description:

                    result.update(
                        {
                            "found": True,
                            "type": "public_page",
                            "title": title,
                            "description": description,
                            "url": url,
                            "source": "telegram_public_page",
                        }
                    )

                    return result

            result["error"] = (
                f"HTTP {response.status_code}"
            )

        except Exception as exc:

            logger.warning(
                "Public target check failed "
                "target=%s error=%s",
                target,
                exc,
            )

            result["error"] = str(exc)

    return result


def create_moderation_case(
    user_id: int,
    target: str,
    target_type: str,
    reason: str,
    description: str,
) -> int:

    now = utcnow()

    with db() as conn:

        row = conn.execute(
            """
            INSERT INTO moderation_cases(
                user_id,
                target,
                target_type,
                reason,
                description,
                evidence_count,
                status,
                created_at,
                updated_at
            )
            VALUES(
                %s,
                %s,
                %s,
                %s,
                %s,
                0,
                'prepared',
                %s,
                %s
            )
            RETURNING id
            """,
            (
                user_id,
                target,
                target_type,
                reason,
                description,
                now,
                now,
            ),
        ).fetchone()

        conn.commit()

    case_id = int(
        row["id"]
    )

    log_event(
        "INFO",
        "moderation_case_created",
        user_id,
        {
            "case_id": case_id,
            "target": target,
            "reason": reason,
        },
    )

    return case_id


def add_moderation_evidence(
    case_id: int,
    user_id: int,
    file_id: str,
    file_type: str,
):

    now = utcnow()

    with db() as conn:

        owner = conn.execute(
            """
            SELECT 1
            FROM moderation_cases
            WHERE id=%s
              AND user_id=%s
            """,
            (
                case_id,
                user_id,
            ),
        ).fetchone()

        if not owner:

            conn.commit()

            raise ValueError(
                "Case does not belong to user"
            )

        conn.execute(
            """
            INSERT INTO moderation_evidence(
                case_id,
                user_id,
                file_id,
                file_type,
                created_at
            )
            VALUES(%s,%s,%s,%s,%s)
            """,
            (
                case_id,
                user_id,
                file_id,
                file_type,
                now,
            ),
        )

        conn.execute(
            """
            UPDATE moderation_cases
            SET evidence_count=evidence_count+1,
                updated_at=%s
            WHERE id=%s
              AND user_id=%s
            """,
            (
                now,
                case_id,
                user_id,
            ),
        )

        conn.commit()


def get_moderation_case(
    case_id: int,
    user_id: int,
):

    with db() as conn:

        return conn.execute(
            """
            SELECT *
            FROM moderation_cases
            WHERE id=%s
              AND user_id=%s
            LIMIT 1
            """,
            (
                case_id,
                user_id,
            ),
        ).fetchone()


def get_moderation_cases(
    user_id: int,
    limit: int = 10,
):

    with db() as conn:

        return conn.execute(
            """
            SELECT *
            FROM moderation_cases
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
            """,
            (
                user_id,
                limit,
            ),
        ).fetchall()


def moderation_case_text(
    row,
) -> str:

    reason_text = MODERATION_REASONS.get(
        row["reason"],
        row["reason"],
    )

    return (
        "📋 ЧЕРНОВИК ОБРАЩЕНИЯ\n\n"
        f"🆔 Номер — #{row['id']}\n"
        f"🎯 Объект — {row['target']}\n"
        f"📌 Тип — "
        f"{moderation_target_type_ru(row['target_type'])}\n"
        f"⚠️ Основание — {reason_text}\n\n"
        "📝 Описание:\n"
        f"{(row['description'] or 'Не указано')[:2500]}\n\n"
        f"📎 Доказательств — {row['evidence_count']}\n\n"
        "🟡 Статус — подготовлено\n\n"
        "Используйте этот материал для официального обращения "
        "в Telegram и прикладывайте только достоверные сведения."
    )


# =========================================================
# REFERRALS
# =========================================================

def add_referral(
    referrer_id: int,
    referred_id: int,
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

        row = conn.execute(
            """
            INSERT INTO referrals(
                referrer_id,
                referred_id,
                created_at,
                rewarded
            )
            VALUES(%s,%s,%s,FALSE)
            ON CONFLICT(referred_id) DO NOTHING
            RETURNING id
            """,
            (
                referrer_id,
                referred_id,
                utcnow(),
            ),
        ).fetchone()

        conn.commit()

    if row:

        increment_stat(
            "referrals"
        )

        log_event(
            "INFO",
            "referral_added",
            referred_id,
            {
                "referrer_id": referrer_id,
            },
        )

        return True

    return False


def get_referral_count(
    user_id: int,
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id=%s
            """,
            (user_id,),
        ).fetchone()

    return int(
        row["total"]
    )


def get_referral_rewarded_count(
    user_id: int,
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id=%s
              AND rewarded=TRUE
            """,
            (user_id,),
        ).fetchone()

    return int(
        row["total"]
    )


def get_referral_bonus_days(
    user_id: int,
) -> int:

    with db() as conn:

        row = conn.execute(
            """
            SELECT referral_bonus_days
            FROM profiles
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()

    return int(
        (
            row["referral_bonus_days"]
            if row
            else 0
        )
        or 0
    )


async def get_referral_link(
    bot,
    user_id: int,
) -> str:

    me = await bot.get_me()

    return (
        f"https://t.me/{me.username}"
        f"?start=ref_{user_id}"
    )


async def reward_referrer_for_purchase(
    bot,
    referred_user_id: int,
):

    with db() as conn:

        referral = conn.execute(
            """
            SELECT *
            FROM referrals
            WHERE referred_id=%s
              AND rewarded=FALSE
            LIMIT 1
            FOR UPDATE
            """,
            (referred_user_id,),
        ).fetchone()

        if not referral:

            conn.commit()

            return False

        referrer_id = int(
            referral["referrer_id"]
        )

        if referrer_id == referred_user_id:

            conn.commit()

            return False

        updated = conn.execute(
            """
            UPDATE referrals
            SET rewarded=TRUE,
                rewarded_at=%s
            WHERE id=%s
              AND rewarded=FALSE
            """,
            (
                utcnow(),
                referral["id"],
            ),
        )

        if updated.rowcount == 0:

            conn.commit()

            return False

        rewarded_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM referrals
                WHERE referrer_id=%s
                  AND rewarded=TRUE
                """,
                (referrer_id,),
            ).fetchone()["total"]
        )

        extra_tier_days = 0

        awarded_tiers: list[
            tuple[int, int]
        ] = []

        for threshold, days in REFERRAL_TIERS:

            if rewarded_count >= threshold:

                tier = conn.execute(
                    """
                    INSERT INTO referral_tier_rewards(
                        referrer_id,
                        threshold,
                        days,
                        created_at
                    )
                    VALUES(%s,%s,%s,%s)
                    ON CONFLICT(
                        referrer_id,
                        threshold
                    )
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        referrer_id,
                        threshold,
                        days,
                        utcnow(),
                    ),
                ).fetchone()

                if tier:

                    extra_tier_days += days

                    awarded_tiers.append(
                        (
                            threshold,
                            days,
                        )
                    )

        total_days = (
            REFERRAL_REWARD_DAYS
            + extra_tier_days
        )

        subscription = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=%s
              AND expires_at > %s
            ORDER BY expires_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            (
                referrer_id,
                utcnow(),
            ),
        ).fetchone()

        if subscription:

            new_expiry = (
                aware(
                    subscription["expires_at"]
                )
                + timedelta(
                    days=total_days
                )
            )

            conn.execute(
                """
                UPDATE subscriptions
                SET expires_at=%s
                WHERE id=%s
                """,
                (
                    new_expiry,
                    subscription["id"],
                ),
            )

        else:

            conn.execute(
                """
                UPDATE profiles
                SET referral_bonus_days=
                    referral_bonus_days+%s
                WHERE user_id=%s
                """,
                (
                    total_days,
                    referrer_id,
                ),
            )

        conn.commit()

    if extra_tier_days:
        increment_stat(
            "tier_rewards",
            extra_tier_days,
        )

    log_event(
        "INFO",
        "referral_rewarded",
        referrer_id,
        {
            "referred_user_id": referred_user_id,
            "base_days": REFERRAL_REWARD_DAYS,
            "tier_days": extra_tier_days,
            "tier_rewards": awarded_tiers,
        },
    )

    tier_text = ""

    if awarded_tiers:

        tier_text = (
            "\n\n🏆 Новые награды:\n"
            + "\n".join(
                f"• {threshold} рефералов — +{days} д."
                for threshold, days in awarded_tiers
            )
        )

    try:

        await bot.send_message(
            referrer_id,
            (
                "🎁 Реферальный бонус!\n\n"
                "Ваш реферал впервые оплатил подписку.\n\n"
                f"Вам начислено +{REFERRAL_REWARD_DAYS} "
                f"день подписки."
                f"{tier_text}"
            ),
        )

    except TelegramError:
        pass

    return True


# =========================================================
# PROMO
# =========================================================

def redeem_promo(
    user_id: int,
    raw_code: str,
) -> tuple[str, str | None]:

    code = (
        raw_code or ""
    ).strip().upper()

    if not code:
        return (
            "empty",
            None,
        )

    now = utcnow()

    with db() as conn:

        row = conn.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=%s
            FOR UPDATE
            """,
            (code,),
        ).fetchone()

        if not row or not row["active"]:

            conn.commit()

            return (
                "invalid",
                None,
            )

        if (
            row["expires_at"]
            and aware(row["expires_at"]) <= now
        ):

            conn.commit()

            return (
                "expired",
                None,
            )

        if row["plan"] not in PLANS:

            conn.commit()

            return (
                "invalid",
                None,
            )

        already = conn.execute(
            """
            SELECT 1
            FROM promo_redemptions
            WHERE code=%s
              AND user_id=%s
            """,
            (
                code,
                user_id,
            ),
        ).fetchone()

        if already:

            conn.commit()

            return (
                "already",
                None,
            )

        if (
            row["max_uses"] is not None
            and int(row["used"]) >= int(
                row["max_uses"]
            )
        ):

            conn.commit()

            return (
                "exhausted",
                None,
            )

        conn.execute(
            """
            INSERT INTO promo_redemptions(
                code,
                user_id,
                redeemed_at
            )
            VALUES(%s,%s,%s)
            """,
            (
                code,
                user_id,
                now,
            ),
        )

        conn.execute(
            """
            UPDATE promo_codes
            SET used=used+1
            WHERE code=%s
            """,
            (code,),
        )

        conn.commit()

    return (
        "ok",
        row["plan"],
    )


async def grant_promo_access(
    bot,
    user_id: int,
    plan: str,
    code: str,
):

    payment_key = (
        f"promo:{code}:{user_id}"
    )

    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:

        expires = aware(
            existing["expires_at"]
        )

    else:

        expires, _created = activate_subscription(
            user_id,
            plan,
            "promo",
            payment_key,
            consume_referral_bonus=False,
        )

    link, invite_status = (
        await issue_channel_invite(
            bot,
            user_id,
            plan,
        )
    )

    text = (
        f"✅ Промокод {code} активирован!\n\n"
        f"💎 Подписка — {PLANS[plan]['title']}\n"
        f"⏳ Действует до — "
        f"{expires.strftime('%d.%m.%Y %H:%M')}"
    )

    if link:

        text += (
            "\n\n👇 Ссылка в канал:"
        )

    elif invite_status == "error":

        text += (
            "\n\n⚠️ Ссылка в канал не была создана."
            f"\nНапишите {SUPPORT_USERNAME}."
        )

    await bot.send_message(
        user_id,
        text,
        reply_markup=kb_after_pay(
            link
        ),
    )


def promo_list():

    with db() as conn:

        return conn.execute(
            """
            SELECT *
            FROM promo_codes
            ORDER BY code
            """
        ).fetchall()


def admin_create_promo(
    code: str,
    plan: str,
    max_uses: int | None,
    expires_at: datetime | None,
):

    code = (
        code.strip()
        .upper()
    )

    if plan not in PLANS:
        raise ValueError(
            "Неверный план"
        )

    if (
        max_uses is not None
        and max_uses < 0
    ):
        raise ValueError(
            "MAX_USES не может быть отрицательным"
        )

    if max_uses == 0:
        max_uses = None

    with db() as conn:

        conn.execute(
            """
            INSERT INTO promo_codes(
                code,
                plan,
                max_uses,
                used,
                active,
                expires_at
            )
            VALUES(%s,%s,%s,0,TRUE,%s)
            ON CONFLICT(code)
            DO UPDATE SET
                plan=EXCLUDED.plan,
                max_uses=EXCLUDED.max_uses,
                active=TRUE,
                expires_at=EXCLUDED.expires_at
            """,
            (
                code,
                plan,
                max_uses,
                expires_at,
            ),
        )

        conn.commit()


def deactivate_promo(
    code: str,
):

    with db() as conn:

        conn.execute(
            """
            UPDATE promo_codes
            SET active=FALSE
            WHERE code=%s
            """,
            (
                code.upper(),
            ),
        )

        conn.commit()


async def apply_promo_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    raw_code: str,
) -> bool:

    status, plan = redeem_promo(
        user_id,
        raw_code,
    )

    messages = {
        "empty": "Введите промокод.",
        "invalid": "❌ Промокод не найден.",
        "expired": "❌ Срок действия промокода истёк.",
        "already": "❌ Вы уже использовали этот промокод.",
        "exhausted": "❌ Этот промокод больше недоступен.",
    }

    if status != "ok":

        await update.message.reply_text(
            messages.get(
                status,
                "❌ Не удалось применить промокод.",
            ),
            reply_markup=kb_profile(),
        )

        return True

    await grant_promo_access(
        context.bot,
        user_id,
        plan,
        raw_code.strip().upper(),
    )

    return True


# =========================================================
# KEYBOARDS
# =========================================================

def kb_home(
    user_id: int | None = None,
):

    rows = [
        [
            InlineKeyboardButton(
                "👤 Личный кабинет",
                callback_data="nav:profile",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Меню",
                callback_data="nav:menu",
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Реферальная система",
                callback_data="nav:referrals",
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Техподдержка",
                callback_data="nav:support",
            )
        ],
    ]

    if (
        user_id is not None
        and is_admin(user_id)
    ):

        rows.append(
            [
                InlineKeyboardButton(
                    "🛠 Админ-панель",
                    callback_data="admin:panel",
                )
            ]
        )

    return InlineKeyboardMarkup(
        rows
    )


def kb_profile():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💎 Оформить подписку",
                    callback_data="nav:sub",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎟 Ввести промокод",
                    callback_data="nav:promo",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ],
        ]
    )


def kb_menu():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "sn1cти аккаунт❄️",
                    callback_data="menu:hug",
                )
            ],
            [
                InlineKeyboardButton(
                    "👀 П0иск",
                    callback_data="menu:search",
                ),
                InlineKeyboardButton(
                    "🔎 Проверить",
                    callback_data="menu:check",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🛡 Модерация / обращения",
                    callback_data="menu:moderation",
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 История поиска",
                    callback_data="menu:history",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ],
        ]
    )


def kb_moderation():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 Проверить объект",
                    callback_data="mod:check",
                )
            ],
            [
                InlineKeyboardButton(
                    "📝 Создать обращение",
                    callback_data="mod:create",
                )
            ],
            [
                InlineKeyboardButton(
                    "📁 Мои обращения",
                    callback_data="mod:cases",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ],
        ]
    )


def kb_mod_reasons():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚫 Спам",
                    callback_data="mod:reason:spam",
                ),
                InlineKeyboardButton(
                    "🎣 Фишинг",
                    callback_data="mod:reason:scam",
                ),
            ],
            [
                InlineKeyboardButton(
                    "©️ Авторские права",
                    callback_data="mod:reason:copyright",
                ),
                InlineKeyboardButton(
                    "👤 Выдача себя за другого",
                    callback_data="mod:reason:impersonation",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚠️ Запрещённый контент",
                    callback_data="mod:reason:illegal",
                )
            ],
            [
                InlineKeyboardButton(
                    "📝 Другое",
                    callback_data="mod:reason:other",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="mod:open",
                )
            ],
        ]
    )


def kb_moderation_evidence(
    case_id: int,
):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📎 Добавить доказательство",
                    callback_data=f"mod:add_evidence:{case_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Завершить обращение",
                    callback_data=f"mod:finish:{case_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🛡 Модерация",
                    callback_data="mod:open",
                )
            ],
        ]
    )


def kb_back_home():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ]
        ]
    )


def kb_confirm_hug():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Да sn1cти аккаунт!",
                    callback_data="hug:yes",
                )
            ],
            [
                InlineKeyboardButton(
                    "Нет, отмена",
                    callback_data="hug:no",
                )
            ],
        ]
    )


def kb_hooray():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ура! 🎉",
                    callback_data="nav:home",
                )
            ]
        ]
    )


def kb_sub_plans():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Неделя — 200⭐",
                    callback_data="sub:week",
                )
            ],
            [
                InlineKeyboardButton(
                    "Месяц — 350⭐",
                    callback_data="sub:month",
                )
            ],
            [
                InlineKeyboardButton(
                    "Год — 500⭐",
                    callback_data="sub:year",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Главное меню",
                    callback_data="nav:home",
                )
            ],
        ]
    )


def kb_pay_methods(
    plan: str,
):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🪙 CryptoBot",
                    callback_data=f"pay:crypto:{plan}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⭐️ Telegram Stars",
                    callback_data=f"pay:stars:{plan}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="pay:back",
                )
            ],
        ]
    )


def kb_after_pay(
    link: str | None,
):

    rows = []

    if link:

        rows.append(
            [
                InlineKeyboardButton(
                    "🔐 Получить доступ к каналу",
                    url=link,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "👤 Личный кабинет",
                callback_data="nav:profile",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                "🏠 Главное меню",
                callback_data="nav:home",
            )
        ]
    )

    return InlineKeyboardMarkup(
        rows
    )


def kb_channel_gate():

    rows = []

    url = required_channel_url()

    if url:

        rows.append(
            [
                InlineKeyboardButton(
                    "📢 Подписаться",
                    url=url,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "✅ Проверить подписку",
                callback_data="gate:check",
            )
        ]
    )

    return InlineKeyboardMarkup(
        rows
    )


def kb_referrals():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ]
        ]
    )


def kb_admin():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 Статистика",
                    callback_data="admin:stats",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Пользователи",
                    callback_data="admin:users",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 Найти пользователя",
                    callback_data="admin:user_search",
                )
            ],
            [
                InlineKeyboardButton(
                    "💳 Покупки",
                    callback_data="admin:payments",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔗 Рефералы",
                    callback_data="admin:referrals",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎟 Промокоды",
                    callback_data="admin:promos",
                )
            ],
            [
                InlineKeyboardButton(
                    "💾 Backup",
                    callback_data="admin:backup",
                )
            ],
            [
                InlineKeyboardButton(
                    "❤️ Health",
                    callback_data="admin:health",
                )
            ],
            [
                InlineKeyboardButton(
                    "📈 По дням",
                    callback_data="admin:daily_stats",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧾 Логи",
                    callback_data="admin:logs",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Рассылка",
                    callback_data="admin:broadcast",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data="admin:panel",
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 На главную",
                    callback_data="nav:home",
                )
            ],
        ]
    )


def kb_admin_promo_list(
    rows,
):

    buttons = []

    for row in rows:

        if row["active"]:

            buttons.append(
                [
                    InlineKeyboardButton(
                        f"❌ Выключить {row['code']}",
                        callback_data=f"admin:promo_off:{row['code']}",
                    )
                ]
            )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "➕ Создать / изменить",
                    callback_data="admin:promo_create",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Админ-панель",
                    callback_data="admin:panel",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        buttons
    )


# =========================================================
# REQUIRED CHANNEL
# =========================================================

def required_channel_url() -> str:

    if REQUIRED_CHANNEL_URL:
        return REQUIRED_CHANNEL_URL

    channel = (
        REQUIRED_CHANNEL_ID.strip()
    )

    if not channel:
        return ""

    if channel.startswith("https://"):
        return channel

    if channel.startswith("t.me/"):
        return (
            f"https://{channel}"
        )

    if channel.startswith("@"):
        return (
            f"https://t.me/{channel[1:]}"
        )

    if channel.lstrip("-").isdigit():
        return ""

    return (
        f"https://t.me/{channel}"
    )


async def is_required_channel_member(
    bot,
    user_id: int,
) -> bool:

    if not REQUIRED_CHANNEL_ID:
        return True

    try:

        member = await bot.get_chat_member(
            REQUIRED_CHANNEL_ID,
            user_id,
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
            exc,
        )

        return False


async def show_channel_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await send_ui(
        update,
        context,
        CHANNEL_GATE_TEXT,
        kb_channel_gate(),
    )


async def channel_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if (
        not REQUIRED_CHANNEL_ID
        or not user
        or user.is_bot
        or is_admin(user.id)
    ):
        return

    q = update.callback_query

    if q and q.data == "gate:check":

        subscribed = (
            await is_required_channel_member(
                context.bot,
                user.id,
            )
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
                context,
            )

        else:

            try:
                await q.answer(
                    "Вы ещё не подписались.",
                    show_alert=True,
                )
            except TelegramError:
                pass

            await show_channel_gate(
                update,
                context,
            )

        raise ApplicationHandlerStop

    if update.pre_checkout_query:
        return

    if (
        update.message
        and update.message.successful_payment
    ):
        return

    if await is_required_channel_member(
        context.bot,
        user.id,
    ):
        return

    if q:

        try:
            await q.answer(
                "Сначала подпишитесь на канал.",
                show_alert=True,
            )
        except TelegramError:
            pass

    await show_channel_gate(
        update,
        context,
    )

    raise ApplicationHandlerStop


# =========================================================
# UI
# =========================================================

async def safe_delete(
    message: Message | None,
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
    replace: bool = True,
):

    chat_id = (
        update.effective_chat.id
    )

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

                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=fh,
                        caption=text,
                        reply_markup=markup,
                        show_caption_above_media=True,
                    )

                except TypeError:

                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=fh,
                        caption=text,
                        reply_markup=markup,
                    )

                return

        except Exception:

            logger.exception(
                "Failed to send photo UI"
            )

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
    )


async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data["state"] = None

    user_id = update.effective_user.id

    await send_ui(
        update,
        context,
        GREETING,
        kb_home(user_id),
    )


async def show_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    profile = get_profile(
        user_id,
        update.effective_user,
    )

    await send_ui(
        update,
        context,
        profile_caption(
            profile,
            user_id,
        ),
        kb_profile(),
        photo=PROFILE_BANNER,
    )


async def show_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await send_ui(
        update,
        context,
        "Выберите действие 👇",
        kb_menu(),
        photo=MENU_BANNER,
    )


async def show_support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await send_ui(
        update,
        context,
        SUPPORT_TEXT,
        kb_back_home(),
    )


def subscription_shop_text() -> str:

    return (
        "‼️ Доступ к основным функциям бота ‼️\n\n"
        f"{DAILY_REQUEST_LIMIT} запросов в день\n"
        "✔️ Защита пользователя\n\n"
        "⭐️ Неделя — 200 Stars\n"
        "⭐️ Месяц — 350 Stars\n"
        "⭐️ Год — 500 Stars\n\n"
        "👇 Выберите срок подписки ниже 👇"
    )


async def show_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await send_ui(
        update,
        context,
        subscription_shop_text(),
        kb_sub_plans(),
    )


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
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
        kb_profile(),
    )

    return False


# =========================================================
# PROFILE TEXT
# =========================================================

def profile_caption(
    profile: dict,
    user_id: int,
) -> str:

    sub_text = (
        "✅ Активна"
        if profile["sub_active"]
        else "❌ Не оформлена"
    )

    expires_text = ""

    if profile["sub"]:

        expires = aware(
            profile["sub"]["expires_at"]
        )

        expires_text = (
            f"\n⏳ До: "
            f"{expires.strftime('%d.%m.%Y %H:%M')}"
        )

    bonus_days = get_referral_bonus_days(
        user_id
    )

    bonus_text = (
        f"\n🎁 Реферальный бонус — "
        f"+{bonus_days} д."
        if bonus_days > 0
        else ""
    )

    return (
        "👤 Личный кабинет\n\n"
        f"🤗 Уровень — {profile['level']}\n"
        f"💞 Приоритет — {profile['warmth']}/1000\n"
        f"💬 Отправлено жалоб — {profile['sent']}\n"
        f"👀 Всего поисков — {profile['searches']}\n\n"
        f"💎 Подписка — {sub_text}"
        f"{expires_text}"
        f"{bonus_text}\n"
        f"📊 Запросов сегодня — "
        f"{usage_today(user_id)}/"
        f"{DAILY_REQUEST_LIMIT}"
    )


# =========================================================
# HUG PROCESS (SIMULATED UI)
# =========================================================

async def run_hug_animation(
    bot,
    chat_id: int,
    message_id: int,
    user_id: int,
    target: str,
):

    count = 356

    try:

        await asyncio.sleep(1)

        for percent in [
            8,
            20,
            34,
            49,
            68,
            71,
            90,
        ]:

            await bot.send_message(
                chat_id=chat_id,
                text=f"💤{percent}%💤",
            )

            await asyncio.sleep(1)

        await bot.send_message(
            chat_id=chat_id,
            text="💤100%💤",
        )

        await asyncio.sleep(
            0.5
        )

        add_hug(
            user_id,
            target,
            count,
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⭕️Отправлено жалоб — "
                f"{count}⭕️"
            ),
            reply_markup=kb_hooray(),
        )

    except Exception:

        logger.exception(
            "Hug animation failed"
        )

        log_event(
            "ERROR",
            "hug_animation_failed",
            user_id,
            {
                "target": target,
            },
        )


async def start_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    if not await require_subscription(
        update,
        context,
        user_id,
    ):
        return

    context.user_data["state"] = (
        "awaiting_hug_target"
    )

    await send_ui(
        update,
        context,
        (
            "Введите @username или ID "
            "аккаунта который будет sнесён"
        ),
        kb_back_home(),
    )


async def confirm_and_send_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    if not await require_subscription(
        update,
        context,
        user_id,
    ):

        context.user_data["state"] = None

        return

    ok, _used = request_usage(
        user_id
    )

    if not ok:

        context.user_data["state"] = None

        await send_ui(
            update,
            context,
            (
                "⛔️ Лимит на сегодня исчерпан.\n\n"
                f"Доступно {DAILY_REQUEST_LIMIT} "
                "запросов в день."
            ),
            kb_menu(),
        )

        return

    context.user_data["state"] = None

    target = context.user_data.pop(
        "hug_target",
        "другу",
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

    if q and q.message:

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
                text=initial_text,
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
            text=initial_text,
        )

        msg_id = msg.message_id

    context.application.create_task(
        run_hug_animation(
            context.bot,
            chat_id,
            msg_id,
            user_id,
            target,
        ),
        update=update,
    )


# =========================================================
# CHECK / SEARCH / HISTORY
# =========================================================

async def start_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    if not await require_subscription(
        update,
        context,
        user_id,
    ):
        return

    context.user_data["state"] = (
        "awaiting_check_target"
    )

    await send_ui(
        update,
        context,
        "Введите @username для проверки:",
        kb_back_home(),
    )


async def do_check(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    target: str,
):

    if not await require_subscription(
        update,
        context,
        user_id,
    ):
        return

    ok, _used = request_usage(
        user_id
    )

    if not ok:

        await update.message.reply_text(
            (
                "⛔️ Лимит на сегодня исчерпан.\n\n"
                f"Доступно {DAILY_REQUEST_LIMIT} "
                "запросов в день."
            ),
            reply_markup=kb_menu(),
        )

        return

    add_check(
        user_id
    )

    await update.message.reply_text(
        (
            f"🔎 Результат проверки {target}\n\n"
            f"🤗 Обнимашковость — "
            f"{random.randint(60,100)}%\n\n"
            "✅ Проверка завершена."
        ),
        reply_markup=kb_menu(),
    )


async def do_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    if not await require_subscription(
        update,
        context,
        user_id,
    ):
        return

    context.user_data["state"] = (
        "awaiting_search"
    )

    await send_ui(
        update,
        context,
        "Введите запрос для поиска:",
        kb_menu(),
    )


async def show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
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
            kb_menu(),
        )

        return

    lines = [
        (
            f"• {row['query']} — "
            f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
        )
        for row in history
    ]

    await send_ui(
        update,
        context,
        (
            "📜 История п0иска:\n\n"
            + "\n".join(lines)
        ),
        kb_menu(),
    )


# =========================================================
# MODERATION UI
# =========================================================

async def show_moderation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data["state"] = None

    await send_ui(
        update,
        context,
        (
            "🛡 МОДЕРАЦИЯ\n\n"
            "Здесь можно проверить публичный объект "
            "Telegram и подготовить официальное обращение.\n\n"
            "🔎 Проверка показывает доступную публичную "
            "информацию.\n"
            "📝 Обращение сохраняется в истории.\n"
            "📎 Можно добавить доказательства.\n\n"
            "⚠️ Автоматическая массовая отправка жалоб "
            "здесь не выполняется."
        ),
        kb_moderation(),
    )


async def show_moderation_cases(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    rows = get_moderation_cases(
        user_id
    )

    if not rows:

        await send_ui(
            update,
            context,
            (
                "📁 МОИ ОБРАЩЕНИЯ\n\n"
                "У вас пока нет подготовленных обращений."
            ),
            kb_moderation(),
        )

        return

    lines = [
        "📁 МОИ ОБРАЩЕНИЯ\n"
    ]

    for row in rows:

        reason_text = MODERATION_REASONS.get(
            row["reason"],
            row["reason"],
        )

        status = {
            "prepared": "🟡 Подготовлено",
            "closed": "⚪ Закрыто",
        }.get(
            row["status"],
            row["status"],
        )

        lines.append(
            f"#{row['id']} — {row['target']}\n"
            f"{reason_text}\n"
            f"📎 Доказательств: "
            f"{row['evidence_count']}\n"
            f"{status}\n"
            f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
        )

    await send_ui(
        update,
        context,
        "\n\n".join(lines),
        kb_moderation(),
    )


# =========================================================
# REFERRAL PAGE
# =========================================================

async def show_referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    link = await get_referral_link(
        context.bot,
        user_id,
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

    tiers = "\n".join(
        f"• {threshold} рефералов → +{days} д."
        for threshold, days
        in REFERRAL_TIERS
    )

    if not tiers:

        tiers = (
            "• Дополнительные уровни "
            "пока не настроены."
        )

    text = (
        "👥 Реферальная система\n\n"
        f"👤 Приглашено — {total}\n"
        f"💳 Оплатили — {rewarded}\n"
        f"🎁 Бонусных дней — {bonus_days}\n\n"
        "🔗 Ваша ссылка:\n"
        f"{link}\n\n"
        f"🎁 За каждого приглашённого, который "
        f"впервые оплатит подписку, "
        f"вы получаете +{REFERRAL_REWARD_DAYS} день.\n\n"
        "🏆 Уровни:\n"
        f"{tiers}"
    )

    await send_ui(
        update,
        context,
        text,
        kb_referrals(),
    )


# =========================================================
# PROFILE ADMIN LOOKUP
# =========================================================

def find_user_admin(
    query: str,
):

    raw = (
        query or ""
    ).strip()

    with db() as conn:

        if raw.lstrip("-").isdigit():

            return conn.execute(
                """
                SELECT *
                FROM profiles
                WHERE user_id=%s
                """,
                (
                    int(raw),
                ),
            ).fetchone()

        username = (
            raw.lstrip("@")
            .lower()
        )

        return conn.execute(
            """
            SELECT *
            FROM profiles
            WHERE LOWER(username)=LOWER(%s)
            LIMIT 1
            """,
            (
                username,
            ),
        ).fetchone()


def admin_user_details(
    user_id: int,
) -> tuple[
    str,
    InlineKeyboardMarkup,
]:

    with db() as conn:

        profile = conn.execute(
            """
            SELECT *
            FROM profiles
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()

        payments = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM payments
            WHERE user_id=%s
              AND status='paid'
            """,
            (user_id,),
        ).fetchone()["total"]

        paid_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE user_id=%s
              AND status='paid'
              AND currency='XTR'
            """,
            (user_id,),
        ).fetchone()["total"]

        crypto_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE user_id=%s
              AND status='paid'
              AND currency=%s
            """,
            (
                user_id,
                CRYPTO_ASSET,
            ),
        ).fetchone()["total"]

        hugs = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(count),0) AS sent
            FROM hugs
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()

        searches = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM searches
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()["total"]

        referrals = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id=%s
            """,
            (user_id,),
        ).fetchone()["total"]

        rewarded = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id=%s
              AND rewarded=TRUE
            """,
            (user_id,),
        ).fetchone()["total"]

        moderation_cases = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM moderation_cases
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()["total"]

    profile = (
        dict(profile)
        if profile
        else None
    )

    if not profile:

        return (
            "❌ Пользователь не найден.",
            kb_admin(),
        )

    sub = get_active_subscription(
        user_id
    )

    sub_text = "❌ Нет"

    if sub:

        sub_text = (
            f"✅ {sub['plan']} до "
            f"{aware(sub['expires_at']).strftime('%d.%m.%Y %H:%M')}"
        )

    bonus = get_referral_bonus_days(
        user_id
    )

    if profile.get("username"):

        text = (
            "👤 ПОЛЬЗОВАТЕЛЬ\n\n"
            f"ID: {profile['user_id']}\n"
            f"Username: @{profile['username']}\n"
        )

    else:

        text = (
            "👤 ПОЛЬЗОВАТЕЛЬ\n\n"
            f"ID: {profile['user_id']}\n"
            "Username: —\n"
        )

    text += (
        f"Имя: "
        f"{(profile.get('first_name') or '')} "
        f"{(profile.get('last_name') or '')}\n"
        f"Регистрация: "
        f"{aware(profile['first_seen_at']).strftime('%d.%m.%Y %H:%M') if profile.get('first_seen_at') else '—'}\n\n"
        f"💎 Подписка: {sub_text}\n"
        f"🎁 Ожидающий бонус: {bonus} д.\n\n"
        f"💳 Оплаченных платежей: {payments}\n"
        f"⭐ Stars: {int(paid_sum or 0)}\n"
        f"🪙 {CRYPTO_ASSET}: "
        f"{float(crypto_sum or 0):g}\n\n"
        f"💬 Сессий sn1c: {hugs['total']}\n"
        f"⭕️ Всего жалоб: "
        f"{int(hugs['sent'] or 0)}\n"
        f"🔎 Поисков: {searches}\n"
        f"👥 Рефералов: {referrals}\n"
        f"✅ Оплативших рефералов: "
        f"{rewarded}\n"
        f"🛡 Обращений: "
        f"{moderation_cases}\n"
        f"📊 Сегодня: "
        f"{usage_today(user_id)}/"
        f"{DAILY_REQUEST_LIMIT}"
    )

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💳 Платежи пользователя",
                    callback_data=(
                        f"admin:user_payments:{user_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "📚 Подписки пользователя",
                    callback_data=(
                        f"admin:user_subs:{user_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Админ-панель",
                    callback_data="admin:panel",
                )
            ],
        ]
    )

    return (
        text,
        markup,
    )


async def show_admin_user_payments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT plan,
                   method,
                   amount,
                   currency,
                   status,
                   created_at
            FROM payments
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT 15
            """,
            (user_id,),
        ).fetchall()

    lines = [
        f"💳 ПЛАТЕЖИ {user_id}\n"
    ]

    if not rows:

        lines.append(
            "Платежей нет."
        )

    else:

        for row in rows:

            status = {
                "paid": "✅",
                "pending": "⏳",
                "failed": "❌",
                "expired": "⌛",
            }.get(
                row["status"],
                "•",
            )

            amount = row["amount"]

            if amount is None:

                price = "—"

            elif row["currency"] == "XTR":

                price = (
                    f"{int(amount)}⭐"
                )

            else:

                price = (
                    f"{float(amount):g} "
                    f"{row['currency'] or ''}"
                )

            lines.append(
                f"{status} "
                f"{row['plan']} | "
                f"{row['method']} | "
                f"{price}\n"
                f"   "
                f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin(),
    )


async def show_admin_user_subs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT plan,
                   method,
                   payment_id,
                   starts_at,
                   expires_at,
                   created_at
            FROM subscriptions
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT 15
            """,
            (user_id,),
        ).fetchall()

    lines = [
        f"📚 ПОДПИСКИ {user_id}\n"
    ]

    if not rows:

        lines.append(
            "Подписок нет."
        )

    else:

        for row in rows:

            lines.append(
                f"• {row['plan']} | "
                f"{row['method']}\n"
                f"  "
                f"{aware(row['starts_at']).strftime('%d.%m.%Y %H:%M')}"
                f" → "
                f"{aware(row['expires_at']).strftime('%d.%m.%Y %H:%M')}\n"
                f"  payment: "
                f"{row['payment_id'] or '—'}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin(),
    )


# =========================================================
# ADMIN STATS
# =========================================================

def admin_stats() -> str:

    today = utcnow().date()

    week_start = (
        today
        - timedelta(days=6)
    )

    month_start = (
        today.replace(day=1)
    )

    with db() as conn:

        total_users = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM profiles
            """
        ).fetchone()["total"]

        active_subs = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS total
            FROM subscriptions
            WHERE expires_at > NOW()
            """
        ).fetchone()["total"]

        new_today = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM profiles
            WHERE first_seen_at::date=%s
            """,
            (
                today,
            ),
        ).fetchone()["total"]

        paid = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM payments
            WHERE status='paid'
            """
        ).fetchone()["total"]

        buyers = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) AS total
            FROM payments
            WHERE status='paid'
            """
        ).fetchone()["total"]

        stars = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE status='paid'
              AND currency='XTR'
            """
        ).fetchone()["total"]

        crypto = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS total
            FROM payments
            WHERE status='paid'
              AND currency=%s
            """,
            (
                CRYPTO_ASSET,
            ),
        ).fetchone()["total"]

        refs = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            """
        ).fetchone()["total"]

        converted_refs = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE rewarded=TRUE
            """
        ).fetchone()["total"]

        usage = conn.execute(
            """
            SELECT COALESCE(SUM(requests),0) AS total
            FROM daily_usage
            WHERE usage_date=%s
            """,
            (
                today,
            ),
        ).fetchone()["total"]

        hugs = conn.execute(
            """
            SELECT COALESCE(SUM(count),0) AS total
            FROM hugs
            """
        ).fetchone()["total"]

        checks = conn.execute(
            """
            SELECT COALESCE(SUM(checks),0) AS total
            FROM profiles
            """
        ).fetchone()["total"]

        searches = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM searches
            """
        ).fetchone()["total"]

        moderation = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM moderation_cases
            """
        ).fetchone()["total"]

        week = conn.execute(
            """
            SELECT
                COALESCE(SUM(new_users),0) AS users,
                COALESCE(SUM(requests),0) AS requests,
                COALESCE(SUM(paid_payments),0) AS paid,
                COALESCE(SUM(stars_revenue),0) AS stars,
                COALESCE(SUM(crypto_revenue),0) AS crypto
            FROM daily_stats
            WHERE stat_date BETWEEN %s AND %s
            """,
            (
                week_start,
                today,
            ),
        ).fetchone()

        month = conn.execute(
            """
            SELECT
                COALESCE(SUM(new_users),0) AS users,
                COALESCE(SUM(requests),0) AS requests,
                COALESCE(SUM(paid_payments),0) AS paid,
                COALESCE(SUM(stars_revenue),0) AS stars,
                COALESCE(SUM(crypto_revenue),0) AS crypto
            FROM daily_stats
            WHERE stat_date BETWEEN %s AND %s
            """,
            (
                month_start,
                today,
            ),
        ).fetchone()

    return (
        "🛠 АДМИН-ПАНЕЛЬ\n\n"
        "👥 Пользователи\n"
        f"• Всего — {total_users}\n"
        f"• Сегодня — {new_today}\n"
        f"• Активных подписок — {active_subs}\n\n"
        "💳 Продажи\n"
        f"• Всего оплат — {paid}\n"
        f"• Уникальных покупателей — {buyers}\n"
        f"• Stars — {int(stars or 0)}⭐\n"
        f"• {CRYPTO_ASSET} — "
        f"{float(crypto or 0):g}\n\n"
        "🔗 Рефералы\n"
        f"• Всего — {refs}\n"
        f"• Оплативших — {converted_refs}\n\n"
        "📊 Активность\n"
        f"• Запросов сегодня — "
        f"{usage}/{DAILY_REQUEST_LIMIT}\n"
        f"• Всего sn1cов — {hugs}\n"
        f"• Всего проверок — {checks}\n"
        f"• Всего поисков — {searches}\n"
        f"• Обращений — {moderation}\n\n"
        "📅 Последние 7 дней\n"
        f"• Новых — {week['users']}\n"
        f"• Запросов — {week['requests']}\n"
        f"• Оплат — {week['paid']}\n"
        f"• Stars — "
        f"{int(week['stars'] or 0)}⭐\n"
        f"• {CRYPTO_ASSET} — "
        f"{float(week['crypto'] or 0):g}\n\n"
        "📅 Текущий месяц\n"
        f"• Новых — {month['users']}\n"
        f"• Запросов — {month['requests']}\n"
        f"• Оплат — {month['paid']}\n"
        f"• Stars — "
        f"{int(month['stars'] or 0)}⭐\n"
        f"• {CRYPTO_ASSET} — "
        f"{float(month['crypto'] or 0):g}"
    )


async def show_admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    if not is_admin(user_id):

        await send_ui(
            update,
            context,
            "❌ Доступ запрещён.",
            kb_home(user_id),
        )

        return

    await send_ui(
        update,
        context,
        admin_stats(),
        kb_admin(),
    )


async def show_admin_users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                user_id,
                username,
                first_name,
                first_seen_at
            FROM profiles
            ORDER BY first_seen_at DESC
            LIMIT 20
            """
        ).fetchall()

    lines = [
        "👥 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ\n"
    ]

    if not rows:

        lines.append(
            "Пользователей пока нет."
        )

    else:

        for row in rows:

            name = (
                f"@{row['username']}"
                if row.get("username")
                else (
                    row.get("first_name")
                    or "—"
                )
            )

            lines.append(
                f"• {row['user_id']} | {name}\n"
                f"  "
                f"{aware(row['first_seen_at']).strftime('%d.%m.%Y %H:%M')}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin(),
    )


async def show_admin_payments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                user_id,
                plan,
                method,
                amount,
                currency,
                status,
                created_at
            FROM payments
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    lines = [
        "💳 ПОСЛЕДНИЕ ПОКУПКИ\n"
    ]

    if not rows:

        lines.append(
            "Покупок пока нет."
        )

    else:

        for row in rows:

            status = {
                "paid": "✅",
                "pending": "⏳",
                "failed": "❌",
                "expired": "⌛",
                "timeout": "⌛",
            }.get(
                row["status"],
                "•",
            )

            if row["amount"] is None:

                price = "—"

            elif row["currency"] == "XTR":

                price = (
                    f"{int(row['amount'])}⭐"
                )

            else:

                price = (
                    f"{float(row['amount']):g} "
                    f"{row['currency'] or ''}"
                )

            lines.append(
                f"{status} "
                f"{row['user_id']} | "
                f"{row['plan']} | "
                f"{row['method']}\n"
                f"   {price} | "
                f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin(),
    )


async def show_admin_referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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

    lines = [
        "🔗 ПОСЛЕДНИЕ РЕФЕРАЛЫ\n"
    ]

    if not rows:

        lines.append(
            "Рефералов пока нет."
        )

    else:

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
                f"   "
                f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin(),
    )


async def show_admin_promos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    rows = promo_list()

    lines = [
        "🎟 ПРОМОКОДЫ\n"
    ]

    if not rows:

        lines.append(
            "Промокодов пока нет."
        )

    else:

        for row in rows:

            limit = (
                "∞"
                if row["max_uses"] is None
                else str(row["max_uses"])
            )

            expires = (
                aware(
                    row["expires_at"]
                ).strftime(
                    "%d.%m.%Y %H:%M"
                )
                if row["expires_at"]
                else
                "без срока"
            )

            status = (
                "🟢"
                if row["active"]
                else "🔴"
            )

            lines.append(
                f"{status} "
                f"{row['code']} → "
                f"{row['plan']}\n"
                f"   Использовано: "
                f"{row['used']}/{limit}\n"
                f"   До: {expires}"
            )

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_admin_promo_list(rows),
    )


async def show_admin_health(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    try:

        latency = db_ping()
        bot = await context.bot.get_me()

        text = (
            "❤️ HEALTH\n\n"
            "🤖 Telegram Bot — ✅\n"
            "🗄 PostgreSQL — ✅\n"
            f"⏱ DB latency — {latency:.0f} ms\n"
            f"👤 Bot — @{bot.username}\n"
            f"📦 DB pool — "
            f"{DB_POOL.get_stats().get('pool_size', 0)} "
            "connections"
        )

        log_event(
            "INFO",
            "health_check",
            user_id,
            {
                "latency_ms": round(
                    latency,
                    2,
                )
            },
        )

    except Exception as exc:

        text = (
            "❤️ HEALTH\n\n"
            "❌ PostgreSQL недоступен.\n"
            f"{exc}"
        )

        logger.exception(
            "Health check failed"
        )

    await send_ui(
        update,
        context,
        text,
        kb_admin(),
    )


# =========================================================
# BACKUP
# =========================================================

def _jsonable(
    value,
):

    if isinstance(
        value,
        datetime,
    ):
        return value.isoformat()

    if isinstance(
        value,
        Decimal,
    ):
        return str(value)

    return value


def create_backup_bytes() -> tuple[
    bytes,
    str,
]:

    table_names = [
        "profiles",
        "hugs",
        "searches",
        "subscriptions",
        "payments",
        "daily_usage",
        "meta",
        "promo_codes",
        "promo_redemptions",
        "referrals",
        "referral_tier_rewards",
        "app_logs",
        "daily_stats",
        "channel_revocations",
        "moderation_cases",
        "moderation_evidence",
    ]

    payload = {
        "format": 1,
        "generated_at": utcnow().isoformat(),
        "project": "HugCollectBot",
        "tables": {},
    }

    with db() as conn:

        for table in table_names:

            rows = conn.execute(
                f"SELECT * FROM {table}"
            ).fetchall()

            payload["tables"][table] = [
                {
                    key: _jsonable(value)
                    for key, value
                    in dict(row).items()
                }
                for row in rows
            ]

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    data = gzip.compress(
        raw,
        compresslevel=6,
    )

    filename = (
        "hugbot_backup_"
        f"{utcnow().strftime('%Y%m%d_%H%M%S')}"
        ".json.gz"
    )

    return (
        data,
        filename,
    )


async def send_backup_to_admin(
    bot,
    admin_id: int,
) -> bool:

    try:

        data, filename = (
            create_backup_bytes()
        )

        document = io.BytesIO(
            data
        )

        document.name = filename

        await bot.send_document(
            chat_id=admin_id,
            document=document,
            caption=(
                "💾 Backup базы готов.\n\n"
                f"Файл: {filename}\n"
                f"Размер: "
                f"{len(data) / 1024:.1f} KB"
            ),
        )

        log_event(
            "INFO",
            "backup_sent",
            admin_id,
            {
                "filename": filename,
                "bytes": len(data),
            },
        )

        return True

    except Exception:

        logger.exception(
            "Backup generation/sending failed"
        )

        log_event(
            "ERROR",
            "backup_failed",
            admin_id,
        )

        return False


# =========================================================
# ACCESS / CHANNEL INVITE
# =========================================================

async def issue_channel_invite(
    bot,
    user_id: int,
    plan: str,
):

    if plan not in PLANS:
        return (
            None,
            "no_channel",
        )

    channel = PLANS[plan]["channel"]

    if not channel:

        logger.error(
            "CHANNEL NOT CONFIGURED: "
            "plan=%s user=%s",
            plan,
            user_id,
        )

        return (
            None,
            "no_channel",
        )

    try:

        member = await bot.get_chat_member(
            channel,
            user_id,
        )

        if member.status in {
            "member",
            "administrator",
            "creator",
        }:

            return (
                None,
                "already_member",
            )

    except TelegramError as exc:

        logger.warning(
            "Could not check channel member: "
            "plan=%s channel=%s user=%s error=%s",
            plan,
            channel,
            user_id,
            exc,
        )

    subscription = get_active_subscription(
        user_id
    )

    expire_date = (
        aware(
            subscription["expires_at"]
        )
        if subscription
        else None
    )

    try:

        invite = (
            await bot.create_chat_invite_link(
                chat_id=channel,
                name=(
                    f"user {user_id} {plan}"
                ),
                member_limit=1,
                expire_date=expire_date,
            )
        )

        return (
            invite.invite_link,
            "ok",
        )

    except BadRequest as exc:

        logger.error(
            "CHANNEL INVITE FAILED: "
            "plan=%s channel=%s user=%s error=%s",
            plan,
            channel,
            user_id,
            exc,
        )

        return (
            None,
            "error",
        )

    except TelegramError as exc:

        logger.error(
            "CHANNEL INVITE TELEGRAM ERROR: "
            "plan=%s channel=%s user=%s error=%s",
            plan,
            channel,
            user_id,
            exc,
        )

        return (
            None,
            "error",
        )


def access_granted_text(
    plan: str,
    expires: datetime,
    invite_status: str,
):

    expires = aware(
        expires
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
            f"ссылку в канал.\n"
            f"Напишите {SUPPORT_USERNAME}."
        )

    return text


async def grant_paid_access(
    bot,
    user_id: int,
    plan: str,
    method: str,
    payment_key: str,
    payment_db_id: int | None = None,
):

    existing = find_subscription_by_payment(
        payment_key
    )

    if existing:
        return aware(
            existing["expires_at"]
        )

    if payment_db_id:

        payment = get_payment(
            payment_db_id
        )

        if (
            not payment
            or int(payment["user_id"])
            != int(user_id)
            or payment["plan"] != plan
        ):
            raise ValueError(
                "Payment record mismatch"
            )

        update_payment(
            payment_db_id,
            "paid",
            external_id=payment_key,
        )

    expires, created = activate_subscription(
        user_id,
        plan,
        method,
        payment_key,
        consume_referral_bonus=True,
    )

    if not created:
        return expires

    if payment_db_id:

        payment = get_payment(
            payment_db_id
        )

        if payment:

            increment_stat(
                "paid_payments"
            )

            if payment["currency"] == "XTR":

                increment_stat(
                    "stars_revenue",
                    payment["amount"] or 0,
                )

            elif (
                payment["currency"]
                == CRYPTO_ASSET
            ):

                increment_stat(
                    "crypto_revenue",
                    payment["amount"] or 0,
                )

    await reward_referrer_for_purchase(
        bot,
        user_id,
    )

    link, invite_status = (
        await issue_channel_invite(
            bot,
            user_id,
            plan,
        )
    )

    text = access_granted_text(
        plan,
        expires,
        invite_status,
    )

    if link:

        text += (
            "\n\n👇 Ваша ссылка в канал:"
        )

    try:

        await bot.send_message(
            user_id,
            text,
            reply_markup=kb_after_pay(link),
        )

    except TelegramError:

        log_event(
            "ERROR",
            "paid_access_message_failed",
            user_id,
            {
                "plan": plan,
            },
        )

    return expires


# =========================================================
# CRYPTO API
# =========================================================

async def crypto_api(
    method: str,
    payload: dict,
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
            headers=headers,
        )

        response.raise_for_status()

        data = response.json()

    if not data.get("ok"):

        raise RuntimeError(
            data.get(
                "error",
                {},
            ).get(
                "name",
                "Crypto Pay API error",
            )
        )

    return data["result"]


async def create_crypto_invoice(
    user_id: int,
    plan: str,
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
        f"hug:{user_id}:{plan}:"
        f"{int(utcnow().timestamp())}"
    )

    result = await crypto_api(
        "createInvoice",
        {
            "asset": CRYPTO_ASSET,
            "amount": amount,
            "description": PLANS[plan]["title"],
            "payload": payload_id,
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 1800,
        },
    )

    return (
        result,
        payload_id,
    )


_crypto_watch_tasks: dict[
    int,
    asyncio.Task
] = {}


async def crypto_watch(
    application: Application,
    user_id: int,
    plan: str,
    payment_db_id: int,
    invoice_id: int,
):

    try:

        for _ in range(180):

            if find_subscription_by_payment(
                str(invoice_id)
            ):
                return

            try:

                result = await crypto_api(
                    "getInvoices",
                    {
                        "invoice_ids":
                            str(invoice_id)
                    },
                )

                items = result.get(
                    "items",
                    [],
                )

                if items:

                    status = items[0].get(
                        "status"
                    )

                    if status == "paid":

                        await grant_paid_access(
                            application.bot,
                            user_id,
                            plan,
                            "cryptobot",
                            str(invoice_id),
                            payment_db_id,
                        )

                        return

                    if status in {
                        "expired",
                        "invalid",
                    }:

                        update_payment(
                            payment_db_id,
                            status,
                        )

                        return

            except Exception:

                logger.exception(
                    "Crypto watcher error "
                    "invoice=%s",
                    invoice_id,
                )

            await asyncio.sleep(
                10
            )

        update_payment(
            payment_db_id,
            "timeout",
        )

    finally:

        _crypto_watch_tasks.pop(
            invoice_id,
            None,
        )


async def start_crypto_watch(
    application: Application,
    user_id: int,
    plan: str,
    payment_db_id: int,
    invoice_id: int,
):

    existing = _crypto_watch_tasks.get(
        invoice_id
    )

    if (
        existing
        and not existing.done()
    ):
        return

    task = application.create_task(
        crypto_watch(
            application,
            user_id,
            plan,
            payment_db_id,
            invoice_id,
        )
    )

    _crypto_watch_tasks[
        invoice_id
    ] = task


async def resume_pending_crypto_payments(
    application: Application,
):

    if not CRYPTO_PAY_API_TOKEN:
        return

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                id,
                user_id,
                plan,
                external_id
            FROM payments
            WHERE method='cryptobot'
              AND status='pending'
              AND external_id IS NOT NULL
              AND created_at >
                  NOW() - INTERVAL '40 minutes'
            ORDER BY id ASC
            LIMIT 50
            """
        ).fetchall()

    for row in rows:

        try:

            invoice_id = int(
                row["external_id"]
            )

        except (
            ValueError,
            TypeError,
        ):

            continue

        await start_crypto_watch(
            application,
            int(row["user_id"]),
            row["plan"],
            int(row["id"]),
            invoice_id,
        )


# =========================================================
# SUBSCRIPTION CALLBACK
# =========================================================

async def subscription_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    q = update.callback_query

    try:
        await q.answer()
    except TelegramError:
        pass

    user_id = q.from_user.id
    data = q.data

    ensure_profile(
        user_id,
        q.from_user,
    )

    if (
        data.startswith("sub:")
        and data.count(":") == 1
    ):

        plan = data.split(":")[1]

        if plan not in PLANS:
            return

        p = PLANS[plan]

        price_text = (
            f"{p['usd']}$ / {p['stars']}⭐"
            if p["usd"]
            else
            f"{p['stars']}⭐"
        )

        text = (
            f"💎 {p['title']}\n\n"
            f"⏳ Срок — {p['days']} дней\n"
            f"💵 Цена — {price_text}\n\n"
            "После оплаты подписка выдаётся автоматически.\n\n"
            "Выберите способ оплаты:"
        )

        await send_ui(
            update,
            context,
            text,
            kb_pay_methods(plan),
        )

        return

    if data == "pay:back":

        await show_subscription(
            update,
            context,
        )

        return

    if data.startswith(
        "pay:crypto:"
    ):

        plan = data.split(":")[-1]

        if plan not in PLANS:
            return

        fallback = {
            "week":
                CRYPTO_FALLBACK_WEEK,
            "month":
                CRYPTO_FALLBACK_MONTH,
            "year":
                CRYPTO_FALLBACK_YEAR,
        }.get(
            plan,
            "",
        )

        if not CRYPTO_PAY_API_TOKEN:

            if fallback:

                markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "💳 Оплатить в CryptoBot",
                                url=fallback,
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "Я оплатил",
                                callback_data=(
                                    f"manual_crypto:{plan}"
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "❌ Отменить",
                                callback_data="pay:cancel",
                            )
                        ],
                    ]
                )

                await send_ui(
                    update,
                    context,
                    (
                        "💳 Оплата через CryptoBot\n\n"
                        f"{PLANS[plan]['title']}\n\n"
                        "После оплаты нажмите "
                        "«Я оплатил»."
                    ),
                    markup,
                )

            else:

                await send_ui(
                    update,
                    context,
                    (
                        "❌ CryptoBot не настроен.\n\n"
                        "Добавьте "
                        "CRYPTO_PAY_API_TOKEN "
                        "в Render."
                    ),
                    kb_pay_methods(plan),
                )

            return

        try:

            invoice, _payload = (
                await create_crypto_invoice(
                    user_id,
                    plan,
                )
            )

            invoice_id = int(
                invoice["invoice_id"]
            )

            amount = float(
                PLANS[plan]["usd"]
            )

            payment_db_id = create_payment(
                user_id,
                plan,
                "cryptobot",
                str(invoice_id),
                amount,
                CRYPTO_ASSET,
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
                    "CryptoBot did not "
                    "return invoice URL"
                )

            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💳 Оплатить",
                            url=url,
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
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить",
                            callback_data="pay:cancel",
                        )
                    ],
                ]
            )

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
                markup,
            )

            await start_crypto_watch(
                context.application,
                user_id,
                plan,
                payment_db_id,
                invoice_id,
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
                kb_pay_methods(plan),
            )

        return

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
            None,
            p["stars"],
            "XTR",
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
                    f"hug:{user_id}:"
                    f"{plan}:"
                    f"{payment_db_id}"
                ),
                provider_token="",
                currency="XTR",
                prices=[
                    LabeledPrice(
                        p["title"],
                        p["stars"],
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
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Отменить",
                                callback_data="pay:cancel",
                            )
                        ]
                    ]
                ),
            )

        except TelegramError:

            logger.exception(
                "Stars invoice failed"
            )

            update_payment(
                payment_db_id,
                "failed",
                failure_reason=(
                    "invoice_create_failed"
                ),
            )

            await context.bot.send_message(
                user_id,
                (
                    "❌ Не удалось создать счёт Stars.\n\n"
                    "Попробуйте CryptoBot."
                ),
                reply_markup=kb_pay_methods(
                    plan
                ),
            )

        return

    if data == "pay:cancel":

        await send_ui(
            update,
            context,
            "❌ Оплата отменена.",
            kb_home(user_id),
        )

        return

    if data.startswith(
        "check_crypto:"
    ):

        parts = data.split(":")

        if len(parts) < 4:
            return

        try:

            invoice_id = int(
                parts[1]
            )

            plan = parts[2]

            payment_db_id = int(
                parts[3]
            )

        except (
            ValueError,
            IndexError,
        ):

            try:

                await q.answer(
                    "Некорректные данные оплаты.",
                    show_alert=True,
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
                kb_after_pay(None),
            )

            return

        if not CRYPTO_PAY_API_TOKEN:

            try:

                await q.answer(
                    "Автопроверка не настроена.",
                    show_alert=True,
                )

            except TelegramError:
                pass

            return

        payment = get_payment(
            payment_db_id
        )

        if (
            not payment
            or int(payment["user_id"])
            != user_id
            or payment["plan"] != plan
        ):

            try:

                await q.answer(
                    "Некорректный платёж.",
                    show_alert=True,
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
                },
            )

            items = result.get(
                "items",
                [],
            )

            if (
                items
                and items[0].get(
                    "status"
                ) == "paid"
            ):

                await grant_paid_access(
                    context.bot,
                    user_id,
                    plan,
                    "cryptobot",
                    str(invoice_id),
                    payment_db_id,
                )

                await safe_delete(
                    q.message
                )

                return

            if (
                items
                and items[0].get(
                    "status"
                ) in {
                    "expired",
                    "invalid",
                }
            ):

                update_payment(
                    payment_db_id,
                    items[0].get(
                        "status"
                    ),
                )

            try:

                await q.answer(
                    (
                        "Оплата ещё не найдена. "
                        "Попробуйте ещё раз."
                    ),
                    show_alert=True,
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
                    show_alert=True,
                )

            except TelegramError:
                pass

        return

    if data.startswith(
        "manual_crypto:"
    ):

        await send_ui(
            update,
            context,
            (
                "⏳ Для автоматической проверки "
                "настройте CRYPTO_PAY_API_TOKEN."
            ),
            kb_back_home(),
        )


# =========================================================
# STARS
# =========================================================

async def pre_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.pre_checkout_query

    payload = (
        query.invoice_payload
        or ""
    )

    parts = payload.split(":")

    valid = False

    error_message = (
        "Счёт недействителен. "
        "Откройте оплату заново."
    )

    if (
        len(parts) >= 4
        and parts[0] == "hug"
        and parts[2] in PLANS
    ):

        try:

            user_id = int(
                parts[1]
            )

            payment_db_id = int(
                parts[3]
            )

            payment = get_payment(
                payment_db_id
            )

            plan = parts[2]

            expected = PLANS[plan]["stars"]

            valid = (
                user_id
                == query.from_user.id
                and payment is not None
                and int(
                    payment["user_id"]
                ) == user_id
                and payment["plan"] == plan
                and payment["status"]
                == "pending"
                and payment["currency"]
                == "XTR"
                and int(
                    payment["amount"]
                    or 0
                ) == expected
                and int(
                    query.total_amount
                ) == expected
                and query.currency
                == "XTR"
            )

            if not valid:

                error_message = (
                    "Сумма или данные счёта "
                    "не совпадают. "
                    "Откройте оплату заново."
                )

        except (
            ValueError,
            TypeError,
        ):

            valid = False

    try:

        if valid:

            await query.answer(
                ok=True
            )

        else:

            await query.answer(
                ok=False,
                error_message=error_message,
            )

    except TelegramError:

        logger.exception(
            "Pre checkout error"
        )


async def successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    payment = (
        update.message.successful_payment
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
            reply_markup=kb_back_home(),
        )

        return

    (
        _,
        payload_user,
        plan,
        payment_db_id,
    ) = parts[:4]

    user_id = (
        update.effective_user.id
    )

    if (
        str(user_id)
        != payload_user
        or plan not in PLANS
    ):

        await update.message.reply_text(
            "❌ Оплата не совпала с аккаунтом.",
            reply_markup=kb_back_home(),
        )

        return

    try:

        db_id = int(
            payment_db_id
        )

    except ValueError:

        db_id = context.user_data.get(
            "pending_star_payment"
        )

    expected = PLANS[plan]["stars"]

    if (
        int(payment.total_amount)
        != expected
    ):

        if db_id:

            update_payment(
                db_id,
                "failed",
                failure_reason=(
                    "amount_mismatch"
                ),
            )

        log_event(
            "ERROR",
            "stars_amount_mismatch",
            user_id,
            {
                "expected": expected,
                "actual": payment.total_amount,
            },
        )

        await update.message.reply_text(
            (
                "❌ Сумма оплаты не совпала.\n"
                f"Напишите {SUPPORT_USERNAME}."
            ),
            reply_markup=kb_back_home(),
        )

        return

    charge_id = (
        payment.telegram_payment_charge_id
    )

    try:

        await grant_paid_access(
            context.bot,
            user_id,
            plan,
            "stars",
            charge_id,
            db_id,
        )

        context.user_data.pop(
            "pending_star_payment",
            None,
        )

    except Exception:

        logger.exception(
            "Stars fulfillment failed"
        )

        if db_id:

            update_payment(
                db_id,
                "failed",
                failure_reason=(
                    "fulfillment_failed"
                ),
            )

        await update.message.reply_text(
            (
                "❌ Оплата получена, но выдача "
                "доступа не завершилась.\n"
                f"Напишите {SUPPORT_USERNAME}."
            ),
            reply_markup=kb_back_home(),
        )


# =========================================================
# NAV CALLBACK
# =========================================================

async def nav_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    q = update.callback_query

    user_id = q.from_user.id

    data = q.data

    ensure_profile(
        user_id,
        q.from_user,
    )

    try:
        await q.answer()
    except TelegramError:
        pass

    if data == "nav:home":

        await show_home(
            update,
            context,
        )

    elif data == "nav:profile":

        await show_profile(
            update,
            context,
        )

    elif data == "nav:menu":

        await show_menu(
            update,
            context,
        )

    elif data == "nav:support":

        await show_support(
            update,
            context,
        )

    elif data == "nav:sub":

        await show_subscription(
            update,
            context,
        )

    elif data == "nav:promo":

        context.user_data["state"] = (
            "awaiting_promo"
        )

        await send_ui(
            update,
            context,
            "🎟 Введите промокод:",
            kb_profile(),
        )

    elif data == "nav:referrals":

        await show_referrals(
            update,
            context,
        )

    elif data == "menu:hug":

        await start_hug(
            update,
            context,
            user_id,
        )

    elif data == "menu:search":

        await do_search(
            update,
            context,
            user_id,
        )

    elif data == "menu:check":

        await start_check(
            update,
            context,
            user_id,
        )

    elif data == "menu:moderation":

        await show_moderation(
            update,
            context,
        )

    elif data == "menu:history":

        await show_history(
            update,
            context,
            user_id,
        )

    elif data == "hug:yes":

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
                kb_menu(),
            )

            return

        await confirm_and_send_hug(
            update,
            context,
            user_id,
        )

    elif data == "hug:no":

        context.user_data["state"] = None

        context.user_data.pop(
            "hug_target",
            None,
        )

        await send_ui(
            update,
            context,
            "❌ Отменено.",
            kb_menu(),
        )


# =========================================================
# ADMIN DAILY STATS
# =========================================================

async def show_admin_daily_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                stat_date,
                new_users,
                requests,
                paid_payments,
                stars_revenue,
                crypto_revenue,
                referrals
            FROM daily_stats
            ORDER BY stat_date DESC
            LIMIT 14
            """
        ).fetchall()

    lines = [
        "📈 СТАТИСТИКА ПО ДНЯМ\n"
    ]

    if not rows:

        lines.append(
            "Данных пока нет."
        )

    else:

        for row in rows:

            lines.append(
                f"📅 "
                f"{row['stat_date'].strftime('%d.%m.%Y')}\n"
                f"👥 +{row['new_users']} | "
                f"💳 {row['paid_payments']} оплат\n"
                f"📊 {row['requests']} запросов | "
                f"🔎 {row['referrals']} рефералов\n"
                f"⭐ "
                f"{int(row['stars_revenue'] or 0)} | "
                f"🪙 "
                f"{float(row['crypto_revenue'] or 0):g} "
                f"{CRYPTO_ASSET}"
            )

    await send_ui(
        update,
        context,
        "\n\n".join(lines),
        kb_admin(),
    )


async def show_admin_logs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    with db() as conn:

        rows = conn.execute(
            """
            SELECT
                level,
                event,
                user_id,
                details,
                created_at
            FROM app_logs
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()

    lines = [
        "🧾 ПОСЛЕДНИЕ ЛОГИ\n"
    ]

    if not rows:

        lines.append(
            "Логов пока нет."
        )

    else:

        for row in rows:

            details = (
                row["details"]
                or {}
            )

            details_text = ""

            if details:

                details_text = (
                    " | "
                    + json.dumps(
                        details,
                        ensure_ascii=False,
                        default=str,
                    )[:180]
                )

            lines.append(
                f"[{row['level']}] "
                f"{row['event']} | "
                f"{row['user_id'] or '—'}\n"
                f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M:%S')}"
                f"{details_text}"
            )

    await send_ui(
        update,
        context,
        "\n\n".join(lines),
        kb_admin(),
    )


async def broadcast_text(
    bot,
    text_value: str,
) -> tuple[int, int]:

    with db() as conn:

        rows = conn.execute(
            """
            SELECT user_id
            FROM profiles
            ORDER BY user_id
            """
        ).fetchall()

    sent = 0
    failed = 0

    for row in rows:

        try:

            await bot.send_message(
                int(row["user_id"]),
                text_value,
            )

            sent += 1

        except TelegramError:

            failed += 1

        await asyncio.sleep(
            0.05
        )

    return (
        sent,
        failed,
    )


# =========================================================
# MODERATION CALLBACK
# =========================================================

async def moderation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    q = update.callback_query

    user_id = q.from_user.id

    data = q.data

    ensure_profile(
        user_id,
        q.from_user,
    )

    try:
        await q.answer()
    except TelegramError:
        pass

    if data == "mod:open":

        await show_moderation(
            update,
            context,
        )

        return

    if data == "mod:cases":

        await show_moderation_cases(
            update,
            context,
        )

        return

    if data == "mod:check":

        if not await require_subscription(
            update,
            context,
            user_id,
        ):
            return

        context.user_data["state"] = (
            "awaiting_mod_check_target"
        )

        await send_ui(
            update,
            context,
            (
                "🔎 ПРОВЕРКА ОБЪЕКТА\n\n"
                "Отправьте:\n"
                "• @username\n"
                "• username\n"
                "• https://t.me/username\n\n"
                "После этого я попробую получить "
                "доступную публичную информацию."
            ),
            kb_moderation(),
        )

        return

    if data == "mod:create":

        if not await require_subscription(
            update,
            context,
            user_id,
        ):
            return

        context.user_data["state"] = (
            "awaiting_case_target"
        )

        context.user_data.pop(
            "moderation_target",
            None,
        )

        context.user_data.pop(
            "moderation_target_type",
            None,
        )

        context.user_data.pop(
            "moderation_reason",
            None,
        )

        await send_ui(
            update,
            context,
            (
                "📝 НОВОЕ ОБРАЩЕНИЕ\n\n"
                "Отправьте @username или публичную "
                "ссылку на канал, группу или другую "
                "публичную страницу."
            ),
            kb_moderation(),
        )

        return

    if data.startswith(
        "mod:reason:"
    ):

        if (
            context.user_data.get("state")
            != "awaiting_case_reason"
        ):
            return

        reason = data.split(
            ":",
            2,
        )[2]

        if reason not in MODERATION_REASONS:
            return

        context.user_data[
            "moderation_reason"
        ] = reason

        context.user_data["state"] = (
            "awaiting_case_description"
        )

        await send_ui(
            update,
            context,
            (
                f"✅ Основание: "
                f"{MODERATION_REASONS[reason]}\n\n"
                "Теперь напишите краткое описание "
                "того, что именно вы обнаружили.\n\n"
                "Опирайтесь только на проверяемые факты."
            ),
            kb_moderation(),
        )

        return

    if data.startswith(
        "mod:add_evidence:"
    ):

        try:

            case_id = int(
                data.rsplit(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            return

        case = get_moderation_case(
            case_id,
            user_id,
        )

        if not case:

            await send_ui(
                update,
                context,
                "❌ Обращение не найдено.",
                kb_moderation(),
            )

            return

        context.user_data["state"] = (
            "moderation_evidence"
        )

        context.user_data[
            "moderation_case_id"
        ] = case_id

        await send_ui(
            update,
            context,
            (
                f"📎 ДОКАЗАТЕЛЬСТВА ДЛЯ "
                f"#{case_id}\n\n"
                "Отправьте фотографию или документ.\n\n"
                "Можно отправить несколько файлов."
            ),
            kb_moderation_evidence(
                case_id
            ),
        )

        return

    if data.startswith(
        "mod:finish:"
    ):

        try:

            case_id = int(
                data.rsplit(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            return

        case = get_moderation_case(
            case_id,
            user_id,
        )

        if not case:

            await send_ui(
                update,
                context,
                "❌ Обращение не найдено.",
                kb_moderation(),
            )

            return

        with db() as conn:

            conn.execute(
                """
                UPDATE moderation_cases
                SET status='closed',
                    updated_at=%s
                WHERE id=%s
                  AND user_id=%s
                """,
                (
                    utcnow(),
                    case_id,
                    user_id,
                ),
            )

            conn.commit()

        updated_case = get_moderation_case(
            case_id,
            user_id,
        )

        context.user_data["state"] = None

        context.user_data.pop(
            "moderation_case_id",
            None,
        )

        await send_ui(
            update,
            context,
            moderation_case_text(
                updated_case
            ),
            kb_moderation(),
        )

        return


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    q = update.callback_query

    user_id = q.from_user.id

    if not is_admin(user_id):

        try:

            await q.answer(
                "Доступ запрещён.",
                show_alert=True,
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
            context,
        )

    elif data == "admin:stats":

        await send_ui(
            update,
            context,
            admin_stats(),
            kb_admin(),
        )

    elif data == "admin:users":

        await show_admin_users(
            update,
            context,
        )

    elif data == "admin:user_search":

        context.user_data["state"] = (
            "admin_user_search"
        )

        await send_ui(
            update,
            context,
            (
                "🔎 Введите Telegram ID "
                "или @username пользователя:"
            ),
            kb_admin(),
        )

    elif data.startswith(
        "admin:user_payments:"
    ):

        try:

            target_id = int(
                data.rsplit(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            return

        await show_admin_user_payments(
            update,
            context,
            target_id,
        )

    elif data.startswith(
        "admin:user_subs:"
    ):

        try:

            target_id = int(
                data.rsplit(
                    ":",
                    1,
                )[1]
            )

        except ValueError:

            return

        await show_admin_user_subs(
            update,
            context,
            target_id,
        )

    elif data == "admin:payments":

        await show_admin_payments(
            update,
            context,
        )

    elif data == "admin:referrals":

        await show_admin_referrals(
            update,
            context,
        )

    elif data == "admin:promos":

        await show_admin_promos(
            update,
            context,
        )

    elif data == "admin:promo_create":

        context.user_data["state"] = (
            "admin_promo_create"
        )

        await send_ui(
            update,
            context,
            (
                "🎟 Создание промокода\n\n"
                "Формат:\n"
                "CODE PLAN MAX_USES YYYY-MM-DD\n\n"
                "Пример:\n"
                "VIP50 month 100 2026-10-31\n\n"
                "Для безлимитных использований: "
                "MAX_USES=0\n"
                "Для бессрочного кода вместо даты: -"
            ),
            kb_admin(),
        )

    elif data.startswith(
        "admin:promo_off:"
    ):

        code = data.split(
            ":",
            2,
        )[2]

        deactivate_promo(
            code
        )

        await show_admin_promos(
            update,
            context,
        )

    elif data == "admin:backup":

        await send_ui(
            update,
            context,
            "⏳ Создаю backup базы...",
            kb_admin(),
        )

        await send_backup_to_admin(
            context.bot,
            user_id,
        )

        await show_admin_panel(
            update,
            context,
        )

    elif data == "admin:health":

        await show_admin_health(
            update,
            context,
        )

    elif data == "admin:daily_stats":

        await show_admin_daily_stats(
            update,
            context,
        )

    elif data == "admin:logs":

        await show_admin_logs(
            update,
            context,
        )

    elif data == "admin:broadcast":

        context.user_data["state"] = (
            "admin_broadcast"
        )

        await send_ui(
            update,
            context,
            (
                "📢 Введите текст рассылки.\n\n"
                "После отправки бот разошлёт сообщение "
                "всем пользователям базы."
            ),
            kb_admin(),
        )


# =========================================================
# START
# =========================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    ensure_profile(
        user_id,
        update.effective_user,
    )

    if context.args:

        start_param = context.args[0]

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
                    user_id,
                )

    subscription = get_active_subscription(
        user_id
    )

    logger.info(
        "START: user=%s "
        "active_subscription=%s",
        user_id,
        bool(subscription),
    )

    context.user_data["state"] = None

    context.user_data.pop(
        "hug_target",
        None,
    )

    await update.message.reply_text(
        "Меню перенесено в сообщение 👇",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        GREETING,
        reply_markup=kb_home(user_id),
    )


# =========================================================
# ADMIN COMMANDS
# =========================================================

async def cmd_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = (
        update.effective_user.id
    )

    if not is_admin(user_id):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    await update.message.reply_text(
        admin_stats(),
        reply_markup=kb_admin(),
    )


async def cmd_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    await update.message.reply_text(
        admin_stats(),
        reply_markup=kb_admin(),
    )


async def cmd_health(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    await show_admin_health(
        update,
        context,
    )


async def cmd_backup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    await update.message.reply_text(
        "⏳ Создаю backup базы..."
    )

    ok = await send_backup_to_admin(
        context.bot,
        update.effective_user.id,
    )

    if not ok:

        await update.message.reply_text(
            (
                "❌ Не удалось создать backup. "
                "Проверьте логи Render."
            )
        )


async def cmd_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ Доступ запрещён."
        )

        return

    if not context.args:

        await update.message.reply_text(
            (
                "Использование: "
                "/user 123456789 "
                "или /user @username"
            )
        )

        return

    profile = find_user_admin(
        " ".join(
            context.args
        )
    )

    if not profile:

        await update.message.reply_text(
            "❌ Пользователь не найден."
        )

        return

    text, markup = admin_user_details(
        int(profile["user_id"])
    )

    await update.message.reply_text(
        text,
        reply_markup=markup,
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
        user_id,
        update.effective_user,
    )

    state = context.user_data.get(
        "state"
    )

    # ---------------------------------------------
    # ADMIN USER SEARCH
    # ---------------------------------------------

    if (
        is_admin(user_id)
        and state == "admin_user_search"
    ):

        context.user_data["state"] = None

        profile = find_user_admin(
            text
        )

        if not profile:

            await update.message.reply_text(
                "❌ Пользователь не найден.",
                reply_markup=kb_admin(),
            )

            return

        details, markup = admin_user_details(
            int(profile["user_id"])
        )

        await update.message.reply_text(
            details,
            reply_markup=markup,
        )

        return

    # ---------------------------------------------
    # ADMIN PROMO CREATE
    # ---------------------------------------------

    if (
        is_admin(user_id)
        and state == "admin_promo_create"
    ):

        context.user_data["state"] = None

        parts = text.split()

        if len(parts) != 4:

            await update.message.reply_text(
                (
                    "❌ Формат: "
                    "CODE PLAN MAX_USES YYYY-MM-DD\n"
                    "Пример: "
                    "VIP50 month 100 2026-10-31"
                ),
                reply_markup=kb_admin(),
            )

            return

        code, plan, max_raw, date_raw = parts

        code = code.upper()
        plan = plan.lower()

        if not max_raw.isdigit():

            await update.message.reply_text(
                (
                    "❌ MAX_USES должен быть числом."
                ),
                reply_markup=kb_admin(),
            )

            return

        max_uses = int(
            max_raw
        )

        expires_at = None

        if date_raw != "-":

            try:

                expires_at = (
                    datetime.strptime(
                        date_raw,
                        "%Y-%m-%d",
                    ).replace(
                        hour=23,
                        minute=59,
                        second=59,
                        tzinfo=timezone.utc,
                    )
                )

            except ValueError:

                await update.message.reply_text(
                    (
                        "❌ Дата должна быть "
                        "YYYY-MM-DD."
                    ),
                    reply_markup=kb_admin(),
                )

                return

        try:

            admin_create_promo(
                code,
                plan,
                max_uses,
                expires_at,
            )

        except ValueError as exc:

            await update.message.reply_text(
                f"❌ {exc}",
                reply_markup=kb_admin(),
            )

            return

        await update.message.reply_text(
            f"✅ Промокод {code} сохранён.",
            reply_markup=kb_admin(),
        )

        return

    # ---------------------------------------------
    # ADMIN BROADCAST
    # ---------------------------------------------

    if (
        is_admin(user_id)
        and state == "admin_broadcast"
    ):

        context.user_data["state"] = None

        await update.message.reply_text(
            "⏳ Начинаю рассылку..."
        )

        sent, failed = await broadcast_text(
            context.bot,
            text,
        )

        log_event(
            "INFO",
            "broadcast_completed",
            user_id,
            {
                "sent": sent,
                "failed": failed,
            },
        )

        await update.message.reply_text(
            (
                "✅ Рассылка завершена.\n\n"
                f"📨 Отправлено: {sent}\n"
                f"❌ Ошибок: {failed}"
            ),
            reply_markup=kb_admin(),
        )

        return

    # ---------------------------------------------
    # MODERATION CHECK
    # ---------------------------------------------

    if state == "awaiting_mod_check_target":

        context.user_data["state"] = None

        if not await require_subscription(
            update,
            context,
            user_id,
        ):
            return

        ok, _used = request_usage(
            user_id
        )

        if not ok:

            await update.message.reply_text(
                (
                    "⛔️ Лимит на сегодня исчерпан.\n\n"
                    f"Доступно {DAILY_REQUEST_LIMIT} "
                    "запросов в день."
                ),
                reply_markup=kb_moderation(),
            )

            return

        result = await inspect_moderation_target(
            context.bot,
            text,
        )

        add_check(
            user_id
        )

        if not result["found"]:

            await update.message.reply_text(
                (
                    "🔎 РЕЗУЛЬТАТ ПРОВЕРКИ\n\n"
                    f"🎯 Объект: "
                    f"{result['target']}\n\n"
                    "❌ Публичная информация "
                    "не получена.\n\n"
                    "Проверьте username/ссылку "
                    "и убедитесь, что объект "
                    "публичный."
                ),
                reply_markup=kb_moderation(),
            )

            return

        title = (
            result["title"]
            or "—"
        )

        description = (
            result["description"]
            or "—"
        )

        await update.message.reply_text(
            (
                "🔎 РЕЗУЛЬТАТ ПРОВЕРКИ\n\n"
                f"🎯 Объект: "
                f"{result['target']}\n"
                f"📌 Тип: "
                f"{moderation_target_type_ru(result['type'])}\n"
                f"🏷 Название: {title}\n\n"
                "📝 Описание:\n"
                f"{description[:1800]}\n\n"
                f"🔗 {result['url'] or '—'}\n\n"
                "✅ Проверка завершена."
            ),
            reply_markup=kb_moderation(),
        )

        return

    # ---------------------------------------------
    # MODERATION CASE TARGET
    # ---------------------------------------------

    if state == "awaiting_case_target":

        if not await require_subscription(
            update,
            context,
            user_id,
        ):
            return

        result = await inspect_moderation_target(
            context.bot,
            text,
        )

        if not result["found"]:

            await update.message.reply_text(
                (
                    "❌ Не удалось получить "
                    "публичную информацию.\n\n"
                    "Попробуйте другой username "
                    "или публичную ссылку."
                ),
                reply_markup=kb_moderation(),
            )

            return

        context.user_data[
            "moderation_target"
        ] = result["target"]

        context.user_data[
            "moderation_target_type"
        ] = result["type"]

        context.user_data[
            "moderation_state_preview"
        ] = result

        context.user_data["state"] = (
            "awaiting_case_reason"
        )

        await update.message.reply_text(
            (
                "✅ Объект найден.\n\n"
                f"🎯 {result['target']}\n"
                f"📌 "
                f"{moderation_target_type_ru(result['type'])}\n"
                f"🏷 {result['title'] or '—'}\n\n"
                "Теперь выберите "
                "основание обращения:"
            ),
            reply_markup=kb_mod_reasons(),
        )

        return

    # ---------------------------------------------
    # MODERATION DESCRIPTION
    # ---------------------------------------------

    if state == "awaiting_case_description":

        target = context.user_data.get(
            "moderation_target"
        )

        target_type = context.user_data.get(
            "moderation_target_type",
            "unknown",
        )

        reason = context.user_data.get(
            "moderation_reason"
        )

        if not target or not reason:

            context.user_data["state"] = None

            await update.message.reply_text(
                (
                    "❌ Сессия обращения устарела. "
                    "Начните заново."
                ),
                reply_markup=kb_moderation(),
            )

            return

        ok, _used = request_usage(
            user_id
        )

        if not ok:

            context.user_data["state"] = None

            await update.message.reply_text(
                (
                    "⛔️ Лимит на сегодня исчерпан.\n\n"
                    f"Доступно {DAILY_REQUEST_LIMIT} "
                    "запросов в день."
                ),
                reply_markup=kb_moderation(),
            )

            return

        description = text[:5000]

        case_id = create_moderation_case(
            user_id=user_id,
            target=target,
            target_type=target_type,
            reason=reason,
            description=description,
        )

        context.user_data["state"] = (
            "moderation_evidence"
        )

        context.user_data[
            "moderation_case_id"
        ] = case_id

        context.user_data.pop(
            "moderation_target",
            None,
        )

        context.user_data.pop(
            "moderation_target_type",
            None,
        )

        context.user_data.pop(
            "moderation_reason",
            None,
        )

        context.user_data.pop(
            "moderation_state_preview",
            None,
        )

        await update.message.reply_text(
            (
                f"✅ Обращение #{case_id} создано.\n\n"
                "Теперь можно добавить доказательства.\n\n"
                "📷 Скриншоты\n"
                "📄 Документы\n\n"
                "Когда закончите, нажмите "
                "«Завершить обращение»."
            ),
            reply_markup=kb_moderation_evidence(
                case_id
            ),
        )

        return

    # ---------------------------------------------
    # MODERATION EVIDENCE
    # ---------------------------------------------

    if state == "moderation_evidence":

        await update.message.reply_text(
            (
                "📎 Сейчас ожидаются доказательства.\n\n"
                "Отправьте фото/документ или нажмите "
                "«Завершить обращение»."
            ),
            reply_markup=kb_moderation_evidence(
                int(
                    context.user_data.get(
                        "moderation_case_id",
                        0,
                    )
                )
            ),
        )

        return

    # ---------------------------------------------
    # HUG TARGET
    # ---------------------------------------------

    if state == "awaiting_hug_target":

        context.user_data[
            "hug_target"
        ] = text

        context.user_data["state"] = (
            "awaiting_confirm"
        )

        await update.message.reply_text(
            (
                f"Вы уверены, что хотите "
                f"отправить жалобы {text}?"
            ),
            reply_markup=kb_confirm_hug(),
        )

        return

    # ---------------------------------------------
    # CONFIRM
    # ---------------------------------------------

    if state == "awaiting_confirm":

        await update.message.reply_text(
            "Нажмите кнопку выше 👆",
            reply_markup=kb_confirm_hug(),
        )

        return

    # ---------------------------------------------
    # PROMO
    # ---------------------------------------------

    if state == "awaiting_promo":

        context.user_data["state"] = None

        await apply_promo_text(
            update,
            context,
            user_id,
            text,
        )

        return

    # ---------------------------------------------
    # CHECK
    # ---------------------------------------------

    if state == "awaiting_check_target":

        context.user_data["state"] = None

        await do_check(
            update,
            context,
            user_id,
            text,
        )

        return

    # ---------------------------------------------
    # SEARCH
    # ---------------------------------------------

    if state == "awaiting_search":

        context.user_data["state"] = None

        if not await require_subscription(
            update,
            context,
            user_id,
        ):
            return

        ok, _used = request_usage(
            user_id
        )

        if not ok:

            await update.message.reply_text(
                (
                    "⛔️ Лимит на сегодня исчерпан.\n\n"
                    f"Доступно {DAILY_REQUEST_LIMIT} "
                    "запросов в день."
                ),
                reply_markup=kb_menu(),
            )

            return

        add_search(
            user_id,
            text,
        )

        await update.message.reply_text(
            (
                f"🔎 Результат поиска: {text}\n\n"
                "✅ Поиск завершён."
            ),
            reply_markup=kb_menu(),
        )

        return

    # ---------------------------------------------
    # PROMO WITHOUT STATE
    # ---------------------------------------------

    if (
        text.upper()
        == PROMO_CODE
        and PROMO_CODE
    ):

        await apply_promo_text(
            update,
            context,
            user_id,
            text,
        )

        return

    await update.message.reply_text(
        "Не понимаю 🙈 Воспользуйтесь меню.",
        reply_markup=kb_home(user_id),
    )


# =========================================================
# MODERATION MEDIA
# =========================================================

async def handle_moderation_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if (
        not update.effective_user
        or not update.message
    ):
        return

    user_id = (
        update.effective_user.id
    )

    if (
        context.user_data.get(
            "state"
        )
        != "moderation_evidence"
    ):
        return

    case_id_raw = context.user_data.get(
        "moderation_case_id"
    )

    try:

        case_id = int(
            case_id_raw
        )

    except (
        TypeError,
        ValueError,
    ):

        context.user_data["state"] = None

        await update.message.reply_text(
            "❌ Обращение не найдено.",
            reply_markup=kb_moderation(),
        )

        return

    case = get_moderation_case(
        case_id,
        user_id,
    )

    if not case:

        context.user_data["state"] = None

        await update.message.reply_text(
            "❌ Обращение не найдено.",
            reply_markup=kb_moderation(),
        )

        return

    file_id = None
    file_type = None

    if update.message.photo:

        file_id = (
            update.message.photo[-1].file_id
        )

        file_type = "photo"

    elif update.message.document:

        file_id = (
            update.message.document.file_id
        )

        file_type = "document"

    if not file_id:
        return

    try:

        add_moderation_evidence(
            case_id=case_id,
            user_id=user_id,
            file_id=file_id,
            file_type=file_type,
        )

    except Exception:

        logger.exception(
            "Failed to save moderation evidence "
            "case=%s user=%s",
            case_id,
            user_id,
        )

        await update.message.reply_text(
            "❌ Не удалось сохранить доказательство.",
            reply_markup=kb_moderation_evidence(
                case_id
            ),
        )

        return

    updated_case = get_moderation_case(
        case_id,
        user_id,
    )

    await update.message.reply_text(
        (
            "✅ Доказательство добавлено.\n\n"
            f"📋 Обращение #{case_id}\n"
            f"📎 Файлов: "
            f"{updated_case['evidence_count']}"
        ),
        reply_markup=kb_moderation_evidence(
            case_id
        ),
    )


# =========================================================
# EXPIRATION
# =========================================================

async def remove_from_channels(
    bot,
    user_id: int,
    expires_at: datetime,
):

    channels = {
        p["channel"]
        for p in PLANS.values()
        if p["channel"]
    }

    expires_at = aware(
        expires_at
    )

    for channel in channels:

        with db() as conn:

            already = conn.execute(
                """
                SELECT 1
                FROM channel_revocations
                WHERE user_id=%s
                  AND channel=%s
                  AND revoked_for_expires_at=%s
                LIMIT 1
                """,
                (
                    user_id,
                    channel,
                    expires_at,
                ),
            ).fetchone()

        if already:
            continue

        try:

            await bot.ban_chat_member(
                channel,
                user_id,
            )

            await bot.unban_chat_member(
                channel,
                user_id,
                only_if_banned=True,
            )

        except TelegramError as exc:

            logger.warning(
                "Could not remove expired "
                "user=%s from channel=%s: %s",
                user_id,
                channel,
                exc,
            )

        with db() as conn:

            conn.execute(
                """
                INSERT INTO channel_revocations(
                    user_id,
                    channel,
                    revoked_for_expires_at,
                    revoked_at
                )
                VALUES(%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    user_id,
                    channel,
                    expires_at,
                    utcnow(),
                ),
            )

            conn.commit()

    try:

        await bot.send_message(
            user_id,
            (
                "⏳ Ваша подписка закончилась.\n\n"
                "Оформите новую подписку, "
                "чтобы вернуть доступ."
            ),
            reply_markup=kb_profile(),
        )

    except TelegramError:
        pass


async def expiration_loop(
    application: Application,
):

    while True:

        try:

            with db() as conn:

                rows = conn.execute(
                    """
                    SELECT
                        user_id,
                        MAX(expires_at) AS expires_at
                    FROM subscriptions
                    GROUP BY user_id
                    HAVING MAX(expires_at) <= NOW()
                    ORDER BY MAX(expires_at) ASC
                    LIMIT 100
                    """
                ).fetchall()

            for row in rows:

                await remove_from_channels(
                    application.bot,
                    int(row["user_id"]),
                    aware(row["expires_at"]),
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
# CLEANUP
# =========================================================

async def cleanup_loop(
    application: Application,
):

    while True:

        try:

            with db() as conn:

                conn.execute(
                    """
                    DELETE FROM app_logs
                    WHERE created_at <
                        NOW() - (
                            %s * INTERVAL '1 day'
                        )
                    """,
                    (
                        LOG_RETENTION_DAYS,
                    ),
                )

                conn.execute(
                    """
                    DELETE FROM daily_usage
                    WHERE usage_date <
                        CURRENT_DATE - (
                            %s * INTERVAL '1 day'
                        )
                    """,
                    (
                        USAGE_RETENTION_DAYS,
                    ),
                )

                conn.execute(
                    """
                    DELETE FROM daily_stats
                    WHERE stat_date <
                        CURRENT_DATE - (
                            %s * INTERVAL '1 day'
                        )
                    """,
                    (
                        STATS_RETENTION_DAYS,
                    ),
                )

                conn.commit()

            log_event(
                "INFO",
                "cleanup_completed",
            )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Cleanup loop failed"
            )

        await asyncio.sleep(
            6 * 60 * 60
        )


# =========================================================
# AUTO BACKUP
# =========================================================

async def auto_backup_loop(
    application: Application,
):

    if (
        not AUTO_BACKUP
        or not AUTO_BACKUP_ADMIN_ID
    ):
        return

    while True:

        try:

            now = utcnow()

            key = (
                now.strftime(
                    "%Y-%m-%d-%H"
                )
            )

            if (
                now.hour
                == AUTO_BACKUP_HOUR_UTC
                and now.minute < 5
            ):

                with db() as conn:

                    row = conn.execute(
                        """
                        SELECT value
                        FROM meta
                        WHERE key='last_auto_backup_hour'
                        """
                    ).fetchone()

                    last = (
                        row["value"]
                        if row
                        else ""
                    )

                if last != key:

                    ok = (
                        await send_backup_to_admin(
                            application.bot,
                            AUTO_BACKUP_ADMIN_ID,
                        )
                    )

                    if ok:

                        with db() as conn:

                            conn.execute(
                                """
                                INSERT INTO meta(
                                    key,
                                    value
                                )
                                VALUES(
                                    'last_auto_backup_hour',
                                    %s
                                )
                                ON CONFLICT(key)
                                DO UPDATE SET
                                    value=EXCLUDED.value
                                """,
                                (key,),
                            )

                            conn.commit()

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Auto backup loop failed"
            )

        await asyncio.sleep(
            30
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):

    application.create_task(
        expiration_loop(
            application
        )
    )

    application.create_task(
        cleanup_loop(
            application
        )
    )

    application.create_task(
        auto_backup_loop(
            application
        )
    )

    await resume_pending_crypto_payments(
        application
    )


# =========================================================
# SHUTDOWN
# =========================================================

async def post_shutdown(
    application: Application,
):

    global DB_OPENED

    try:

        if (
            DB_OPENED
            and not DB_POOL.closed
        ):

            DB_POOL.close()

            DB_OPENED = False

    except Exception:

        logger.exception(
            "Database pool close failed"
        )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    logger.info(
        "Database ready: PostgreSQL/Supabase"
    )

    logger.info(
        "Config: daily_limit=%s "
        "referral_reward=%s "
        "tiers=%s "
        "auto_backup=%s",
        DAILY_REQUEST_LIMIT,
        REFERRAL_REWARD_DAYS,
        REFERRAL_TIERS,
        AUTO_BACKUP,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        TypeHandler(
            Update,
            channel_gate,
        ),
        group=-1,
    )

    app.add_handler(
        CommandHandler(
            "start",
            cmd_start,
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            cmd_admin,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            cmd_stats,
        )
    )

    app.add_handler(
        CommandHandler(
            "health",
            cmd_health,
        )
    )

    app.add_handler(
        CommandHandler(
            "backup",
            cmd_backup,
        )
    )

    app.add_handler(
        CommandHandler(
            "user",
            cmd_user,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            nav_callback,
            pattern=r"^(nav:|menu:|hug:)",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            moderation_callback,
            pattern=r"^mod:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            subscription_callback,
            pattern=r"^(sub:|pay:|manual_crypto:|check_crypto:)",
        )
    )

    app.add_handler(
        PreCheckoutQueryHandler(
            pre_checkout
        )
    )

    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            successful_payment,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.Document.ALL,
            handle_moderation_media,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_text,
        )
    )

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
            drop_pending_updates=True,
        )

    else:

        logger.info(
            "Starting polling"
        )

        app.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()