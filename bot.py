import asyncio
import logging
import sys
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import Command
from aiogram.types import Message, ContentType

from config import BOT_TOKEN, MESSAGES, SUPPORTED_FORMATS, MAX_FILE_SIZE, TEMP_DIR
from transcriber import Transcriber

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Отдельный экземпляр только для прогресс-бара
_progress_transcriber = Transcriber()

async def update_progress(chat_id: int, message_id: int, percentage: int, error_message: str = None):
    try:
        bar = _progress_transcriber.get_progress_bar(percentage, error_message)
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=bar)
    except Exception as e:
        logger.error(f"Error updating progress: {e}")

def make_callback(chat_id: int, message_id: int, loop: asyncio.AbstractEventLoop):
    def _cb(pct: int, err: str = None):
        asyncio.run_coroutine_threadsafe(
            update_progress(chat_id, message_id, pct, err), loop
        )
    return _cb

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(MESSAGES["start"])

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(MESSAGES["help"])

@dp.message(F.content_type.in_([
    ContentType.VOICE,
    ContentType.AUDIO,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
]))
async def handle_media(message: Message):
    file_path = None
    try:
        # Определяем вложение и расширение
        if message.voice:
            attachment = message.voice
            raw_ext = ".ogg"
            is_video = False
        elif message.audio:
            attachment = message.audio
            raw_ext = os.path.splitext(attachment.file_name or "")[1] or ".mp3"
            is_video = False
        elif message.video:
            attachment = message.video
            raw_ext = os.path.splitext(attachment.file_name or "")[1] or ".mp4"
            is_video = True
        else:  # DOCUMENT
            attachment = message.document
            raw_ext = os.path.splitext(attachment.file_name or "")[1]
            is_video = False

        ext = raw_ext.lstrip(".").lower()

        # Проверяем поддерживаемость формата:
        # — все голосовые (message.voice) пропускаем сразу,
        # — для остальных проверяем наличие в SUPPORTED_FORMATS
        if not message.voice:
            all_formats = SUPPORTED_FORMATS.get("audio", []) + SUPPORTED_FORMATS.get("video", [])
            if ext not in all_formats:
                return await message.reply(MESSAGES["unsupported_format"])

        # Сообщение о старте обработки
        status_msg = await message.reply(MESSAGES["processing_start"])

        # Скачиваем файл через Bot API
        file_path = os.path.join(TEMP_DIR, f"{attachment.file_id}.{ext}")
        logger.info(f"Downloading file {attachment.file_id} to {file_path}")
        file_info = await bot.get_file(attachment.file_id)
        await bot.download_file(file_info.file_path, destination=file_path)

        # Проверка размера (для аудио)
        if not is_video and os.path.getsize(file_path) > MAX_FILE_SIZE:
            return await message.reply(MESSAGES["file_too_large"])

        # Готовим колбэк прогресса
        loop = asyncio.get_event_loop()
        progress_cb = make_callback(message.chat.id, status_msg.message_id, loop)

        # Транскрибация
        async with Transcriber() as transcriber:
            text = await transcriber.process_file(file_path, progress_cb)

        # Отправляем результат
        await message.reply(text)

    except Exception as e:
        logger.error(f"Error processing file: {e}")
        await message.reply(MESSAGES["processing_error"])

    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Deleted temp file {file_path}")

async def main():
    logger.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
