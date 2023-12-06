import asyncio
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (BotCommand, InlineKeyboardButton,
                           InlineKeyboardMarkup)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import bot_token, db_host, db_name, db_password, db_user

API_TOKEN = bot_token

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
dp.middleware.setup(LoggingMiddleware())

add_task_button = InlineKeyboardButton("Добавить задачу", callback_data="add")
remove_task_button = InlineKeyboardButton("Удалить задачу(и)", callback_data="remove")
task_list_button = InlineKeyboardButton("Список задач", callback_data="list")
set_deadline_button = InlineKeyboardButton("Установить дедлайн для задачи", callback_data="deadline")
clear_list_button = InlineKeyboardButton("Очистить список задач", callback_data="clear")
task_keyboard = InlineKeyboardMarkup(row_width=2).add(add_task_button, remove_task_button, clear_list_button)
list_keyboard = InlineKeyboardMarkup(row_width=2).add(task_list_button)
after_add_keyboard = InlineKeyboardMarkup(row_width=2).add(task_list_button, add_task_button, set_deadline_button)
add_task_keyboard = InlineKeyboardMarkup(row_width=2).add(add_task_button)


async def set_main_menu(bot: Bot):
    main_menu_commands = [
        BotCommand(command="/start", description="Начать работу с ботом"),
        BotCommand(command="/help", description="Помощь и справка"),
        BotCommand(command="/list", description="Список задач"),
        BotCommand(command="/timezone", description="Установить часовой пояс"),
    ]
    await bot.set_my_commands(main_menu_commands)


# Параметры подключения к базе данных
async def on_startup(dp):
    asyncio.create_task(run_scheduler())
    # Создаем пул подключения к базе данных при старте бота
    dp["db_pool"] = await asyncpg.create_pool(
        user=db_user,
        password=db_password,
        host=db_host,
        database=db_name,
        port=5432,  # Используйте целое число, а не строку
    )


async def on_shutdown(dp):
    # Закрываем пул подключения к базе данных при выключении бота
    db_pool = dp.get("db_pool")
    if db_pool:
        await db_pool.close()


async def close_database_connection(connection):
    await connection.close()


async def execute_query(connection, query):
    try:
        result = await connection.fetch(query)
        return result
    except asyncpg.exceptions.PostgresError as e:
        print(f"Error executing query: {e}")
        return None


async def add_user_to_db(dp, user_id):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING;
                    """,
                    user_id,
                )


async def add_task_to_db(dp, user_id, task):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO tasks (user_id, task, task_date) VALUES ($1, $2, CURRENT_TIMESTAMP(0));
                    """,
                    user_id,
                    task,
                )


async def remove_task_from_db(dp, user_id, task):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """
                    DELETE FROM tasks WHERE ctid IN (
                        SELECT ctid FROM (
                            SELECT ctid, user_id, ROW_NUMBER () OVER (
                            PARTITION BY user_id ORDER BY task_date) AS task_number FROM tasks
                            ) 
                        WHERE user_id = $1 AND task_number = ANY($2));
                        """,
                    user_id,
                    task,
                )
                print(result)
                return result != "DELETE 0"
    return False


async def add_deadline_to_db(dp, user_id, deadline):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                last_task = await connection.fetchrow(
                    """
                    SELECT ctid FROM tasks WHERE user_id = $1 ORDER BY task_date DESC LIMIT 1;
                    """,
                    user_id,
                )
                if last_task:
                    task_ctid = last_task["ctid"]
                    await connection.execute("""UPDATE tasks SET deadline = $1 WHERE ctid = $2;""", deadline, task_ctid)


async def get_user_tasks(dp, user_id):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            return await connection.fetch(
                """
                SELECT task, deadline AT TIME ZONE (
                    SELECT tz::interval FROM users WHERE user_id = $1
                    ) AS deadline
                FROM tasks WHERE user_id = $1 ORDER BY task_date;
            """,
                user_id,
            )


async def get_tz(dp, user_id):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            return await connection.fetchval(
                """
                SELECT tz FROM users WHERE user_id = $1;
            """,
                user_id,
            )


async def add_timezone_to_db(dp, user_id, timezone):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("""UPDATE users SET tz = $1 WHERE user_id = $2""", timezone, user_id)


async def clear_list(dp, user_id):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("""DELETE FROM tasks WHERE user_id = $1""", user_id)


class AddTask(StatesGroup):
    WaitingForTask = State()


class RemoveTask(StatesGroup):
    WaitingForTask = State()


class SetDeadline(StatesGroup):
    WaitingForDeadline = State()


class AddTimezone(StatesGroup):
    WaitingForTimezone = State()


@dp.message_handler(commands=["start", "help"])
async def send_welcome(message: types.Message):
    message_text = "Привет! Я твой бот-помощник для задач. Добавь задачу, отметь ее как выполненную или удали."
    await add_user_to_db(dp, message.from_user.id)
    await message.answer(message_text, reply_markup=list_keyboard)


@dp.message_handler(commands=["timezone"])
async def get_timezone(message: types.Message):
    await AddTimezone.WaitingForTimezone.set()
    await message.answer("Пожалуйста, укажите ваш часовой пояс в формате ±ЧЧ:ММ")


@dp.message_handler(commands=["list"])
async def list_tasks(message: types.Message):
    user_id = message.from_user.id
    user_tasks = await get_user_tasks(dp, user_id)
    if user_tasks:
        task_list = "\n".join(
            f"<b>{str(i + 1)}.</b>\n    <b>Задача:</b> <i>{task['task']}</i>\n    <b>Дедлайн:</b> <i>{task['deadline'].strftime('%H:%M %d.%m.%y') if task['deadline'] else 'не установлен'}</i>"
            for i, task in enumerate(user_tasks)
        )
        await message.answer(f"Ваши задачи:\n{task_list}", parse_mode="html", reply_markup=task_keyboard)
    else:
        await message.answer("У вас нет активных задач.", reply_markup=add_task_keyboard)


@dp.callback_query_handler(lambda c: c.data == "clear")
async def callback_clear(call: types.callback_query):
    await clear_list(dp, call.from_user.id)
    await call.message.edit_text("Список задач очищен.", reply_markup=add_task_keyboard)


@dp.callback_query_handler(lambda c: c.data == "add")
async def callback_add(call: types.callback_query):
    await AddTask.WaitingForTask.set()
    await call.message.edit_text("Пожалуйста, укажите задачу для добавления.")


@dp.callback_query_handler(lambda c: c.data == "remove")
async def callback_remove(call: types.callback_query):
    await RemoveTask.WaitingForTask.set()
    await call.message.edit_text("Пожалуйста, укажите номера задач для удаления через запятую.")


@dp.callback_query_handler(lambda c: c.data == "list")
async def callback_list(call: types.callback_query):
    user_id = call.from_user.id
    user_tasks = await get_user_tasks(dp, user_id)
    if user_tasks:
        task_list = "\n".join(
            f"<b>{str(i + 1)}.</b>\n    <b>Задача:</b> <i>{task['task']}</i>\n    <b>Дедлайн:</b> <i>{task['deadline'].strftime('%H:%M %d.%m.%y') if task['deadline'] else 'не установлен'}</i>"
            for i, task in enumerate(user_tasks)
        )
        await call.message.edit_text(f"Ваши задачи:\n{task_list}", parse_mode="html", reply_markup=task_keyboard)
    else:
        await call.message.edit_text("У вас нет активных задач.", reply_markup=add_task_keyboard)


@dp.callback_query_handler(lambda c: c.data == "deadline")
async def callback_deadline(call: types.callback_query):
    await SetDeadline.WaitingForDeadline.set()
    await call.message.edit_text("Введите дату и время в формате ГГГГ-ММ-ДД чч:мм:сс")


# @dp.message_handler(commands=['add'])
# async def add_task(message: types.Message, state: FSMContext):
#     # Сообщаем ожидаемому состоянию, что мы ждем ввода задачи
#     await AddTask.WaitingForTask.set()
#     await message.answer("Пожалуйста, укажите задачу для добавления.")

# @dp.message_handler(commands=['remove'])
# async def remove_task_start(message: types.Message, state: FSMContext):
#     await RemoveTask.WaitingForTask.set()
#     await message.answer("Пожалуйста, укажите задачу для удаления.")


@dp.message_handler(state=AddTask.WaitingForTask)
async def process_task(message: types.Message, state: FSMContext):
    task = message.text
    user_id = message.from_user.id
    await add_task_to_db(dp, user_id, task)
    await state.finish()
    await message.answer(f"Задача '{task}' добавлена.", reply_markup=after_add_keyboard)


@dp.message_handler(state=RemoveTask.WaitingForTask)
async def remove_task(message: types.Message, state: FSMContext):
    task = [int(i.strip()) for i in message.text.split(",") if i.strip().isdigit()]
    user_id = message.from_user.id
    removed = await remove_task_from_db(dp, user_id, task)
    await state.finish()
    if removed:
        await message.answer(f"Задача(и) '{', '.join(map(str, task))}' удалена(ы).", reply_markup=list_keyboard)
    else:
        await message.answer("Указанной задачи не найдено.", reply_markup=list_keyboard)


@dp.message_handler(state=SetDeadline)
async def set_deadline(message: types.Message, state: FSMContext):
    deadline_str = message.text + " " + await get_tz(dp, message.from_user.id)
    deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S %z")
    user_id = message.from_user.id
    await add_deadline_to_db(dp, user_id, deadline)
    await state.finish()
    await message.answer(f"Дедлайн добавлен.", reply_markup=list_keyboard)


@dp.message_handler(state=AddTimezone)
async def set_timezone(message: types.Message, state: FSMContext):
    timezone = message.text
    user_id = message.from_user.id
    await add_timezone_to_db(dp, user_id, timezone)
    await state.finish()
    await message.answer(f"Часовой пояс добавлен.", reply_markup=list_keyboard)


async def remind():
    db_pool = dp.get("db_pool")
    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("""SELECT * FROM tasks WHERE deadline >= $1""", datetime.now(timezone.utc))
        for task in tasks:
            if not task["is_reminded"]:
                if (task["deadline"] - datetime.now(timezone.utc)) <= timedelta(days=1):
                    user_id = task["user_id"]
                    task_name = task["task"]
                    await conn.execute(
                        "UPDATE tasks SET is_reminded = TRUE WHERE user_id = $1 AND task = $2", user_id, task_name
                    )
                    await bot.send_message(user_id, f'Напоминание: у задачи "{task_name}" остался только один день!')


async def run_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(remind, "interval", minutes=1, start_date="2023-12-06 00:00:00")
    scheduler.start()


if __name__ == "__main__":
    from aiogram import executor

    executor.start(dp, set_main_menu(bot))
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
