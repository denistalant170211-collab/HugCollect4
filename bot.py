import asyncio
import base64
import gzip
import hashlib
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
from cryptography.fernet import Fernet, InvalidToken
from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Bot,
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
logger = logging.getLogger("darkbot")


# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set")

# Token encryption for user-created mirror bots.
# Prefer a dedicated MIRROR_ENCRYPTION_KEY in Render; when it is not set,
# derive a stable key from BOT_TOKEN so the project still works without an
# additional mandatory variable. Plain mirror tokens are never written to DB.
MIRROR_ENCRYPTION_KEY_RAW = os.environ.get("MIRROR_ENCRYPTION_KEY", "").strip()
if MIRROR_ENCRYPTION_KEY_RAW:
    try:
        MIRROR_CIPHER = Fernet(MIRROR_ENCRYPTION_KEY_RAW.encode())
    except Exception as exc:
        raise SystemExit(
            "MIRROR_ENCRYPTION_KEY must be a valid Fernet key (use Fernet.generate_key())"
        ) from exc
else:
    _mirror_key_digest = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    MIRROR_CIPHER = Fernet(base64.urlsafe_b64encode(_mirror_key_digest))

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
# SECURITY / CAPTCHA / MIRRORS
# =========================================================

CAPTCHA_TTL_SECONDS = max(300, int(os.environ.get("CAPTCHA_TTL_SECONDS", "86400")))
CAPTCHA_MAX_ATTEMPTS = max(1, int(os.environ.get("CAPTCHA_MAX_ATTEMPTS", "3")))
RATE_LIMIT_WINDOW_SECONDS = max(5, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "20")))
RATE_LIMIT_MAX_UPDATES = max(3, int(os.environ.get("RATE_LIMIT_MAX_UPDATES", "18")))
MIRROR_CHECK_INTERVAL = max(60, int(os.environ.get("MIRROR_CHECK_INTERVAL", "300")))

ACTIVE_ADMIN_IDS: set[int] = set(ADMIN_IDS)
USER_RATE_BUCKETS: dict[int, list[float]] = {}
CURRENT_ADMIN_ID_CONTEXT: dict[str, int] = {}

MAINTENANCE_TEXT = (
    "🛠 ТЕХНИЧЕСКИЕ РАБОТЫ\n\n"
    "Бот временно находится на технических работах.\n\n"
    "Мы уже занимаемся восстановлением доступа. "
    "Попробуйте немного позже.\n\n"
    "🌐 Ниже доступны резервные адреса проекта."
)


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

GREETING = (
    "🤖 DARKCOLLECT\n\n"
    "Добро пожаловать!\n"
    "Выберите нужный раздел ниже 👇"
)

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
# SECURITY / ADMIN / MAINTENANCE HELPERS
# =========================================================

def load_admin_ids_from_db() -> None:
    global ACTIVE_ADMIN_IDS
    ids = set(ADMIN_IDS)
    try:
        with db() as conn:
            rows = conn.execute("SELECT user_id FROM bot_admins").fetchall()
        ids.update(int(row["user_id"]) for row in rows)
    except Exception:
        logger.exception("Could not load dynamic admin IDs")
    ACTIVE_ADMIN_IDS = ids


def is_admin(user_id: int) -> bool:
    return user_id in ACTIVE_ADMIN_IDS


def is_root_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def meta_get(key: str, default: str = "") -> str:
    try:
        with db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
        return str(row["value"]) if row else default
    except Exception:
        logger.exception("meta_get failed key=%s", key)
        return default


def meta_set(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO meta(key,value)
            VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
            """,
            (key, value),
        )
        conn.commit()


def maintenance_enabled() -> bool:
    return meta_get("maintenance_enabled", "0") == "1"


def set_maintenance(enabled: bool, admin_id: int | None = None) -> None:
    meta_set("maintenance_enabled", "1" if enabled else "0")
    log_event("INFO", "maintenance_changed", admin_id, {"enabled": enabled})


def get_user_restriction(user_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT banned, banned_until, ban_reason
            FROM profiles
            WHERE user_id=%s
            """,
            (user_id,),
        ).fetchone()


def user_is_banned(user_id: int) -> bool:
    row = get_user_restriction(user_id)
    if not row or not row["banned"]:
        return False

    until = aware(row["banned_until"])
    if until is not None and until <= utcnow():
        unban_user(user_id, 0)
        return False

    return True


def ban_user(user_id: int, admin_id: int, days: int = 0, reason: str = "") -> None:
    banned_until = utcnow() + timedelta(days=days) if days > 0 else None
    with db() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET banned=TRUE,
                banned_until=%s,
                ban_reason=%s
            WHERE user_id=%s
            """,
            (banned_until, reason[:500], user_id),
        )
        conn.commit()
    log_event(
        "INFO",
        "user_banned",
        admin_id or None,
        {"target_user_id": user_id, "days": days, "reason": reason[:500]},
    )


def unban_user(user_id: int, admin_id: int) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE profiles
            SET banned=FALSE,
                banned_until=NULL,
                ban_reason=NULL
            WHERE user_id=%s
            """,
            (user_id,),
        )
        conn.commit()
    if admin_id:
        log_event(
            "INFO",
            "user_unbanned",
            admin_id,
            {"target_user_id": user_id},
        )


def revoke_subscription(user_id: int, admin_id: int | None = None) -> int:
    with db() as conn:
        result = conn.execute(
            """
            UPDATE subscriptions
            SET expires_at=NOW()
            WHERE user_id=%s
              AND expires_at > NOW()
            """,
            (user_id,),
        )
        conn.commit()
    if admin_id:
        log_event(
            "INFO",
            "subscription_revoked",
            admin_id,
            {"target_user_id": user_id, "rows": result.rowcount},
        )
    return result.rowcount


def grant_admin_subscription(user_id: int, plan: str, admin_id: int):
    ensure_profile(user_id)
    payment_key = (
        f"admin:{admin_id}:{user_id}:{plan}:{int(utcnow().timestamp())}"
    )
    expires, _created = activate_subscription(
        user_id,
        plan,
        "admin",
        payment_key,
        consume_referral_bonus=False,
    )
    log_event(
        "INFO",
        "subscription_granted_by_admin",
        admin_id,
        {"target_user_id": user_id, "plan": plan, "expires_at": expires.isoformat()},
    )
    return expires


def get_user_removal_credits(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT removal_credits FROM profiles WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    return int(row["removal_credits"] or 0) if row else 0


def add_removal_credit_from_captcha(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT removal_credits FROM profiles WHERE user_id=%s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return False
        if int(row["removal_credits"] or 0) > 0:
            conn.commit()
            return False
        conn.execute(
            "UPDATE profiles SET removal_credits=1 WHERE user_id=%s",
            (user_id,),
        )
        conn.commit()
    log_event("INFO", "captcha_reward_granted", user_id, {"removal_credits": 1})
    return True


def consume_removal_credit(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT removal_credits FROM profiles WHERE user_id=%s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if not row or int(row["removal_credits"] or 0) <= 0:
            conn.commit()
            return False
        conn.execute(
            """
            UPDATE profiles
            SET removal_credits=removal_credits-1,
                captcha_verified_at=NULL
            WHERE user_id=%s
            """,
            (user_id,),
        )
        conn.commit()
    return True


def captcha_is_valid(user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT captcha_verified_at FROM profiles WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    if not row or not row["captcha_verified_at"]:
        return False
    verified_at = aware(row["captcha_verified_at"])
    return bool(
        verified_at
        and verified_at + timedelta(seconds=CAPTCHA_TTL_SECONDS) > utcnow()
    )


def set_captcha_verified(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET captcha_verified_at=%s WHERE user_id=%s",
            (utcnow(), user_id),
        )
        conn.commit()


def invalidate_captcha(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE profiles SET captcha_verified_at=NULL WHERE user_id=%s",
            (user_id,),
        )
        conn.commit()


def user_rate_limited(user_id: int) -> bool:
    now = utcnow().timestamp()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    bucket = USER_RATE_BUCKETS.setdefault(user_id, [])
    bucket[:] = [stamp for stamp in bucket if stamp >= cutoff]
    if len(bucket) >= RATE_LIMIT_MAX_UPDATES:
        return True
    bucket.append(now)
    return False


def get_bot_admins():
    with db() as conn:
        return conn.execute(
            """
            SELECT user_id, added_by, created_at
            FROM bot_admins
            ORDER BY created_at ASC
            """
        ).fetchall()


def add_bot_admin(user_id: int, added_by: int) -> bool:
    ensure_profile(user_id)
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO bot_admins(user_id, added_by, created_at)
            VALUES(%s,%s,%s)
            ON CONFLICT(user_id) DO NOTHING
            RETURNING user_id
            """,
            (user_id, added_by, utcnow()),
        ).fetchone()
        conn.commit()
    load_admin_ids_from_db()
    if row:
        log_event("INFO", "admin_added", added_by, {"target_admin_id": user_id})
        return True
    return False


def remove_bot_admin(user_id: int, removed_by: int) -> bool:
    if is_root_admin(user_id):
        return False
    with db() as conn:
        result = conn.execute(
            "DELETE FROM bot_admins WHERE user_id=%s",
            (user_id,),
        )
        conn.commit()
    load_admin_ids_from_db()
    if result.rowcount:
        log_event("INFO", "admin_removed", removed_by, {"target_admin_id": user_id})
        return True
    return False


def get_active_mirrors():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bot_mirrors WHERE active=TRUE ORDER BY id ASC"
        ).fetchall()


def get_all_mirrors():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bot_mirrors ORDER BY id ASC"
        ).fetchall()


def get_mirror(mirror_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bot_mirrors WHERE id=%s",
            (mirror_id,),
        ).fetchone()


def mirror_token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def encrypt_mirror_token(token: str) -> str:
    return MIRROR_CIPHER.encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_mirror_token(token_enc: str) -> str:
    try:
        return MIRROR_CIPHER.decrypt(token_enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Не удалось расшифровать токен зеркала.") from exc


def add_mirror(
    name: str,
    url: str,
    owner_id: int | None = None,
) -> bool:
    """Legacy URL mirror support for already configured admin mirrors."""
    name = (name or "").strip()[:100]
    url = (url or "").strip().rstrip("/")

    if not name or not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("Название и URL должны быть заполнены, URL — http(s).")

    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO bot_mirrors(name,url,active,created_at)
            VALUES(%s,%s,TRUE,%s)
            ON CONFLICT(url)
            DO UPDATE SET name=EXCLUDED.name, active=TRUE
            RETURNING id
            """,
            (name, url, utcnow()),
        ).fetchone()
        conn.commit()
    return bool(row)


async def add_mirror_bot(
    token: str,
    owner_id: int,
    name: str | None = None,
) -> tuple[int, str, str]:
    """Validate a BotFather token, encrypt it, store it, and return mirror info."""
    token = (token or "").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Неверный формат токена BotFather.")
    if token == BOT_TOKEN:
        raise ValueError("Нельзя добавить основной BOT_TOKEN как зеркало.")

    validator = Bot(token=token)
    try:
        await validator.initialize()
        me = await validator.get_me()
    except TelegramError as exc:
        raise ValueError("Telegram не принял токен. Проверь токен от @BotFather.") from exc
    finally:
        try:
            await validator.shutdown()
        except Exception:
            pass

    if not me.username:
        raise ValueError("У зеркала нет username, Telegram-бот должен иметь @username.")

    mirror_name = (name or me.first_name or me.username).strip()[:100]
    username = me.username
    url = f"https://t.me/{username}"
    fingerprint = mirror_token_fingerprint(token)
    encrypted = encrypt_mirror_token(token)

    try:
        with db() as conn:
            # The token fingerprint index is a partial unique index, so
            # PostgreSQL cannot use ON CONFLICT(token_fingerprint) inference
            # without the matching predicate. Use an explicit lookup/update
            # path instead; this fixes mirror creation on existing databases.
            existing = conn.execute(
                "SELECT id FROM bot_mirrors WHERE token_fingerprint=%s LIMIT 1",
                (fingerprint,),
            ).fetchone()

            if existing:
                row = conn.execute(
                    """
                    UPDATE bot_mirrors
                    SET name=%s,
                        url=%s,
                        owner_id=%s,
                        active=TRUE,
                        bot_token_enc=%s,
                        bot_username=%s,
                        last_status=200,
                        last_checked_at=%s
                    WHERE id=%s
                    RETURNING id
                    """,
                    (
                        mirror_name,
                        url,
                        owner_id,
                        encrypted,
                        username,
                        utcnow(),
                        existing["id"],
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    INSERT INTO bot_mirrors(
                        name,
                        url,
                        owner_id,
                        active,
                        bot_token_enc,
                        bot_username,
                        token_fingerprint,
                        last_status,
                        last_checked_at,
                        created_at
                    )
                    VALUES(%s,%s,%s,TRUE,%s,%s,%s,200,%s,%s)
                    RETURNING id
                    """,
                    (
                        mirror_name,
                        url,
                        owner_id,
                        encrypted,
                        username,
                        fingerprint,
                        utcnow(),
                        utcnow(),
                    ),
                ).fetchone()

            conn.commit()
    except psycopg_errors.UniqueViolation as exc:
        raise ValueError(
            "Этот username уже занят другим зеркалом. Удали старое зеркало перед повторным добавлением."
        ) from exc

    except Exception as exc:
        logger.exception("Mirror database save failed: username=%s", username)
        raise ValueError(f"Не удалось сохранить зеркало в базе: {exc}") from exc

    if not row:
        raise ValueError("Не удалось сохранить зеркало.")

    log_event(
        "INFO",
        "mirror_bot_added",
        owner_id,
        {"mirror_id": int(row["id"]), "bot_username": username},
    )
    return int(row["id"]), mirror_name, username


def deactivate_mirror(mirror_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE bot_mirrors SET active=FALSE WHERE id=%s",
            (mirror_id,),
        )
        conn.commit()
    log_event("INFO", "mirror_deactivated", None, {"mirror_id": mirror_id})


def format_mirror_status(row) -> str:
    if row.get("bot_token_enc"):
        return "🟢 Подключено" if row.get("active") else "🔴 Отключено"
    if not row.get("last_checked_at"):
        return "⚪ Ещё не проверялось"
    status = row.get("last_status")
    latency = row.get("last_latency_ms")
    if status is not None and 100 <= int(status) < 500:
        icon = "🟢"
    else:
        icon = "🔴"
    latency_text = f"{float(latency):.0f} ms" if latency is not None else "—"
    return f"{icon} HTTP {status or '—'} · {latency_text}"


async def check_mirror(url: str):
    started = utcnow()
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(url)
        latency = (utcnow() - started).total_seconds() * 1000
        return response.status_code, latency
    except Exception:
        latency = (utcnow() - started).total_seconds() * 1000
        return None, latency


async def check_all_mirrors() -> None:
    rows = get_all_mirrors()
    for row in rows:
        # BotFather mirrors are checked by their running worker, not via HTTP.
        if row.get("bot_token_enc"):
            continue

        status, latency = await check_mirror(row["url"])
        with db() as conn:
            conn.execute(
                """
                UPDATE bot_mirrors
                SET last_status=%s,
                    last_latency_ms=%s,
                    last_checked_at=%s
                WHERE id=%s
                """,
                (status, latency, utcnow(), row["id"]),
            )
            conn.commit()


MIRROR_TASKS: dict[int, asyncio.Task] = {}
MIRROR_APPS: dict[int, Application] = {}


def build_bot_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    register_handlers(app)
    return app


def schedule_mirror(application: Application, mirror_id: int) -> None:
    if mirror_id in MIRROR_TASKS and not MIRROR_TASKS[mirror_id].done():
        return
    task = asyncio.create_task(
        mirror_bot_runner(mirror_id),
        name=f"mirror-bot-{mirror_id}",
    )
    MIRROR_TASKS[mirror_id] = task


async def mirror_bot_runner(mirror_id: int):
    app = None
    try:
        row = get_mirror(mirror_id)
        if not row or not row.get("active") or not row.get("bot_token_enc"):
            return

        token = decrypt_mirror_token(row["bot_token_enc"])
        app = build_bot_application(token)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        MIRROR_APPS[mirror_id] = app

        with db() as conn:
            conn.execute(
                "UPDATE bot_mirrors SET last_status=200,last_checked_at=%s WHERE id=%s",
                (utcnow(), mirror_id),
            )
            conn.commit()

        logger.info("Mirror bot started: mirror_id=%s username=%s", mirror_id, row.get("bot_username"))

        while True:
            await asyncio.sleep(15)
            current = get_mirror(mirror_id)
            if not current or not current.get("active"):
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Mirror bot failed: mirror_id=%s", mirror_id)
        log_event("ERROR", "mirror_bot_failed", None, {"mirror_id": mirror_id, "error": str(exc)[:500]})
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE bot_mirrors SET last_status=NULL,last_checked_at=%s WHERE id=%s",
                    (utcnow(), mirror_id),
                )
                conn.commit()
        except Exception:
            pass
    finally:
        MIRROR_APPS.pop(mirror_id, None)
        if app is not None:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
            except Exception:
                pass
            try:
                if app.running:
                    await app.stop()
            except Exception:
                pass
            try:
                await app.shutdown()
            except Exception:
                pass


def register_handlers(app: Application):
    app.add_handler(TypeHandler(Update, channel_gate), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CallbackQueryHandler(captcha_callback, pattern=r"^captcha:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^(nav:|menu:|hug:)"))
    app.add_handler(CallbackQueryHandler(moderation_callback, pattern=r"^mod:"))
    app.add_handler(CallbackQueryHandler(subscription_callback, pattern=r"^(sub:|pay:|manual_crypto:|check_crypto:)"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_moderation_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


def captcha_markup(context: ContextTypes.DEFAULT_TYPE):
    captcha = context.user_data.get("captcha") or {}
    options = captcha.get("options") or []
    rows = []
    pair = []
    for value in options:
        pair.append(
            InlineKeyboardButton(
                str(value),
                callback_data=f"captcha:answer:{value}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton(
            "🔄 Новый пример",
            callback_data="captcha:refresh",
        )
    ])
    return InlineKeyboardMarkup(rows)


def create_captcha(context: ContextTypes.DEFAULT_TYPE):
    a = random.randint(2, 9)
    b = random.randint(1, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(max(0, answer + random.randint(-5, 5)))
    values = list(options)
    random.shuffle(values)
    context.user_data["captcha"] = {
        "answer": answer,
        "attempts": 0,
        "created_at": utcnow().timestamp(),
        "options": values,
    }
    return a, b


async def show_captcha(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    alert_text: str | None = None,
):
    a, b = create_captcha(context)
    text = (
        "🛡️ ПРОВЕРКА БЕЗОПАСНОСТИ\n\n"
        "Подтвердите, что вы человек.\n"
        "Решите пример:\n\n"
        f"🧩 {a} + {b} = ?\n\n"
        "После успешной проверки будет доступна одна попытка Cn1сtи."
    )
    if alert_text:
        text = f"{alert_text}\n\n{text}"
    await send_ui(
        update,
        context,
        text,
        captcha_markup(context),
    )



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
                referral_bonus_days INTEGER NOT NULL DEFAULT 0,
                captcha_verified_at TIMESTAMPTZ,
                removal_credits INTEGER NOT NULL DEFAULT 0,
                banned BOOLEAN NOT NULL DEFAULT FALSE,
                banned_until TIMESTAMPTZ,
                ban_reason TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hugs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'user',
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id BIGINT PRIMARY KEY,
                added_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_mirrors (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                owner_id BIGINT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                last_status INTEGER,
                last_latency_ms NUMERIC(18,3),
                last_checked_at TIMESTAMPTZ,
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
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS captcha_verified_at TIMESTAMPTZ",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS removal_credits INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS banned_until TIMESTAMPTZ",
            "ALTER TABLE hugs ADD COLUMN IF NOT EXISTS target_type TEXT NOT NULL DEFAULT 'user'",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS owner_id BIGINT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS bot_token_enc TEXT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS bot_username TEXT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS token_fingerprint TEXT",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ban_reason TEXT",
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profiles_banned "
            "ON profiles(banned, banned_until)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bot_mirrors_active "
            "ON bot_mirrors(active)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_mirrors_token_fingerprint "
            "ON bot_mirrors(token_fingerprint) WHERE token_fingerprint IS NOT NULL"
        )

        for root_admin_id in ADMIN_IDS:
            conn.execute(
                """
                INSERT INTO bot_admins(user_id, added_by, created_at)
                VALUES(%s,%s,%s)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (root_admin_id, root_admin_id, utcnow()),
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

    load_admin_ids_from_db()
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

        if telegram_user is not None:
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
    target_type: str = "user",
):
    now = utcnow()
    target_type = target_type if target_type in HUG_TARGET_TYPES else "user"

    with db() as conn:
        conn.execute(
            """
            INSERT INTO hugs(
                user_id,
                target,
                target_type,
                count,
                created_at
            )
            VALUES(%s,%s,%s,%s,%s)
            """,
            (
                user_id,
                target,
                target_type,
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
            "target_type": target_type,
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
            InlineKeyboardButton("👤 Кабинет", callback_data="nav:profile"),
            InlineKeyboardButton("📋 Функции", callback_data="nav:menu"),
        ],
        [
            InlineKeyboardButton("👥 Рефералы", callback_data="nav:referrals"),
            InlineKeyboardButton("💬 Поддержка", callback_data="nav:support"),
        ],
        [
            InlineKeyboardButton("🌐 Зеркала", callback_data="nav:mirrors"),
        ],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([
            InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:panel")
        ])
    return InlineKeyboardMarkup(rows)


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
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💥 Снести аккаунт", callback_data="menu:hug"),
        ],
        [
            InlineKeyboardButton("👀 Поиск", callback_data="menu:search"),
            InlineKeyboardButton("🔎 Проверка", callback_data="menu:check"),
        ],
        [
            InlineKeyboardButton("🛡 Модерация", callback_data="menu:moderation"),
            InlineKeyboardButton("📜 История", callback_data="menu:history"),
        ],
        [
            InlineKeyboardButton("🌐 Зеркала", callback_data="nav:mirrors"),
        ],
        [
            InlineKeyboardButton("🏠 На главную", callback_data="nav:home"),
        ],
    ])


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


def kb_mirrors(user_id: int | None = None):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Добавить своё зеркало",
                callback_data="nav:mirror_add",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Обновить",
                callback_data="nav:mirrors",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 На главную",
                callback_data="nav:home",
            )
        ],
    ])


def kb_admin_access():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💎 Выдать подписку", callback_data="admin:grant_sub"),
            InlineKeyboardButton("❌ Снять подписку", callback_data="admin:revoke_sub"),
        ],
        [
            InlineKeyboardButton("🔨 Забанить", callback_data="admin:ban"),
            InlineKeyboardButton("✅ Разбанить", callback_data="admin:unban"),
        ],
        [InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:panel")],
    ])


def kb_admin_admins():
    rows = []
    current_id = CURRENT_ADMIN_ID_CONTEXT.get("user_id", 0)
    if is_root_admin(current_id):
        rows.append([InlineKeyboardButton("➕ Добавить админа", callback_data="admin:add_admin")])
        rows.append([InlineKeyboardButton("➖ Удалить админа", callback_data="admin:remove_admin")])
    rows.append([InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


def kb_admin_mirrors():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Добавить зеркало", callback_data="admin:mirror_add"),
            InlineKeyboardButton("🔎 Проверить", callback_data="admin:mirror_check"),
        ],
        [InlineKeyboardButton("❌ Отключить зеркало", callback_data="admin:mirror_off")],
        [InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:panel")],
    ])


def kb_maintenance(enabled: bool):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 Выключить техработы" if enabled else "🔴 Включить техработы",
                callback_data="admin:maintenance_off" if enabled else "admin:maintenance_on",
            )
        ],
        [InlineKeyboardButton("⬅️ Админ-панель", callback_data="admin:panel")],
    ])


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


HUG_TARGET_TYPES = {
    "user": "👤 аккаунта",
    "group": "👥 группы",
    "channel": "📢 канала",
    "bot": "🤖 бота",
}


def kb_hug_target_type():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Аккаунт", callback_data="hug:type:user"),
            InlineKeyboardButton("👥 Группа", callback_data="hug:type:group"),
        ],
        [
            InlineKeyboardButton("📢 Канал", callback_data="hug:type:channel"),
            InlineKeyboardButton("🤖 Бот", callback_data="hug:type:bot"),
        ],
        [InlineKeyboardButton("🏠 На главную", callback_data="nav:home")],
    ])


def kb_confirm_hug():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💥 Cn1сtи аккаунт!",
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
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton("💳 Покупки", callback_data="admin:payments"),
            InlineKeyboardButton("🔗 Рефералы", callback_data="admin:referrals"),
        ],
        [
            InlineKeyboardButton("🎟 Промокоды", callback_data="admin:promos"),
            InlineKeyboardButton("📢 Рассылка", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton("👑 Доступ", callback_data="admin:access"),
            InlineKeyboardButton("👮 Админы", callback_data="admin:admins"),
        ],
        [
            InlineKeyboardButton("🌐 Зеркала", callback_data="admin:mirrors"),
            InlineKeyboardButton("🛠 Техработы", callback_data="admin:maintenance"),
        ],
        [
            InlineKeyboardButton("💾 Backup", callback_data="admin:backup"),
            InlineKeyboardButton("❤️ Health", callback_data="admin:health"),
        ],
        [
            InlineKeyboardButton("📈 По дням", callback_data="admin:daily_stats"),
            InlineKeyboardButton("🧾 Логи", callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton("🔄 Обновить", callback_data="admin:panel"),
            InlineKeyboardButton("🏠 Главная", callback_data="nav:home"),
        ],
    ])


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
# MIRRORS / MAINTENANCE / CAPTCHA UI
# =========================================================

async def show_mirrors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_mirrors()
    primary = WEBHOOK_BASE or "https://hugcollect4.onrender.com"
    lines = [
        "🌐 ДОСТУП К ПРОЕКТУ",
        "",
        "🟢 Основной адрес:",
        primary,
        "",
    ]

    active_found = False
    for row in rows:
        if not row["active"]:
            continue
        active_found = True
        status_text = (
            "🔵 Добавлено пользователем"
            if row.get("owner_id")
            else format_mirror_status(row)
        )
        lines.extend([
            status_text,
            f"• {row['name']}",
            row["url"],
            "",
        ])

    if not active_found:
        lines.append("Резервные адреса пока не добавлены.")

    lines.extend([
        "",
        "ℹ️ Можно подключить собственное зеркало через токен @BotFather.",
        "🔐 Токен шифруется перед сохранением.",
    ])

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_mirrors(update.effective_user.id),
    )


async def show_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_active_mirrors()
    text = MAINTENANCE_TEXT
    if rows:
        text += "\n\n🌐 Резервные адреса:\n"
        for row in rows[:8]:
            text += f"• {row['name']} — {row['url']}\n"
    await send_ui(update, context, text, None)


async def show_admin_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_ui(
        update,
        context,
        (
            "👑 УПРАВЛЕНИЕ ДОСТУПОМ\n\n"
            "💎 Выдать подписку\n"
            "❌ Снять подписку\n"
            "🔨 Заблокировать пользователя\n"
            "✅ Разблокировать пользователя"
        ),
        kb_admin_access(),
    )


async def show_admin_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CURRENT_ADMIN_ID_CONTEXT["user_id"] = update.effective_user.id
    rows = get_bot_admins()
    lines = ["👮 АДМИНИСТРАТОРЫ\n"]
    if not rows:
        lines.append("Администраторов пока нет.")
    else:
        for row in rows:
            role = "👑 ROOT" if is_root_admin(int(row["user_id"])) else "🛡 ADMIN"
            lines.append(
                f"{role} — {row['user_id']}\n"
                f"Добавлен: {aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
            )
    await send_ui(update, context, "\n\n".join(lines), kb_admin_admins())


async def show_admin_mirrors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_mirrors()
    lines = ["🌐 ЗЕРКАЛА\n"]
    if not rows:
        lines.append("Зеркала ещё не добавлены.")
    else:
        for row in rows:
            active = "🟢" if row["active"] else "⚪"
            lines.append(
                f"{active} #{row['id']} — {row['name']}\n"
                f"{row['url']}\n"
                f"{format_mirror_status(row)}"
            )
    await send_ui(update, context, "\n\n".join(lines), kb_admin_mirrors())


async def show_admin_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    enabled = maintenance_enabled()
    await send_ui(
        update,
        context,
        (
            "🛠 ТЕХНИЧЕСКИЕ РАБОТЫ\n\n"
            f"Статус: {'🔴 ВКЛЮЧЕНЫ' if enabled else '🟢 ВЫКЛЮЧЕНЫ'}\n\n"
            "В режиме техработ обычные пользователи не получают доступ "
            "к функциям бота."
        ),
        kb_maintenance(enabled),
    )


async def captcha_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data or ""

    try:
        await q.answer()
    except TelegramError:
        pass

    if data == "captcha:refresh":
        await show_captcha(update, context)
        return

    captcha = context.user_data.get("captcha")
    if not captcha or not data.startswith("captcha:answer:"):
        await show_captcha(update, context)
        return

    try:
        answer = int(data.rsplit(":", 1)[1])
    except ValueError:
        await show_captcha(update, context, "❌ Некорректный ответ.")
        return

    age = utcnow().timestamp() - float(captcha.get("created_at", 0))
    if age > CAPTCHA_TTL_SECONDS:
        context.user_data.pop("captcha", None)
        await show_captcha(update, context, "⌛ Срок действия капчи истёк.")
        return

    correct = int(captcha.get("answer", -1))
    if answer != correct:
        captcha["attempts"] = int(captcha.get("attempts", 0)) + 1
        if captcha["attempts"] >= CAPTCHA_MAX_ATTEMPTS:
            context.user_data.pop("captcha", None)
            await show_captcha(update, context, "❌ Слишком много ошибок. Создана новая капча.")
            return
        left = CAPTCHA_MAX_ATTEMPTS - captcha["attempts"]
        await show_captcha(
            update,
            context,
            f"❌ Неверно. Осталось попыток: {left}.",
        )
        return

    context.user_data.pop("captcha", None)
    set_captcha_verified(user_id)
    granted = add_removal_credit_from_captcha(user_id)

    await send_ui(
        update,
        context,
        (
            "✅ ПРОВЕРКА ПРОЙДЕНА\n\n"
            + (
                "🎁 Вам начислена 1 попытка Cn1сtи."
                if granted
                else "ℹ️ У вас уже есть неиспользованная попытка."
            )
            + "\n\nТеперь можно пользоваться меню."
        ),
        kb_home(user_id),
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

    if not user or user.is_bot:
        return

    # Commands should reach their dedicated CommandHandlers.
    if update.message and update.message.text and update.message.text.startswith("/"):
        return

    q = update.callback_query

    if q and (q.data or "").startswith("captcha:"):
        return

    # Payment updates must not be blocked by captcha/maintenance/channel gates.
    if update.pre_checkout_query:
        return
    if update.message and update.message.successful_payment:
        return

    ensure_profile(user.id, user)

    if is_admin(user.id):
        return

    if user_is_banned(user.id):
        row = get_user_restriction(user.id)
        until = aware(row["banned_until"]) if row else None
        until_text = until.strftime("%d.%m.%Y %H:%M") if until else "навсегда"
        await send_ui(
            update,
            context,
            (
                "⛔ ДОСТУП ОГРАНИЧЕН\n\n"
                f"Срок: {until_text}\n"
                f"Причина: {(row['ban_reason'] if row else '') or 'не указана'}"
            ),
            None,
        )
        raise ApplicationHandlerStop

    if maintenance_enabled():
        await show_maintenance(update, context)
        raise ApplicationHandlerStop

    if user_rate_limited(user.id):
        invalidate_captcha(user.id)
        await show_captcha(
            update,
            context,
            "⚠️ Слишком много действий. Пройдите проверку снова.",
        )
        raise ApplicationHandlerStop

    if not captcha_is_valid(user.id):
        await show_captcha(update, context)
        raise ApplicationHandlerStop

    if not REQUIRED_CHANNEL_ID:
        return

    if q and q.data == "gate:check":
        subscribed = await is_required_channel_member(
            context.bot,
            user.id,
        )
        if subscribed:
            try:
                await q.answer("Подписка найдена ✅")
            except TelegramError:
                pass
            await show_home(update, context)
        else:
            try:
                await q.answer("Вы ещё не подписались.", show_alert=True)
            except TelegramError:
                pass
            await show_channel_gate(update, context)
        raise ApplicationHandlerStop

    if await is_required_channel_member(context.bot, user.id):
        return

    if q:
        try:
            await q.answer(
                "Сначала подпишитесь на канал.",
                show_alert=True,
            )
        except TelegramError:
            pass

    await show_channel_gate(update, context)
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
        f"{DAILY_REQUEST_LIMIT}\n"
        f"💥 Попыток сноса доступно — {get_user_removal_credits(user_id)}"
    )


# =========================================================
# HUG PROCESS
# =========================================================

async def run_hug_animation(
    bot,
    chat_id: int,
    message_id: int,
    user_id: int,
    target: str,
    target_type: str = "user",
):
    try:
        await asyncio.sleep(1)
        for percent in [12, 25, 41, 58, 73, 89]:
            await bot.send_message(
                chat_id=chat_id,
                text=f"💤{percent}%💤",
            )
            await asyncio.sleep(1)

        await bot.send_message(
            chat_id=chat_id,
            text="💤100%💤",
        )
        await asyncio.sleep(0.5)

        add_hug(user_id, target, 1, target_type=target_type)
        log_event(
            "INFO",
            "processing_attempt",
            user_id,
            {"target": target, "target_type": target_type},
        )

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ Попытка сноса {type_label} завершена."
            ),
            reply_markup=kb_hooray(),
        )
    except Exception:
        logger.exception("Hug animation failed")
        log_event(
            "ERROR",
            "hug_animation_failed",
            user_id,
            {"target": target, "target_type": target_type},
        )


async def start_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    if not await require_subscription(update, context, user_id):
        return

    if not captcha_is_valid(user_id):
        await show_captcha(update, context)
        return

    if get_user_removal_credits(user_id) <= 0:
        await send_ui(
            update,
            context,
            (
                "💥 CN1СTИ АККАУНТ\n\n"
                "Доступных попыток нет.\n\n"
                "Пройдите капчу, чтобы получить одну попытку сноса."
            ),
            kb_home(user_id),
        )
        return

    context.user_data["state"] = "awaiting_hug_type"
    context.user_data.pop("hug_target_type", None)

    await send_ui(
        update,
        context,
        (
            "💥 CN1СTИ АККАУНТ\n\n"
            "Выберите тип объекта:\n\n"
            "👤 Аккаунт\n"
            "👥 Группа\n"
            "📢 Канал\n"
            "🤖 Бот\n"
        ),
        kb_hug_target_type(),
    )


async def confirm_and_send_hug(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
):
    if not await require_subscription(update, context, user_id):
        context.user_data["state"] = None
        return

    if not captcha_is_valid(user_id):
        context.user_data["state"] = None
        await show_captcha(update, context)
        return

    target = context.user_data.get("hug_target", "объект")
    target_type = context.user_data.get("hug_target_type", "user")

    if get_user_removal_credits(user_id) <= 0:
        context.user_data["state"] = None
        await send_ui(update, context, "❌ Доступная попытка сноса уже использована.", kb_home(user_id))
        return

    ok, _used = request_usage(user_id)
    if not ok:
        context.user_data["state"] = None
        await send_ui(
            update,
            context,
            f"⛔️ Лимит на сегодня исчерпан.\n\nДоступно {DAILY_REQUEST_LIMIT} запросов в день.",
            kb_menu(),
        )
        return

    if not consume_removal_credit(user_id):
        context.user_data["state"] = None
        await send_ui(update, context, "❌ Попытка Cn1сtи уже использована.", kb_home(user_id))
        return

    context.user_data["state"] = None
    context.user_data.pop("hug_target", None)
    context.user_data.pop("hug_target_type", None)

    initial_text = (
        "💤 Идёт обработка 💤\n\n"
        "3%\n\n"
        "🔰 Выполняется одна попытка 🔰"
    )

    chat_id = update.effective_chat.id
    q = update.callback_query

    if q and q.message:
        try:
            await q.message.edit_text(initial_text)
            msg_id = q.message.message_id
        except BadRequest:
            msg = await context.bot.send_message(chat_id=chat_id, text=initial_text)
            msg_id = msg.message_id
    elif update.message:
        msg = await update.message.reply_text(initial_text)
        msg_id = msg.message_id
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=initial_text)
        msg_id = msg.message_id

    context.application.create_task(
        run_hug_animation(
            context.bot,
            chat_id,
            msg_id,
            user_id,
            target,
            target_type,
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
            "📎 Можно добавить доказательства."
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
        banned_users = conn.execute(
            "SELECT COUNT(*) AS total FROM profiles WHERE banned=TRUE"
        ).fetchone()["total"]
        active_mirrors = conn.execute(
            "SELECT COUNT(*) AS total FROM bot_mirrors WHERE active=TRUE"
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
        f"• Заблокированных — {banned_users}\n"
        f"• Активных зеркал — {active_mirrors}\n"
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
        "bot_admins",
        "bot_mirrors",
    ]

    payload = {
        "format": 1,
        "generated_at": utcnow().isoformat(),
        "project": "DarkCollect",
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

    elif data == "nav:mirror_add":

        context.user_data["state"] = "user_mirror_add"
        await send_ui(
            update,
            context,
            (
                "➕ ДОБАВИТЬ СВОЁ ЗЕРКАЛО\n\n"
                "Создайте бота через @BotFather и отправьте сюда его токен.\n\n"
                "Пример:\n"
                "123456789:AAExampleToken...\n\n"
                "🔐 После проверки токен сохраняется в зашифрованном виде."
            ),
            kb_mirrors(user_id),
        )

    elif data == "nav:mirrors":

        await show_mirrors(
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

    elif data.startswith("hug:type:"):

        target_type = data.split(":", 2)[2]

        if target_type not in HUG_TARGET_TYPES:
            await send_ui(
                update,
                context,
                "❌ Неизвестный тип объекта.",
                kb_hug_target_type(),
            )
            return

        context.user_data["hug_target_type"] = target_type
        context.user_data["state"] = "awaiting_hug_target"

        label = HUG_TARGET_TYPES[target_type]

        await send_ui(
            update,
            context,
            (
                f"💥 CN1СTИ {label.upper()}\n\n"
                "Введите @username, публичную ссылку или другой "
                "идентификатор объекта.\n\n"
                ""
            ),
            kb_back_home(),
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
    CURRENT_ADMIN_ID_CONTEXT["user_id"] = user_id

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

    elif data == "admin:access":
        await show_admin_access(update, context)

    elif data == "admin:grant_sub":
        context.user_data["state"] = "admin_grant_sub"
        await send_ui(
            update,
            context,
            (
                "💎 ВЫДАТЬ ПОДПИСКУ\n\n"
                "Формат:\nUSER_ID PLAN\n\n"
                "Пример: 123456789 month\n\n"
                "Доступно: week / month / year"
            ),
            kb_admin_access(),
        )

    elif data == "admin:revoke_sub":
        context.user_data["state"] = "admin_revoke_sub"
        await send_ui(
            update,
            context,
            "❌ СНЯТЬ ПОДПИСКУ\n\nВведите Telegram ID пользователя:",
            kb_admin_access(),
        )

    elif data == "admin:ban":
        context.user_data["state"] = "admin_ban"
        await send_ui(
            update,
            context,
            (
                "🔨 ЗАБАНИТЬ ПОЛЬЗОВАТЕЛЯ\n\n"
                "Формат:\nUSER_ID DAYS REASON\n\n"
                "DAYS=0 — навсегда."
            ),
            kb_admin_access(),
        )

    elif data == "admin:unban":
        context.user_data["state"] = "admin_unban"
        await send_ui(
            update,
            context,
            "✅ РАЗБАНИТЬ\n\nВведите Telegram ID пользователя:",
            kb_admin_access(),
        )

    elif data == "admin:admins":
        await show_admin_admins(update, context)

    elif data == "admin:add_admin":
        if not is_root_admin(user_id):
            await q.answer("Только root-администратор может менять список админов.", show_alert=True)
            return
        context.user_data["state"] = "admin_add_admin"
        await send_ui(update, context, "➕ ДОБАВИТЬ АДМИНА\n\nВведите Telegram ID:", kb_admin_admins())

    elif data == "admin:remove_admin":
        if not is_root_admin(user_id):
            await q.answer("Только root-администратор может менять список админов.", show_alert=True)
            return
        context.user_data["state"] = "admin_remove_admin"
        await send_ui(update, context, "➖ УДАЛИТЬ АДМИНА\n\nВведите Telegram ID:", kb_admin_admins())

    elif data == "admin:mirrors":
        await show_admin_mirrors(update, context)

    elif data == "admin:mirror_add":
        context.user_data["state"] = "admin_mirror_add"
        await send_ui(
            update,
            context,
            "➕ ДОБАВИТЬ ЗЕРКАЛО\n\nФормат:\nНазвание | URL",
            kb_admin_mirrors(),
        )

    elif data == "admin:mirror_check":
        await send_ui(update, context, "🔎 Проверяю зеркала...", kb_admin_mirrors())
        await check_all_mirrors()
        await show_admin_mirrors(update, context)

    elif data == "admin:mirror_off":
        context.user_data["state"] = "admin_mirror_off"
        await send_ui(update, context, "❌ ОТКЛЮЧИТЬ ЗЕРКАЛО\n\nВведите ID зеркала:", kb_admin_mirrors())

    elif data == "admin:maintenance":
        await show_admin_maintenance(update, context)

    elif data == "admin:maintenance_on":
        set_maintenance(True, user_id)
        await show_admin_maintenance(update, context)

    elif data == "admin:maintenance_off":
        set_maintenance(False, user_id)
        await show_admin_maintenance(update, context)

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
    user_id = update.effective_user.id
    ensure_profile(user_id, update.effective_user)

    if context.args:
        start_param = context.args[0]
        if start_param.startswith("ref_"):
            try:
                referrer_id = int(start_param[4:])
            except ValueError:
                referrer_id = None
            if referrer_id and referrer_id != user_id:
                add_referral(referrer_id, user_id)

    subscription = get_active_subscription(user_id)
    logger.info(
        "START: user=%s active_subscription=%s",
        user_id,
        bool(subscription),
    )

    context.user_data["state"] = None
    context.user_data.pop("hug_target", None)

    if not is_admin(user_id):
        if user_is_banned(user_id):
            row = get_user_restriction(user_id)
            until = aware(row["banned_until"]) if row else None
            until_text = until.strftime("%d.%m.%Y %H:%M") if until else "навсегда"
            await update.message.reply_text(
                (
                    "⛔ ДОСТУП ОГРАНИЧЕН\n\n"
                    f"Срок: {until_text}\n"
                    f"Причина: {(row['ban_reason'] if row else '') or 'не указана'}"
                )
            )
            return

        if maintenance_enabled():
            await show_maintenance(update, context)
            return

        if not captcha_is_valid(user_id):
            await show_captcha(update, context)
            return

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
    # ADMIN SUBSCRIPTION / BAN / ADMINS / MIRRORS
    # ---------------------------------------------

    if is_admin(user_id) and state == "admin_grant_sub":
        context.user_data["state"] = None
        parts = text.split()
        if len(parts) != 2 or parts[1].lower() not in PLANS:
            await update.message.reply_text(
                "❌ Формат: USER_ID PLAN\nПример: 123456789 month",
                reply_markup=kb_admin_access(),
            )
            return
        try:
            target_id = int(parts[0])
        except ValueError:
            await update.message.reply_text("❌ USER_ID должен быть числом.", reply_markup=kb_admin_access())
            return
        ensure_profile(target_id)
        plan = parts[1].lower()
        expires = grant_admin_subscription(target_id, plan, user_id)
        try:
            await context.bot.send_message(
                target_id,
                (
                    "💎 Вам выдана подписка администратором.\n\n"
                    f"План: {PLANS[plan]['title']}\n"
                    f"До: {expires.strftime('%d.%m.%Y %H:%M')}"
                ),
            )
        except TelegramError:
            pass
        await update.message.reply_text(
            f"✅ Подписка выдана пользователю {target_id}.\nДо: {expires.strftime('%d.%m.%Y %H:%M')}",
            reply_markup=kb_admin_access(),
        )
        return

    if is_admin(user_id) and state == "admin_revoke_sub":
        context.user_data["state"] = None
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.", reply_markup=kb_admin_access())
            return
        count = revoke_subscription(target_id, user_id)
        try:
            await context.bot.send_message(target_id, "❌ Ваша подписка была отменена администратором.")
        except TelegramError:
            pass
        await update.message.reply_text(
            f"✅ Завершено активных подписок: {count}",
            reply_markup=kb_admin_access(),
        )
        return

    if is_admin(user_id) and state == "admin_ban":
        context.user_data["state"] = None
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await update.message.reply_text("❌ Формат: USER_ID DAYS REASON", reply_markup=kb_admin_access())
            return
        try:
            target_id = int(parts[0])
            days = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ USER_ID и DAYS должны быть числами.", reply_markup=kb_admin_access())
            return
        if target_id in ACTIVE_ADMIN_IDS:
            await update.message.reply_text("❌ Нельзя заблокировать администратора.", reply_markup=kb_admin_access())
            return
        reason = parts[2] if len(parts) == 3 else ""
        ensure_profile(target_id)
        ban_user(target_id, user_id, max(0, days), reason)
        revoke_subscription(target_id, user_id)
        try:
            await context.bot.send_message(target_id, "⛔ Доступ к боту ограничен администратором.")
        except TelegramError:
            pass
        until_text = "навсегда" if days <= 0 else f"на {days} дн."
        await update.message.reply_text(
            f"✅ Пользователь {target_id} заблокирован {until_text}.",
            reply_markup=kb_admin_access(),
        )
        return

    if is_admin(user_id) and state == "admin_unban":
        context.user_data["state"] = None
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.", reply_markup=kb_admin_access())
            return
        unban_user(target_id, user_id)
        try:
            await context.bot.send_message(target_id, "✅ Ограничение снято. Доступ восстановлен.")
        except TelegramError:
            pass
        await update.message.reply_text(f"✅ Пользователь {target_id} разблокирован.", reply_markup=kb_admin_access())
        return

    if is_admin(user_id) and state == "admin_add_admin":
        context.user_data["state"] = None
        if not is_root_admin(user_id):
            await update.message.reply_text("❌ Только root-администратор.", reply_markup=kb_admin())
            return
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.", reply_markup=kb_admin_admins())
            return
        added = add_bot_admin(target_id, user_id)
        await update.message.reply_text(
            "✅ Администратор добавлен." if added else "ℹ️ Пользователь уже является администратором.",
            reply_markup=kb_admin_admins(),
        )
        return

    if is_admin(user_id) and state == "admin_remove_admin":
        context.user_data["state"] = None
        if not is_root_admin(user_id):
            await update.message.reply_text("❌ Только root-администратор.", reply_markup=kb_admin())
            return
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.", reply_markup=kb_admin_admins())
            return
        if is_root_admin(target_id):
            await update.message.reply_text("❌ Root-администраторов из ENV удалить нельзя.", reply_markup=kb_admin_admins())
            return
        removed = remove_bot_admin(target_id, user_id)
        await update.message.reply_text(
            "✅ Администратор удалён." if removed else "❌ Администратор не найден.",
            reply_markup=kb_admin_admins(),
        )
        return

    if state == "user_mirror_add":
        context.user_data["state"] = None
        token = text.strip()

        # Token is sensitive: remove the user's message as soon as possible.
        try:
            await update.message.delete()
        except TelegramError:
            pass

        try:
            mirror_id, mirror_name, username = await add_mirror_bot(
                token,
                owner_id=user_id,
            )
            schedule_mirror(context.application, mirror_id)
        except ValueError as exc:
            await update.message.reply_text(
                f"❌ {exc}",
                reply_markup=kb_mirrors(user_id),
            )
            return

        await update.message.reply_text(
            f"✅ Зеркало подключено.\n\n🤖 @{username}\n🌐 https://t.me/{username}\n\n"
            "Бот запускается на сервере.",
            reply_markup=kb_mirrors(user_id),
        )
        return

    if is_admin(user_id) and state == "admin_mirror_add":
        context.user_data["state"] = None
        token = text.strip()
        try:
            await update.message.delete()
        except TelegramError:
            pass
        try:
            mirror_id, mirror_name, username = await add_mirror_bot(
                token,
                owner_id=user_id,
            )
            schedule_mirror(context.application, mirror_id)
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}", reply_markup=kb_admin_mirrors())
            return
        await update.message.reply_text(
            f"✅ Зеркало подключено: @{username}\nhttps://t.me/{username}",
            reply_markup=kb_admin_mirrors(),
        )
        return

    if is_admin(user_id) and state == "admin_mirror_off":
        context.user_data["state"] = None
        try:
            mirror_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.", reply_markup=kb_admin_mirrors())
            return
        deactivate_mirror(mirror_id)
        await update.message.reply_text(f"✅ Зеркало #{mirror_id} отключено.", reply_markup=kb_admin_mirrors())
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

        hug_target_type = context.user_data.get(
            "hug_target_type",
            "user",
        )
        hug_target_label = HUG_TARGET_TYPES.get(
            hug_target_type,
            "👤 аккаунта",
        )

        await update.message.reply_text(
            (
                f"Вы уверены, что хотите "
                f"выполнить обработку для "
                f"{hug_target_label} {text}?"
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
# MIRROR MONITOR
# =========================================================

async def mirror_monitor_loop(application: Application):
    while True:
        try:
            await check_all_mirrors()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Mirror monitor loop failed")
        await asyncio.sleep(MIRROR_CHECK_INTERVAL)


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):

    asyncio.create_task(
        expiration_loop(
            application
        ),
        name="expiration-loop",
    )

    asyncio.create_task(
        cleanup_loop(
            application
        ),
        name="cleanup-loop",
    )

    asyncio.create_task(
        auto_backup_loop(
            application
        ),
        name="auto-backup-loop",
    )

    asyncio.create_task(
        mirror_monitor_loop(
            application
        ),
        name="mirror-monitor-loop",
    )

    # Start all active BotFather-based mirrors after the main bot is ready.
    for row in get_active_mirrors():
        if row.get("bot_token_enc"):
            schedule_mirror(application, int(row["id"]))

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

    # Stop dynamically launched mirror bot applications first.
    mirror_tasks = list(MIRROR_TASKS.values())
    for task in mirror_tasks:
        if not task.done():
            task.cancel()
    if mirror_tasks:
        await asyncio.gather(*mirror_tasks, return_exceptions=True)
    MIRROR_TASKS.clear()
    MIRROR_APPS.clear()

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

    register_handlers(app)

    if WEBHOOK_BASE:

        webhook_path = BOT_TOKEN

        webhook_url = (
            f"{WEBHOOK_BASE.rstrip('/')}/{webhook_path}"
        )

        logger.info("Starting webhook")

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )

    else:

        logger.info("Starting polling")

        app.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()