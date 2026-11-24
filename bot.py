import asyncio
import csv
import io
import logging
import os
from dotenv import load_dotenv

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile

import database as db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- 1. Машина состояний (FSM) ---
class FinanceForm(StatesGroup):
    waiting_for_amount = State()
    waiting_for_category = State()
    waiting_for_limit = State()


# --- 2. Клавиатуры ---
def get_main_kb():
    """Главное меню бота"""
    kb = [
        [KeyboardButton(text="➕ Доход"), KeyboardButton(text="➖ Расход")],
        [KeyboardButton(text="📊 Отчет за месяц"), KeyboardButton(text="📈 График расходов")],
        [KeyboardButton(text="🎯 Лимит"), KeyboardButton(text="📜 История")],
        [KeyboardButton(text="📥 Экспорт CSV"), KeyboardButton(text="🗑 Удалить последнюю")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_categories_kb(trans_type: str):
    """Кнопки категорий"""
    if trans_type == "income":
        kb = [[KeyboardButton(text="Стипендия"), KeyboardButton(text="Подработка")],
              [KeyboardButton(text="Подарок"), KeyboardButton(text="❌ Отмена")]]
    else:
        kb = [[KeyboardButton(text="Еда"), KeyboardButton(text="Транспорт")],
              [KeyboardButton(text="Учеба"), KeyboardButton(text="Развлечения")],
              [KeyboardButton(text="❌ Отмена")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)


# --- 3. Базовые команды ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await db.add_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        "Добро пожаловать в <b>CashCompass</b> — твой финансовый помощник.\n\n"
        "Используй меню ниже для учета операций и аналитики:",
        parse_mode="HTML",
        reply_markup=get_main_kb()
    )


@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_kb())


# --- 4. Добавление дохода и расхода ---
@dp.message(F.text.in_(["➕ Доход", "➖ Расход"]))
async def start_transaction(message: types.Message, state: FSMContext):
    trans_type = "income" if message.text == "➕ Доход" else "expense"
    await state.update_data(trans_type=trans_type)
    await state.set_state(FinanceForm.waiting_for_amount)
    await message.answer("Введите сумму (например: 250 или 150.50):", reply_markup=get_cancel_kb())


@dp.message(FinanceForm.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите корректное положительное число (например: 500):")
        return

    await state.update_data(amount=amount)
    await state.set_state(FinanceForm.waiting_for_category)

    data = await state.get_data()
    await message.answer(
        "Выберите категорию или напишите свою:",
        reply_markup=get_categories_kb(data["trans_type"])
    )


@dp.message(FinanceForm.waiting_for_category)
async def process_category(message: types.Message, state: FSMContext):
    category = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id

    await db.add_transaction(
        user_id=user_id,
        trans_type=data["trans_type"],
        category=category,
        amount=data["amount"]
    )
    await state.clear()

    type_word = "Доход" if data["trans_type"] == "income" else "Расход"
    reply_text = f"✅ Записано!\n{type_word}: {data['amount']:.2f} ₽\nКатегория: {category}"

    # Проверяем месячный лимит, если это расход
    if data["trans_type"] == "expense":
        limit = await db.get_monthly_limit(user_id)
        if limit > 0:
            summary = await db.get_monthly_summary(user_id)
            spent = summary["expense"]
            percent = (spent / limit) * 100
            if spent > limit:
                reply_text += f"\n\n🚨 <b>Внимание! Лимит превышен!</b>\nПотрачено {spent:.2f} ₽ из {limit:.2f} ₽ ({percent:.0f}%)."
            elif percent >= 80:
                reply_text += f"\n\n⚠️ <b>Осторожно!</b> Использовано {percent:.0f}% месячного лимита ({spent:.2f} из {limit:.2f} ₽)."

    await message.answer(reply_text, parse_mode="HTML", reply_markup=get_main_kb())


# --- 5. Установка лимита ---
@dp.message(F.text == "🎯 Лимит")
async def start_limit(message: types.Message, state: FSMContext):
    current_limit = await db.get_monthly_limit(message.from_user.id)
    await state.set_state(FinanceForm.waiting_for_limit)
    await message.answer(
        f"Текущий месячный лимит расходов: <b>{current_limit:.2f} ₽</b>\n\n"
        "Введите новую сумму лимита (или <code>0</code>, чтобы отключить):",
        parse_mode="HTML",
        reply_markup=get_cancel_kb()
    )


@dp.message(FinanceForm.waiting_for_limit)
async def process_limit(message: types.Message, state: FSMContext):
    try:
        limit = float(message.text.replace(",", "."))
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите число больше или равное нулю:")
        return

    await db.set_monthly_limit(message.from_user.id, limit)
    await state.clear()

    if limit == 0:
        await message.answer("🎯 Контроль месячного лимита отключен.", reply_markup=get_main_kb())
    else:
        await message.answer(f"🎯 Новый месячный лимит установлен: <b>{limit:.2f} ₽</b>", parse_mode="HTML",
                             reply_markup=get_main_kb())


# --- 6. Аналитика, графики, история и экспорт ---
@dp.message(F.text == "📊 Отчет за месяц")
async def show_report(message: types.Message):
    user_id = message.from_user.id
    summary = await db.get_monthly_summary(user_id)
    limit = await db.get_monthly_limit(user_id)

    income = summary["income"]
    expense = summary["expense"]
    balance = income - expense

    text = (
        f"📊 <b>Сводка за текущий месяц:</b>\n\n"
        f"🟢 Доходы: {income:.2f} ₽\n"
        f"🔴 Расходы: {expense:.2f} ₽\n"
        f"💰 <b>Остаток: {balance:.2f} ₽</b>\n"
    )

    if income > 0:
        savings_rate = max(0.0, (balance / income) * 100)
        text += f"📈 Норма сбережений: {savings_rate:.1f}%\n"

    if limit > 0:
        remaining_limit = limit - expense
        text += f"\n🎯 Лимит: {limit:.2f} ₽ (осталось: {remaining_limit:.2f} ₽)"

    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "📈 График расходов")
async def send_expense_chart(message: types.Message):
    data = await db.get_monthly_expenses_by_category(message.from_user.id)
    if not data:
        await message.answer("В этом месяце еще нет расходов для построения графика.")
        return

    categories = [row for row in data]
    amounts = [row for row in data]

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(amounts, labels=categories, autopct="%1.1f%%", startangle=140)
    ax.set_title("Расходы по категориям за текущий месяц")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)

    details = "\n".join([f"• {cat}: {amt:.2f} ₽" for cat, amt in data])
    photo = BufferedInputFile(buf.read(), filename="chart.png")

    await message.answer_photo(
        photo=photo,
        caption=f"📉 <b>Расходы за текущий месяц:</b>\n\n{details}",
        parse_mode="HTML"
    )


@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    rows = await db.get_recent_transactions(message.from_user.id)
    if not rows:
        await message.answer("История операций пока пуста.")
        return

    lines = []
    for t_type, cat, amt, date_str in rows:
        icon = "🟢 +" if t_type == "income" else "🔴 -"
        lines.append(f"{icon}{amt:.2f} ₽ | {cat} ({date_str})")

    await message.answer("📜 <b>Последние 5 операций:</b>\n\n" + "\n".join(lines), parse_mode="HTML")


@dp.message(F.text == "🗑 Удалить последнюю")
async def delete_last(message: types.Message):
    if await db.delete_last_transaction(message.from_user.id):
        await message.answer("🗑 Последняя запись удалена.", reply_markup=get_main_kb())
    else:
        await message.answer("Удалять нечего — история пуста.")


@dp.message(F.text == "📥 Экспорт CSV")
async def export_csv(message: types.Message):
    rows = await db.get_all_transactions(message.from_user.id)
    if not rows:
        await message.answer("Нет данных для выгрузки.")
        return

    # Используем StringIO и кодировку utf-8-sig для корректного открытия в Excel
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Дата и время", "Тип", "Категория", "Сумма (руб.)"])

    for row in rows:
        t_type_ru = "Доход" if row == "income" else "Расход"
        writer.writerow([row, row, t_type_ru, row, row])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    doc = BufferedInputFile(csv_bytes, filename="cashcompass_report.csv")

    await message.answer_document(
        document=doc,
        caption="📥 Выгрузка всех операций (файл готов для открытия в Excel)."
    )


# --- 7. Запуск бота ---
async def on_startup():
    await db.init_db()
    if ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🟢 <b>Бот CashCompass запущен</b> и готов к работе!",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление о запуске: {e}")

async def on_shutdown():
    if ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🔴 <b>Бот CashCompass выключен.</b> Прием данных временно приостановлен.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление об остановке: {e}")

# --- Запуск ---
# --- Хуки жизненного цикла (уведомления админу) ---
async def on_startup(bot: Bot):
    await db.init_db()
    if ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🟢 <b>Бот CashCompass запущен</b> и готов к работе!",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление о запуске: {e}")


async def on_shutdown(bot: Bot):
    if ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🔴 <b>Бот CashCompass выключен.</b> Прием данных временно приостановлен.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление об остановке: {e}")


# --- Вспомогательная функция массовой рассылки ---
async def broadcast_message(bot: Bot, text: str):
    users = await db.get_all_users()
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")


# --- Хуки жизненного цикла (уведомления всем пользователям) ---
async def on_startup(bot: Bot):
    await db.init_db()
    await broadcast_message(
        bot,
        "🟢 <b>Бот CashCompass снова в сети</b> и готов к работе!"
    )


async def on_shutdown(bot: Bot):
    await broadcast_message(
        bot,
        "🔴 <b>Бот CashCompass временно выключен</b> на техническое обслуживание."
    )


# --- Запуск ---
async def main():
    bot = Bot(token=BOT_TOKEN)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
