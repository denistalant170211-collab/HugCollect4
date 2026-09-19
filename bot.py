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
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hugbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@your_support_username")
PORT = int(os.environ.get("PORT", "10000"))

WEBHOOK_BASE = (
    os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("PUBLIC_URL")
)

BASE_DIR = Path(__file__).resolve().parent

ASSET_CANDIDATES = [
    BASE_DIR / "assets",
    BASE_DIR / "HugCollectBot" / "assets",
    Path.cwd() / "assets",
    Path.cwd() / "HugCollectBot" / "assets",
]

ASSETS_DIR = next(
    (p for p in ASSET_CANDIDATES if p.is_dir()),
    ASSET_CANDIDATES[0]
)

PROFILE_BANNER = ASSETS_DIR / "profile_banner.png"
MENU_BANNER = ASSETS_DIR / "menu_banner.png"

logger.info("BASE_DIR=%s", BASE_DIR)
logger.info("CWD=%s", Path.cwd())
logger.info("ASSETS_DIR=%s exists=%s", ASSETS_DIR, ASSETS_DIR.is_dir())
logger.info(
    "PROFILE_BANNER=%s exists=%s",
    PROFILE_BANNER,
    PROFILE_BANNER.is_file()
)
logger.info(
    "MENU_BANNER=%s exists=%s",
    MENU_BANNER,
    MENU_BANNER.is_file()
)

DB_PATH = Path(
    os.environ.get(
        "DB_PATH",
        str(BASE_DIR / "hugcollect.db")
    )
)

CRYPTO_PAY_API_TOKEN = os.environ.get(
    "CRYPTO_PAY_API_TOKEN",
    ""
).strip()

CRYPTO_ASSET = os.environ.get(
    "CRYPTO_ASSET",
    "USDT"
).strip().upper()

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

logger.info(
    "REQUIRED_CHANNEL_ID=%s",
    REQUIRED_CHANNEL_ID or "(not set)"
)

PLANS = {
    "week": {
        "title": "Недельная подписка",
        "days": 7,
        "usd": "5",
        "stars": 400,
        "channel": WEEKLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID,
    },
    "month": {
        "title": "Месячная подписка",
        "days": 30,
        "usd": "9",
        "stars": 700,
        "channel": MONTHLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID,
    },
}

CRYPTO_FALLBACK_WEEK = os.environ.get(
    "CRYPTO_FALLBACK_WEEK",
    ""
).strip()

CRYPTO_FALLBACK_MONTH = os.environ.get(
    "CRYPTO_FALLBACK_MONTH",
    ""
).strip()

GREETING = (
    "Привет, пользователь!\n"
    "Чем я могу вам помочь?"
)

SUPPORT_TEXT = (
    f"Если вы столкнулись с проблемой — напишите: "
    f"{SUPPORT_USERNAME}"
)

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



def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 0,
                warmth INTEGER NOT NULL DEFAULT 1000,
                ref_code TEXT NOT NULL DEFAULT 'HUGGER',
                checks INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS hugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target TEXT NOT NULL,
                count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
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
        """)

        conn.execute("""
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
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                plan TEXT NOT NULL,
                max_uses INTEGER,
                used INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                PRIMARY KEY (code, user_id)
            )
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_payment
            ON subscriptions(payment_id)
            WHERE payment_id IS NOT NULL
        """)

        migrated = conn.execute(
            "SELECT value FROM meta WHERE key='level_zero_v1'"
        ).fetchone()

        if not migrated:
            conn.execute("UPDATE profiles SET level=0")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('level_zero_v1', '1')"
            )

        if PROMO_CODE:
            plan = PROMO_PLAN if PROMO_PLAN in PLANS else "month"

            max_uses = (
                int(PROMO_MAX_USES)
                if PROMO_MAX_USES.isdigit()
                else None
            )

            conn.execute(
                """
                INSERT OR IGNORE INTO promo_codes
                (code, plan, max_uses, used, active)
                VALUES (?, ?, ?, 0, 1)
                """,
                (PROMO_CODE, plan, max_uses),
            )

        conn.commit()


def required_channel_url() -> str:
    if REQUIRED_CHANNEL_URL:
        return REQUIRED_CHANNEL_URL

    ch = REQUIRED_CHANNEL_ID.strip()

    if ch.startswith("https://"):
        return ch

    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"

    if ch.startswith("t.me/"):
        return f"https://{ch}"

    if ch and not ch.lstrip("-").isdigit():
        return f"https://t.me/{ch}"

    return ""


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


CHANNEL_GATE_TEXT = (
    "🔒 Сначала подпишитесь на канал\n\n"
    "Без подписки бот закрыт: меню, кабинет и функции недоступны.\n\n"
    "1️⃣ Нажмите «Подписаться»\n"
    "2️⃣ Подпишитесь на канал\n"
    "3️⃣ Вернитесь сюда и нажмите «Проверить подписку»"
)


async def is_required_channel_member(
    bot,
    user_id: int
) -> bool:

    if not REQUIRED_CHANNEL_ID:
        logger.warning(
            "REQUIRED_CHANNEL_ID is not configured. "
            "Subscription check skipped."
        )
        return True

    try:
        member = await bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL_ID,
            user_id=user_id
        )

        logger.info(
            "Subscription check: "
            "user=%s channel=%s status=%s",
            user_id,
            REQUIRED_CHANNEL_ID,
            member.status
        )

        is_member = member.status in {
            "creator",
            "administrator",
            "member",
            "restricted",
        }

        if is_member:
            logger.info(
                "User %s IS subscribed to channel %s",
                user_id,
                REQUIRED_CHANNEL_ID
            )
        else:
            logger.info(
                "User %s IS NOT subscribed to channel %s. "
                "Status=%s",
                user_id,
                REQUIRED_CHANNEL_ID,
                member.status
            )

        return is_member

    except TelegramError as exc:
        logger.error(
            "Subscription check ERROR: "
            "user=%s channel=%s error=%s",
            user_id,
            REQUIRED_CHANNEL_ID,
            exc
        )

        return False


async def show_channel_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.message:
        text = update.message.text or ""

        if text.startswith("/start"):
            await update.message.reply_text(
                "Меню перенесено в сообщение 👇",
                reply_markup=ReplyKeyboardRemove()
            )

        await update.message.reply_text(
            CHANNEL_GATE_TEXT,
            reply_markup=kb_channel_gate()
        )

        return

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
    if not REQUIRED_CHANNEL_ID:
        return

    chat = update.effective_chat

    if not chat or chat.type != "private":
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    if update.pre_checkout_query:
        return

    if update.message and update.message.successful_payment:
        return

    q = update.callback_query

    if q and q.data == "gate:check":

        subscribed = await is_required_channel_member(
            context.bot,
            user.id
        )

        if subscribed:
            await q.answer("Подписка найдена ✅")
            await show_home(update, context)
        else:
            await q.answer(
                "Вы ещё не подписаны на канал.",
                show_alert=True
            )
            await show_channel_gate(update, context)

        raise ApplicationHandlerStop

    if await is_required_channel_member(
        context.bot,
        user.id
    ):
        return

    if q:
        await q.answer(
            "Сначала подпишитесь на канал.",
            show_alert=True
        )

    await show_channel_gate(
        update,
        context
    )

    raise ApplicationHandlerStop


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


def kb_pay_methods(plan: str):
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


def kb_after_pay(link: str | None):
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


def ensure_profile(user_id: int):
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO profiles
            (user_id, level, warmth, ref_code, checks)
            VALUES (?, 0, 1000, 'HUGGER', 0)
            """,
            (user_id,)
        )

        conn.commit()


def get_profile(user_id: int) -> dict:
    ensure_profile(user_id)

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id=?",
            (user_id,)
        ).fetchone()

        sent = conn.execute(
            "SELECT COUNT(*) FROM hugs WHERE user_id=?",
            (user_id,)
        ).fetchone()[0]

    p = dict(row)
    p["sent"] = sent

    sub = get_active_subscription(user_id)

    p["sub"] = sub
    p["sub_active"] = sub is not None
    p["sub_days_left"] = 0

    if sub:
        exp = datetime.fromisoformat(
            sub["expires_at"]
        )

        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)

        p["sub_days_left"] = max(
            0,
            (exp - datetime.now(timezone.utc)).days
        )

    return p


def get_active_subscription(user_id: int):
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=?
            AND expires_at>?
            ORDER BY expires_at DESC
            LIMIT 1
            """,
            (user_id, now)
        ).fetchone()

    return dict(row) if row else None


def has_subscription(user_id: int) -> bool:
    return get_active_subscription(user_id) is not None


def create_payment(
    user_id: int,
    plan: str,
    method: str,
    external_id: str | None,
    status: str = "pending"
) -> int:

    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO payments
            (user_id, plan, method, external_id, status,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        if external_id:
            conn.execute(
                """
                UPDATE payments
                SET status=?, external_id=?, updated_at=?
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
                SET status=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    now,
                    payment_id
                )
            )

        conn.commit()


def claim_payment(
    payment_id: int | None,
    payment_key: str
) -> bool:

    if payment_key and find_subscription_by_payment(payment_key):
        return False

    if payment_id is None:
        return True

    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        cur = conn.execute(
            """
            UPDATE payments
            SET status=?, external_id=?, updated_at=?
            WHERE id=? AND status!=?
            """,
            (
                "paid",
                payment_key,
                now,
                payment_id,
                "paid"
            )
        )

        conn.commit()

        return cur.rowcount > 0


def get_payment(payment_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE id=?",
            (payment_id,)
        ).fetchone()

    return dict(row) if row else None


def find_subscription_by_payment(payment_id: str):
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE payment_id=?
            """,
            (payment_id,)
        ).fetchone()

    return dict(row) if row else None


def activate_subscription(
    user_id: int,
    plan: str,
    method: str,
    payment_id: str | None
):
    if payment_id:
        existing = find_subscription_by_payment(
            payment_id
        )

        if existing:
            return datetime.fromisoformat(
                existing["expires_at"]
            )

    now = datetime.now(timezone.utc)

    current = get_active_subscription(user_id)

    if current:
        current_exp = datetime.fromisoformat(
            current["expires_at"]
        )

        if current_exp.tzinfo is None:
            current_exp = current_exp.replace(
                tzinfo=timezone.utc
            )

        start = max(now, current_exp)
    else:
        start = now

    expires = start + timedelta(
        days=PLANS[plan]["days"]
    )

    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO subscriptions
                (user_id, plan, method, payment_id,
                 starts_at, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    plan,
                    method,
                    payment_id,
                    start.isoformat(),
                    expires.isoformat(),
                    now.isoformat()
                )
            )

            conn.commit()

        except sqlite3.IntegrityError:
            existing = (
                find_subscription_by_payment(payment_id)
                if payment_id
                else None
            )

            if existing:
                return datetime.fromisoformat(
                    existing["expires_at"]
                )

            raise

    return expires


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
            "SELECT * FROM promo_codes WHERE code=?",
            (code,)
        ).fetchone()

        if not row or not int(row["active"]):
            return "invalid", None

        plan = row["plan"]

        if plan not in PLANS:
            return "invalid", None

        already = conn.execute(
            """
            SELECT 1
            FROM promo_redemptions
            WHERE code=? AND user_id=?
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
                INSERT INTO promo_redemptions
                (code, user_id, redeemed_at)
                VALUES (?, ?, ?)
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


def request_usage(user_id: int) -> tuple[bool, int]:
    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    with db() as conn:
        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=? AND usage_date=?
            """,
            (
                user_id,
                today
            )
        ).fetchone()

        used = int(row[0]) if row else 0

        if used >= 50:
            return False, used

        if row:
            conn.execute(
                """
                UPDATE daily_usage
                SET requests=requests+1
                WHERE user_id=? AND usage_date=?
                """,
                (
                    user_id,
                    today
                )
            )
        else:
            conn.execute(
                """
                INSERT INTO daily_usage
                (user_id, usage_date, requests)
                VALUES (?, ?, 1)
                """,
                (
                    user_id,
                    today
                )
            )

        conn.commit()

        return True, used + 1


def usage_today(user_id: int) -> int:
    today = datetime.now(
        timezone.utc
    ).date().isoformat()

    with db() as conn:
        row = conn.execute(
            """
            SELECT requests
            FROM daily_usage
            WHERE user_id=? AND usage_date=?
            """,
            (
                user_id,
                today
            )
        ).fetchone()

    return int(row[0]) if row else 0


def add_check(user_id: int):
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
            INSERT INTO hugs
            (user_id, target, count, created_at)
            VALUES (?, ?, ?, ?)
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
            SET warmth=MIN(1000, warmth+10)
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()


def get_history(
    user_id: int,
    limit=10
):
    with db() as conn:
        return conn.execute(
            """
            SELECT target, count, created_at
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


def progress_bar(
    percent: int,
    width: int = 10
) -> str:

    filled = max(
        0,
        min(
            width,
            round(
                percent / 100 * width
            )
        )
    )

    return (
        "█" * filled
        + "░" * (width - filled)
    )


def hug_progress_text(
    target: str,
    percent: int,
    stage: str
) -> str:

    return (
        "╭────────────────────╮\n"
        "│   DarkCollect     │\n"
        "╰────────────────────╯\n\n"
        f"Кому: {target}\n"
        f"{stage}\n\n"
        f"{progress_bar(percent)}  {percent}%"
    )


def profile_caption(
    profile: dict,
    user_id: int
) -> str:

    sub_text = (
        "Оформлена"
        if profile["sub_active"]
        else "Не оформлена"
    )

    expires = ""

    if profile["sub"]:
        exp = datetime.fromisoformat(
            profile["sub"]["expires_at"]
        )

        expires = (
            f"\n⏳ До — "
            f"{exp.strftime('%d.%m.%Y %H:%M')}"
        )

    return (
        "👤 Личный кабинет\n\n"
        f"🤗 Уровень — {profile['level']}\n"
        f"💞 Приоритет — {profile['warmth']}/1000\n"
        f"💬 Отправлено жалоб — {profile['sent']}\n"
        f"👀 Всего поисков — {profile['checks']}\n\n"
        f"💎 Подписка — {sub_text}{expires}\n"
        f"📊 Запросов сегодня — "
        f"{usage_today(user_id)}/50"
    )


def subscription_shop_text() -> str:
    return (
        "‼️ Доступ к основным функциям бота ‼️\n\n"
        "5️⃣0️⃣ запросов в день\n"
        "✔️ Защита пользователя\n"
        "🔐 Доступ в закрытый канал после оплаты\n\n"
        "1️⃣ Неделя — 5$ / 400⭐\n"
        "2️⃣ Месяц — 9$ / 700⭐\n\n"
        "👇 Выберите срок подписки ниже 👇"
    )


def subscription_required_text():
    return (
        "❌ Упс\n\n"
        "⭕️ У вас не имеется подписка\n\n"
        "❗️ Оформите подписку в личном кабинете — "
        "после оплаты доступ выдастся автоматически."
    )


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
    replace: bool = True,
):
    chat_id = update.effective_chat.id
    q = update.callback_query

    if replace and q and q.message:
        await safe_delete(q.message)

    if photo and photo.is_file():
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

    profile = get_profile(user_id)

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

    if has_subscription(user_id):
        return True

    await send_ui(
        update,
        context,
        subscription_required_text(),
        kb_profile()
    )

    return False


async def issue_channel_invite(
    bot,
    user_id: int,
    plan: str
):
    channel = PLANS[plan]["channel"]

    if not channel:
        logger.warning(
            "No channel configured for plan=%s",
            plan
        )
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

    sub = get_active_subscription(user_id)

    expire_ts = None

    if sub:
        exp = datetime.fromisoformat(
            sub["expires_at"]
        )

        if exp.tzinfo is None:
            exp = exp.replace(
                tzinfo=timezone.utc
            )

        expire_ts = int(
            exp.timestamp()
        )

    try:
        link = await bot.create_chat_invite_link(
            chat_id=channel,
            name=f"user {user_id} {plan}",
            member_limit=1,
            expire_date=expire_ts
        )

        return link.invite_link, "ok"

    except TelegramError:
        logger.exception(
            "Could not create invite link "
            "for channel=%s user=%s",
            channel,
            user_id
        )

        return None, "error"


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


_notified_payments: set[str] = set()


def _access_granted_text(
    plan: str,
    expires: datetime,
    invite_status: str,
    header: str,
    link: str | None = None
) -> str:

    if expires.tzinfo is None:
        expires = expires.replace(
            tzinfo=timezone.utc
        )

    text = (
        f"{header}\n\n"
        f"💎 Подписка: {PLANS[plan]['title']}\n"
        f"⏳ Действует до: "
        f"{expires.strftime('%d.%m.%Y %H:%M')}\n"
        "✔️ Функции бота уже открыты"
    )

    if invite_status == "ok" and link:
        text += (
            "\n\n👇 Ваша одноразовая ссылка в канал:"
        )

    elif invite_status == "already_member":
        text += (
            "\n\n🔐 Вы уже состоите "
            "в канале подписки."
        )

    elif invite_status == "no_channel":
        text += (
            "\n\nℹ️ Канал ещё настраивается "
            "администратором — доступ в боте уже активен."
        )

    else:
        text += (
            f"\n\n⚠️ Ссылку в канал не удалось создать. "
            f"Напишите {SUPPORT_USERNAME} — "
            "подписка в боте уже выдана."
        )

    return text


async def grant_paid_access(
    bot,
    user_id: int,
    plan: str,
    method: str,
    payment_key: str,
    payment_db_id: int | None = None
):

    if payment_key in _notified_payments:
        return activate_subscription(
            user_id,
            plan,
            method,
            payment_key
        )

    _notified_payments.add(payment_key)

    claim_payment(
        payment_db_id,
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

    await bot.send_message(
        user_id,
        _access_granted_text(
            plan,
            expires,
            invite_status,
            "✅ Оплата получена!",
            link
        ),
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
        f"promo:{code}:{user_id}"
    )

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

    await bot.send_message(
        user_id,
        _access_granted_text(
            plan,
            expires,
            invite_status,
            f"✅ Промокод {code} активирован!",
            link
        ),
        reply_markup=kb_after_pay(link)
    )

    return expires


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
        await update.effective_message.reply_text(
            "Введите промокод текстом.",
            reply_markup=kb_profile()
        )
        return True

    if status == "invalid":
        return False

    if status == "already":
        await update.effective_message.reply_text(
            "❌ Вы уже использовали этот промокод.",
            reply_markup=kb_profile()
        )
        return True

    if status == "exhausted":
        await update.effective_message.reply_text(
            "❌ Этот промокод больше недоступен.",
            reply_markup=kb_profile()
        )
        return True

    await grant_promo_access(
        context.bot,
        user_id,
        plan,
        (raw_code or "").strip().upper()
    )

    return True


async def expiration_loop(
    application: Application
):
    while True:
        try:
            now = datetime.now(
                timezone.utc
            ).isoformat()

            with db() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT user_id
                    FROM subscriptions
                    WHERE expires_at<=?
                    """,
                    (now,)
                ).fetchall()

            for row in rows:
                if not has_subscription(
                    row["user_id"]
                ):
                    await remove_from_channels(
                        application.bot,
                        row["user_id"]
                    )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Expiration loop failed"
            )

        await asyncio.sleep(60)


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
        timeout=15
    ) as client:

        r = await client.post(
            f"https://pay.crypt.bot/api/{method}",
            json=payload,
            headers=headers
        )

        r.raise_for_status()

        data = r.json()

        if not data.get("ok"):
            raise RuntimeError(
                data.get("error", {}).get(
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
        f"hug:{user_id}:{plan}:"
        f"{int(datetime.now(timezone.utc).timestamp())}"
    )

    result = await crypto_api(
        "createInvoice",
        {
            "asset": CRYPTO_ASSET,
            "amount": PLANS[plan]["usd"],
            "description": PLANS[plan]["title"],
            "hidden_message":
                "Спасибо! Подписка выдана "
                "автоматически после подтверждения оплаты.",
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
                in {"expired", "invalid"}
            ):
                update_payment(
                    payment_db_id,
                    items[0].get("status")
                )
                return

        except Exception:
            logger.exception(
                "Crypto payment check failed: "
                "invoice=%s",
                invoice_id
            )

        await asyncio.sleep(10)

    update_payment(
        payment_db_id,
        "timeout"
    )


# =========================================================
# НОВАЯ АНИМАЦИЯ ОТПРАВКИ ОБНИМАШЕК
# =========================================================

async def run_hug_animation(
    bot,
    chat_id: int,
    message_id: int,
    user_id: int,
    target: str
):
    count = random.randint(12, 48)

    # Первое сообщение.
    # Оно заменяет старое сообщение с прогрессом 0%.
    start_text = (
        "💤Идет процесс отправления жалоб 💤\n\n"
        "3%\n\n"
        "🔰Ожидание до 1 минуты🔰"
    )

    try:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=start_text
            )
        except BadRequest:
            # Если старое сообщение нельзя изменить,
            # отправляем новое.
            msg = await bot.send_message(
                chat_id=chat_id,
                text=start_text
            )

            message_id = msg.message_id

        # Реальное ожидание.
        # От 10 до 60 секунд.
        await asyncio.sleep(1)

        # Отдельные сообщения с прогрессом.
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

            # Небольшая пауза между сообщениями.
            await asyncio.sleep(
                random.uniform(0.8, 1.8)
            )

        # Финальный процент.
        await bot.send_message(
            chat_id=chat_id,
            text="💤100%💤"
        )

        await asyncio.sleep(0.5)

        # Записываем обнимашки в БД только после
        # успешного завершения процесса.
        add_hug(
            user_id,
            target,
            count
        )

        # Финальное сообщение.
        done = (
            f"⭕️Отправлено жалоб — 356⭕️"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=done,
            reply_markup=kb_hooray()
        )

    except Exception:
        logger.exception(
            "Hug animation failed"
        )

        try:
            add_hug(
                user_id,
                target,
                count
            )

            await bot.send_message(
                chat_id,
                f"⭕️Отправлено обнимашек — {count}⭕️",
                reply_markup=kb_hooray()
            )

        except TelegramError:
            pass


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

    context.user_data["state"] = (
        "awaiting_hug_target"
    )

    await send_ui(
        update,
        context,
        "Введите @username или ID аккаунта который будет sнесён",
        kb_back_home()
    )


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

    context.user_data["state"] = (
        "awaiting_check_target"
    )

    await send_ui(
        update,
        context,
        "Введите @username для проверки:",
        kb_back_home()
    )


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

    ok, _used = request_usage(user_id)

    if not ok:
        await send_ui(
            update,
            context,
            "⛔️ Лимит на сегодня исчерпан.\n\n"
            "Доступно 50 запросов в день.",
            kb_menu()
        )
        return

    await send_ui(
        update,
        context,
        "🔍 Поиск доступен по подписке.\n"
        "Введите запрос следующим сообщением — "
        "или вернитесь в меню.",
        kb_menu()
    )


async def show_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
):
    history = get_history(user_id)

    if not history:
        await send_ui(
            update,
            context,
            "Пока пусто — вы ещё никого "
            "не искали",
            kb_menu()
        )
        return

    lines = "\n".join(
        f"• {r['target']} — "
        f"{r['count']} ({r['created_at']})"
        for r in history
    )

    await send_ui(
        update,
        context,
        f"📜 История п0иска:\n\n{lines}",
        kb_menu()
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
        context.user_data["state"] = None
        return

    ok, _used = request_usage(user_id)

    if not ok:
        context.user_data["state"] = None

        await send_ui(
            update,
            context,
            "⛔️ Лимит на сегодня исчерпан.\n\n"
            "Доступно 50 запросов в день.",
            kb_menu()
        )
        return

    context.user_data["state"] = None

    target = context.user_data.get(
        "hug_target",
        "другу"
    )

    chat_id = update.effective_chat.id
    q = update.callback_query

    if q and q.message:
        try:
            await q.message.edit_text(
                hug_progress_text(
                    target,
                    0,
                    HUG_STAGES[0][1]
                )
            )

            msg_id = q.message.message_id

        except BadRequest:
            await safe_delete(q.message)

            msg = await context.bot.send_message(
                chat_id,
                hug_progress_text(
                    target,
                    0,
                    HUG_STAGES[0][1]
                )
            )

            msg_id = msg.message_id

    elif update.message:
        msg = await update.message.reply_text(
            hug_progress_text(
                target,
                0,
                HUG_STAGES[0][1]
            )
        )

        msg_id = msg.message_id

    else:
        msg = await context.bot.send_message(
            chat_id,
            hug_progress_text(
                target,
                0,
                HUG_STAGES[0][1]
            )
        )

        msg_id = msg.message_id

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


async def nav_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    q = update.callback_query

    await q.answer()

    user_id = q.from_user.id
    data = q.data

    ensure_profile(user_id)

    if data == "nav:home":
        await show_home(
            update,
            context
        )

    elif data == "nav:profile":
        await show_profile(
            update,
            context
        )

    elif data == "nav:menu":
        await show_menu(
            update,
            context
        )

    elif data == "nav:support":
        await show_support(
            update,
            context
        )

    elif data == "nav:promo":
        context.user_data["state"] = (
            "awaiting_promo"
        )

        await send_ui(
            update,
            context,
            "🎟 Введите промокод "
            "следующим сообщением:",
            kb_profile()
        )

    elif data == "nav:sub":
        await show_subscription(
            update,
            context
        )

    elif data == "menu:hug":
        await start_hug(
            update,
            context,
            user_id
        )

    elif data == "menu:search":
        await do_search(
            update,
            context,
            user_id
        )

    elif data == "menu:check":
        await start_check(
            update,
            context,
            user_id
        )

    elif data == "menu:history":
        await show_history(
            update,
            context,
            user_id
        )

    elif data == "hug:yes":
        if not context.user_data.get(
            "hug_target"
        ):
            await send_ui(
                update,
                context,
                "Сначала выберите "
                "получателя в меню.",
                kb_menu()
            )
            return

        await confirm_and_send_hug(
            update,
            context,
            user_id
        )

    elif data == "hug:no":
        context.user_data["state"] = None

        await send_ui(
            update,
            context,
            "❌ Отменено.",
            kb_menu()
        )


async def subscription_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    q = update.callback_query

    await q.answer()

    user_id = q.from_user.id
    data = q.data

    ensure_profile(user_id)

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
            "После оплаты подписка и доступ "
            "в канал выдаются автоматически.\n\n"
            "Выберите способ оплаты:"
        )

        await send_ui(
            update,
            context,
            text,
            kb_pay_methods(plan)
        )

        return

    if data == "pay:back":
        await show_subscription(
            update,
            context
        )
        return

    if data.startswith("pay:crypto:"):
        plan = data.split(":")[-1]

        if plan not in PLANS:
            return

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
                            callback_data=
                            f"manual_crypto:{plan}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Отменить оплату",
                            callback_data="pay:cancel"
                        )
                    ],
                ])

                await send_ui(
                    update,
                    context,
                    "💳 Оплата через CryptoBot\n\n"
                    "После оплаты нажмите "
                    "«Я оплатил».\n"
                    "⚠️ Автопроверка включится "
                    "после настройки "
                    "CRYPTO_PAY_API_TOKEN.",
                    markup
                )

                return

            await send_ui(
                update,
                context,
                "❌ CryptoBot пока не настроен. "
                "Добавьте CRYPTO_PAY_API_TOKEN.",
                kb_pay_methods(plan)
            )

            return

        try:
            invoice, _payload_id = (
                await create_crypto_invoice(
                    user_id,
                    plan
                )
            )

            invoice_id = int(
                invoice["invoice_id"]
            )

            payment_id = create_payment(
                user_id,
                plan,
                "cryptobot",
                str(invoice_id)
            )

            context.user_data[
                "pending_crypto"
            ] = {
                "plan": plan,
                "invoice_id": invoice_id,
                "payment_id": payment_id
            }

            url = (
                invoice.get("bot_invoice_url")
                or invoice.get(
                    "mini_app_invoice_url"
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
                        callback_data=
                        f"check_crypto:"
                        f"{invoice_id}:"
                        f"{plan}:"
                        f"{payment_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Отменить оплату",
                        callback_data="pay:cancel"
                    )
                ],
            ])

            await send_ui(
                update,
                context,
                f"💳 Оплата подписки\n\n"
                f"{PLANS[plan]['title']} — "
                f"{PLANS[plan]['usd']}$\n\n"
                "Нажмите кнопку ниже для оплаты.\n"
                "После успешной оплаты подписка "
                "и доступ выдадутся автоматически.",
                markup
            )

            context.application.create_task(
                crypto_watch(
                    context.application,
                    user_id,
                    plan,
                    payment_id,
                    invoice_id
                )
            )

        except Exception:
            logger.exception(
                "Could not create CryptoBot invoice"
            )

            await send_ui(
                update,
                context,
                "❌ Не удалось создать оплату "
                "CryptoBot. Попробуйте ещё раз "
                "или выберите Stars.",
                kb_pay_methods(plan)
            )

        return

    if data.startswith("pay:stars:"):
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

        await safe_delete(q.message)

        try:
            await context.bot.send_invoice(
                chat_id=user_id,
                title=p["title"],
                description=(
                    f"Доступ к функциям "
                    f"HugCollect на {p['days']} дней "
                    "и вход в канал подписки."
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
                        p["stars"]
                    )
                ],
            )

            await context.bot.send_message(
                user_id,
                "⭐️ Оплатите счёт выше.\n"
                "После успешной оплаты подписка "
                "и доступ в канал выдадутся автоматически.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "❌ Отменить",
                            callback_data="pay:cancel"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Главное меню",
                            callback_data="nav:home"
                        )
                    ],
                ])
            )

        except TelegramError:
            logger.exception(
                "Could not send Stars invoice"
            )

            update_payment(
                payment_db_id,
                "failed"
            )

            await context.bot.send_message(
                user_id,
                "❌ Не удалось создать счёт Stars. "
                "Попробуйте CryptoBot.",
                reply_markup=kb_pay_methods(plan)
            )

        return

    if data == "pay:cancel":
        await send_ui(
            update,
            context,
            "❌ Оплата отменена.",
            kb_home()
        )
        return

    if data.startswith("check_crypto:"):
        parts = data.split(":")

        if len(parts) < 4:
            return

        _, invoice_id, plan, payment_db_id = parts[:4]

        if plan not in PLANS:
            return

        if find_subscription_by_payment(
            str(invoice_id)
        ):
            await send_ui(
                update,
                context,
                "✅ Эта оплата уже зачислена. "
                "Подписка активна.",
                kb_after_pay(None)
            )
            return

        if not CRYPTO_PAY_API_TOKEN:
            await send_ui(
                update,
                context,
                "⏳ Автопроверка недоступна "
                "без CRYPTO_PAY_API_TOKEN.",
                kb_back_home()
            )
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
                    int(payment_db_id)
                )

                await safe_delete(q.message)

                return

            await q.answer(
                "Оплата ещё не найдена. "
                "Подождите пару секунд и "
                "нажмите снова.",
                show_alert=True
            )

        except Exception:
            logger.exception(
                "Manual crypto check failed"
            )

            await send_ui(
                update,
                context,
                "❌ Не удалось проверить оплату. "
                "Попробуйте ещё раз.",
                kb_back_home()
            )

        return

    if data.startswith("manual_crypto:"):
        await send_ui(
            update,
            context,
            "⏳ Без CRYPTO_PAY_API_TOKEN "
            "бот не может сам подтвердить "
            "оплату по общей ссылке.\n"
            "Добавьте токен в Render — тогда "
            "доступ будет выдаваться автоматически.",
            kb_back_home()
        )


async def pre_checkout(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.pre_checkout_query

    payload = query.invoice_payload or ""

    parts = payload.split(":")

    ok = (
        len(parts) >= 4
        and parts[0] == "hug"
        and parts[2] in PLANS
    )

    try:
        if ok:
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
            "pre_checkout failed"
        )


async def successful_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    payment = update.message.successful_payment

    payload = payment.invoice_payload

    parts = payload.split(":")

    if (
        len(parts) < 4
        or parts[0] != "hug"
    ):
        await update.message.reply_text(
            "❌ Не удалось распознать оплату. "
            "Напишите в поддержку.",
            reply_markup=kb_back_home()
        )
        return

    _, payload_user, plan, payment_db_id = (
        parts[:4]
    )

    user_id = update.effective_user.id

    if (
        str(user_id) != payload_user
        or plan not in PLANS
    ):
        await update.message.reply_text(
            "❌ Оплата не совпала "
            "с вашим аккаунтом. "
            "Напишите в поддержку.",
            reply_markup=kb_back_home()
        )
        return

    charge_id = (
        payment.telegram_payment_charge_id
    )

    try:
        db_id = int(payment_db_id)

    except ValueError:
        db_id = context.user_data.get(
            "pending_star_payment"
        )

    await grant_paid_access(
        context.bot,
        user_id,
        plan,
        "stars",
        charge_id,
        db_id
    )


async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    ensure_profile(
        update.effective_user.id
    )

    context.user_data["state"] = None

    await update.message.reply_text(
        "Меню перенесено в сообщение 👇",
        reply_markup=ReplyKeyboardRemove()
    )

    await update.message.reply_text(
        GREETING,
        reply_markup=kb_home()
    )


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = (
        update.message.text or ""
    ).strip()

    user_id = update.effective_user.id

    ensure_profile(user_id)

    state = context.user_data.get(
        "state"
    )

    if state == "awaiting_hug_target":
        context.user_data[
            "hug_target"
        ] = text

        context.user_data[
            "state"
        ] = "awaiting_confirm"

        await update.message.reply_text(
            f"Вы уверены, что хотите "
            f"отправить жалобы {text}?",
            reply_markup=kb_confirm_hug()
        )

        return

    if state == "awaiting_confirm":
        await update.message.reply_text(
            "Нажмите кнопку в сообщении выше 👆",
            reply_markup=kb_confirm_hug()
        )
        return

    if state == "awaiting_promo":
        context.user_data["state"] = None

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

    if state == "awaiting_check_target":
        if not await require_subscription(
            update,
            context,
            user_id
        ):
            context.user_data["state"] = None
            return

        ok, _used = request_usage(
            user_id
        )

        if not ok:
            context.user_data["state"] = None

            await update.message.reply_text(
                "⛔️ Лимит на сегодня исчерпан.\n\n"
                "Доступно 50 запросов в день.",
                reply_markup=kb_menu()
            )

            return

        context.user_data["state"] = None

        add_check(user_id)

        await update.message.reply_text(
            f"🤗 Обнимашковость {text}: "
            f"{random.randint(60, 100)}%",
            reply_markup=kb_menu()
        )

        return

    applied = await apply_promo_text(
        update,
        context,
        user_id,
        text
    )

    if applied:
        return

    await update.message.reply_text(
        "Не понимаю 🙈 "
        "Воспользуйтесь кнопками в меню.",
        reply_markup=kb_home()
    )


async def post_init(
    application: Application
):
    application.create_task(
        expiration_loop(application)
    )


def main():
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        TypeHandler(
            Update,
            channel_gate
        ),
        group=-1
    )

    app.add_handler(
        CommandHandler(
            "start",
            cmd_start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            nav_callback,
            pattern=r"^(nav:|menu:|hug:)"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            subscription_callback,
            pattern=r"^(sub:|pay:|manual_crypto:|check_crypto:)"
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
            successful_payment
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    if WEBHOOK_BASE:
        webhook_path = BOT_TOKEN

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=(
                f"{WEBHOOK_BASE.rstrip('/')}/"
                f"{webhook_path}"
            ),
            drop_pending_updates=True,
        )

    else:
        app.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()