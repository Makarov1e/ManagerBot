import logging
import time
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import openai
from config import bot_token, api_key
from message_templates import message_templates

logging.basicConfig(level=logging.INFO)

bot = Bot(token=bot_token)
dp = Dispatcher(bot)

openai.api_key = api_key

messages = {}
user_languages = {}  # Keep track of user's current language
is_bot_running = True  # Флаг, указывающий, запущен ли бот


# Create keyboard with command buttons
command_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
button_stop = KeyboardButton("/stop")
button_start = KeyboardButton("/start")
button_help = KeyboardButton("/help")
button_about = KeyboardButton("/about")
command_keyboard.add(button_stop, button_start)
command_keyboard.add(button_help, button_about)


async def send_message(user_id, message_key, with_keyboard=True):
    language = user_languages.get(user_id, 'ru')  # Default to English
    message_template = message_templates[language][message_key]

    if with_keyboard:
        await bot.send_message(user_id, message_template, reply_markup=command_keyboard, disable_notification=True)
    else:
        await bot.send_message(user_id, message_template, disable_notification=True)


@dp.callback_query_handler(lambda c: c.data in ['en', 'ru'])
async def process_callback(callback_query: types.CallbackQuery):
    user_languages[callback_query.from_user.id] = callback_query.data
    await bot.answer_callback_query(callback_query.id)


async def generate_image(prompt):
    response = openai.Image.create(
        prompt=prompt,
        n=1,
        size="512x512",
        response_format="url",
    )
    return response['data'][0]['url']


@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    global is_bot_running  # Вернули global для изменения флага
    username = message.from_user.username
    messages[username] = []
    language = user_languages.get(message.from_user.id, 'ru')  # Get the selected language

    if is_bot_running:
        await message.reply(message_templates[language]['start'])
    else:
        is_bot_running = True  # Восстанавливаем работу бота
        await message.reply("Бот возобновил работу и будет отвечать на сообщения.")


@dp.message_handler(commands=['help'])
async def help_cmd(message: types.Message):
    language = user_languages.get(message.from_user.id, 'ru')
    await message.reply(message_templates[language]['help'])


@dp.message_handler(commands=['about'])
async def about_cmd(message: types.Message):
    language = user_languages.get(message.from_user.id, 'ru')
    await message.reply(message_templates[language]['about'])


@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    global is_bot_running  # Используем глобальный флаг
    username = message.from_user.username
    messages[username] = []
    language = user_languages.get(message.from_user.id, 'ru')  # Get the selected language

    if is_bot_running:
        await message.reply(message_templates[language]['start'])
    else:
        await message.reply("Бот остановлен и больше не отвечает на сообщения.")


@dp.message_handler(commands=['stop'])
async def stop_cmd(message: types.Message):
    global is_bot_running  # Используем глобальный флаг
    user_id = message.from_user.id

    if is_bot_running:
        is_bot_running = False
        await bot.send_message(user_id, "Бот остановлен. Он больше не будет отвечать на сообщения.")
    else:
        await bot.send_message(user_id, "Бот уже остановлен.")


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

                completion = await openai.ChatCompletion.acreate(
                    model="gpt-3.5-turbo-1106",
                    messages=messages[userid],
                    max_tokens=2500,
                    temperature=0.7,
                    frequency_penalty=0,
                    presence_penalty=0,
                    user=userid
                )
                chatgpt_response = completion.choices[0]['message']

                messages[userid].append({"role": "assistant", "content": chatgpt_response['content']})
                logging.info(f'ChatGPT response: {chatgpt_response["content"]}')

                await message.reply(chatgpt_response['content'])

    except Exception as ex:
        if ex == "context_length_exceeded":
            language = user_languages.get(message.from_user.id, 'ru')

            await bot.send_chat_action(chat_id=message.chat.id, action="typing")

            await message.reply(message_templates[language]['error'])
            await echo_msg(message)


if __name__ == '__main__':
    executor.start_polling(dp)
