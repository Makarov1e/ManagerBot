import asyncio
import logging
from datetime import datetime, timedelta, timezone
import time

import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (BotCommand, InlineKeyboardButton,
                           InlineKeyboardMarkup)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import bot_token, api_key, db_host, db_name, db_password, db_user
from message_templates import message_templates

import openai

openai.api_key = api_key

API_TOKEN = bot_token

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
dp.middleware.setup(LoggingMiddleware())

messages = {}
user_languages = {}
is_bot_running = True

add_task_button = InlineKeyboardButton("Добавить задачу", callback_data="add")
remove_task_button = InlineKeyboardButton("Удалить задачу(и)", callback_data="remove")
task_list_button = InlineKeyboardButton("Список задач", callback_data="list")
set_deadline_button = InlineKeyboardButton("Установить дедлайн", callback_data="deadline")
clear_list_button = InlineKeyboardButton("Очистить список задач", callback_data="clear")
edit_task_button = InlineKeyboardButton("Редактировать задачу", callback_data="edit")


task_keyboard = InlineKeyboardMarkup(row_width=1).add(add_task_button, remove_task_button, edit_task_button, clear_list_button)
list_keyboard = InlineKeyboardMarkup(row_width=1).add(task_list_button)
after_add_keyboard = InlineKeyboardMarkup(row_width=1).add(set_deadline_button, task_list_button, add_task_button, )
add_task_keyboard = InlineKeyboardMarkup(row_width=1).add(add_task_button)


async def set_main_menu(bot: Bot):
    main_menu_commands = [
        BotCommand(command="/start", description="Начать работу с ботом"),
        BotCommand(command="/help", description="Помощь и справка"),
        BotCommand(command="/list", description="Список задач"),
        BotCommand(command="/timezone", description="Установить часовой пояс"),
        BotCommand(command="/chatgpt", description="Режим работы с ChatGPT")
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


async def get_task(dp, user_id, task_number):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            return await connection.fetchrow(
                """
                SELECT task, deadline
                FROM (
                    SELECT user_id, task, deadline AT TIME ZONE (SELECT tz::interval FROM users WHERE user_id = $1) AS deadline,
                    ROW_NUMBER () OVER (PARTITION BY user_id ORDER BY task_date) AS task_number FROM tasks
                )
                WHERE user_id = $1 AND task_number = $2;
            """,
                user_id,
                task_number,
            )


async def get_user_tasks(dp, user_id):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            return await connection.fetch(
                """
                SELECT task, is_done, deadline AT TIME ZONE (
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


async def edit_task_deadline_in_db(dp, user_id, task_number, deadline):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE tasks SET deadline = $1, is_reminded = false WHERE ctid IN (
                        SELECT ctid FROM (
                            SELECT ctid, user_id, ROW_NUMBER () OVER (
                            PARTITION BY user_id ORDER BY task_date) AS task_number FROM tasks
                            )
                        WHERE user_id = $2 AND task_number = $3);
                        """,
                    deadline,
                    user_id,
                    task_number
                )


async def edit_task_text_in_db(dp, user_id, task_number, task):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE tasks SET task = $1 WHERE ctid IN (
                        SELECT ctid FROM (
                            SELECT ctid, user_id, ROW_NUMBER () OVER (
                            PARTITION BY user_id ORDER BY task_date) AS task_number FROM tasks
                            )
                        WHERE user_id = $2 AND task_number = $3);
                        """,
                    task,
                    user_id,
                    task_number
                )


async def set_is_done(dp, user_id, task_number):
    db_pool = dp.get("db_pool")
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE tasks SET is_done = TRUE WHERE ctid IN (
                        SELECT ctid FROM (
                            SELECT ctid, user_id, ROW_NUMBER () OVER (
                            PARTITION BY user_id ORDER BY task_date) AS task_number FROM tasks
                            )
                        WHERE user_id = $1 AND task_number = $2);
                        """,
                    user_id,
                    task_number
                )


class AddTask(StatesGroup):
    WaitingForTask = State()


class RemoveTask(StatesGroup):
    WaitingForTask = State()


class SetDeadline(StatesGroup):
    WaitingForDeadline = State()


class AddTimezone(StatesGroup):
    WaitingForTimezone = State()


class EditTask(StatesGroup):
    WaitingForTaskNumber = State()
    WaitingForText = State()
    WaitingForDeadline = State()


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
            f"<b>{str(i + 1)}. {'✅' if task['is_done'] else '❌'}</b>\n    <b>Задача:</b> <i>{task['task']}</i>\n    <b>Дедлайн:</b> <i>{task['deadline'].strftime('%H:%M %d.%m.%y') if task['deadline'] else 'не установлен'}</i>"
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
            f"<b>{str(i + 1)}. {'✅' if task['is_done'] else '❌'}</b>\n    <b>Задача:</b> <i>{task['task']}</i>\n    <b>Дедлайн:</b> <i>{task['deadline'].strftime('%H:%M %d.%m.%y') if task['deadline'] else 'не установлен'}</i>"
            for i, task in enumerate(user_tasks)
        )
        await call.message.edit_text(f"Ваши задачи:\n{task_list}", parse_mode="html", reply_markup=task_keyboard)
    else:
        await call.message.edit_text("У вас нет активных задач.", reply_markup=add_task_keyboard)


@dp.callback_query_handler(lambda c: c.data == "edit")
async def callback_edit(call: types.callback_query):
    await EditTask.WaitingForTaskNumber.set()
    await call.message.edit_text("Пожалуйста, укажите номер задачи для изменения.")


@dp.callback_query_handler(lambda c: c.data == "edit_task_text")
async def callback_edit_task_text(call: types.callback_query):
    await EditTask.WaitingForText.set()
    await call.message.edit_text("Введите новый текст задачи:")


@dp.callback_query_handler(lambda c: c.data == "edit_task_deadline")
async def callback_edit_task_deadline(call: types.callback_query):
    await EditTask.WaitingForDeadline.set()
    await call.message.edit_text("Введите новый дедлайн в формате ГГГГ-ММ-ДД чч:мм:сс")


@dp.callback_query_handler(lambda c: c.data == "edit_task_status")
async def callback_edit_task_status(call: types.callback_query, state: FSMContext):
    task_number = int((await state.get_data())["task_number"])
    await set_is_done(dp, call.from_user.id, task_number)
    await call.message.edit_text("Задача отмечена как выполненная.", reply_markup=list_keyboard)
    await state.finish()


@dp.callback_query_handler(lambda c: c.data == "deadline")
async def callback_deadline(call: types.callback_query):
    await SetDeadline.WaitingForDeadline.set()
    await call.message.edit_text("Введите дату и время в формате ГГГГ-ММ-ДД чч:мм:сс")


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


@dp.message_handler(state=SetDeadline.WaitingForDeadline)
async def set_deadline(message: types.Message, state: FSMContext):
    deadline_str = message.text + " " + await get_tz(dp, message.from_user.id)
    deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S %z")
    user_id = message.from_user.id
    await add_deadline_to_db(dp, user_id, deadline)
    await state.finish()
    await message.answer(f"Дедлайн добавлен.", reply_markup=list_keyboard)


@dp.message_handler(state=AddTimezone.WaitingForTimezone)
async def set_timezone(message: types.Message, state: FSMContext):
    timezone = message.text
    user_id = message.from_user.id
    await add_timezone_to_db(dp, user_id, timezone)
    await state.finish()
    await message.answer(f"Часовой пояс добавлен.", reply_markup=list_keyboard)


@dp.message_handler(state=EditTask.WaitingForTaskNumber)
async def edit_task(message: types.Message, state: FSMContext):
    await state.update_data(task_number=message.text)
    task_number = int(message.text)
    task = await get_task(dp, message.from_user.id, task_number)
    await state.reset_state(with_data=False)
    await message.answer(
        f"<b>Задача:</b> <i>{task['task']}</i>\n<b>Дедлайн:</b> <i>{task['deadline'].strftime('%H:%M %d.%m.%y') if task['deadline'] else 'не установлен'}</i>",
        parse_mode='html',
        reply_markup=InlineKeyboardMarkup(row_width=2).add(InlineKeyboardButton("Изменить задачу", callback_data="edit_task_text"),
                                                InlineKeyboardButton("Изменить дедлайн", callback_data="edit_task_deadline"),
                                                InlineKeyboardButton("Отметить выполнение", callback_data="edit_task_status")))


@dp.message_handler(state=EditTask.WaitingForText)
async def edit_task_text(message: types.Message, state: FSMContext):
    task_number = int((await state.get_data())["task_number"])
    await edit_task_text_in_db(dp, message.from_user.id, task_number, message.text)
    await message.answer("Текст задачи изменен.", reply_markup=list_keyboard)
    await state.finish()


@dp.message_handler(state=EditTask.WaitingForDeadline)
async def edit_task_deadline(message: types.Message, state: FSMContext):
    task_number = int((await state.get_data())["task_number"])
    deadline_str = message.text + " " + await get_tz(dp, message.from_user.id)
    deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M:%S %z")
    await edit_task_deadline_in_db(dp, message.from_user.id, task_number, deadline)
    await message.answer("Дедлайн изменен.", reply_markup=list_keyboard)
    await state.finish()


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


'''ЧАСТЬ КОДА С ЧАТИКОМ'''


@dp.message_handler(commands=['chatgpt'])
async def enable_chatgpt(message: types.Message):
    # Check if ChatGPT is already enabled for the user
    chatgpt_enabled = await dp.storage.get_data(user=message.from_user.id)
    if chatgpt_enabled and chatgpt_enabled.get("chatgpt_enabled", False):
        await message.answer("ChatGPT уже включен.")
    else:
        # Создаем инлайновую клавиатуру с кнопкой "Включить ChatGPT"
        keyboard = InlineKeyboardMarkup()
        enable_button = InlineKeyboardButton("Включить ChatGPT", callback_data='enable_chatgpt')
        disable_button = InlineKeyboardButton("Выключить ChatGPT", callback_data='disable_chatgpt')
        keyboard.add(enable_button, disable_button)

        # Отправляем сообщение с клавиатурой
        await message.answer("Режим ChatGPT", reply_markup=keyboard)


@dp.callback_query_handler(lambda c: c.data == 'enable_chatgpt')
async def enable_chatgpt_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)

    # Отправляем сообщение с подтверждением включения режима ChatGPT
    await bot.send_message(callback_query.from_user.id, "Режим ChatGPT включен. Теперь вы можете отправлять сообщения для общения с ChatGPT.")

    # Переводим бота в режим ответов ChatGPT
    await dp.storage.update_data(user=callback_query.from_user.id, data={"chatgpt_enabled": True})


@dp.callback_query_handler(lambda c: c.data == 'disable_chatgpt')
async def disable_chatgpt_callback(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)

    # Отправляем сообщение с подтверждением выключения режима ChatGPT
    await bot.send_message(callback_query.from_user.id, "Режим ChatGPT выключен. Теперь бот больше не будет отвечать на ваши сообщения.")

    # Отключаем режим ответов ChatGPT
    await dp.storage.update_data(user=callback_query.from_user.id, data={"chatgpt_enabled": False})

    # Автоматически вызываем код, который соответствует команде /start
    await bot.send_message(callback_query.from_user.id, "Можете продолжить работу с таск менеджером при помощи команды /start")


@dp.message_handler()
async def echo_msg(message: types.Message):
    try:
        user_message = message.text
        userid = message.from_user.username

        if is_bot_running:
            if userid not in messages:
                messages[userid] = []
            messages[userid].append({"role": "user", "content": user_message})
            messages[userid].append({"role": "user",
                                    "content": f"chat: {message.chat} Now {time.strftime('%d/%m/%Y %H:%M:%S')} user: {message.from_user.first_name} message: {message.text}"})
            logging.info(f'{userid}: {user_message}')

            should_respond = not message.reply_to_message or message.reply_to_message.from_user.id == bot.id

            if should_respond:
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")

                try:
                    logging.info("Before ChatGPT completion")
                    completion = await openai.ChatCompletion.acreate(
                        model="gpt-3.5-turbo-1106",
                        messages=messages[userid],
                        max_tokens=2500,
                        temperature=0.7,
                        frequency_penalty=0,
                        presence_penalty=0,
                        user=userid
                    )
                    logging.info("After ChatGPT completion")
                    chatgpt_response = completion.choices[0]['message']
                    logging.info(f'ChatGPT response: {chatgpt_response}')

                    messages[userid].append({"role": "assistant", "content": chatgpt_response['content']})
                    logging.info(f'Ответ ChatGPT: {chatgpt_response["content"]}')

                    await message.reply(chatgpt_response['content'])
                except Exception as ex:
                    logging.error(f"Error during ChatGPT completion: {ex}")

    except Exception as ex:
        if ex == "context_length_exceeded":
            language = user_languages.get(message.from_user.id, 'ru')

            await bot.send_chat_action(chat_id=message.chat.id, action="typing")

            await message.reply(message_templates[language]['error'])
            await echo_msg(message)





if __name__ == "__main__":
    from aiogram import executor

    executor.start(dp, set_main_menu(bot))
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
