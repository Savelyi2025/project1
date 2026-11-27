import asyncio
import calendar
import csv
from datetime import datetime
import io
import logging
import os
import re
from dotenv import load_dotenv

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)

import database as db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")


logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


class FinanceForm(StatesGroup):
    waiting_for_limit = State()
    waiting_for_broadcast = State()


def get_main_kb():
    kb = [
        [KeyboardButton(text="📊 Отчет и прогноз"), KeyboardButton(text="📈 График расходов")],
        [KeyboardButton(text="🎯 Лимит"), KeyboardButton(text="📜 История")],
        [KeyboardButton(text="📥 Экспорт CSV")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)


async def build_transaction_feedback(user_id: int, trans_type: str) -> str:
    balance = await db.get_total_balance(user_id)
    text = f"\n\n💰 <b>Вам доступно:</b> {balance:.2f} ₽"

    if trans_type == "income":
        return text

    if balance < 0:
        text += " ⚠️ <i>(отрицательный баланс)</i>"

    limit = await db.get_monthly_limit(user_id)
    if limit > 0:
        summary = await db.get_monthly_summary(user_id)
        spent = summary["expense"]
        remaining = limit - spent
        percent = (spent / limit) * 100

        if remaining < 0:
            text += (
                f"\n🚨 <b>Ваш месячный лимит превышен!</b>\n"
                f"Вы потратили {spent:.2f} из {limit:.2f} ₽ (перерасход: {abs(remaining):.2f} ₽)"
            )
        elif percent >= 80:
            text += (
                f"\n⚠️ <b>Лимит на исходе:</b> у Вас осталось {remaining:.2f} из {limit:.2f} ₽ ({percent:.0f}%)"
            )
        else:
            text += (
                f"\n🎯 <b>Остаток Вашего лимита на месяц:</b> {remaining:.2f} из {limit:.2f} ₽ ({percent:.0f}%)"
            )
    else:
        text += "\n🎯 <i>Вы ещё не задали общий лимит на месяц (кнопка «🎯 Лимит»)</i>"

    return text
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await db.add_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Здравствуйте, {message.from_user.first_name}! 👋\n"
        "Добро пожаловать в <b>CashCompass</b> — Ваш финансовый помощник.\n\n"
        "✍️ <b>Чтобы записать операцию, просто отправьте сообщение:</b>\n"
        "• <code>Еда 350</code> — запишет расход\n"
        "• <code>+5000 Стипендия</code> — запишет доход\n\n"
        "Аналитика и настройки доступны в меню ниже 👇",
        parse_mode="HTML",
        reply_markup=get_main_kb()
    )


@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Вы вернулись в главное меню.", reply_markup=get_main_kb())


@dp.message(F.text == "🎯 Лимит")
async def start_limit(message: types.Message, state: FSMContext):
    current_limit = await db.get_monthly_limit(message.from_user.id)
    await state.set_state(FinanceForm.waiting_for_limit)
    await message.answer(
        f"Ваш текущий общий лимит на месяц: <b>{current_limit:.2f} ₽</b>\n"
        "Введите новую сумму лимита (или <code>0</code>, чтобы отключить контроль):",
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
        await message.answer("⚠️ Пожалуйста, введите число больше или равное нулю:")
        return

    await db.set_monthly_limit(message.from_user.id, limit)
    await state.clear()
    msg = (
        "🎯 Контроль месячного лимита отключен."
        if limit == 0
        else f"🎯 Установлен Ваш новый лимит на месяц: <b>{limit:.2f} ₽</b>"
    )
    await message.answer(msg, parse_mode="HTML", reply_markup=get_main_kb())

# --- Админ-рассылка всем пользователям (/broadcast) ---
@dp.message(Command("broadcast"))
async def start_broadcast(message: types.Message, state: FSMContext):
    # Проверяем, что команду вызвал именно администратор
    if not ADMIN_ID or str(message.from_user.id) != str(ADMIN_ID):
        return

    await state.set_state(FinanceForm.waiting_for_broadcast)
    await message.answer(
        "📢 <b>Режим рассылки всем пользователям.</b>\n\n"
        "Введите текст сообщения, которое получат все зарегистрированные пользователи бота "
        "(можно использовать HTML-разметку, например <code>&lt;b&gt;жирный&lt;/b&gt;</code>):",
        parse_mode="HTML",
        reply_markup=get_cancel_kb()
    )


@dp.message(FinanceForm.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    # На случай если нажали «Отмена»
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=get_main_kb())
        return

    await state.clear()
    users = await db.get_all_users()
    sent_count = 0
    error_count = 0

    status_msg = await message.answer(f"⏳ Начинаю рассылку для {len(users)} пользователей...")

    for user_id in users:
        try:
            # copy_to позволяет рассылать не только текст, но и фото, видео или стикеры!
            await message.copy_to(chat_id=user_id)
            sent_count += 1
            await asyncio.sleep(0.05)  # Защита от лимитов Telegram
        except Exception as e:
            error_count += 1
            logging.warning(f"Ошибка доставки пользователю {user_id}: {e}")

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📨 Успешно доставлено: <b>{sent_count}</b>\n"
        f"🚫 Не доставлено (бот заблокирован): <b>{error_count}</b>",
        parse_mode="HTML"
    )
    await message.answer("Вы вернулись в главное меню:", reply_markup=get_main_kb())


@dp.message(F.text == "📊 Отчет и прогноз")
async def show_report(message: types.Message):
    user_id = message.from_user.id
    total_balance = await db.get_total_balance(user_id)
    summary = await db.get_monthly_summary(user_id)
    limit = await db.get_monthly_limit(user_id)

    income = summary["income"]
    expense = summary["expense"]

    now = datetime.now()
    days_passed = max(1, now.day)
    _, days_in_month = calendar.monthrange(now.year, now.month)
    avg_daily_expense = expense / days_passed
    projected_expense = avg_daily_expense * days_in_month

    text = (
        f"💰 <b>Всего Вам доступно: {total_balance:.2f} ₽</b>\n\n"
        f"📊 <b>Ваши операции за текущий месяц:</b>\n"
        f"🟢 Доходы за месяц: {income:.2f} ₽\n"
        f"🔴 Расходы за месяц: {expense:.2f} ₽\n\n"
        f"📅 Ваш средний расход в день: <b>{avg_daily_expense:.2f} ₽</b>\n"
        f"🔮 Прогноз трат до конца месяца: <b>{projected_expense:.2f} ₽</b>\n"
    )

    if income > 0:
        savings_rate = max(0.0, ((income - expense) / income) * 100)
        text += f"📈 Ваша норма сбережений за месяц: {savings_rate:.1f}%\n"

    if limit > 0:
        text += f"\n🎯 Ваш лимит на месяц: {limit:.2f} ₽ (осталось: {limit - expense:.2f} ₽)"
        if projected_expense > limit:
            text += "\n⚠️ <i>При текущем темпе трат Вы превысите лимит до конца месяца.</i>"

    await message.answer(text, parse_mode="HTML")

def create_chart_image(data: list[tuple[str, float]]) -> io.BytesIO:
    """Генерирует стильный кольцевой график расходов в памяти"""
    categories = [cat for cat, _ in data]
    amounts = [amt for _, amt in data]
    total_spent = sum(amounts)

    colors = [
        "#4E79A7", "#F28E2B", "#E15759", "#76B7B2",
        "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7",
        "#9C755F", "#BAB0AC"
    ]

    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=120)
    fig.patch.set_facecolor("#F8F9FA")

    wedges, _ = ax.pie(
        amounts,
        colors=colors[:len(categories)],
        startangle=140,
        wedgeprops=dict(width=0.42, edgecolor="#F8F9FA", linewidth=2)
    )
    ax.text(
        0, 0, f"Всего:\n{total_spent:,.0f} ₽".replace(",", " "),
        ha="center", va="center", fontsize=12, fontweight="bold", color="#212529"
    )
    legend_labels = [
        f"{cat} — {amt:,.0f} ₽ ({amt / total_spent * 100:.1f}%)".replace(",", " ")
        for cat, amt in data
    ]
    ax.legend(
        wedges, legend_labels,
        title="Категории",
        loc="center left",
        bbox_to_anchor=(0.95, 0.5),
        frameon=False,
        fontsize=9
    )
    ax.set_title("Структура расходов за месяц", fontsize=13, fontweight="bold", pad=12, color="#212529")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf



@dp.message(F.text == "📈 График расходов")
async def send_expense_chart(message: types.Message):
    data = await db.get_monthly_expenses_by_category(message.from_user.id)
    if not data:
        await message.answer("У Вас ещё нет расходов в этом месяце для построения графика.")
        return

    buf = create_chart_image(data)
    photo = BufferedInputFile(buf.read(), filename="chart.png")

    await message.answer_photo(
        photo=photo,
        caption="📉 <b>Ваши расходы за текущий месяц</b>",
        parse_mode="HTML"
    )

@dp.message(F.text == "📜 История")
async def show_history(message: types.Message):
    rows = await db.get_recent_transactions(message.from_user.id)
    if not rows:
        await message.answer("Ваша история операций пока пуста.")
        return

    lines = []
    inline_kb = []
    for trans_id, t_type, cat, amt, date_str in rows:
        icon = "🟢 +" if t_type == "income" else "🔴 -"
        lines.append(f"<b>#{trans_id}</b> | {icon}{amt:.2f} ₽ — {cat} ({date_str})")
        inline_kb.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить #{trans_id} ({cat} {amt:.0f} ₽)",
                callback_data=f"del_{trans_id}"
            )
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=inline_kb)
    await message.answer(
        "📜 <b>Ваши последние 5 операций:</b>\n<i>Нажмите на кнопку ниже, если хотите удалить запись:</i>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=markup
    )


@dp.callback_query(F.data.startswith("del_"))
async def process_delete_callback(callback: types.CallbackQuery):
    _, trans_id_str = callback.data.split("_")
    trans_id = int(trans_id_str)
    deleted = await db.delete_transaction_by_id(callback.from_user.id, trans_id)
    if deleted:
        await callback.answer(f"Запись #{trans_id} удалена!")
        await callback.message.edit_text(
            f"✅ Операция <b>#{trans_id}</b> удалена. Нажмите «📜 История», чтобы обновить список.",
            parse_mode="HTML"
        )
    else:
        await callback.answer("Эта запись уже была удалена.", show_alert=True)


@dp.message(F.text == "📥 Экспорт CSV")
async def export_csv(message: types.Message):
    rows = await db.get_all_transactions(message.from_user.id)
    if not rows:
        await message.answer("За текущий месяц у Вас пока нет операций для выгрузки.")
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Дата и время", "Тип", "Категория", "Сумма (руб.)"])

    total_income = 0.0
    total_expense = 0.0

    for trans_id, created_at, t_type, category, amount in rows:
        if t_type == "income":
            t_type_ru = "Доход"
            total_income += amount
        else:
            t_type_ru = "Расход"
            total_expense += amount
        writer.writerow([trans_id, created_at, t_type_ru, category, f"{amount:.2f}"])

    balance = total_income - total_expense
    writer.writerow([])
    writer.writerow(["", "", "", "ИТОГО ДОХОД ЗА МЕСЯЦ:", f"{total_income:.2f}"])
    writer.writerow(["", "", "", "ИТОГО РАСХОД ЗА МЕСЯЦ:", f"{total_expense:.2f}"])
    writer.writerow(["", "", "", "ИТОГОВЫЙ БАЛАНС:", f"{balance:.2f}"])

    doc = BufferedInputFile(
        output.getvalue().encode("utf-8-sig"),
        filename="cashcompass_month_report.csv"
    )
    await message.answer_document(
        document=doc,
        caption=(
            f"📥 <b>Ваш отчёт за текущий месяц готов!</b>\n\n"
            f"🟢 Общий доход: <b>{total_income:.2f} ₽</b>\n"
            f"🔴 Общий расход: <b>{total_expense:.2f} ₽</b>\n"
            f"💰 Баланс за месяц: <b>{balance:.2f} ₽</b>"
        ),
        parse_mode="HTML"
    )


@dp.message(F.text)
async def quick_entry_handler(message: types.Message):
    text = message.text.strip()
    match_cat_first = re.match(r"^(\+?)([а-яА-Яa-zA-Z\s]+?)\s+(\d+(?:[.,]\d+)?)$", text)
    match_num_first = re.match(r"^(\+?)(\d+(?:[.,]\d+)?)\s+([а-яА-Яa-zA-Z\s]+)$", text)

    if match_cat_first:
        sign, category, amount_str = match_cat_first.groups()
    elif match_num_first:
        sign, amount_str, category = match_num_first.groups()
    else:
        await message.answer(
            "Извините, я не понял команду 🤔 Используйте кнопки меню или напишите в формате:\n"
            "• <code>Еда 350</code> — для расхода\n"
            "• <code>+5000 Стипендия</code> — для дохода",
            parse_mode="HTML"
        )
        return

    amount = float(amount_str.replace(",", "."))
    if amount <= 0:
        return

    trans_type = "income" if sign == "+" else "expense"
    category = category.strip().capitalize()
    user_id = message.from_user.id

    await db.add_transaction(user_id, trans_type, category, amount)
    type_word = "Доход" if trans_type == "income" else "Расход"
    reply_text = f"⚡ Быстрая запись выполнена!\n{type_word}: {amount:.2f} ₽\nКатегория: {category}"
    reply_text += await build_transaction_feedback(user_id, trans_type)

    await message.answer(reply_text, parse_mode="HTML", reply_markup=get_main_kb())


async def broadcast_message(bot: Bot, text: str):
    users = await db.get_all_users()
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")


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
