import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hugbot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "@your_support_username")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_BASE = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
BASE_DIR = Path(__file__).resolve().parent

ASSET_CANDIDATES = [
    BASE_DIR / "assets",
    BASE_DIR / "HugCollectBot" / "assets",
    Path.cwd() / "assets",
    Path.cwd() / "HugCollectBot" / "assets",
]
ASSETS_DIR = next((p for p in ASSET_CANDIDATES if p.is_dir()), ASSET_CANDIDATES[0])
PROFILE_BANNER = ASSETS_DIR / "profile_banner.png"
MENU_BANNER = ASSETS_DIR / "menu_banner.png"

logger.info("BASE_DIR=%s", BASE_DIR)
logger.info("CWD=%s", Path.cwd())
logger.info("ASSETS_DIR=%s exists=%s", ASSETS_DIR, ASSETS_DIR.is_dir())
logger.info("PROFILE_BANNER=%s exists=%s", PROFILE_BANNER, PROFILE_BANNER.is_file())
logger.info("MENU_BANNER=%s exists=%s", MENU_BANNER, MENU_BANNER.is_file())

DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "hugcollect.db")))

# --- Subscription/payment settings ---
CRYPTO_PAY_API_TOKEN = os.environ.get("CRYPTO_PAY_API_TOKEN", "").strip()
CRYPTO_ASSET = os.environ.get("CRYPTO_ASSET", "USDT").strip().upper()

# Private channels where paid subscribers get access. Set at least one pair.
WEEKLY_CHANNEL_ID = os.environ.get("WEEKLY_CHANNEL_ID", "").strip()
MONTHLY_CHANNEL_ID = os.environ.get("MONTHLY_CHANNEL_ID", "").strip()
# Optional single-channel fallback.
SUBSCRIPTION_CHANNEL_ID = os.environ.get("SUBSCRIPTION_CHANNEL_ID", "").strip()

PLANS = {
    "week": {"title": "Недельная подписка", "days": 7, "usd": "5", "stars": 400, "channel": WEEKLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID},
    "month": {"title": "Месячная подписка", "days": 30, "usd": "9", "stars": 700, "channel": MONTHLY_CHANNEL_ID or SUBSCRIPTION_CHANNEL_ID},
}

# Existing CryptoBot link can be supplied as a fallback/manual payment link.
# It is NOT enough for automatic verification; automatic CryptoBot verification requires CRYPTO_PAY_API_TOKEN.
CRYPTO_FALLBACK_WEEK = os.environ.get("CRYPTO_FALLBACK_WEEK", "").strip()
CRYPTO_FALLBACK_MONTH = os.environ.get("CRYPTO_FALLBACK_MONTH", "").strip()

BTN_MAIN = "Главная"
BTN_PROFILE = "Личный кабинет"
BTN_MENU = "Меню"
BTN_SUPPORT = "Техническая поддержка"
BTN_BACK = "Вернуться на главную"
BTN_PROMO = "Ввести промокод"
BTN_SUB = "Оформить подписку"
BTN_HUG = "Обнять юзера 🤗"
BTN_SEARCH = "Поиск 👀"
BTN_CHECK = "Проверить получателя"
BTN_HISTORY = "История обнимашек"
BTN_YES = "Да, обнять! 🤗"
BTN_NO = "Нет, отмена"
BTN_HOORAY = "Ура!"

kb_start = ReplyKeyboardMarkup([[BTN_MAIN]], resize_keyboard=True)
kb_home = ReplyKeyboardMarkup([[BTN_PROFILE], [BTN_MENU], [BTN_SUPPORT]], resize_keyboard=True)
kb_profile = ReplyKeyboardMarkup([[BTN_PROMO], [BTN_SUB], [BTN_BACK]], resize_keyboard=True)
kb_menu = ReplyKeyboardMarkup([[BTN_HUG], [BTN_SEARCH, BTN_CHECK], [BTN_HISTORY], [BTN_BACK]], resize_keyboard=True)
kb_support = ReplyKeyboardMarkup([[BTN_BACK]], resize_keyboard=True)
kb_confirm = ReplyKeyboardMarkup([[BTN_YES], [BTN_NO]], resize_keyboard=True)
kb_hooray = ReplyKeyboardMarkup([[BTN_HOORAY]], resize_keyboard=True)

GREETING = "Привет, обнимашка! 🤗\nЧем я могу вам помочь?"
SUPPORT_TEXT = f"Если вы столкнулись с проблемой — напишите: {SUPPORT_USERNAME}"
PROMO_CODES = {}  # subscriptions are disabled by default; add promo codes here if needed


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 3,
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
        conn.commit()


def ensure_profile(user_id: int):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (user_id,))
        conn.commit()


def get_profile(user_id: int) -> dict:
    ensure_profile(user_id)
    with db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        sent = conn.execute("SELECT COUNT(*) FROM hugs WHERE user_id=?", (user_id,)).fetchone()[0]
    p = dict(row)
    p["sent"] = sent
    sub = get_active_subscription(user_id)
    p["sub"] = sub
    p["sub_active"] = sub is not None
    p["sub_days_left"] = max(0, (datetime.fromisoformat(sub["expires_at"]) - datetime.now(timezone.utc)).days) if sub else 0
    return p


def get_active_subscription(user_id: int):
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()
    return dict(row) if row else None


def has_subscription(user_id: int) -> bool:
    return get_active_subscription(user_id) is not None


def create_payment(user_id: int, plan: str, method: str, external_id: str | None, status: str = "pending") -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO payments(user_id,plan,method,external_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, plan, method, external_id, status, now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_payment(payment_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE payments SET status=?, updated_at=? WHERE id=?", (status, datetime.now(timezone.utc).isoformat(), payment_id))
        conn.commit()


def activate_subscription(user_id: int, plan: str, method: str, payment_id: str | None):
    now = datetime.now(timezone.utc)
    current = get_active_subscription(user_id)
    start = max(now, datetime.fromisoformat(current["expires_at"])) if current else now
    expires = start + timedelta(days=PLANS[plan]["days"])
    with db() as conn:
        conn.execute(
            "INSERT INTO subscriptions(user_id,plan,method,payment_id,starts_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, plan, method, payment_id, start.isoformat(), expires.isoformat(), now.isoformat()),
        )
        conn.commit()
    return expires


def request_usage(user_id: int) -> tuple[bool, int]:
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute("SELECT requests FROM daily_usage WHERE user_id=? AND usage_date=?", (user_id, today)).fetchone()
        used = int(row[0]) if row else 0
        if used >= 50:
            return False, used
        if row:
            conn.execute("UPDATE daily_usage SET requests=requests+1 WHERE user_id=? AND usage_date=?", (user_id, today))
        else:
            conn.execute("INSERT INTO daily_usage(user_id,usage_date,requests) VALUES(?,?,1)", (user_id, today))
        conn.commit()
        return True, used + 1


def usage_today(user_id: int) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        row = conn.execute("SELECT requests FROM daily_usage WHERE user_id=? AND usage_date=?", (user_id, today)).fetchone()
    return int(row[0]) if row else 0


def add_check(user_id: int):
    with db() as conn:
        conn.execute("UPDATE profiles SET checks=checks+1 WHERE user_id=?", (user_id,))
        conn.commit()


def add_hug(user_id: int, target: str, count: int):
    with db() as conn:
        conn.execute("INSERT INTO hugs(user_id,target,count,created_at) VALUES(?,?,?,?)", (user_id, target, count, datetime.now().strftime("%d.%m.%Y %H:%M")))
        conn.execute("UPDATE profiles SET warmth=MIN(1000,warmth+10) WHERE user_id=?", (user_id,))
        conn.commit()


def get_history(user_id: int, limit=10):
    with db() as conn:
        return conn.execute("SELECT target,count,created_at FROM hugs WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()


async def send_profile(update: Update, profile: dict):
    sub_text = "Оформлена" if profile["sub_active"] else "Не оформлена"
    expires = ""
    if profile["sub"]:
        expires = f"\n⏳ До — {datetime.fromisoformat(profile['sub']['expires_at']).strftime('%d.%m.%Y %H:%M')}"
    caption = (
        "👤 Личный кабинет\n\n"
        f"🤗 Уровень — {profile['level']}\n"
        f"💞 Теплота — {profile['warmth']}/1000\n"
        f"💬 Отправлено обнимашек — {profile['sent']}\n"
        f"👀 Проверок совместимости — {profile['checks']}\n\n"
        f"💎 Подписка — {sub_text}{expires}\n"
        f"📊 Запросов сегодня — {usage_today(update.effective_user.id)}/50"
    )
    if PROFILE_BANNER.is_file():
        try:
            with PROFILE_BANNER.open("rb") as photo:
                await update.message.reply_photo(photo=photo, caption=caption, reply_markup=kb_profile)
                return
        except Exception:
            logger.exception("Failed to send profile banner")
    await update.message.reply_text(caption, reply_markup=kb_profile)


async def send_menu(update: Update):
    if MENU_BANNER.is_file():
        try:
            with MENU_BANNER.open("rb") as photo:
                await update.message.reply_photo(photo=photo, caption="Выберите действие 👇", reply_markup=kb_menu)
                return
        except Exception:
            logger.exception("Failed to send menu banner")
    await update.message.reply_text("Выберите действие 👇", reply_markup=kb_menu)


def subscription_required_text():
    return "❌ Упс\n\n⭕️ У вас не имеется подписка\n\n❗️ Перейдите в личный кабинет для оформления подписки!"


async def require_subscription(update: Update, user_id: int) -> bool:
    if has_subscription(user_id):
        return True
    await update.message.reply_text(subscription_required_text(), reply_markup=kb_profile)
    return False


async def issue_channel_invite(bot, user_id: int, plan: str):
    channel = PLANS[plan]["channel"]
    if not channel:
        logger.warning("No channel configured for plan=%s", plan)
        return None
    try:
        member = await bot.get_chat_member(channel, user_id)
        if member.status in {"member", "administrator", "creator"}:
            return None
    except TelegramError:
        pass
    expires = get_active_subscription(user_id)
    expire_ts = None
    if expires:
        expire_ts = int(datetime.fromisoformat(expires["expires_at"]).timestamp())
    try:
        link = await bot.create_chat_invite_link(
            chat_id=channel,
            name=f"user {user_id} {plan}",
            member_limit=1,
            expire_date=expire_ts,
        )
        return link.invite_link
    except TelegramError:
        logger.exception("Could not create invite link for channel=%s user=%s", channel, user_id)
        return None


async def remove_from_channels(bot, user_id: int):
    channels = {p["channel"] for p in PLANS.values() if p["channel"]}
    for channel in channels:
        try:
            await bot.ban_chat_member(channel, user_id)
            await bot.unban_chat_member(channel, user_id, only_if_banned=True)
        except TelegramError as exc:
            logger.warning("Could not remove expired user=%s from channel=%s: %s", user_id, channel, exc)


async def expiration_loop(application: Application):
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with db() as conn:
                rows = conn.execute("SELECT DISTINCT user_id FROM subscriptions WHERE expires_at<=?", (now,)).fetchall()
            for row in rows:
                if not has_subscription(row["user_id"]):
                    await remove_from_channels(application.bot, row["user_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expiration loop failed")
        await asyncio.sleep(60)


async def crypto_api(method: str, payload: dict):
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"https://pay.crypt.bot/api/{method}", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", {}).get("name", "Crypto Pay API error"))
        return data["result"]


async def create_crypto_invoice(user_id: int, plan: str):
    payload_id = f"hug:{user_id}:{plan}:{int(datetime.now(timezone.utc).timestamp())}"
    result = await crypto_api("createInvoice", {
        "asset": CRYPTO_ASSET,
        "amount": PLANS[plan]["usd"],
        "description": PLANS[plan]["title"],
        "hidden_message": "Спасибо! Подписка будет выдана автоматически после подтверждения оплаты.",
        "payload": payload_id,
        "allow_comments": False,
        "allow_anonymous": False,
    })
    return result, payload_id


async def crypto_watch(application: Application, user_id: int, plan: str, payment_db_id: int, invoice_id: int):
    for _ in range(180):  # about 30 minutes
        try:
            result = await crypto_api("getInvoices", {"invoice_ids": str(invoice_id)})
            items = result.get("items", [])
            if items and items[0].get("status") == "paid":
                update_payment(payment_db_id, "paid")
                expires = activate_subscription(user_id, plan, "cryptobot", str(invoice_id))
                link = await issue_channel_invite(application.bot, user_id, plan)
                text = f"✅ Оплата получена!\n\n💎 Подписка: {PLANS[plan]['title']}\n⏳ Действует до: {expires.strftime('%d.%m.%Y %H:%M')}"
                if link:
                    text += "\n\n👇 Ваша ссылка для входа в канал:"
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Получить доступ", url=link)]])
                else:
                    markup = None
                await application.bot.send_message(user_id, text, reply_markup=markup)
                return
            if items and items[0].get("status") in {"expired", "invalid"}:
                update_payment(payment_db_id, items[0].get("status"))
                return
        except Exception:
            logger.exception("Crypto payment check failed: invoice=%s", invoice_id)
        await asyncio.sleep(10)
    update_payment(payment_db_id, "timeout")


async def show_subscription(update: Update):
    text = (
        "‼️ Доступ к основным функциям бота ‼️\n\n"
        "5️⃣0️⃣ запросов в день\n\n"
        "✔️ Защита пользователя\n\n"
        "1️⃣ Цена на неделю — 5$\n"
        "2️⃣ Цена на месяц — 9$\n\n"
        "⭐️ Stars ⭐️\n\n"
        "⭐️ Неделя — 400\n"
        "⭐️ Месяц — 700\n\n"
        "👇 Выберите срок подписки ниже 👇"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Неделя — 5$", callback_data="sub:week")],
        [InlineKeyboardButton("Месяц — 9$", callback_data="sub:month")],
        [InlineKeyboardButton("Главное меню", callback_data="sub:back")],
    ])
    await update.message.reply_text(text, reply_markup=markup)


async def subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data == "sub:back":
        await q.message.edit_text(GREETING)
        return
    if data.startswith("sub:") and data.count(":") == 1:
        plan = data.split(":")[1]
        if plan not in PLANS:
            return
        p = PLANS[plan]
        text = (
            f"💎 {p['title']}\n\n"
            f"⏳ Срок — {p['days']} дней\n"
            f"💵 Цена — {p['usd']}$\n"
            f"⭐️ Stars — {p['stars']}\n\n"
            "Выберите способ оплаты:"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("CryptoBot", callback_data=f"pay:crypto:{plan}")],
            [InlineKeyboardButton("⭐️ Stars", callback_data=f"pay:stars:{plan}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="pay:back")],
        ])
        await q.message.edit_text(text, reply_markup=markup)
        return

    if data == "pay:back":
        await q.message.edit_text("👇 Выберите срок подписки ниже 👇", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Неделя — 5$", callback_data="sub:week")],
            [InlineKeyboardButton("Месяц — 9$", callback_data="sub:month")],
            [InlineKeyboardButton("Главное меню", callback_data="sub:back")],
        ]))
        return

    if data.startswith("pay:crypto:"):
        plan = data.split(":")[-1]
        if not CRYPTO_PAY_API_TOKEN:
            fallback = CRYPTO_FALLBACK_WEEK if plan == "week" else CRYPTO_FALLBACK_MONTH
            if fallback:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатить в CryptoBot", url=fallback)],
                    [InlineKeyboardButton("Я оплатил", callback_data=f"manual_crypto:{plan}")],
                    [InlineKeyboardButton("❌ Отменить оплату", callback_data="pay:cancel")],
                ])
                await q.message.edit_text("💳 Оплата через CryptoBot\n\nПосле оплаты нажмите «Я оплатил».\n⚠️ Автоматическая проверка включится после настройки CRYPTO_PAY_API_TOKEN.", reply_markup=markup)
                return
            await q.message.edit_text("❌ CryptoBot пока не настроен. Добавьте CRYPTO_PAY_API_TOKEN в Render.")
            return
        try:
            invoice, payload_id = await create_crypto_invoice(user_id, plan)
            invoice_id = int(invoice["invoice_id"])
            payment_id = create_payment(user_id, plan, "cryptobot", str(invoice_id))
            url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url")
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оплатить", url=url)],
                [InlineKeyboardButton("❌ Отменить оплату", callback_data="pay:cancel")],
            ])
            await q.message.edit_text(
                f"💳 Оплата подписки\n\n{PLANS[plan]['title']} — {PLANS[plan]['usd']}$\n\nНажмите кнопку ниже для оплаты.\nПосле успешной оплаты подписка выдастся автоматически.",
                reply_markup=markup,
            )
            context.application.create_task(crypto_watch(context.application, user_id, plan, payment_id, invoice_id))
        except Exception:
            logger.exception("Could not create CryptoBot invoice")
            await q.message.edit_text("❌ Не удалось создать оплату CryptoBot. Попробуйте ещё раз или выберите Stars.")
        return

    if data.startswith("pay:stars:"):
        plan = data.split(":")[-1]
        p = PLANS[plan]
        payment_db_id = create_payment(user_id, plan, "stars", None)
        context.user_data["pending_star_payment"] = payment_db_id
        try:
            await q.message.delete()
        except TelegramError:
            pass
        await context.bot.send_invoice(
            chat_id=user_id,
            title=p["title"],
            description=f"Доступ к функциям HugCollect на {p['days']} дней.",
            payload=f"hug:{user_id}:{plan}:{payment_db_id}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(p["title"], p["stars"])],
        )
        await context.bot.send_message(user_id, "⭐️ После успешной оплаты подписка будет выдана автоматически.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="pay:cancel")]]))
        return

    if data == "pay:cancel":
        await q.message.edit_text("❌ Оплата отменена, вы возвращены на главную.")
        return

    if data.startswith("manual_crypto:"):
        await q.message.edit_text("⏳ Если вы оплатили по ссылке CryptoBot, автоматическая проверка недоступна без API-токена. Добавьте CRYPTO_PAY_API_TOKEN в Render — тогда бот будет проверять оплату сам.")


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    try:
        await query.answer(ok=True)
    except TelegramError:
        logger.exception("pre_checkout failed")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    parts = payload.split(":")
    if len(parts) < 4 or parts[0] != "hug":
        return
    _, payload_user, plan, payment_db_id = parts[:4]
    user_id = update.effective_user.id
    if str(user_id) != payload_user or plan not in PLANS:
        return
    update_payment(int(payment_db_id), "paid")
    expires = activate_subscription(user_id, plan, "stars", payment.telegram_payment_charge_id)
    link = await issue_channel_invite(context.bot, user_id, plan)
    text = f"⭐️ Оплата получена!\n\n💎 {PLANS[plan]['title']}\n⏳ Действует до: {expires.strftime('%d.%m.%Y %H:%M')}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Получить доступ к каналу", url=link)]]) if link else None
    await update.message.reply_text(text, reply_markup=markup)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_profile(update.effective_user.id)
    context.user_data["state"] = None
    await update.message.reply_text(GREETING, reply_markup=kb_home)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    profile = get_profile(user_id)
    state = context.user_data.get("state")

    if state == "awaiting_hug_target":
        context.user_data["hug_target"] = text
        context.user_data["state"] = "awaiting_confirm"
        await update.message.reply_text(f"Вы уверены, что хотите отправить обнимашки {text}? 🤗", reply_markup=kb_confirm)
        return

    if state == "awaiting_confirm":
        if text == BTN_YES:
            if not await require_subscription(update, user_id):
                context.user_data["state"] = None
                return
            ok, used = request_usage(user_id)
            if not ok:
                context.user_data["state"] = None
                await update.message.reply_text("⛔️ Лимит на сегодня исчерпан.\n\nДоступно 50 запросов в день.", reply_markup=kb_menu)
                return
            context.user_data["state"] = None
            target = context.user_data.get("hug_target", "другу")
            msg = await update.message.reply_text("😴 Идёт процесс отправки обнимашек 😴\n0%", reply_markup=ReplyKeyboardRemove())
            context.application.create_task(run_hug_animation(context.bot, update.effective_chat.id, msg.message_id, user_id, target), update=update)
        elif text == BTN_NO:
            context.user_data["state"] = None
            await update.message.reply_text("❌ Отменено.", reply_markup=kb_menu)
        return

    if state == "awaiting_promo":
        context.user_data["state"] = None
        await update.message.reply_text("❌ Промокоды сейчас отключены.", reply_markup=kb_profile)
        return

    if state == "awaiting_check_target":
        if not await require_subscription(update, user_id):
            context.user_data["state"] = None
            return
        ok, used = request_usage(user_id)
        if not ok:
            context.user_data["state"] = None
            await update.message.reply_text("⛔️ Лимит на сегодня исчерпан.\n\nДоступно 50 запросов в день.", reply_markup=kb_menu)
            return
        context.user_data["state"] = None
        add_check(user_id)
        await update.message.reply_text(f"🤗 Обнимашковость {text}: {random.randint(60,100)}%", reply_markup=kb_menu)
        return

    if text in (BTN_MAIN, BTN_BACK, BTN_HOORAY):
        context.user_data["state"] = None
        await update.message.reply_text(GREETING, reply_markup=kb_home)
    elif text == BTN_PROFILE:
        await send_profile(update, profile)
    elif text == BTN_MENU:
        await send_menu(update)
    elif text == BTN_SUPPORT:
        await update.message.reply_text(SUPPORT_TEXT, reply_markup=kb_support)
    elif text == BTN_PROMO:
        await update.message.reply_text("Промокоды сейчас отключены.", reply_markup=kb_profile)
    elif text == BTN_SUB:
        await show_subscription(update)
    elif text == BTN_HUG:
        if not await require_subscription(update, user_id):
            return
        context.user_data["state"] = "awaiting_hug_target"
        await update.message.reply_text("Введите @username или ID аккаунта, которому отправим виртуальные обнимашки 🤗", reply_markup=ReplyKeyboardRemove())
    elif text == BTN_SEARCH:
        if not await require_subscription(update, user_id):
            return
        ok, used = request_usage(user_id)
        if not ok:
            await update.message.reply_text("⛔️ Лимит на сегодня исчерпан.\n\nДоступно 50 запросов в день.", reply_markup=kb_menu)
            return
        await update.message.reply_text("🔍 Поиск доступен по подписке. Введите запрос.", reply_markup=kb_menu)
    elif text == BTN_CHECK:
        if not await require_subscription(update, user_id):
            return
        context.user_data["state"] = "awaiting_check_target"
        await update.message.reply_text("Введите @username для проверки:", reply_markup=ReplyKeyboardRemove())
    elif text == BTN_HISTORY:
        history = get_history(user_id)
        if not history:
            await update.message.reply_text("Пока пусто — вы ещё никого не обнимали 🤗", reply_markup=kb_menu)
        else:
            await update.message.reply_text("История обнимашек:\n\n" + "\n".join(f"• {r['target']} — {r['count']} ({r['created_at']})" for r in history), reply_markup=kb_menu)
    else:
        await update.message.reply_text("Не понимаю 🙈 Воспользуйтесь кнопками ниже.", reply_markup=kb_home)


async def post_init(application: Application):
    application.create_task(expiration_loop(application))


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(subscription_callback, pattern=r"^(sub:|pay:|manual_crypto:)"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if WEBHOOK_BASE:
        webhook_path = BOT_TOKEN
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=webhook_path, webhook_url=f"{WEBHOOK_BASE.rstrip('/')}/{webhook_path}", drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
