import logging
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import asyncpg
from config import db_user, db_password, db_host, db_name, bot_token

API_TOKEN = bot_token

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
dp.middleware.setup(LoggingMiddleware())

# Клавиатура для кнопки "Добавить задачу"
add_task_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
add_task_button = KeyboardButton('/add')
add_task_keyboard.add(add_task_button)

# Параметры подключения к базе данных
async def on_startup(dp):
    # Создаем пул подключения к базе данных при старте бота
    dp['db_pool'] = await asyncpg.create_pool(
        user=db_user,
        password=db_password,
        host=db_host,
        database=db_name,
        port=5432  # Используйте целое число, а не строку
    )

async def on_shutdown(dp):
    # Закрываем пул подключения к базе данных при выключении бота
    db_pool = dp.get('db_pool')
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

async def add_task_to_db(dp, user_id, task):
    db_pool = dp.get('db_pool')
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('''
                    INSERT INTO tasks (user_id, task ) VALUES ($1, $2);
                ''', user_id, task)

async def remove_task_from_db(dp, user_id, task):
    db_pool = dp.get('db_pool')
    if db_pool:
        async with db_pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute('''
                    DELETE FROM tasks WHERE user_id = $1 AND task = $2;
                ''', user_id, task)
                return result == 'DELETE 1'
    return False

async def get_user_tasks(dp, user_id):
    db_pool = dp.get('db_pool')
    if db_pool:
        async with db_pool.acquire() as connection:
            return await connection.fetch('''
                SELECT task FROM tasks WHERE user_id = $1;
            ''', user_id)

class AddTask(StatesGroup):
    WaitingForTask = State()

class RemoveTask(StatesGroup):
    WaitingForTask = State()

@dp.message_handler(commands=['start', 'help'])
async def send_welcome(message: types.Message):
    message_text = "Привет! Я твой бот-помощник для задач. Добавь задачу, отметь ее как выполненную или удали."
    await message.answer(message_text, reply_markup=add_task_keyboard)

@dp.message_handler(commands=['add'])
async def add_task(message: types.Message, state: FSMContext):
    # Сообщаем ожидаемому состоянию, что мы ждем ввода задачи
    await AddTask.WaitingForTask.set()
    await message.answer("Пожалуйста, укажите задачу для добавления.")

# Добавьте обработчик для ожидания ввода задачи
@dp.message_handler(state=AddTask.WaitingForTask)
async def process_task(message: types.Message, state: FSMContext):
    task = message.text
    user_id = message.from_user.id
    await add_task_to_db(dp, user_id, task)
    await state.finish()
    await message.answer(f"Задача '{task}' добавлена.")

@dp.message_handler(commands=['list'])
async def list_tasks(message: types.Message):
    user_id = message.from_user.id
    user_tasks = await get_user_tasks(dp, user_id)
    if user_tasks:
        task_list = "\n".join(task['task'] for task in user_tasks)
        await message.answer(f"Ваши задачи:\n{task_list}")
    else:
        await message.answer("У вас нет активных задач.")

@dp.message_handler(commands=['remove'])
async def remove_task_start(message: types.Message, state: FSMContext):
    await RemoveTask.WaitingForTask.set()
    await message.answer("Пожалуйста, укажите задачу для удаления.")

@dp.message_handler(state=RemoveTask.WaitingForTask)
async def remove_task(message: types.Message, state: FSMContext):
    task = message.text
    user_id = message.from_user.id
    removed = await remove_task_from_db(dp, user_id, task)
    await state.finish()
    if removed:
        await message.answer(f"Задача '{task}' удалена.")
    else:
        await message.answer("Указанной задачи не найдено.")

if __name__ == '__main__':
    from aiogram import executor
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown)
