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

# Создаем единый экземпляр Transcriber для прогресс-бара
_progress_transcriber = None

async def init_progress_transcriber():
    global _progress_transcriber
    if _progress_transcriber is None:
        _progress_transcriber = await Transcriber.create()

async def update_progress(chat_id: int, message_id: int, percentage: int, error_message: str = None):
    try:
        if _progress_transcriber is None:
            await init_progress_transcriber()
        bar = _progress_transcriber.get_progress_bar(percentage, error_message)
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=bar)
    except Exception as e:
        logger.error(f"Error updating progress: {e}")

def make_callback(chat_id: int, message_id: int, loop: asyncio.AbstractEventLoop):
    def _cb(pct: int, err: str = None):
        try:
            future = asyncio.run_coroutine_threadsafe(
                update_progress(chat_id, message_id, pct, err), loop
            )
            # Ждем завершения операции с таймаутом
            future.result(timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Progress update timeout")
        except Exception as e:
            logger.error(f"Error in progress callback: {e}")
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
    transcriber = None
    try:
        # Определяем вложение и расширение
        if message.voice:
            attachment = message.voice
            raw_ext = ".ogg"
            is_video = False
            file_size = attachment.file_size
        elif message.audio:
            attachment = message.audio
            raw_ext = os.path.splitext(attachment.file_name or "")[1] or ".mp3"
            is_video = False
            file_size = attachment.file_size
        elif message.video:
            attachment = message.video
            raw_ext = os.path.splitext(attachment.file_name or "")[1] or ".mp4"
            is_video = True
            file_size = attachment.file_size
        else:  # DOCUMENT
            attachment = message.document
            raw_ext = os.path.splitext(attachment.file_name or "")[1]
            is_video = False
            file_size = attachment.file_size

        # Проверяем размер файла до скачивания
        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            max_mb = MAX_FILE_SIZE / (1024 * 1024)
            if is_video:
                return await message.reply(
                    f"❌ Видео слишком большое ({size_mb:.1f} МБ). "
                    f"Максимальный размер: {max_mb:.1f} МБ.\n"
                    "Пожалуйста, отправьте видео меньшего размера или сожмите его.\n"
                    "Рекомендации:\n"
                    "1. Уменьшите разрешение видео\n"
                    "2. Уменьшите битрейт\n"
                    "3. Разбейте длинное видео на части"
                )
            else:
                return await message.reply(
                    f"❌ Файл слишком большой ({size_mb:.1f} МБ). "
                    f"Максимальный размер: {max_mb:.1f} МБ.\n"
                    "Пожалуйста, отправьте файл меньшего размера или сожмите его."
                )

        ext = raw_ext.lstrip(".").lower()
        logger.info(f"Processing file with extension: {ext}")

        # Проверяем поддерживаемость формата
        if not message.voice:
            all_formats = SUPPORTED_FORMATS.get("audio", []) + SUPPORTED_FORMATS.get("video", [])
            if ext not in all_formats:
                return await message.reply(MESSAGES["unsupported_format"])

        # Сообщение о старте обработки
        status_msg = await message.reply(MESSAGES["processing_start"])

        # Создаем путь к файлу
        file_path = os.path.join(TEMP_DIR, f"{attachment.file_id}.{ext}")
        logger.info(f"Preparing to download file {attachment.file_id} to {file_path}")

        try:
            # Скачиваем файл через Bot API
            logger.info(f"Downloading file {attachment.file_id}")
            file_info = await bot.get_file(attachment.file_id)
            await bot.download_file(file_info.file_path, destination=file_path)
            logger.info(f"File downloaded successfully to {file_path}")

            # Готовим колбэк прогресса
            loop = asyncio.get_event_loop()
            progress_cb = make_callback(message.chat.id, status_msg.message_id, loop)

            # Транскрибация
            logger.info("Creating Transcriber instance")
            transcriber = await Transcriber.create()
            try:
                logger.info("Starting file processing")
                if is_video:
                    logger.info("Extracting audio from video")
                    # Обновляем прогресс
                    await update_progress(message.chat.id, status_msg.message_id, 10)
                    # Извлекаем аудио из видео
                    audio_path = await transcriber.extract_audio(file_path)
                    logger.info(f"Audio extracted to {audio_path}")
                    # Обновляем прогресс
                    await update_progress(message.chat.id, status_msg.message_id, 20)
                    # Обрабатываем аудио
                    text = await transcriber.process_file(audio_path, progress_cb)
                    # Удаляем временный аудиофайл
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                        logger.info(f"Removed temp audio file: {audio_path}")
                else:
                    text = await transcriber.process_file(file_path, progress_cb)
                
                logger.info("File processing completed")
                # Проверяем текст перед отправкой
                if not text or text.isspace():
                    await message.reply(MESSAGES["processing_error"] + "\n❌ Не удалось распознать текст")
                else:
                    # Отправляем результат
                    await message.reply(text)
            finally:
                logger.info("Cleaning up Transcriber")
                await transcriber.__aexit__(None, None, None)

        except Exception as e:
            logger.error(f"Error processing file: {str(e)}", exc_info=True)
            await message.reply(MESSAGES["processing_error"])

        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Deleted temp file {file_path}")

    except Exception as e:
        logger.error(f"Error processing file: {str(e)}", exc_info=True)
        await message.reply(MESSAGES["processing_error"])

async def main():
    logger.info("Bot started")
    # Инициализируем Transcriber для прогресс-бара
    await init_progress_transcriber()
    try:
        await dp.start_polling(bot)
    finally:
        # Закрываем Transcriber при завершении работы бота
        if _progress_transcriber is not None:
            await _progress_transcriber.__aexit__(None, None, None)

if __name__ == "__main__":
    asyncio.run(main())
