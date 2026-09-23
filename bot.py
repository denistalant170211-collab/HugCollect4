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
from supabase import create_client
from telethon import TelegramClient, functions, types
from telethon.errors.rpcerrorlist import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import JoinChannelRequest
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

PRIMARY_BOT_URL = os.environ.get(
    "PRIMARY_BOT_URL",
    "https://t.me/darkcollecttbot",
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
# INTERNAL SESSION STORAGE
# =========================================================
# The app uses Supabase Storage only for persistent .session files.
# Actual reporting/sending actions are intentionally not implemented here.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
# Prefer the current server-side secret key name, but keep compatibility
# with the legacy service_role variable used by older Supabase projects.
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_SERVER_KEY = SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY
SUPABASE_SESSIONS_BUCKET = os.environ.get(
    "SUPABASE_SESSIONS_BUCKET",
    "internal-sessions",
).strip()

TELEGRAM_API_ID_RAW = os.environ.get("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID_RAW) if TELEGRAM_API_ID_RAW else 0
except ValueError:
    TELEGRAM_API_ID = 0

INTERNAL_SESSION_CACHE_DIR = Path(
    os.environ.get(
        "INTERNAL_SESSION_CACHE_DIR",
        "/tmp/darkcollect_sessions",
    ).strip()
)
INTERNAL_SESSION_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SUPABASE_CLIENT = None
if SUPABASE_URL and SUPABASE_SERVER_KEY:
    if not SUPABASE_URL.startswith(("https://", "http://")):
        logger.error(
            "Invalid SUPABASE_URL. Expected the Supabase Project URL like "
            "https://<project-ref>.supabase.co, NOT DATABASE_URL/postgresql://..."
        )
    else:
        try:
            SUPABASE_CLIENT = create_client(
                SUPABASE_URL,
                SUPABASE_SERVER_KEY,
            )
        except Exception:
            logger.exception("Could not initialize Supabase Storage client")
            SUPABASE_CLIENT = None


def internal_storage_ready() -> bool:
    return bool(
        SUPABASE_CLIENT
        and SUPABASE_SESSIONS_BUCKET
        and TELEGRAM_API_ID
        and TELEGRAM_API_HASH
    )


# =========================================================
# INTERNAL REPORT ENGINE (TelReper technology, fixed)
# =========================================================
# Реальный внутряк: жалобы уходят через userbot-сессии из
# Supabase Storage. Старый код слал messages.ReportRequest с
# пустым option и считал это отправкой — Telegram на такое
# отвечает ReportResultChooseOption (списком опций), а не
# принятием жалобы. Здесь двухшаговая схема:
#   1) account.ReportPeerRequest — репорт уровня канала/чата;
#   2) messages.ReportRequest: пустой option -> ChooseOption ->
#      повторный вызов с выбранной опцией.

REPORTS_PER_ACCOUNT = max(
    1,
    int(os.environ.get("REPORTS_PER_ACCOUNT", "2")),
)
INTERNAL_MAX_PARALLEL = max(
    1,
    min(10, int(os.environ.get("INTERNAL_MAX_PARALLEL", "5"))),
)

INTERNAL_REPORT_REASONS: dict[str, tuple[Any, str]] = {
    "spam": (
        types.InputReportReasonSpam,
        "Прошу проверить объект на признаки спама.",
    ),
    "fake": (
        types.InputReportReasonFake,
        "Прошу проверить объект на выдачу себя за другое лицо.",
    ),
    "violence": (
        types.InputReportReasonViolence,
        "Прошу проверить объект на материалы с пропагандой насилия.",
    ),
    "child_abuse": (
        types.InputReportReasonChildAbuse,
        "Прошу проверить объект на материалы о жестоком обращении с детьми.",
    ),
    "pornography": (
        types.InputReportReasonPornography,
        "Прошу проверить объект на порнографический контент.",
    ),
    "geo_irrelevant": (
        types.InputReportReasonGeoIrrelevant,
        "Прошу проверить объект на нерелевантное географическое содержание.",
    ),
    "copyright": (
        types.InputReportReasonCopyright,
        "Прошу проверить объект на нарушение авторских прав.",
    ),
    "illegal_drugs": (
        types.InputReportReasonIllegalDrugs,
        "Прошу проверить объект на материалы о незаконных наркотиках.",
    ),
    "personal_details": (
        types.InputReportReasonPersonalDetails,
        "Прошу проверить объект на раскрытие персональных данных.",
    ),
    "other": (
        types.InputReportReasonOther,
        "Прошу проверить объект по указанному основанию.",
    ),
}

INTERNAL_DEFAULT_REASON = (
    os.environ.get("INTERNAL_DEFAULT_REASON", "spam").strip().lower()
)
if INTERNAL_DEFAULT_REASON not in INTERNAL_REPORT_REASONS:
    INTERNAL_DEFAULT_REASON = "spam"

JOB_STATUS_RU = {
    "prepared": "🟡 Подготовлено",
    "running": "🔵 Выполняется",
    "done": "🟢 Выполнено",
    "failed": "🔴 Ошибка",
    "no_sessions": "⚪ Нет сессий",
}


def set_internal_job_status(
    job_id: int,
    status: str,
    error: str | None = None,
    sent_count: int | None = None,
) -> None:
    with db() as conn:
        if sent_count is None:
            conn.execute(
                """
                UPDATE internal_jobs
                SET status=%s,
                    error=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (status, error, utcnow(), job_id),
            )
        else:
            conn.execute(
                """
                UPDATE internal_jobs
                SET status=%s,
                    error=%s,
                    sent_count=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (status, error, sent_count, utcnow(), job_id),
            )
        conn.commit()


async def _report_with_session(
    session_base: str,
    target: str,
    reason: Any,
    text: str,
    per_account: int,
) -> int:
    sent = 0
    client = TelegramClient(
        session_base,
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return 0
        try:
            entity = await client.get_entity(target)
        except Exception:
            return 0
        try:
            await client(JoinChannelRequest(entity))
            await asyncio.sleep(1)
        except Exception:
            pass
        try:
            recent = await client.get_messages(entity, limit=5)
            message_ids = [m.id for m in recent if m and m.id]
        except Exception:
            message_ids = []
        for _ in range(per_account):
            try:
                peer_ok = await client(
                    functions.account.ReportPeerRequest(
                        peer=entity,
                        reason=reason,
                        message=text,
                    )
                )
                if peer_ok is True:
                    sent += 1
                else:
                    logger.warning(
                        "peer report not accepted: base=%s result=%r",
                        session_base,
                        peer_ok,
                    )
                if message_ids:
                    try:
                        res = await client(
                            functions.messages.ReportRequest(
                                peer=entity,
                                id=message_ids,
                                option=b"",
                                message=text,
                            )
                        )
                        if isinstance(
                            res,
                            types.ReportResultChooseOption,
                        ) and res.options:
                            res2 = await client(
                                functions.messages.ReportRequest(
                                    peer=entity,
                                    id=message_ids,
                                    option=res.options[0].option,
                                    message=text,
                                )
                            )
                            logger.info(
                                "msg report receipt: base=%s %s",
                                session_base,
                                type(res2).__name__,
                            )
                        else:
                            logger.info(
                                "msg report receipt: base=%s %s",
                                session_base,
                                type(res).__name__,
                            )
                    except Exception:
                        pass
            except FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 2)
            except Exception as exc:
                logger.warning("internal report failed: %s", exc)
                await asyncio.sleep(2)
        return sent
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_internal_reports(
    target: str,
    reason_key: str,
    per_account: int | None = None,
    report_text: str | None = None,
    progress_cb=None,
) -> tuple[int, int, list[str]]:
    """Report *target* from every Supabase session.

    Returns (sent_total, accounts_used, errors).
    """
    per_account = per_account or REPORTS_PER_ACCOUNT
    reason_cls, default_text = INTERNAL_REPORT_REASONS.get(
        reason_key or INTERNAL_DEFAULT_REASON,
        INTERNAL_REPORT_REASONS["other"],
    )
    text = (report_text or default_text)[:500]
    target = (target or "").strip()
    if not target:
        return (0, 0, ["empty_target"])
    if not internal_storage_ready():
        return (0, 0, ["storage_not_configured"])
    names = await list_internal_session_files()
    if not names:
        return (0, 0, ["no_sessions"])

    sem = asyncio.Semaphore(INTERNAL_MAX_PARALLEL)
    total_sent = 0
    used = 0
    errors: list[str] = []
    done = 0
    plan = len(names) * per_account

    async def worker(name: str):
        nonlocal total_sent, used, done
        local: Path | None = None
        try:
            async with sem:
                local = await download_internal_session(name)
                base = str(local.with_suffix(""))
                n = await _report_with_session(
                    base,
                    target,
                    reason_cls(),
                    text,
                    per_account,
                )
                total_sent += n
                if n:
                    used += 1
                logger.info(
                    "internal session done: storage=%s sent=%d/%d",
                    name,
                    n,
                    per_account,
                )
        except Exception as exc:
            errors.append(f"{name}: {exc}"[:200])
        finally:
            done += per_account
            if progress_cb:
                try:
                    if asyncio.iscoroutinefunction(progress_cb):
                        await progress_cb(min(done, plan), plan)
                    else:
                        progress_cb(min(done, plan), plan)
                except Exception:
                    pass
            if local is not None:
                try:
                    local.unlink(missing_ok=True)
                    Path(str(local.with_suffix("")) + ".session-journal").unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

    await asyncio.gather(*[worker(n) for n in names])
    return (total_sent, used, errors)


async def execute_internal_job(
    job_id: int,
    target: str,
    reason_key: str,
    prepared_text: str,
    user_id: int,
    bot,
) -> None:
    set_internal_job_status(job_id, "running")
    try:
        await bot.send_message(
            user_id,
            f"🚀 Задание #{job_id} запущено.\n🎯 {target}",
        )
    except Exception:
        pass
    try:
        sent, used, errors = await run_internal_reports(
            target,
            reason_key,
            REPORTS_PER_ACCOUNT,
            prepared_text,
        )
    except Exception as exc:
        logger.exception("internal job failed: job=%s", job_id)
        set_internal_job_status(job_id, "failed", str(exc)[:500], 0)
        try:
            await bot.send_message(
                user_id,
                f"❌ Задание #{job_id} упало с ошибкой.",
            )
        except Exception:
            pass
        return

    if errors == ["no_sessions"] or errors == ["storage_not_configured"]:
        status = "no_sessions"
    elif sent > 0:
        status = "done"
    else:
        status = "failed"
    set_internal_job_status(
        job_id,
        status,
        ("; ".join(errors))[:500] if errors else None,
        sent,
    )
    try:
        if status == "done":
            await bot.send_message(
                user_id,
                f"✅ Задание #{job_id} выполнено.\n\n"
                f"📤 Отправлено жалоб — {sent}\n"
                f"👥 Аккаунтов сработало — {used}",
            )
        elif status == "no_sessions":
            await bot.send_message(
                user_id,
                f"⚪ Задание #{job_id}: нет рабочих сессий.\n\n"
                "Загрузите .session в Supabase Storage.",
            )
        else:
            await bot.send_message(
                user_id,
                f"❌ Задание #{job_id} не дало отправок.\n\n"
                f"{('; '.join(errors))[:500] if errors else ''}",
            )
    except Exception:
        pass


# =========================================================
# SECURITY / MIRRORS
# =========================================================

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
    "violence": "⚠️ Насилие",
    "pornography": "🔞 Порнография",
    "child_abuse": "🚸 Жестокое обращение с детьми",
    "other": "📝 Другое",
    "copyright": "©️ Авторские права",
    "geo_irrelevant": "📍 Нерелевантная геогруппа",
    "fake": "👤 Выдача себя за другого",
    "illegal_drugs": "💊 Незаконные наркотики",
    "personal_details": "🔐 Раскрытие персональных данных",
}

INTERNAL_TARGET_TYPES = {
    "bot": "🤖 Бот",
    "channel": "📢 Канал",
    "group": "👥 Группа",
}

INTERNAL_VISIBILITIES = {
    "public": "🌐 Публичный",
    "private": "🔒 Приватный",
}

INTERNAL_REASON_TEMPLATES = {
    "spam": "Прошу проверить объект на признаки спама.\n\nОбъект: {target}",
    "violence": "Прошу проверить объект на материалы с пропагандой или описанием насилия.\n\nОбъект: {target}",
    "pornography": "Прошу проверить объект на порнографический контент.\n\nОбъект: {target}",
    "child_abuse": "Прошу проверить объект на материалы, связанные с жестоким обращением с детьми.\n\nОбъект: {target}",
    "other": "Прошу проверить объект по указанному пользователем основанию.\n\nОбъект: {target}",
    "copyright": "Прошу проверить объект на возможное нарушение авторских прав.\n\nОбъект: {target}",
    "geo_irrelevant": "Прошу проверить объект на нерелевантное географическое содержание.\n\nОбъект: {target}",
    "fake": "Прошу проверить объект на выдачу себя за другое лицо или организацию.\n\nОбъект: {target}",
    "illegal_drugs": "Прошу проверить объект на материалы, связанные с незаконными наркотиками.\n\nОбъект: {target}",
    "personal_details": "Прошу проверить объект на раскрытие персональных данных.\n\nОбъект: {target}",
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
WELCOME_BANNER = ASSETS_DIR / "darkcollect_banner.png"


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


def get_user_remaining_requests(user_id: int) -> int:
    """Return remaining daily request quota for an active subscriber."""
    if not has_subscription(user_id):
        return 0
    return max(0, DAILY_REQUEST_LIMIT - usage_today(user_id))


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


def get_user_active_mirrors(user_id: int):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM bot_mirrors
            WHERE active=TRUE
              AND owner_id=%s
            ORDER BY id ASC
            """,
            (user_id,),
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
        if token == BOT_TOKEN:
            logger.error(
                "Mirror #%s holds the main BOT_TOKEN, deactivating",
                mirror_id,
            )
            deactivate_mirror(mirror_id)
            return
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
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^(nav:|menu:|hug:)"))
    app.add_handler(CallbackQueryHandler(internal_callback, pattern=r"^internal:"))
    app.add_handler(CallbackQueryHandler(subscription_callback, pattern=r"^(sub:|pay:|manual_crypto:|check_crypto:)"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_moderation_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))




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
            CREATE TABLE IF NOT EXISTS internal_jobs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                target TEXT NOT NULL,
                target_type TEXT NOT NULL,
                visibility TEXT NOT NULL,
                reason TEXT NOT NULL,
                text_mode TEXT NOT NULL,
                prepared_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared',
                sent_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_sessions (
                id BIGSERIAL PRIMARY KEY,
                storage_path TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'unknown',
                telegram_user_id BIGINT,
                username TEXT,
                first_name TEXT,
                last_error TEXT,
                last_checked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS target_watch (
                target TEXT PRIMARY KEY,
                last_status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL
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
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS banned_until TIMESTAMPTZ",
            "ALTER TABLE hugs ADD COLUMN IF NOT EXISTS target_type TEXT NOT NULL DEFAULT 'user'",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS owner_id BIGINT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS bot_token_enc TEXT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS bot_username TEXT",
            "ALTER TABLE bot_mirrors ADD COLUMN IF NOT EXISTS token_fingerprint TEXT",
            "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ban_reason TEXT",
            "ALTER TABLE internal_jobs ADD COLUMN IF NOT EXISTS sent_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE internal_jobs ADD COLUMN IF NOT EXISTS error TEXT",
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
            "CREATE INDEX IF NOT EXISTS idx_internal_jobs_user "
            "ON internal_jobs(user_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_internal_jobs_status "
            "ON internal_jobs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_internal_sessions_status "
            "ON internal_sessions(status)"
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


def _storage_list_session_files_sync() -> list[str]:
    if not SUPABASE_CLIENT:
        return []

    response = SUPABASE_CLIENT.storage.from_(SUPABASE_SESSIONS_BUCKET).list(
        "",
        {
            "limit": 1000,
            "offset": 0,
            "sortBy": {"column": "name", "order": "asc"},
        },
    )

    names: list[str] = []
    for item in response or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name.lower().endswith(".session") and not name.startswith("."):
            names.append(name)
    return names


async def list_internal_session_files() -> list[str]:
    if not internal_storage_ready():
        return []
    try:
        return await asyncio.to_thread(
            _storage_list_session_files_sync
        )
    except Exception:
        logger.exception("Could not list session files from Supabase Storage")
        return []


def _download_internal_session_sync(
    storage_path: str,
    local_path: Path,
) -> None:
    if not SUPABASE_CLIENT:
        raise RuntimeError("Supabase Storage не настроен.")

    payload = SUPABASE_CLIENT.storage.from_(
        SUPABASE_SESSIONS_BUCKET
    ).download(storage_path)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(payload)


async def download_internal_session(storage_path: str) -> Path:
    safe_name = Path(storage_path).name
    local_path = INTERNAL_SESSION_CACHE_DIR / safe_name
    await asyncio.to_thread(
        _download_internal_session_sync,
        storage_path,
        local_path,
    )
    return local_path


async def check_internal_session(
    storage_path: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "storage_path": storage_path,
        "status": "error",
        "telegram_user_id": None,
        "username": None,
        "first_name": None,
        "error": None,
    }

    if not internal_storage_ready():
        result["status"] = "not_configured"
        result["error"] = (
            "Нужно настроить SUPABASE_URL, "
            "SUPABASE_SERVICE_ROLE_KEY, TELEGRAM_API_ID и TELEGRAM_API_HASH."
        )
        return result

    local_path = None
    client = None
    try:
        local_path = await download_internal_session(storage_path)
        session_base = local_path.with_suffix("")
        client = TelegramClient(
            str(session_base),
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )
        await client.connect()

        if not await client.is_user_authorized():
            result["status"] = "unauthorized"
            result["error"] = "Сессия не авторизована."
            return result

        me = await client.get_me()
        result.update(
            {
                "status": "ok",
                "telegram_user_id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
            }
        )
        return result

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:500]
        return result

    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if local_path is not None:
            try:
                local_path.unlink(missing_ok=True)
            except Exception:
                pass


def save_internal_session_result(result: dict[str, Any]) -> None:
    now = utcnow()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO internal_sessions(
                storage_path, status, telegram_user_id, username, first_name,
                last_error, last_checked_at, created_at, updated_at
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(storage_path) DO UPDATE SET
                status=EXCLUDED.status,
                telegram_user_id=EXCLUDED.telegram_user_id,
                username=EXCLUDED.username,
                first_name=EXCLUDED.first_name,
                last_error=EXCLUDED.last_error,
                last_checked_at=EXCLUDED.last_checked_at,
                updated_at=EXCLUDED.updated_at
            """,
            (
                result["storage_path"],
                result["status"],
                result.get("telegram_user_id"),
                result.get("username"),
                result.get("first_name"),
                result.get("error"),
                now,
                now,
                now,
            ),
        )
        conn.commit()


async def refresh_internal_sessions() -> list[dict[str, Any]]:
    names = await list_internal_session_files()
    results: list[dict[str, Any]] = []
    for name in names:
        result = await check_internal_session(name)
        save_internal_session_result(result)
        results.append(result)
    return results


def get_internal_session_rows():
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM internal_sessions
            ORDER BY COALESCE(username, storage_path) ASC
            LIMIT 100
            """
        ).fetchall()


def create_internal_job(
    user_id: int,
    target: str,
    target_type: str,
    visibility: str,
    reason: str,
    text_mode: str,
    prepared_text: str,
) -> int:
    now = utcnow()
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO internal_jobs(
                user_id, target, target_type, visibility, reason,
                text_mode, prepared_text, status, created_at, updated_at
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,'prepared',%s,%s)
            RETURNING id
            """,
            (
                user_id,
                target[:1000],
                target_type,
                visibility,
                reason,
                text_mode,
                prepared_text[:5000],
                now,
                now,
            ),
        ).fetchone()
        conn.commit()
    return int(row["id"])


def get_internal_jobs(user_id: int, limit: int = 20):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM internal_jobs
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()


def normalize_internal_target(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None

    value = value.replace("https://telegram.me/", "https://t.me/")
    value = value.replace("http://telegram.me/", "https://t.me/")

    if re.fullmatch(r"@[A-Za-z0-9_]{4,64}", value):
        return value

    if re.fullmatch(r"[A-Za-z0-9_]{4,64}", value):
        return "@" + value

    if re.fullmatch(r"https?://t\.me/[A-Za-z0-9_+\-/]+", value, re.IGNORECASE):
        return value.rstrip("/")

    if re.fullmatch(r"t\.me/[A-Za-z0-9_+\-/]+", value, re.IGNORECASE):
        return "https://" + value.rstrip("/")

    return None


def render_internal_template(reason: str, target: str) -> str:
    template = INTERNAL_REASON_TEMPLATES.get(
        reason,
        INTERNAL_REASON_TEMPLATES["other"],
    )
    return template.format(target=target)


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
            InlineKeyboardButton("⚙️ Внутряк", callback_data="menu:internal"),
        ],
        [
            InlineKeyboardButton("🌐 Зеркала", callback_data="nav:mirrors"),
        ],
        [
            InlineKeyboardButton("🏠 На главную", callback_data="nav:home"),
        ],
    ])


def kb_internal(user_id=None):
    rows = [
        [InlineKeyboardButton("➕ Новое задание", callback_data="internal:new")],
        [
            InlineKeyboardButton("📁 Мои задания", callback_data="internal:jobs"),
        ],
        [InlineKeyboardButton("🏠 На главную", callback_data="nav:home")],
    ]
    # Сессии видит только админ. У остальных кнопки нет вообще.
    if user_id is not None and is_admin(user_id):
        rows[1].append(
            InlineKeyboardButton("🔐 Сессии", callback_data="internal:sessions")
        )
    return InlineKeyboardMarkup(rows)


def kb_internal_target_types():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Бот", callback_data="internal:type:bot"),
            InlineKeyboardButton("📢 Канал", callback_data="internal:type:channel"),
        ],
        [InlineKeyboardButton("👥 Группа", callback_data="internal:type:group")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="internal:open")],
    ])


def kb_internal_visibility():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Публичный", callback_data="internal:visibility:public"),
            InlineKeyboardButton("🔒 Приватный", callback_data="internal:visibility:private"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="internal:open")],
    ])


def kb_internal_reasons():
    items = list(MODERATION_REASONS.items())
    rows = []
    pair = []
    for key, label in items:
        pair.append(InlineKeyboardButton(label, callback_data=f"internal:reason:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="internal:open")])
    return InlineKeyboardMarkup(rows)


def kb_internal_text_mode():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Наш шаблон", callback_data="internal:text:template")],
        [InlineKeyboardButton("✍️ Свой текст", callback_data="internal:text:custom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="internal:open")],
    ])


def kb_internal_sessions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Проверить сессии", callback_data="internal:sessions:refresh")],
        [InlineKeyboardButton("⬅️ Внутряк", callback_data="internal:open")],
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
# MIRRORS / MAINTENANCE
# =========================================================

async def show_mirrors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_active_mirrors(user_id)
    primary = PRIMARY_BOT_URL

    lines = [
        "🌐 ДОСТУП К ПРОЕКТУ",
        "",
        "🟢 Основной адрес:",
        primary,
        "",
    ]

    if rows:
        lines.append("🔵 МОИ ЗЕРКАЛА:")
        for row in rows:
            lines.extend([
                f"• {row['name']}",
                row["url"],
                format_mirror_status(row),
                "",
            ])
    else:
        lines.append("У вас пока нет подключённых зеркал.")

    lines.extend([
        "",
        "ℹ️ Зеркала, подключённые другими пользователями, вам не показываются.",
        "🔐 Добавить своё зеркало можно через токен @BotFather.",
    ])

    await send_ui(
        update,
        context,
        "\n".join(lines),
        kb_mirrors(user_id),
    )

async def show_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_user_active_mirrors(user_id)
    text = MAINTENANCE_TEXT
    if rows:
        text += "\n\n🌐 Ваши зеркала:\n"
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
    q = update.callback_query

    if not user or user.is_bot:
        return

    # Commands should reach their dedicated CommandHandlers.
    if update.message and update.message.text and update.message.text.startswith("/"):
        return

    # Payment updates must not be blocked by maintenance/channel gates.
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
        await send_ui(
            update,
            context,
            "⚠️ Слишком много действий. Подождите немного.",
            kb_home(user.id),
        )
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
    caption_above_media: bool | None = None,
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

                    photo_kwargs = {
                        "chat_id": chat_id,
                        "photo": fh,
                        "caption": text,
                        "reply_markup": markup,
                    }
                    if caption_above_media is not None:
                        photo_kwargs["show_caption_above_media"] = caption_above_media
                    await context.bot.send_photo(**photo_kwargs)

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
        photo=WELCOME_BANNER,
        caption_above_media=False,
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
        photo=WELCOME_BANNER,
        caption_above_media=False,
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
        f"💬 Выполнено работ — {profile['sent']}\n"
        f"👀 Всего поисков — {profile['searches']}\n\n"
        f"💎 Подписка — {sub_text}"
        f"{expires_text}"
        f"{bonus_text}\n"
        f"📊 Запросов сегодня — "
        f"{usage_today(user_id)}/"
        f"{DAILY_REQUEST_LIMIT}\n"
        f"🚀 Работ доступно — {get_user_remaining_requests(user_id)}"
    )


# =========================================================
# HUG PROCESS
# =========================================================







# =========================================================
# CHECK / SEARCH / HISTORY
# =========================================================





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
# INTERNAL UI
# =========================================================

async def show_internal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data["state"] = None
    await send_ui(
        update,
        context,
        (
            "⚙️ ВНУТРЯК\n\n"
            "Подготовка внутренних заданий Telegram.\n\n"
            "Можно выбрать тип объекта, публичность, причину "
            "и шаблон текста.\n\n"
            "🔐 Сессии хранятся в приватном Supabase Storage."
        ),
        kb_internal(update.effective_user.id),
    )


async def show_internal_jobs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    rows = get_internal_jobs(user_id)
    if not rows:
        await send_ui(
            update,
            context,
            "📁 МОИ ЗАДАНИЯ\n\nПока нет подготовленных заданий.",
            kb_internal(update.effective_user.id),
        )
        return

    lines = ["📁 МОИ ЗАДАНИЯ\n"]
    for row in rows:
        label = INTERNAL_TARGET_TYPES.get(row["target_type"], row["target_type"])
        vis = INTERNAL_VISIBILITIES.get(row["visibility"], row["visibility"])
        reason = MODERATION_REASONS.get(row["reason"], row["reason"])
        status = JOB_STATUS_RU.get(row["status"], row["status"])
        sent_line = f"\n📤 Отправлено — {row.get('sent_count', 0)}" if row.get("sent_count") else ""
        lines.append(
            f"#{row['id']} · {label} · {vis}\n"
            f"🎯 {row['target']}\n"
            f"{reason}\n"
            f"📝 {row['text_mode']}\n"
            f"{status}{sent_line}\n"
            f"{aware(row['created_at']).strftime('%d.%m.%Y %H:%M')}"
        )

    await send_ui(update, context, "\n\n".join(lines), kb_internal(update.effective_user.id))


async def show_internal_sessions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await send_ui(update, context, "❌ Доступ только для администратора.", kb_internal(update.effective_user.id))
        return

    if not internal_storage_ready():
        await send_ui(
            update,
            context,
            (
                "🔐 СЕССИИ\n\n"
                "Хранилище не настроено.\n\n"
                "Нужны:\n"
                "• SUPABASE_URL\n"
                "• SUPABASE_SERVICE_ROLE_KEY\n"
                "• TELEGRAM_API_ID\n"
                "• TELEGRAM_API_HASH"
            ),
            kb_internal_sessions(),
        )
        return

    rows = get_internal_session_rows()
    if not rows:
        await send_ui(
            update,
            context,
            "🔐 СЕССИИ\n\nСессий пока нет. Загрузите `.session` в Supabase Storage и нажмите проверку.",
            kb_internal_sessions(),
        )
        return

    lines = ["🔐 СЕССИИ\n"]
    for row in rows:
        status_map = {
            "ok": "🟢 Работает",
            "unauthorized": "🔴 Не авторизована",
            "error": "⚠️ Ошибка",
            "not_configured": "⚪ Не настроено",
            "unknown": "⚪ Не проверялась",
        }
        status = status_map.get(row["status"], row["status"])
        account = f"@{row['username']}" if row["username"] else (row["first_name"] or row["storage_path"])
        lines.append(f"{status} · {account}\n📄 {row['storage_path']}")
        if row["last_error"]:
            lines.append(f"↳ {str(row['last_error'])[:180]}")

    await send_ui(update, context, "\n\n".join(lines), kb_internal_sessions())


async def start_internal_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    for key in (
        "internal_target_type",
        "internal_visibility",
        "internal_target",
        "internal_reason",
        "internal_text_mode",
    ):
        context.user_data.pop(key, None)
    context.user_data["state"] = "internal_target_type"
    await send_ui(
        update,
        context,
        "⚙️ НОВОЕ ЗАДАНИЕ\n\nШаг 1/5 · выберите объект:",
        kb_internal_target_types(),
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
        f"✅ Всего работ: "
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
        "internal_jobs",
        "internal_sessions",
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

    elif data == "menu:internal":

        if not await require_subscription(update, context, user_id):
            return

        await show_internal(
            update,
            context,
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
# INTERNAL CALLBACK
# =========================================================

async def internal_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    q = update.callback_query
    user_id = q.from_user.id
    data = q.data or ""
    ensure_profile(user_id, q.from_user)

    try:
        await q.answer()
    except TelegramError:
        pass

    if data == "internal:open":
        if not await require_subscription(update, context, user_id):
            return
        await show_internal(update, context)
        return

    if data == "internal:new":
        if not await require_subscription(update, context, user_id):
            return
        context.user_data["internal_target_type"] = None
        context.user_data["internal_visibility"] = None
        context.user_data["internal_target"] = None
        context.user_data["internal_reason"] = None
        context.user_data["internal_text_mode"] = None
        context.user_data["state"] = "internal_target_type"
        await send_ui(update, context, "⚙️ НОВОЕ ЗАДАНИЕ\n\nШаг 1/5 · выберите объект:", kb_internal_target_types())
        return

    if data.startswith("internal:type:"):
        target_type = data.rsplit(":", 1)[1]
        if target_type not in INTERNAL_TARGET_TYPES:
            return
        context.user_data["internal_target_type"] = target_type
        if target_type == "bot":
            # У ботов не бывает приватности — доступ всегда публичный.
            context.user_data["internal_visibility"] = "public"
            context.user_data["state"] = "internal_target"
            await send_ui(
                update, context,
                "🤖 Бот\n\nБоты всегда публичные, вопрос доступа пропускаю.\n\nШаг 3/5 · пришлите @username бота:",
                kb_internal(update.effective_user.id),
            )
            return
        context.user_data["state"] = "internal_visibility"
        await send_ui(
            update, context,
            f"{INTERNAL_TARGET_TYPES[target_type]}\n\nШаг 2/5 · выберите тип доступа:",
            kb_internal_visibility(),
        )
        return

    if data.startswith("internal:visibility:"):
        visibility = data.rsplit(":", 1)[1]
        if visibility not in INTERNAL_VISIBILITIES:
            return
        context.user_data["internal_visibility"] = visibility
        context.user_data["state"] = "internal_target"
        await send_ui(
            update, context,
            f"{INTERNAL_VISIBILITIES[visibility]}\n\nШаг 3/5 · пришлите @username или ссылку t.me:",
            kb_internal(update.effective_user.id),
        )
        return

    if data.startswith("internal:reason:"):
        reason = data.rsplit(":", 1)[1]
        if reason not in MODERATION_REASONS:
            return
        context.user_data["internal_reason"] = reason
        context.user_data["state"] = "internal_text_mode"
        await send_ui(
            update, context,
            f"📄 Шаг 4/5 · причина\n{MODERATION_REASONS[reason]}\n\nВыберите текст:",
            kb_internal_text_mode(),
        )
        return

    if data == "internal:text:template":
        target = context.user_data.get("internal_target")
        reason = context.user_data.get("internal_reason")
        target_type = context.user_data.get("internal_target_type")
        visibility = context.user_data.get("internal_visibility")
        if not all([target, reason, target_type, visibility]):
            await send_ui(update, context, "❌ Данные задания устарели.", kb_internal(update.effective_user.id))
            return
        prepared = render_internal_template(reason, target)
        job_id = create_internal_job(
            user_id, target, target_type, visibility, reason, "Наш шаблон", prepared
        )
        for key in (
            "internal_target_type",
            "internal_visibility",
            "internal_target",
            "internal_reason",
            "internal_text_mode",
        ):
            context.user_data.pop(key, None)
        context.user_data["state"] = None
        await send_ui(
            update, context,
            f"✅ Задание #{job_id} создано.\n\n{prepared}\n\n🚀 Запускаю выполнение...",
            kb_internal(update.effective_user.id),
        )
        context.application.create_task(
            execute_internal_job(
                job_id,
                target,
                reason,
                prepared,
                user_id,
                context.bot,
            ),
            update=update,
        )
        return

    if data == "internal:text:custom":
        context.user_data["internal_text_mode"] = "custom"
        context.user_data["state"] = "internal_custom_text"
        await send_ui(
            update, context,
            "✍️ Шаг 5/5 · пришлите свой текст обращения:",
            kb_internal(update.effective_user.id),
        )
        return

    if data == "internal:jobs":
        await show_internal_jobs(update, context)
        return

    if data == "internal:sessions":
        await show_internal_sessions(update, context)
        return

    if data == "internal:sessions:refresh":
        if not is_admin(user_id):
            await send_ui(update, context, "❌ Доступ только для администратора.", kb_internal(update.effective_user.id))
            return
        if not internal_storage_ready():
            await show_internal_sessions(update, context)
            return
        await send_ui(update, context, "⏳ Проверяю сессии...", kb_internal_sessions())
        results = await refresh_internal_sessions()
        ok = sum(r["status"] == "ok" for r in results)
        bad = len(results) - ok
        await send_ui(
            update, context,
            f"✅ Проверка завершена.\n\n🟢 Рабочих: {ok}\n⚠️ Остальных: {bad}",
            kb_internal_sessions(),
        )
        return


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
    # INTERNAL JOBS
    # ---------------------------------------------

    if state == "internal_target":
        normalized = normalize_internal_target(text)
        if not normalized:
            await update.message.reply_text(
                "❌ Неверная ссылка. Используйте @username или ссылку t.me/...",
                reply_markup=kb_internal(update.effective_user.id),
            )
            return

        context.user_data["internal_target"] = normalized
        context.user_data["state"] = "internal_reason"
        await update.message.reply_text(
            "✅ Объект принят.\n\nШаг 4/5 · выберите причину:",
            reply_markup=kb_internal_reasons(),
        )
        return

    if state == "internal_custom_text":
        target = context.user_data.get("internal_target")
        reason = context.user_data.get("internal_reason")
        target_type = context.user_data.get("internal_target_type")
        visibility = context.user_data.get("internal_visibility")
        if not all([target, reason, target_type, visibility]):
            context.user_data.clear()
            await update.message.reply_text("❌ Данные задания устарели.", reply_markup=kb_internal(update.effective_user.id))
            return

        prepared = text[:5000]
        job_id = create_internal_job(
            user_id, target, target_type, visibility, reason, "Свой текст", prepared
        )
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Задание #{job_id} создано.\n\n{prepared}\n\n🚀 Запускаю выполнение...",
            reply_markup=kb_internal(update.effective_user.id),
        )
        context.application.create_task(
            execute_internal_job(
                job_id,
                target,
                reason,
                prepared,
                user_id,
                context.bot,
            ),
            update=update,
        )
        return

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
# TARGET WATCH — сторож блокировок
# =========================================================
# Следит за целями выполненных заданий: если канал/группа/бот,
# по которому кидали жалобы, становится недоступен (вероятно,
# заблокирован Telegram) — каждый пользователь, кидавший на него
# репорт, получает оповещение в бота.

TARGET_WATCH_INTERVAL_MIN = max(
    5,
    int(os.environ.get("TARGET_WATCH_INTERVAL_MIN", "30")),
)


def get_watched_targets(limit: int = 200) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT target
            FROM internal_jobs
            WHERE status='done'
            GROUP BY target
            ORDER BY MAX(id) DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [row["target"] for row in rows]


def get_target_subscribers(target: str) -> list[int]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM internal_jobs
            WHERE target=%s
              AND status IN ('done', 'running')
            """,
            (target,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def get_target_watch(target: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM target_watch WHERE target=%s",
            (target,),
        ).fetchone()


def set_target_watch(target: str, status: str) -> None:
    now = utcnow()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO target_watch(target, last_status, last_checked_at, updated_at)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(target) DO UPDATE SET
                last_status=EXCLUDED.last_status,
                last_checked_at=EXCLUDED.last_checked_at,
                updated_at=EXCLUDED.updated_at
            """,
            (target, status, now, now),
        )
        conn.commit()


def touch_target_watch(target: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE target_watch
            SET last_checked_at=%s
            WHERE target=%s
            """,
            (utcnow(), target),
        )
        conn.commit()


def is_watchable_target(target: str) -> bool:
    value = (target or "").strip().lower()
    if not value:
        return False
    # Приватные инвайт-ссылки userbot'ом без вступления не проверить.
    if "/+" in value or "joinchat" in value:
        return False
    return True


async def check_target_accessible(client, target: str) -> str:
    """'ok' | 'gone' | 'private' | 'unknown'."""
    for _attempt in range(2):
        try:
            await client.get_entity(target)
            return "ok"
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            continue
        except (UsernameInvalidError, UsernameNotOccupiedError):
            return "gone"
        except ChannelPrivateError:
            return "private"
        except Exception as exc:
            logger.warning("watch check failed target=%s: %s", target, exc)
            return "unknown"
    return "unknown"


async def target_watch_loop(application: Application):
    await asyncio.sleep(60)
    while True:
        try:
            if not internal_storage_ready():
                await asyncio.sleep(TARGET_WATCH_INTERVAL_MIN * 60)
                continue
            targets = [
                t for t in get_watched_targets()
                if is_watchable_target(t)
            ]
            if not targets:
                await asyncio.sleep(TARGET_WATCH_INTERVAL_MIN * 60)
                continue
            names = await list_internal_session_files()
            if not names:
                await asyncio.sleep(TARGET_WATCH_INTERVAL_MIN * 60)
                continue

            local = None
            client = None
            try:
                local = await download_internal_session(names[0])
                client = TelegramClient(
                    str(local.with_suffix("")),
                    TELEGRAM_API_ID,
                    TELEGRAM_API_HASH,
                )
                await client.connect()
                if not await client.is_user_authorized():
                    return

                for target in targets:
                    status = await check_target_accessible(client, target)
                    if status == "unknown":
                        touch_target_watch(target)
                        continue
                    row = get_target_watch(target)
                    if row is None:
                        # Первый замер — baseline, без оповещений.
                        set_target_watch(target, status)
                        continue
                    old = row["last_status"]
                    if old == "ok" and status in ("gone", "private"):
                        set_target_watch(target, status)
                        for user_id in get_target_subscribers(target):
                            try:
                                await application.bot.send_message(
                                    user_id,
                                    "🔔 Цель недоступна\n\n"
                                    f"🎯 {target}\n\n"
                                    "Объект стал недоступен — вероятно, "
                                    "заблокирован Telegram.\n\n"
                                    "Твои жалобы сработали 💥",
                                )
                            except Exception:
                                pass
                        log_event(
                            "INFO",
                            "target_blocked",
                            None,
                            {"target": target, "status": status},
                        )
                    elif old != status:
                        set_target_watch(target, status)
                    else:
                        touch_target_watch(target)
                    await asyncio.sleep(1)
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                if local is not None:
                    try:
                        local.unlink(missing_ok=True)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Target watch loop failed")
        await asyncio.sleep(TARGET_WATCH_INTERVAL_MIN * 60)


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

    asyncio.create_task(
        target_watch_loop(
            application
        ),
        name="target-watch-loop",
    )

    # Start all active BotFather-based mirrors after the main bot is ready.
    # A mirror holding the MAIN token would poll the same bot twice and
    # cause getUpdates Conflict — skip and deactivate such rows.
    for row in get_active_mirrors():
        if not row.get("bot_token_enc"):
            continue
        try:
            if decrypt_mirror_token(row["bot_token_enc"]) == BOT_TOKEN:
                logger.error(
                    "Mirror #%s holds the main BOT_TOKEN, deactivating",
                    row["id"],
                )
                deactivate_mirror(int(row["id"]))
                continue
        except Exception:
            logger.exception(
                "Could not decrypt mirror #%s, skipping",
                row["id"],
            )
            continue
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