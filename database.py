import os
import aiosqlite

DB_NAME = os.getenv("DB_PATH", "finance.db")


async def init_db():
    """Создает таблицы при запуске бота"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                monthly_limit REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                category TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount > 0),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
        """)
        await db.commit()


async def add_user(telegram_id: int, username: str):
    """Регистрирует пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username)
        )
        await db.commit()


async def set_monthly_limit(user_id: int, limit: float):
    """Устанавливает месячный лимит расходов"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET monthly_limit = ? WHERE telegram_id = ?",
            (limit, user_id)
        )
        await db.commit()


async def get_monthly_limit(user_id: int) -> float:
    """Возвращает установленный лимит пользователя в виде числа"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT monthly_limit FROM users WHERE telegram_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0



async def add_transaction(user_id: int, trans_type: str, category: str, amount: float):
    """Добавляет доход или расход"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, type, category, amount) VALUES (?, ?, ?, ?)",
            (user_id, trans_type, category, amount)
        )
        await db.commit()


async def get_monthly_summary(user_id: int):
    """Возвращает доходы и расходы за текущий календарный месяц"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
                """
                SELECT type, SUM(amount) 
                FROM transactions 
                WHERE user_id = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
                GROUP BY type
                """,
                (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            summary = {'income': 0.0, 'expense': 0.0}
            for row in rows:
                summary[row] = row
            return summary


async def get_monthly_expenses_by_category(user_id: int):
    """Возвращает расходы по категориям за текущий месяц"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
                """
                SELECT category, SUM(amount) as total 
                FROM transactions 
                WHERE user_id = ? 
                  AND type = 'expense' 
                  AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
                GROUP BY category 
                ORDER BY total DESC
                """,
                (user_id,)
        ) as cursor:
            return await cursor.fetchall()


async def get_recent_transactions(user_id: int, limit: int = 5):
    """Возвращает последние 5 операций"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
                """
                SELECT type, category, amount, strftime('%d.%m %H:%M', created_at)
                FROM transactions
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit)
        ) as cursor:
            return await cursor.fetchall()


async def delete_last_transaction(user_id: int) -> bool:
    """Удаляет последнюю операцию пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
                "SELECT id FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            await db.execute("DELETE FROM transactions WHERE id = ?", (row,))
            await db.commit()
            return True


async def get_all_transactions(user_id: int):
    """Выгружает всю историю операций для экспорта в CSV"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
                """
                SELECT id, created_at, type, category, amount
                FROM transactions
                WHERE user_id = ?
                ORDER BY created_at DESC
                """,
                (user_id,)
        ) as cursor:
            return await cursor.fetchall()
async def get_all_users() -> list[int]:
    """Возвращает список telegram_id всех зарегистрированных пользователей"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT telegram_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [uid for (uid,) in rows]

