import asyncio
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- НАСТРОЙКИ ---
logging.basicConfig(level=logging.INFO)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан! Создай файл .env с BOT_TOKEN=твой_токен")

# Владелец бота — всегда имеет доступ к админке
OWNER_ID = int(os.getenv("ADMIN_OWNER", "759391249"))

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БД ---
conn = sqlite3.connect("appointments.db", check_same_thread=False)
cursor = conn.cursor()


def db_execute(query, params=(), fetch=False, one=False):
    try:
        cursor.execute(query, params)
        conn.commit()
        if fetch:
            return cursor.fetchone() if one else cursor.fetchall()
    except Exception as e:
        logging.error(f"DB error: {e}")
        return None


def db_init():
    cursor.execute("""CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        name TEXT,
        phone TEXT,
        date TEXT,
        time TEXT,
        services TEXT,
        total_price INTEGER,
        reminded INTEGER DEFAULT 0
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price INTEGER,
        duration INTEGER DEFAULT 60
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY,
        work_start TEXT,
        work_end TEXT,
        slot_interval INTEGER
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS days_off (
        date TEXT PRIMARY KEY
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS blocked_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        time TEXT,
        UNIQUE(date, time)
    )""")

    cursor.execute("""CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        channel_title TEXT,
        channel_link TEXT
    )""")

    cursor.execute("INSERT OR IGNORE INTO settings VALUES (1, '10:00', '18:00', 60)")

    # Владелец всегда добавляется в админы автоматически
    cursor.execute("INSERT OR IGNORE INTO admins VALUES (?)", (OWNER_ID,))

    # Миграция: добавляем username если колонки ещё нет
    try:
        cursor.execute("ALTER TABLE appointments ADD COLUMN username TEXT")
    except Exception:
        pass  # Колонка уже существует

    conn.commit()


db_init()


# --- СОСТОЯНИЯ ---
class Booking(StatesGroup):
    name = State()
    phone = State()


class AdminService(StatesGroup):
    name = State()
    price = State()


class AdminSettings(StatesGroup):
    work_start = State()
    work_end = State()
    slot_interval = State()


class AdminBroadcast(StatesGroup):
    message = State()


class BlockSlot(StatesGroup):
    date = State()


class AdminChannel(StatesGroup):
    waiting = State()


# --- ПРОВЕРКА ПОДПИСКИ ---
async def check_subscriptions(user_id: int) -> list:
    """Возвращает список каналов на которые пользователь НЕ подписан"""
    channels = db_execute("SELECT channel_id, channel_title, channel_link FROM required_channels", fetch=True) or []
    not_subscribed = []
    for channel_id, title, link in channels:
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subscribed.append((title, link))
        except Exception:
            # Если бот не админ канала — пропускаем проверку этого канала
            pass
    return not_subscribed


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_admin(user_id):
    return db_execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,), fetch=True, one=True)


def get_services():
    return db_execute("SELECT id, name, price FROM services", fetch=True) or []


def generate_services_kb(selected=[]):
    buttons = []
    for s_id, name, price in get_services():
        mark = "✅ " if s_id in selected else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{name} — {price}₽",
            callback_data=f"toggle_{s_id}"
        )])
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="done")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_booking")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- СТАРТ ---
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Записаться", callback_data="start_booking")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_appointments")],
        [InlineKeyboardButton(text="📖 Помощь", callback_data="help_inline")]
    ])
    await message.answer(
        "👋 Привет! Я бот для записи на услуги.\n\nВыбери действие:",
        reply_markup=kb
    )


# --- МОИ ЗАПИСИ ---
@dp.callback_query(F.data == "my_appointments")
async def my_appointments(callback: types.CallbackQuery):
    rows = db_execute(
        """SELECT id, date, time, services, total_price 
           FROM appointments 
           WHERE user_id=? 
           ORDER BY date DESC, time DESC 
           LIMIT 5""",
        (callback.from_user.id,),
        fetch=True
    )

    if not rows:
        await callback.answer("У тебя нет записей", show_alert=True)
        return

    text = "📋 Твои записи:\n\n"
    buttons = []
    for row in rows:
        appt_id, date, time, services, price = row
        text += f"📆 {date} в {time}\n{services} — {price}₽\n\n"
        buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить {date} {time}",
            callback_data=f"cancel_appt_{appt_id}"
        )])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# --- ОТМЕНА ЗАПИСИ ПОЛЬЗОВАТЕЛЕМ ---
@dp.callback_query(F.data.startswith("cancel_appt_"))
async def cancel_appointment(callback: types.CallbackQuery):
    appt_id = int(callback.data.split("_")[2])

    appt = db_execute(
        "SELECT user_id, date, time FROM appointments WHERE id=?",
        (appt_id,),
        fetch=True, one=True
    )

    if not appt:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    if appt[0] != callback.from_user.id:
        await callback.answer("Это не твоя запись", show_alert=True)
        return

    db_execute("DELETE FROM appointments WHERE id=?", (appt_id,))

    # Уведомить админов
    admins = db_execute("SELECT user_id FROM admins", fetch=True) or []
    for a in admins:
        try:
            await bot.send_message(a[0], f"⚠️ Отмена записи!\n{appt[1]} в {appt[2]}")
        except Exception:
            pass

    await callback.answer("Запись отменена ✅", show_alert=True)
    await my_appointments(callback)


# --- НАЗАД НА СТАРТ ---
@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Записаться", callback_data="start_booking")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_appointments")]
    ])
    await callback.message.edit_text("👋 Выбери действие:", reply_markup=kb)


@dp.callback_query(F.data == "help_inline")
async def help_inline(callback: types.CallbackQuery):
    if is_admin(callback.from_user.id):
        text = (
            "📖 Инструкция для администратора\n\n"
            "🔧 Команды:\n"
            "/admin — открыть админ-панель\n"
            "/add_admin [ID] — добавить администратора\n"
            "/help — показать инструкцию\n\n"
            "📋 Записи — просмотр и удаление записей\n"
            "📊 Статистика — выручка за день/неделю/всё время\n"
            "🛠 Услуги — добавление и удаление услуг\n"
            "🚫 Выходные — блокировка целых дней для записи\n"
            "🔒 Блокировка времени — закрыть конкретные слоты в конкретный день\n"
            "📣 Рассылка — сообщение всем клиентам\n"
            "📢 Каналы для подписки — добавь каналы на которые клиент обязан подписаться перед записью. Бот должен быть администратором каждого канала!\n"
            "⚙️ Настройки — рабочие часы и интервал слотов\n"
            "💬 В уведомлении о записи приходит @username клиента для связи\n"
            "⏰ Напоминания клиентам отправляются автоматически за 1 час"
        )
    else:
        text = (
            "📖 Как пользоваться ботом\n\n"
            "📅 Записаться — выбери дату, время и услуги\n"
            "📋 Мои записи — посмотри записи или отмени ненужную\n\n"
            "Если есть вопросы — свяжись с администратором."
        )
    await callback.message.answer(text)
    await callback.answer()


# --- ВЫБОР ДАТЫ ---
@dp.callback_query(F.data == "start_booking")
async def choose_date(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()

    if not get_services():
        await callback.answer("Услуги ещё не добавлены. Обратитесь к администратору.", show_alert=True)
        return

    # Проверка подписки
    not_subscribed = await check_subscriptions(callback.from_user.id)
    if not_subscribed:
        buttons = []
        for title, link in not_subscribed:
            url = f"https://t.me/{link.lstrip('@')}" if link.startswith("@") else link
            buttons.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
        buttons.append([InlineKeyboardButton(text="✅ Я подписался, проверить", callback_data="start_booking")])
        text = (
            "❗️ Для записи нужно подписаться на канал(ы):\n\n"
            + "\n".join(f"• {t}" for t, _ in not_subscribed)
        )
        try:
            await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception:
            await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    today = datetime.now()
    buttons = []
    for i in range(7):
        day = today + timedelta(days=i)
        date_str = day.strftime('%d.%m')
        is_day_off = db_execute("SELECT 1 FROM days_off WHERE date=?", (date_str,), fetch=True, one=True)
        if is_day_off:
            label = f"{date_str} 🚫"
            buttons.append([InlineKeyboardButton(text=label, callback_data="day_off")])
        else:
            label = date_str + (" (сегодня)" if i == 0 else "")
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"date_{date_str}")])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_start")])
    await callback.message.edit_text(
        "📅 Выбери дату:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# --- ВЫБОР ВРЕМЕНИ ---
@dp.callback_query(F.data.startswith("date_"))
async def choose_time(callback: types.CallbackQuery, state: FSMContext):
    date = callback.data.split("_")[1]
    await state.update_data(date=date)

    result = db_execute("SELECT work_start, work_end, slot_interval FROM settings WHERE id=1", fetch=True, one=True)
    if not result:
        await callback.answer("Ошибка настроек", show_alert=True)
        return

    start, end, interval = result
    curr = datetime.strptime(start, "%H:%M")
    end_dt = datetime.strptime(end, "%H:%M")

    buttons = []
    while curr < end_dt:
        t = curr.strftime("%H:%M")
        busy = db_execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=?",
            (date, t), fetch=True, one=True
        )
        blocked = db_execute(
            "SELECT 1 FROM blocked_slots WHERE date=? AND time=?",
            (date, t), fetch=True, one=True
        )
        unavailable = busy or blocked
        buttons.append(InlineKeyboardButton(
            text=f"{t} ❌" if unavailable else t,
            callback_data="busy" if unavailable else f"time_{t}"
        ))
        curr += timedelta(minutes=interval)

    kb = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="start_booking")])
    await callback.message.edit_text(
        f"🕐 Выбери время на {date}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@dp.callback_query(F.data == "busy")
async def busy_slot(callback: types.CallbackQuery):
    await callback.answer("Это время уже занято", show_alert=True)


@dp.callback_query(F.data == "day_off")
async def day_off_slot(callback: types.CallbackQuery):
    await callback.answer("Этот день выходной 🚫", show_alert=True)


# --- ВЫБОР УСЛУГ ---
@dp.callback_query(F.data.startswith("time_"))
async def choose_services(callback: types.CallbackQuery, state: FSMContext):
    time = callback.data.split("_")[1]
    await state.update_data(time=time, selected=[])
    await callback.message.edit_text(
        "🛠 Выбери услуги (можно несколько):",
        reply_markup=generate_services_kb()
    )


@dp.callback_query(F.data.startswith("toggle_") & ~F.data.startswith("toggle_day_off_"))
async def toggle(callback: types.CallbackQuery, state: FSMContext):
    s_id = int(callback.data.split("_")[1])
    data = await state.get_data()
    selected = data.get("selected", [])

    if s_id in selected:
        selected.remove(s_id)
    else:
        selected.append(s_id)

    await state.update_data(selected=selected)
    await callback.message.edit_reply_markup(reply_markup=generate_services_kb(selected))


@dp.callback_query(F.data == "done")
async def ask_name(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected"):
        await callback.answer("Выбери хотя бы одну услугу", show_alert=True)
        return

    await state.set_state(Booking.name)
    await callback.message.answer("👤 Введи своё имя:", reply_markup=ReplyKeyboardRemove())


@dp.callback_query(F.data == "cancel_booking")
async def cancel_booking(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await back_to_start(callback, state)


# --- ИМЯ ---
@dp.message(Booking.name)
async def get_name(message: types.Message, state: FSMContext):
    if len(message.text.strip()) < 2:
        await message.answer("Введи нормальное имя 😊")
        return

    await state.update_data(name=message.text.strip())
    await state.set_state(Booking.phone)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("📞 Введи номер телефона или отправь контакт:", reply_markup=kb)


# --- ТЕЛЕФОН ---
@dp.message(Booking.phone)
async def get_phone(message: types.Message, state: FSMContext):
    raw = message.contact.phone_number if message.contact else message.text
    phone = "".join(filter(str.isdigit, str(raw)))

    if len(phone) < 10:
        await message.answer("❌ Неверный номер. Попробуй ещё раз:")
        return

    data = await state.get_data()

    exists = db_execute(
        "SELECT 1 FROM appointments WHERE user_id=? AND date=? AND time=?",
        (message.from_user.id, data['date'], data['time']),
        fetch=True, one=True
    )

    if exists:
        await message.answer("⚠️ Ты уже записан на это время!", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    await state.update_data(phone=phone)

    services_info = []
    total = 0
    for s_id in data['selected']:
        row = db_execute("SELECT name, price FROM services WHERE id=?", (s_id,), fetch=True, one=True)
        if row:
            services_info.append(f"  • {row[0]} — {row[1]}₽")
            total += row[1]

    text = (
        f"📋 Подтверди запись:\n\n"
        f"📆 Дата: {data['date']}\n"
        f"🕐 Время: {data['time']}\n"
        f"👤 Имя: {data['name']}\n"
        f"📞 Телефон: +{phone}\n"
        f"🛠 Услуги:\n" + "\n".join(services_info) + f"\n\n💰 Итого: {total}₽"
    )

    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Подтверди запись:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_booking")]
        ])
    )


# --- СОХРАНЕНИЕ ---
@dp.callback_query(F.data == "confirm_yes")
async def save(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if not data.get("selected") or not data.get("name") or not data.get("phone"):
        await callback.answer("Ошибка данных. Начни заново.", show_alert=True)
        await state.clear()
        return

    services, total = [], 0
    for s_id in data['selected']:
        row = db_execute("SELECT name, price FROM services WHERE id=?", (s_id,), fetch=True, one=True)
        if row:
            services.append(row[0])
            total += row[1]

    username = callback.from_user.username
    username_str = f"@{username}" if username else "нет username"

    db_execute(
        """INSERT INTO appointments (user_id, username, name, phone, date, time, services, total_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (callback.from_user.id, username_str, data['name'], data['phone'],
         data['date'], data['time'], ", ".join(services), total)
    )

    admins = db_execute("SELECT user_id FROM admins", fetch=True) or []
    for a in admins:
        try:
            await bot.send_message(
                a[0],
                f"🆕 Новая запись!\n"
                f"👤 {data['name']} | 📞 +{data['phone']}\n"
                f"💬 Telegram: {username_str}\n"
                f"📆 {data['date']} в {data['time']}\n"
                f"🛠 {', '.join(services)}\n"
                f"💰 {total}₽"
            )
        except Exception:
            pass

    await callback.message.edit_text(
        f"✅ Ты записан!\n\n📆 {data['date']} в {data['time']}\n"
        f"Ждём тебя! 🎉"
    )
    await state.clear()


# =====================
# АДМИН-ПАНЕЛЬ
# =====================

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        return

    await message.answer(
        "🔧 Админ-панель:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Записи", callback_data="admin_orders")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🛠 Услуги", callback_data="admin_services")],
            [InlineKeyboardButton(text="🚫 Выходные дни", callback_data="admin_days_off")],
            [InlineKeyboardButton(text="🔒 Блокировка времени", callback_data="admin_block_slots")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="📢 Каналы для подписки", callback_data="admin_channels")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings")],
            [InlineKeyboardButton(text="📖 Помощь", callback_data="help_inline")]
        ])
    )


# --- ЗАПИСИ ---
@dp.callback_query(F.data == "admin_orders")
async def admin_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    today = datetime.now()
    buttons = []
    for i in range(7):
        day = today + timedelta(days=i)
        date_str = day.strftime("%d.%m")
        label = date_str + (" (сегодня)" if i == 0 else "")
        count = db_execute(
            "SELECT COUNT(*) FROM appointments WHERE date=?",
            (date_str,), fetch=True, one=True
        )
        cnt = count[0] if count else 0
        if cnt > 0:
            label += f" [{cnt}]"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"orders_date_{date_str}")])

    buttons.append([InlineKeyboardButton(text="📋 Все записи подряд", callback_data="orders_all")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])

    await callback.message.edit_text(
        "📅 Выбери дату для просмотра записей:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


def format_appointment_card(r, index=None):
    appt_id, date, time, name, phone, username, services, price = r
    uname = username if username else "не указан"
    prefix = f"#{index} " if index else ""
    return (
        f"{'─' * 20}\n"
        f"{prefix}📆 {date} в {time}\n"
        f"👤 Имя: {name}\n"
        f"📞 Телефон: +{phone}\n"
        f"💬 Telegram: {uname}\n"
        f"🛠 Услуги: {services}\n"
        f"💰 Стоимость: {price}₽\n"
    )


@dp.callback_query(F.data.startswith("orders_date_"))
async def admin_orders_by_date(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    date_str = callback.data.replace("orders_date_", "")

    rows = db_execute(
        """SELECT id, date, time, name, phone, username, services, total_price
           FROM appointments WHERE date=? ORDER BY time ASC""",
        (date_str,), fetch=True
    )

    if not rows:
        await callback.answer(f"На {date_str} записей нет", show_alert=True)
        return

    text = f"📋 Записи на {date_str}:\n\n"
    buttons = []
    for i, r in enumerate(rows, 1):
        text += format_appointment_card(r, index=i)
        buttons.append([InlineKeyboardButton(
            text=f"❌ Удалить запись #{i} ({r[2]})",
            callback_data=f"admin_del_{r[0]}"
        )])

    buttons.append([InlineKeyboardButton(text="🔙 К датам", callback_data="admin_orders")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data == "orders_all")
async def admin_orders_all(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    rows = db_execute(
        """SELECT id, date, time, name, phone, username, services, total_price
           FROM appointments 
           ORDER BY 
               CAST(SUBSTR(date, 4, 2) AS INTEGER) ASC,
               CAST(SUBSTR(date, 1, 2) AS INTEGER) ASC,
               time ASC
           LIMIT 20""",
        fetch=True
    )

    if not rows:
        await callback.answer("Записей нет", show_alert=True)
        return

    text = "📋 Все ближайшие записи:\n\n"
    buttons = []
    for i, r in enumerate(rows, 1):
        text += format_appointment_card(r, index=i)
        buttons.append([InlineKeyboardButton(
            text=f"❌ Удалить запись #{i} ({r[1]} {r[2]})",
            callback_data=f"admin_del_{r[0]}"
        )])

    buttons.append([InlineKeyboardButton(text="🔙 К датам", callback_data="admin_orders")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("admin_del_"))
async def admin_delete(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    appt_id = int(callback.data.split("_")[2])
    appt = db_execute("SELECT user_id, date, time FROM appointments WHERE id=?", (appt_id,), fetch=True, one=True)

    if appt:
        db_execute("DELETE FROM appointments WHERE id=?", (appt_id,))
        try:
            await bot.send_message(
                appt[0],
                f"⚠️ Твоя запись на {appt[1]} в {appt[2]} была отменена администратором."
            )
        except Exception:
            pass
        await callback.answer("Запись удалена ✅", show_alert=True)
    else:
        await callback.answer("Запись не найдена", show_alert=True)


# --- УПРАВЛЕНИЕ УСЛУГАМИ ---
@dp.callback_query(F.data == "admin_services")
async def admin_services(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    services = get_services()
    text = "🛠 Услуги:\n\n"

    buttons = []
    if services:
        for s_id, name, price in services:
            text += f"• {name} — {price}₽\n"
            buttons.append([InlineKeyboardButton(
                text=f"❌ Удалить «{name}»",
                callback_data=f"del_service_{s_id}"
            )])
    else:
        text += "Услуг пока нет\n"

    buttons.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="add_service")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "add_service")
async def add_service_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminService.name)
    await callback.message.answer("✏️ Введи название услуги:")


@dp.message(AdminService.name)
async def add_service_name(message: types.Message, state: FSMContext):
    await state.update_data(service_name=message.text.strip())
    await state.set_state(AdminService.price)
    await message.answer("💰 Введи цену (только цифры):")


@dp.message(AdminService.price)
async def add_service_price(message: types.Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введи число:")
        return

    data = await state.get_data()
    db_execute("INSERT INTO services (name, price) VALUES (?, ?)", (data['service_name'], price))
    await state.clear()
    await message.answer(f"✅ Услуга «{data['service_name']}» за {price}₽ добавлена!")


@dp.callback_query(F.data.startswith("del_service_"))
async def del_service(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    s_id = int(callback.data.split("_")[2])
    db_execute("DELETE FROM services WHERE id=?", (s_id,))
    await callback.answer("Услуга удалена ✅", show_alert=True)
    await admin_services(callback)


# --- НАСТРОЙКИ ---
@dp.callback_query(F.data == "admin_settings")
async def admin_settings(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    result = db_execute("SELECT work_start, work_end, slot_interval FROM settings WHERE id=1", fetch=True, one=True)
    start, end, interval = result

    await callback.message.edit_text(
        f"⚙️ Текущие настройки:\n\n"
        f"🕐 Начало работы: {start}\n"
        f"🕕 Конец работы: {end}\n"
        f"⏱ Интервал слотов: {interval} мин",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить расписание", callback_data="edit_schedule")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
        ])
    )


@dp.callback_query(F.data == "edit_schedule")
async def edit_schedule(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminSettings.work_start)
    await callback.message.answer("🕐 Введи время начала работы (формат ЧЧ:ММ, например 09:00):")


@dp.message(AdminSettings.work_start)
async def set_work_start(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Введи в формате ЧЧ:ММ:")
        return
    await state.update_data(work_start=message.text.strip())
    await state.set_state(AdminSettings.work_end)
    await message.answer("🕕 Введи время конца работы:")


@dp.message(AdminSettings.work_end)
async def set_work_end(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Введи в формате ЧЧ:ММ:")
        return
    await state.update_data(work_end=message.text.strip())
    await state.set_state(AdminSettings.slot_interval)
    await message.answer("⏱ Введи интервал между слотами в минутах (например 30 или 60):")


@dp.message(AdminSettings.slot_interval)
async def set_slot_interval(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text.strip())
        if interval < 15 or interval > 240:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи число от 15 до 240:")
        return

    data = await state.get_data()
    db_execute(
        "UPDATE settings SET work_start=?, work_end=?, slot_interval=? WHERE id=1",
        (data['work_start'], data['work_end'], interval)
    )
    await state.clear()
    await message.answer(
        f"✅ Расписание обновлено!\n"
        f"🕐 {data['work_start']} — {data['work_end']}\n"
        f"⏱ Интервал: {interval} мин"
    )


# --- СТАТИСТИКА ---
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    today = datetime.now().strftime("%d.%m")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%d.%m")

    total_all = db_execute("SELECT COUNT(*), SUM(total_price) FROM appointments", fetch=True, one=True)
    total_today = db_execute(
        "SELECT COUNT(*), SUM(total_price) FROM appointments WHERE date=?",
        (today,), fetch=True, one=True
    )
    total_week = db_execute(
        "SELECT COUNT(*), SUM(total_price) FROM appointments WHERE date >= ?",
        (week_ago,), fetch=True, one=True
    )

    def fmt(row):
        count = row[0] or 0
        revenue = row[1] or 0
        return count, revenue

    ac, ar = fmt(total_all)
    tc, tr = fmt(total_today)
    wc, wr = fmt(total_week)

    text = (
        f"📊 Статистика\n\n"
        f"📅 Сегодня ({today}):\n"
        f"  Записей: {tc} | Выручка: {tr}₽\n\n"
        f"📆 За последние 7 дней:\n"
        f"  Записей: {wc} | Выручка: {wr}₽\n\n"
        f"📈 За всё время:\n"
        f"  Записей: {ac} | Выручка: {ar}₽"
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
        ])
    )


# --- ВЫХОДНЫЕ ДНИ ---
@dp.callback_query(F.data == "admin_days_off")
async def admin_days_off(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    today = datetime.now()
    buttons = []
    for i in range(14):
        day = today + timedelta(days=i)
        date_str = day.strftime("%d.%m")
        is_off = db_execute("SELECT 1 FROM days_off WHERE date=?", (date_str,), fetch=True, one=True)
        mark = "🚫 " if is_off else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{date_str}",
            callback_data=f"toggle_day_off_{date_str}"
        )])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        "🚫 Управление выходными днями\n\nНажми на дату чтобы сделать её выходной (или убрать):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("toggle_day_off_"))
async def toggle_day_off(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    date_str = callback.data.replace("toggle_day_off_", "")
    exists = db_execute("SELECT 1 FROM days_off WHERE date=?", (date_str,), fetch=True, one=True)

    if exists:
        db_execute("DELETE FROM days_off WHERE date=?", (date_str,))
        await callback.answer(f"{date_str} — рабочий день ✅", show_alert=True)
    else:
        db_execute("INSERT OR IGNORE INTO days_off VALUES (?)", (date_str,))
        await callback.answer(f"{date_str} — выходной 🚫", show_alert=True)

    await admin_days_off(callback)


# --- РАССЫЛКА ---
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    count = db_execute("SELECT COUNT(DISTINCT user_id) FROM appointments", fetch=True, one=True)
    total = count[0] if count else 0

    await state.set_state(AdminBroadcast.message)
    await callback.message.answer(
        f"📣 Рассылка\n\nВсего уникальных клиентов: {total}\n\nВведи текст сообщения для рассылки:"
    )


@dp.message(AdminBroadcast.message)
async def do_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    users = db_execute("SELECT DISTINCT user_id FROM appointments", fetch=True) or []
    sent, failed = 0, 0

    for u in users:
        try:
            await bot.send_message(u[0], f"📣 Сообщение от мастера:\n\n{message.text}")
            sent += 1
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(
        f"✅ Рассылка завершена!\n"
        f"Отправлено: {sent}\nОшибок: {failed}"
    )


# --- БЛОКИРОВКА СЛОТОВ ---
@dp.callback_query(F.data == "admin_block_slots")
async def admin_block_slots(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    today = datetime.now()
    buttons = []
    for i in range(14):
        day = today + timedelta(days=i)
        date_str = day.strftime("%d.%m")
        label = date_str + (" (сегодня)" if i == 0 else "")
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"block_date_{date_str}"
        )])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    await callback.message.edit_text(
        "🔒 Выбери дату для блокировки слотов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("block_date_"))
async def admin_block_slots_time(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    date_str = callback.data.replace("block_date_", "")
    await show_block_slots_for_date(callback, date_str)


async def show_block_slots_for_date(callback: types.CallbackQuery, date_str: str):
    result = db_execute("SELECT work_start, work_end, slot_interval FROM settings WHERE id=1", fetch=True, one=True)
    if not result:
        await callback.answer("Ошибка настроек", show_alert=True)
        return

    start, end, interval = result
    curr = datetime.strptime(start, "%H:%M")
    end_dt = datetime.strptime(end, "%H:%M")

    buttons = []
    while curr < end_dt:
        t = curr.strftime("%H:%M")

        is_blocked = db_execute(
            "SELECT 1 FROM blocked_slots WHERE date=? AND time=?",
            (date_str, t), fetch=True, one=True
        )
        is_busy = db_execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=?",
            (date_str, t), fetch=True, one=True
        )

        if is_busy:
            label = f"{t} 👤"
            cb = "slot_has_client"
        elif is_blocked:
            label = f"{t} 🔒"
            cb = f"unblock_{date_str}_{t}"
        else:
            label = t
            cb = f"blockslot_{date_str}_{t}"

        buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])
        curr += timedelta(minutes=interval)

    buttons.append([InlineKeyboardButton(text="🔙 К датам", callback_data="admin_block_slots")])
    await callback.message.edit_text(
        f"🔒 Слоты на {date_str}:\n\n"
        f"👤 — занят клиентом\n"
        f"🔒 — заблокирован тобой (нажми чтобы снять)\n"
        f"Свободный — нажми чтобы заблокировать",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data == "slot_has_client")
async def slot_has_client(callback: types.CallbackQuery):
    await callback.answer("Этот слот занят клиентом — сначала удали запись", show_alert=True)


@dp.callback_query(F.data.startswith("blockslot_"))
async def block_slot(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    parts = callback.data.replace("blockslot_", "").rsplit("_", 1)
    date_str, time_str = parts[0], parts[1]

    db_execute(
        "INSERT OR IGNORE INTO blocked_slots (date, time) VALUES (?, ?)",
        (date_str, time_str)
    )
    await callback.answer(f"{time_str} заблокировано 🔒", show_alert=True)
    await show_block_slots_for_date(callback, date_str)


@dp.callback_query(F.data.startswith("unblock_"))
async def unblock_slot(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    parts = callback.data.replace("unblock_", "").rsplit("_", 1)
    date_str, time_str = parts[0], parts[1]

    db_execute(
        "DELETE FROM blocked_slots WHERE date=? AND time=?",
        (date_str, time_str)
    )
    await callback.answer(f"{time_str} разблокировано ✅", show_alert=True)
    await show_block_slots_for_date(callback, date_str)


# --- УПРАВЛЕНИЕ КАНАЛАМИ ---
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    channels = db_execute("SELECT id, channel_title, channel_link FROM required_channels", fetch=True) or []

    text = "📢 Обязательные каналы для подписки:\n\n"
    buttons = []

    if channels:
        for ch_id, title, link in channels:
            text += f"• {title} — {link}\n"
            buttons.append([InlineKeyboardButton(
                text=f"❌ Удалить «{title}»",
                callback_data=f"del_channel_{ch_id}"
            )])
    else:
        text += "Каналов пока нет — проверка подписки отключена\n"

    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "add_channel")
async def add_channel_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminChannel.waiting)
    await callback.message.answer(
        "📢 Отправь канал в формате:\n\n"
        "<b>Название | @username_канала</b>\n\n"
        "Например:\n"
        "<i>Мой канал | @mychannel</i>\n\n"
        "⚠️ Бот должен быть администратором канала чтобы проверять подписку!",
        parse_mode="HTML"
    )


@dp.message(AdminChannel.waiting)
async def add_channel_save(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        parts = message.text.split("|")
        if len(parts) != 2:
            raise ValueError

        title = parts[0].strip()
        username = parts[1].strip()

        if not username.startswith("@"):
            username = f"@{username}"

        try:
            chat = await bot.get_chat(username)
            channel_id = str(chat.id)
            channel_title = chat.title or title
        except Exception:
            await message.answer(
                "❌ Не могу найти канал. Убедись что:\n"
                "1. Username правильный\n"
                "2. Бот добавлен в администраторы канала"
            )
            return

        db_execute(
            "INSERT INTO required_channels (channel_id, channel_title, channel_link) VALUES (?, ?, ?)",
            (channel_id, channel_title, username)
        )
        await state.clear()
        await message.answer(f"✅ Канал «{channel_title}» добавлен!")

    except ValueError:
        await message.answer("❌ Неверный формат. Отправь в виде:\nНазвание | @username")


@dp.callback_query(F.data.startswith("del_channel_"))
async def del_channel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    ch_id = int(callback.data.split("_")[2])
    db_execute("DELETE FROM required_channels WHERE id=?", (ch_id,))
    await callback.answer("Канал удалён ✅", show_alert=True)
    await admin_channels(callback)


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(
        "🔧 Админ-панель:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Записи", callback_data="admin_orders")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🛠 Услуги", callback_data="admin_services")],
            [InlineKeyboardButton(text="🚫 Выходные дни", callback_data="admin_days_off")],
            [InlineKeyboardButton(text="🔒 Блокировка времени", callback_data="admin_block_slots")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="📢 Каналы для подписки", callback_data="admin_channels")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings")],
            [InlineKeyboardButton(text="📖 Помощь", callback_data="help_inline")]
        ])
    )


# --- ПОМОЩЬ ---
@dp.message(Command("help"))
async def help_command(message: types.Message):
    if is_admin(message.from_user.id):
        text = (
            "📖 Инструкция для администратора\n\n"
            "🔧 Команды:\n"
            "/admin — открыть админ-панель\n"
            "/add_admin [ID] — добавить нового администратора\n"
            "/remove_admin [ID] — удалить администратора\n"
            "/help — показать эту инструкцию\n\n"
            "📋 Записи\n"
            "Просмотр последних 10 записей клиентов. "
            "Можно удалить любую запись — клиент получит уведомление об отмене.\n\n"
            "📊 Статистика\n"
            "Количество записей и выручка за сегодня, за 7 дней и за всё время.\n\n"
            "🛠 Услуги\n"
            "Добавление и удаление услуг с ценами. "
            "Клиенты видят актуальный список при записи.\n\n"
            "🚫 Выходные дни\n"
            "Нажми на дату в ближайших 14 днях чтобы сделать её выходной. "
            "Клиенты увидят 🚫 и не смогут записаться на этот день.\n\n"
            "📣 Рассылка\n"
            "Отправка сообщения всем клиентам которые когда-либо записывались через бота.\n\n"
            "⚙️ Настройки\n"
            "Изменение рабочих часов и интервала между слотами записи.\n\n"
            "💬 Связь с клиентом\n"
            "В уведомлении о новой записи приходит @username клиента — "
            "нажми на него чтобы написать напрямую в Telegram.\n\n"
            "⏰ Напоминания\n"
            "Бот автоматически напоминает клиентам о записи за 1 час. Ничего настраивать не нужно."
        )
    else:
        text = (
            "📖 Как пользоваться ботом\n\n"
            "📅 Записаться — выбери дату, время и услуги\n"
            "📋 Мои записи — посмотри свои записи или отмени ненужную\n\n"
            "Если возникли вопросы — свяжись с администратором."
        )

    await message.answer(text)


# --- ДОБАВИТЬ АДМИНА ---
@dp.message(Command("add_admin"))
async def add_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        return
    try:
        new_id = int(message.text.split()[1])
        db_execute("INSERT OR IGNORE INTO admins VALUES (?)", (new_id,))
        await message.answer(f"✅ Пользователь {new_id} добавлен как админ")
    except Exception:
        await message.answer("❌ Ошибка. Используй: /add_admin 123456789")


# --- УДАЛИТЬ АДМИНА ---
@dp.message(Command("remove_admin"))
async def remove_admin(message: types.Message):
    try:
        target_id = int(message.text.split()[1])
        if target_id == message.from_user.id:
            await message.answer("❌ Нельзя удалить самого себя")
            return
        exists = db_execute("SELECT 1 FROM admins WHERE user_id=?", (target_id,), fetch=True, one=True)
        if not exists:
            await message.answer(f"❌ Пользователь {target_id} не является админом")
            return
        db_execute("DELETE FROM admins WHERE user_id=?", (target_id,))
        await message.answer(f"✅ Пользователь {target_id} удалён из админов")
    except Exception:
        await message.answer("❌ Ошибка. Используй: /remove_admin 123456789")


# =====================
# НАПОМИНАНИЯ
# =====================

async def send_reminders():
    """Фоновая задача: отправляет напоминания за 1 час до записи"""
    while True:
        try:
            now = datetime.now()
            reminder_time = now + timedelta(hours=1)
            target_date = reminder_time.strftime("%d.%m")
            target_time = reminder_time.strftime("%H:%M")

            rows = db_execute(
                """SELECT id, user_id, name, date, time, services 
                   FROM appointments 
                   WHERE date=? AND time=? AND reminded=0""",
                (target_date, target_time),
                fetch=True
            )

            if rows:
                for row in rows:
                    appt_id, user_id, name, date, time, services = row
                    try:
                        await bot.send_message(
                            user_id,
                            f"⏰ Напоминание!\n\n"
                            f"Привет, {name}! Через 1 час у тебя запись.\n"
                            f"📆 {date} в {time}\n"
                            f"🛠 {services}\n\n"
                            f"Ждём тебя! 👋"
                        )
                        db_execute("UPDATE appointments SET reminded=1 WHERE id=?", (appt_id,))
                    except Exception as e:
                        logging.error(f"Reminder error for user {user_id}: {e}")

        except Exception as e:
            logging.error(f"Reminder loop error: {e}")

        await asyncio.sleep(60)


# --- ЗАПУСК ---
async def main():
    print("🤖 Бот запущен!")
    asyncio.create_task(send_reminders())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())