import logging
import asyncio
import os
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext
import requests
import urllib.parse
import nest_asyncio
from datetime import datetime, timedelta
import telegram.error
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.base import JobLookupError
import httpx

# Указываем использование UTF-8 в самом скрипте
sys.stdout.reconfigure(encoding='utf-8')


# Настройка логирования для краткого вывода ошибок
class ShortErrorFormatter(logging.Formatter):
    def formatException(self, exc_info):
        result = super().formatException(exc_info)
        return f"{exc_info[0].__name__}: {exc_info[1]}"


# Устанавливаем форматтер и уровень логирования
formatter = ShortErrorFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)

# Отключение логирования httpx на уровне INFO
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)


# Специальный обработчик для отправки print сообщений в Telegram-бот
class PrintToTelegramHandler:
    def __init__(self, bot_token, chat_id):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

    def send_message(self, message):
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(self._send_message(message))
        else:
            asyncio.run(self._send_message(message))

    async def _send_message(self, message):
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message)
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение в Telegram: {e}")


print_handler = PrintToTelegramHandler(bot_token=os.environ["TELEGRAM_LOG_BOT_TOKEN"], chat_id=os.environ["TELEGRAM_LOG_CHAT_ID"])


def print_to_telegram(message):
    print(message)
    print_handler.send_message(message)


# Константы конфигурации
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # Токен вашего основного бота
TONKEEPER_TEST_URL = 'https://app.tonkeeper.com/transfer'  # URL для Tonkeeper Wallet тестовой сети
TONAPI_CHECK_PAYMENT_URL = 'https://toncenter.com/api/v2/getTransactions'  # URL для проверки платежа в реальной сети
# TONAPI_CHECK_PAYMENT_URL = 'https://testnet.toncenter.com/api/v2/getTransactions'  # URL для проверки платежа в тестовой сети
TON_WALLET_ADDRESS = 'YOUR_TON_WALLET_ADDRESS'  # Адрес вашего кошелька TON в тестовой сети
CHECK_INTERVAL = 10  # Проверочный интервал в секундах
# EXPECTED_AMOUNT = '100000000'  # Ожидаемая сумма (для тестовой сети)
EXPECTED_AMOUNT = '2000000000'  # Ожидаемая сумма (2 Toncoin)

# Переменные для хранения состояния
last_checked_lt = 0  # Время последней проверенной транзакции
first_check_done = {}  # Отслеживание первого выполнения проверки платежа


# Функция для повторных попыток выполнения асинхронных функций
async def retry_async(func, *args, retries=5, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return await func(*args, **kwargs)
        except (telegram.error.NetworkError, telegram.error.TimedOut, httpx.ConnectError) as e:
            logging.error(f"Ошибка сети: {e}. Повторная попытка {attempt + 1} из {retries}.")
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    logging.error(f"Не удалось выполнить функцию {func.__name__} после {retries} попыток.")


# Обработчик команды /start
async def start(update: Update, context: CallbackContext) -> None:
    user_language = update.message.from_user.language_code
    logging.info(f"User language code: {user_language}")

    if user_language == 'ru':
        keyboard = [
            [InlineKeyboardButton("Купить бота для Blum", callback_data='buy_bot_blum')],
            [InlineKeyboardButton("Купить бота для Clayton Game", callback_data='buy_bot_clayton')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await retry_async(update.message.reply_text,
                          '*Боты для автоматизации процесса игры в Telegram-приложении "Blum" и "Clayton Game".*\n\n Стоимость любого бота составляет *2 Toncoin*. '
                          'Для оплаты необходимо иметь установленное приложение Tonkeeper на смартфоне или на компьютере. '
                          'После проведения оплаты появится ссылка для скачивания бота.🤖\n\n'
                          '🔹Бот для в Telegram-приложения *"Blum"* автоматизирует игру с падающими цветами.\n\n'
                          '🔹Бот для в Telegram-приложения *"Clayton Game"* автоматизирует игру "512".\n\n',
                          reply_markup=reply_markup, parse_mode='Markdown')
    else:
        keyboard = [
            [InlineKeyboardButton("Buy Blum bot", callback_data='buy_bot_blum')],
            [InlineKeyboardButton("Buy Clayton Game bot", callback_data='buy_bot_clayton')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await retry_async(update.message.reply_text,
                          '*Bots for automating the gaming process in the "Blum" and "Clayton Game" Telegram applications.*\n\n The cost of any bot is *2 Toncoin*. '
                          'To make a payment, you need to have the Tonkeeper app installed on your smartphone or computer. '
                          'After completing the payment, a link to download the bot will appear.🤖\n\n'
                          '🔹The bot for the Telegram application *"Blum"* automates the game with falling flowers.\n\n'
                          '🔹The bot for the Telegram application *"Clayton Game"* automates the "512" game.\n\n',
                          reply_markup=reply_markup, parse_mode='Markdown')


# Обработчик нажатия на кнопку "Купить бота для Blum"
async def buy_bot(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    print_to_telegram(f"User ID: {user_id}")

    payment_link = generate_tonkeeper_payment_link(TON_WALLET_ADDRESS, EXPECTED_AMOUNT, f'Payment from user {user_id}')
    user_language = query.from_user.language_code

    if user_language == 'ru':
        keyboard = [[InlineKeyboardButton("Оплатить 2 Toncoin", url=payment_link)]]
        await retry_async(context.bot.send_message, chat_id=query.message.chat_id,
                          text="Оплатите бота, нажав на кнопку ниже:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[InlineKeyboardButton("Pay 2 Toncoin", url=payment_link)]]
        await retry_async(context.bot.send_message, chat_id=query.message.chat_id,
                          text="Pay for the bot by clicking the button below:",
                          reply_markup=InlineKeyboardMarkup(keyboard))

    job_id = f"check_payment_{user_id}_blum"
    first_check_done[user_id] = False
    scheduler.add_job(check_payment, 'interval', seconds=CHECK_INTERVAL,
                      args=[context, user_id, query.message.chat_id, job_id, 'blum'], id=job_id, max_instances=3)
    scheduler.add_job(cancel_payment_check, 'date', run_date=datetime.now() + timedelta(minutes=12),
                      args=[job_id, context, query.message.chat_id], id=f"cancel_{job_id}")
    print_to_telegram(f"Создан новый счёт на оплату: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# Обработчик нажатия на кнопку "Купить бота для Clayton Game"
async def buy_bot_clayton(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    print_to_telegram(f"User ID: {user_id}")

    payment_link = generate_tonkeeper_payment_link(TON_WALLET_ADDRESS, EXPECTED_AMOUNT, f'Payment from user {user_id}')
    user_language = query.from_user.language_code

    if user_language == 'ru':
        keyboard = [[InlineKeyboardButton("Оплатить 2 Toncoin", url=payment_link)]]
        await retry_async(context.bot.send_message, chat_id=query.message.chat_id,
                          text="Оплатите бота, нажав на кнопку ниже:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[InlineKeyboardButton("Pay 2 Toncoin", url=payment_link)]]
        await retry_async(context.bot.send_message, chat_id=query.message.chat_id,
                          text="Pay for the bot by clicking the button below:",
                          reply_markup=InlineKeyboardMarkup(keyboard))

    job_id = f"check_payment_{user_id}_clayton"
    first_check_done[user_id] = False
    scheduler.add_job(check_payment, 'interval', seconds=CHECK_INTERVAL,
                      args=[context, user_id, query.message.chat_id, job_id, 'clayton'], id=job_id, max_instances=3)
    scheduler.add_job(cancel_payment_check, 'date', run_date=datetime.now() + timedelta(minutes=12),
                      args=[job_id, context, query.message.chat_id], id=f"cancel_{job_id}")
    print_to_telegram(f"Создан новый счёт на оплату: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def generate_tonkeeper_payment_link(address, amount, text):
    return f"{TONKEEPER_TEST_URL}/{address}?amount={amount}&text={urllib.parse.quote(text)}"


async def check_payment(context: CallbackContext, user_id, chat_id, job_id, bot_type) -> None:
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    user_language = chat_member.user.language_code
    if not first_check_done.get(user_id, False):
        first_check_done[user_id] = True

        if user_language == 'ru':
            await retry_async(context.bot.send_message, chat_id=chat_id, text="Ожидание подтверждения транзакции.")
            print_to_telegram("Ожидание подтверждения транзакции.")
        else:
            await retry_async(context.bot.send_message, chat_id=chat_id, text="Waiting for payment confirmation.")
            print_to_telegram("Waiting for payment confirmation.")

    if await check_payment_status(user_id):
        if bot_type == "blum":
            download_link = "https://drive.google.com/file/d/1k7eKvp6qrGNYiHiGeCddikS6daEf80WW/view?usp=sharing"
        else:
            download_link = "https://drive.google.com/file/d/1xJJanYKLA_HRn8GYdRcfnCQGLyOWeqnc/view?usp=sharing"

        if user_language == 'ru':
            keyboard = [[InlineKeyboardButton("Скачать приложение", url=download_link)]]
            caption = "Оплата прошла успешно! Пожалуйста, скачайте ваше приложение, нажав на кнопку ниже."
        else:
            keyboard = [[InlineKeyboardButton("Download the application", url=download_link)]]
            caption = "Payment was successful! Please download your application by clicking the button below."

        reply_markup = InlineKeyboardMarkup(keyboard)

        await retry_async(context.bot.send_message, chat_id=chat_id, text=caption, reply_markup=reply_markup)
        print_to_telegram(f"Оплата прошла успешно. Ссылка на приложение {download_link} отправлена.")
        scheduler.remove_job(job_id)
        scheduler.remove_job(f"cancel_{job_id}")
        del first_check_done[user_id]


async def cancel_payment_check(job_id, context, chat_id):
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        logging.warning(f"Не найдена задача с id {job_id}")
    await retry_async(context.bot.send_message, chat_id=chat_id,
                      text="Время ожидания подтверждения транзакции истекло. Попробуйте снова.")
    print_to_telegram("Время ожидания подтверждения транзакции истекло. Счёт отменён.")


async def check_payment_status(user_id):
    global last_checked_lt
    headers = {'accept': 'application/json', 'Content-Type': 'application/json'}
    params = {'address': TON_WALLET_ADDRESS, 'limit': 1}
    try:
        response = requests.get(TONAPI_CHECK_PAYMENT_URL, headers=headers, params=params, timeout=20)
        if response.status_code == 200:
            data = response.json()
            transactions = data.get('result', [])
            target_text = f'Payment from user {user_id}'
            new_transaction_found = False

            for tx in transactions:
                in_msg = tx.get('in_msg', {})
                message = in_msg.get('message', '')
                created_lt = int(in_msg.get('created_lt', 0))
                value = in_msg.get('value', '')
                timestamp = datetime.fromtimestamp(int(tx.get('utime', 0)))

                if created_lt > last_checked_lt:
                    if target_text in message and value == EXPECTED_AMOUNT:
                        print_to_telegram(
                            f"Оплата для пользователя {user_id} найдена в транзакции с hash: {tx.get('transaction_id', {}).get('hash', 'unknown')}")
                        print_to_telegram(
                            f"Поступил новый платёж от юзера {user_id} (время оплаты: {timestamp.strftime('%Y-%m-%d %H:%M:%S')})")
                        new_transaction_found = True

            if new_transaction_found:
                last_checked_lt = max(int(tx['in_msg']['created_lt']) for tx in transactions)
                return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка сети при проверке оплаты: {e}")
    return False


def restart_program():
    try:
        print_to_telegram("Перезапуск программы...")
        python = sys.executable
        os.execl(python, python, *sys.argv)
    except Exception as e:
        print_to_telegram(f"Не удалось перезапустить программу: {e}")


async def main() -> None:
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(buy_bot, pattern='^buy_bot_blum$'))
        application.add_handler(CallbackQueryHandler(buy_bot_clayton, pattern='^buy_bot_clayton$'))
        print_to_telegram("Бот начал работу.")
        await application.run_polling(stop_signals=None)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        # restart_program()


if __name__ == '__main__':
    nest_asyncio.apply()
    scheduler = AsyncIOScheduler()
    scheduler.start()
    asyncio.run(main())
