import asyncio
import logging
import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# =========================
# CONFIG
# =========================
@dataclass(frozen=True)
class Config:
              # сюда свой ID админа
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    PAY_PHONE: str = os.getenv("PAY_PHONE", "")
    PAY_CARD: str = os.getenv("PAY_CARD", "")



cfg = Config()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# DB
# =========================
DB_PATH = str(Path(__file__).with_name("payments.db"))


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS access_periods (
            user_id INTEGER PRIMARY KEY,
            expires_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS receipt_waiting (user_id INTEGER PRIMARY KEY, pending_id INTEGER NOT NULL)")
    con.commit()
    con.close()


def set_access(user_id: int, expires_at: datetime) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO access_periods(user_id, expires_at)
        VALUES(?, ?)
        ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at
    """, (user_id, expires_at.isoformat()))
    con.commit()
    con.close()


def get_access_expiry(user_id: int) -> datetime | None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT expires_at FROM access_periods WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()

    if not row:
        return None

    try:
        value = datetime.fromisoformat(row[0])
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except Exception:
        return None


def create_pending(user_id: int, plan_key: str) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO pending_payments(user_id, plan_key, created_at, status)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, plan_key, datetime.now(timezone.utc).isoformat(), "pending"),
    )
    pid = cur.lastrowid
    con.commit()
    con.close()
    return int(pid)


def get_pending(pid: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT id, user_id, plan_key, created_at, status
        FROM pending_payments
        WHERE id=?
        """,
        (pid,),
    )
    row = cur.fetchone()
    con.close()
    return row


def set_pending_status(pid: int, status: str) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE pending_payments SET status=? WHERE id=?", (status, pid))
    con.commit()
    con.close()


# =========================
# ТАРИФЫ
# =========================
PLANS = {
    "half_1": {
        "days": 15,
        "price_rub": 750,
        "title": "с 1 по 15 число",
    },
    "half_2": {
        "days": 15,
        "price_rub": 750,
        "title": "с 15 по 30 число",
    },
    "full_month": {
        "days": 30,
        "price_rub": 1500,
        "title": "полный месяц",
    },
}


def approve_payment(pid: int) -> datetime | None:
    # One transaction prevents duplicate approval and lost renewals.
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT user_id, plan_key, status FROM pending_payments WHERE id=?", (pid,)).fetchone()
        if not row or row[2] != "pending" or row[1] not in PLANS:
            return None
        now = datetime.now(timezone.utc)
        old = con.execute("SELECT expires_at FROM access_periods WHERE user_id=?", (row[0],)).fetchone()
        base = now
        if old:
            expiry = datetime.fromisoformat(old[0])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            base = max(now, expiry)
        expiry = base + timedelta(days=PLANS[row[1]]["days"])
        con.execute("INSERT INTO access_periods VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at", (row[0], expiry.isoformat()))
        con.execute("UPDATE pending_payments SET status='approved' WHERE id=?", (pid,))
        return expiry


def reject_payment(pid: int) -> bool:
    with sqlite3.connect(DB_PATH, timeout=10) as con:
        return con.execute("UPDATE pending_payments SET status='rejected' WHERE id=? AND status='pending'", (pid,)).rowcount == 1

# user_id -> pending_id
class ReceiptWaiting:
    def __setitem__(self, user_id, pending_id):
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT INTO receipt_waiting VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET pending_id=excluded.pending_id", (user_id, pending_id))

    def get(self, user_id):
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute("SELECT pending_id FROM receipt_waiting WHERE user_id=?", (user_id,)).fetchone()
            return row[0] if row else None

    def pop(self, user_id, default=None):
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM receipt_waiting WHERE user_id=?", (user_id,))


WAITING_RECEIPT_FOR = ReceiptWaiting()

router = Router()


# =========================
# КЛАВИАТУРЫ
# =========================
def main_menu_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 С 1 по 15 число — 750₽")],
            [KeyboardButton(text="💳 С 15 по 30 число — 750₽")],
            [KeyboardButton(text="💳 Полный месяц — 1500₽")],
            [KeyboardButton(text="📌 Проверить оплату"), KeyboardButton(text="ℹ️ Реквизиты")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )


def buy_inline_kb(plan_key: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Перейти к оплате", callback_data=f"buy:{plan_key}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def paid_kb(pending_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Я оплатил(а) — отправить чек", callback_data=f"paid:{pending_id}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_confirm_kb(pending_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"admin_ok:{pending_id}")
    kb.button(text="❌ Отклонить", callback_data=f"admin_no:{pending_id}")
    kb.adjust(2)
    return kb.as_markup()


# =========================
# ТЕКСТЫ
# =========================
def get_welcome_text() -> str:
    return (
        "👋 Добро пожаловать, водитель!\n\n"
        "🚖 Выберите удобный вариант оплаты:\n\n"
        "• с 1 по 15 число — 750₽\n"
        "• с 15 по 30 число — 750₽\n"
        "• полный месяц — 1500₽\n\n"
        "После перевода нажмите нужную кнопку оплаты и отправьте чек.\n"
        "Админ проверит оплату и подтвердит доступ."
    )


def get_requisites_text() -> str:
    return (
        "💳 Реквизиты для оплаты:\n\n"
        f"📱 Телефон (СБП): {cfg.PAY_PHONE}\n"
        f"💳 Карта: {cfg.PAY_CARD}\n\n"
        "После оплаты выберите нужный вариант и отправьте чек."
    )


# =========================
# COMMANDS
# =========================
async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="check", description="Проверить оплату"),
        BotCommand(command="req", description="Реквизиты"),
    ]
    await bot.set_my_commands(commands)


# =========================
# HANDLERS
# =========================
@router.message(CommandStart())
async def start(message: Message):
    await message.answer(get_welcome_text(), reply_markup=main_menu_reply_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("📋 Главное меню:", reply_markup=main_menu_reply_kb())


@router.message(Command("check"))
async def cmd_check(message: Message):
    await show_payment_status(message)


@router.message(Command("req"))
async def cmd_req(message: Message):
    await message.answer(get_requisites_text(), reply_markup=main_menu_reply_kb())


@router.message(F.text == "ℹ️ Реквизиты")
async def requisites_btn(message: Message):
    await message.answer(get_requisites_text(), reply_markup=main_menu_reply_kb())


@router.message(F.text == "📌 Проверить оплату")
async def check_btn(message: Message):
    await show_payment_status(message)


async def show_payment_status(message: Message):
    user_id = message.from_user.id
    expiry = get_access_expiry(user_id)
    now = datetime.now(timezone.utc)

    if not expiry or expiry <= now:
        await message.answer(
            "❌ Оплата не активна.\nВыберите нужный вариант в меню.",
            reply_markup=main_menu_reply_kb()
        )
        return

    left = expiry - now
    days_left = left.days
    hours_left = left.seconds // 3600

    await message.answer(
        "✅ Оплата активна.\n"
        f"Действует до: {expiry.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')}\n"
        f"Осталось: {days_left} дн. {hours_left} ч.",
        reply_markup=main_menu_reply_kb()
    )


@router.message(F.text == "💳 С 1 по 15 число — 750₽")
async def choose_half_1(message: Message):
    await message.answer(
        "Вы выбрали оплату:\n\n"
        "📦 Период: с 1 по 15 число\n"
        "💵 Сумма: 750₽",
        reply_markup=buy_inline_kb("half_1")
    )


@router.message(F.text == "💳 С 15 по 30 число — 750₽")
async def choose_half_2(message: Message):
    await message.answer(
        "Вы выбрали оплату:\n\n"
        "📦 Период: с 15 по 30 число\n"
        "💵 Сумма: 750₽",
        reply_markup=buy_inline_kb("half_2")
    )


@router.message(F.text == "💳 Полный месяц — 1500₽")
async def choose_full_month(message: Message):
    await message.answer(
        "Вы выбрали оплату:\n\n"
        "📦 Период: полный месяц\n"
        "💵 Сумма: 1500₽",
        reply_markup=buy_inline_kb("full_month")
    )


@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery):
    await callback.message.answer("📋 Главное меню:", reply_markup=main_menu_reply_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery):
    plan_key = callback.data.split(":", 1)[1]
    plan = PLANS.get(plan_key)

    if not plan:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return

    pending_id = create_pending(callback.from_user.id, plan_key)

    text = (
        "💳 Оплата\n\n"
        f"📦 Период: {plan['title']}\n"
        f"💵 Сумма: {plan['price_rub']}₽\n\n"
        "Переведите деньги по реквизитам:\n"
        f"📱 Телефон (СБП): {cfg.PAY_PHONE}\n"
        f"💳 Карта: {cfg.PAY_CARD}\n\n"
        f"📝 Комментарий к переводу, если получится: Telegram ID {callback.from_user.id}\n\n"
        "После оплаты нажмите кнопку ниже и отправьте чек."
    )

    await callback.message.answer(text, reply_markup=paid_kb(pending_id))
    await callback.answer()


@router.callback_query(F.data.startswith("paid:"))
async def paid(callback: CallbackQuery):
    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    row = get_pending(pending_id)

    if not row:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    _, user_id, _, _, status = row

    if user_id != callback.from_user.id:
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    if status != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    WAITING_RECEIPT_FOR[callback.from_user.id] = pending_id

    await callback.message.answer(
        "✅ Теперь отправьте чек одним сообщением:\n\n"
        "• фото или скрин перевода\n"
        "или\n"
        "• текстом последние 4 цифры карты и время перевода"
    )
    await callback.answer()


@router.message(F.photo)
async def handle_receipt_photo(message: Message, bot: Bot):
    user_id = message.from_user.id
    pending_id = WAITING_RECEIPT_FOR.get(user_id)

    if not pending_id:
        return

    row = get_pending(pending_id)
    if not row or row[4] != "pending":
        WAITING_RECEIPT_FOR.pop(user_id, None)
        await message.answer("Заявка уже не активна. Нажмите /start")
        return

    plan_key = row[2]
    plan = PLANS.get(plan_key)
    if not plan:
        WAITING_RECEIPT_FOR.pop(user_id, None)
        await message.answer("Период не найден. Нажмите /start")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "—"

    caption = (
        "🧾 Заявка на подтверждение оплаты\n\n"
        f"👤 Водитель: {message.from_user.full_name}\n"
        f"🆔 user_id: {user_id}\n"
        f"🔗 username: {username}\n\n"
        f"📦 Период: {plan['title']}\n"
        f"💵 Сумма: {plan['price_rub']}₽\n"
        f"🧩 Заявка ID: {pending_id}\n\n"
        "Нажмите кнопку ниже:"
    )

    file_id = message.photo[-1].file_id

    try:
        await bot.send_photo(
            chat_id=cfg.ADMIN_ID,
            photo=file_id,
            caption=caption,
            reply_markup=admin_confirm_kb(pending_id),
        )
    except TelegramAPIError:
        logger.warning("Receipt delivery failed for request %s", pending_id)
        await message.answer("Не удалось отправить чек. Попробуйте ещё раз позже.")
        return

    WAITING_RECEIPT_FOR.pop(user_id, None)

    await message.answer(
        "✅ Чек отправлен админу.\nОжидайте подтверждения.",
        reply_markup=main_menu_reply_kb()
    )


@router.message(F.text)
async def handle_receipt_text(message: Message, bot: Bot):
    user_id = message.from_user.id
    pending_id = WAITING_RECEIPT_FOR.get(user_id)

    if not pending_id:
        return

    row = get_pending(pending_id)
    if not row or row[4] != "pending":
        WAITING_RECEIPT_FOR.pop(user_id, None)
        await message.answer("Заявка уже не активна. Нажмите /start")
        return

    plan_key = row[2]
    plan = PLANS.get(plan_key)
    if not plan:
        WAITING_RECEIPT_FOR.pop(user_id, None)
        await message.answer("Период не найден. Нажмите /start")
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "—"

    text = (
        "🧾 Заявка на подтверждение оплаты\n\n"
        f"👤 Водитель: {message.from_user.full_name}\n"
        f"🆔 user_id: {user_id}\n"
        f"🔗 username: {username}\n\n"
        f"📦 Период: {plan['title']}\n"
        f"💵 Сумма: {plan['price_rub']}₽\n"
        f"🧩 Заявка ID: {pending_id}\n\n"
        f"📝 Сообщение от водителя:\n{message.text}"
    )

    try:
        await bot.send_message(
            chat_id=cfg.ADMIN_ID,
            text=text,
            reply_markup=admin_confirm_kb(pending_id),
        )
    except TelegramAPIError:
        logger.warning("Receipt delivery failed for request %s", pending_id)
        await message.answer("Не удалось отправить чек. Попробуйте ещё раз позже.")
        return

    WAITING_RECEIPT_FOR.pop(user_id, None)

    await message.answer(
        "✅ Данные отправлены админу.\nОжидайте подтверждения.",
        reply_markup=main_menu_reply_kb()
    )


@router.callback_query(F.data.startswith("admin_ok:"))
async def admin_ok(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != cfg.ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    row = get_pending(pending_id)

    if not row:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    _, user_id, plan_key, _, status = row

    if status != "pending":
        await callback.answer("Уже обработано", show_alert=True)
        return

    plan = PLANS.get(plan_key)
    if not plan:
        await callback.answer("Период не найден", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    expires_at = approve_payment(pending_id)
    if expires_at is None:
        await callback.answer("Уже обработано", show_alert=True)
        return

    await bot.send_message(
        chat_id=user_id,
        text=(
            "✅ Оплата подтверждена!\n"
            f"Период активирован: {plan['title']}\n"
            f"Действует до: {expires_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')}"
        ),
        reply_markup=main_menu_reply_kb()
    )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Оплата подтверждена.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_no:"))
async def admin_no(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != cfg.ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        pending_id = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    row = get_pending(pending_id)

    if not row:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    _, user_id, _, _, status = row

    if status != "pending":
        await callback.answer("Уже обработано", show_alert=True)
        return

    if not reject_payment(pending_id):
        await callback.answer("Уже обработано", show_alert=True)
        return

    await bot.send_message(
        chat_id=user_id,
        text=(
            "❌ Оплата не подтверждена.\n"
            "Проверьте перевод и отправьте чек заново через меню."
        ),
        reply_markup=main_menu_reply_kb()
    )

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ Оплата отклонена.")
    await callback.answer()


# =========================
# MAIN
# =========================
async def main():
    if not cfg.BOT_TOKEN or cfg.ADMIN_ID <= 0 or not (cfg.PAY_PHONE or cfg.PAY_CARD):
        raise RuntimeError("Заполните BOT_TOKEN, ADMIN_ID и реквизиты в .env")

    init_db()

    bot = Bot(token=cfg.BOT_TOKEN)
    await set_bot_commands(bot)

    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
