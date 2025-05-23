import asyncio
import logging
import sys
import os
import re
import io
import subprocess
import aiohttp
import tempfile
from urllib.parse import urlparse, parse_qs, urljoin
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import Command
from aiogram.types import Message, ContentType, FSInputFile

from config import BOT_TOKEN, MESSAGES, SUPPORTED_FORMATS, MAX_FILE_SIZE, TEMP_DIR, GOOGLE_APPLICATION_CREDENTIALS
from transcriber import Transcriber

# Максимум символов в одном сообщении Telegram
TELEGRAM_TEXT_LIMIT = 4096

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

# --- Инициализация Drive API клиента ---
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
_creds = service_account.Credentials.from_service_account_file(
    GOOGLE_APPLICATION_CREDENTIALS,
    scopes=SCOPES
)
drive_service = build('drive', 'v3', credentials=_creds)


def _sync_download(file_id: str) -> io.BytesIO:
    """Скачивает файл из Drive синхронно, возвращает BytesIO."""
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=2 * 1024 * 1024)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        # при желании: print(f"Download {status.progress() * 100:.1f}%")
    buffer.seek(0)
    return buffer


async def download_from_drive(file_id: str) -> io.BytesIO:
    """
    Обёртка для _sync_download, чтобы не блокировать event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_download, file_id)

# Создаем единый экземпляр Transcriber для прогресс-бара
_progress_transcriber = None

# Добавляем регулярное выражение для проверки URL
URL_PATTERN = re.compile(
    r'^(?:http|https)://'  # http:// или https://
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # домен
    r'localhost|'  # localhost
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # IP
    r'(?::\d+)?'  # порт
    r'(?:/?|[/?]\S+)$', re.IGNORECASE)

async def init_progress_transcriber():
    global _progress_transcriber
    if _progress_transcriber is None:
        _progress_transcriber = await Transcriber.create()

async def update_progress(chat_id: int, message_id: int, percentage: int, error_message: str = None):
    try:
        if _progress_transcriber is None:
            await init_progress_transcriber()
        
        # Ограничиваем процент в пределах 0-100
        percentage = max(0, min(100, percentage))
        
        bar = _progress_transcriber.get_progress_bar(percentage, error_message)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=bar)
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
    except Exception as e:
        logger.error(f"Error updating progress: {e}")

def make_callback(chat_id: int, message_id: int, loop: asyncio.AbstractEventLoop):
    last_update = 0
    last_percentage = 0
    start_time = None
    upload_completed = False
    transcription_completed = False
    
    async def update_progress_task():
        nonlocal last_update, last_percentage, upload_completed, start_time, transcription_completed
        while not transcription_completed:
            current_time = asyncio.get_event_loop().time()
            
            if upload_completed and start_time is not None:
                elapsed_time = current_time - start_time
                time_based_progress = min(95, 12 + int(elapsed_time * 2))  # 2% в секунду от 12% до 95%
                
                if time_based_progress > last_percentage:
                    try:
                        await update_progress(chat_id, message_id, time_based_progress)
                        last_percentage = time_based_progress
                    except Exception as e:
                        if "message is not modified" not in str(e):
                            logger.error(f"Error in progress task: {e}")
            
            await asyncio.sleep(0.5)  # Обновляем каждые 0.5 секунды
    
    def _cb(pct: int, err: str = None):
        nonlocal last_update, last_percentage, upload_completed, start_time, transcription_completed
        current_time = asyncio.get_event_loop().time()
        
        # Если транскрибация завершена
        if pct == 100:
            transcription_completed = True
            try:
                future = asyncio.run_coroutine_threadsafe(
                    update_progress(chat_id, message_id, 100, err), loop
                )
                future.result(timeout=3)
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.error(f"Error in progress callback: {e}")
            return
        
        # Инициализируем start_time при первом вызове
        if start_time is None:
            start_time = current_time
        
        # Если загрузка завершена, начинаем плавное увеличение
        if pct >= 12 and not upload_completed:
            upload_completed = True
            start_time = current_time
            # Запускаем задачу обновления прогресса
            asyncio.run_coroutine_threadsafe(update_progress_task(), loop)
        
        # Обновляем прогресс до загрузки, но не показываем 0%
        if not upload_completed and pct > 0:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    update_progress(chat_id, message_id, pct, err), loop
                )
                future.result(timeout=3)
                last_update = current_time
                last_percentage = pct
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.error(f"Error in progress callback: {e}")
    
    return _cb

@dp.message(F.text.regexp(r'https?://drive\.google\.com/.*'))
async def handle_google_drive(message: Message):
    """
    Скачиваем файл из Google Drive, транскрибируем и публикуем результат.
    """
    # 1) Извлекаем file_id
    m = re.search(r'(?:file/d/|id=)([A-Za-z0-9_-]+)', message.text)
    if not m:
        return await message.reply("Не удалось распознать ID файла в ссылке.")
    file_id = m.group(1)

    # 2) Стартовое сообщение и статус
    status_msg = await message.reply("Скачиваю файл из Google Drive…")

    local_path = None
    try:
        # 3) Берём метаданные, чтобы получить имя и mimeType
        meta = drive_service.files().get(
            fileId=file_id,
            fields="name,mimeType"
        ).execute()
        filename = meta.get("name", file_id)
        mime = meta.get("mimeType", "")
        is_video = mime.startswith("video/")

        # 4) Скачиваем в память и сохраняем в temp
        buf = await download_from_drive(file_id)  # :contentReference[oaicite:0]{index=0}
        os.makedirs(TEMP_DIR, exist_ok=True)
        local_path = os.path.join(TEMP_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(buf.getvalue())

        # 5) Обновляем прогресс загрузки
        await update_progress(message.chat.id, status_msg.message_id, 5)

        # 6) Готовим колбэк и запускаем транскрипцию
        loop = asyncio.get_event_loop()
        progress_cb = make_callback(message.chat.id, status_msg.message_id, loop)
        transcriber = await Transcriber.create()

        try:
            if is_video:
                # извлекаем аудио и транскрибируем его
                audio_path = await transcriber.extract_audio(local_path)
                text = await transcriber.process_file(audio_path, progress_cb)
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            else:
                text = await transcriber.process_file(local_path, progress_cb)
        finally:
            # завершаем работу транскрайбера
            await transcriber.__aexit__(None, None, None)

        # 7) Отправляем результат или ошибку
        if not text or text.isspace():
            await message.reply(
                MESSAGES["processing_error"] + "\n❌ Не удалось распознать текст"
            )
        else:
            if len(text) <= TELEGRAM_TEXT_LIMIT:
                await message.reply(text)
            else:
                txt_filename = f"transcript_{file_id}.txt"
                txt_path = os.path.join(TEMP_DIR, txt_filename)
                with open(txt_path, "w", encoding="utf-8") as txt_file:
                    txt_file.write(text)

                # Отправляем файл как документ
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=FSInputFile(txt_path),
                    caption="📄 Результат транскрипции"
                )
                os.remove(txt_path)
            await update_progress(message.chat.id, status_msg.message_id, 100)
    except Exception as e:
        logger.error(f"Error in handle_google_drive: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка: {str(e).splitlines()[0]}")
    finally:
        # 8) Убираем временный файл
        if local_path and os.path.exists(local_path):
            os.remove(local_path)



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

        # Первичная проверка размера файла (50 МБ)
        if file_size > 50 * 1024 * 1024:  # 50 МБ в байтах
            return await message.reply(
                "⚠️ Файл слишком большой для прямой загрузки через Telegram "
                "(больше 50 МБ).\n"
                "Пожалуйста, загрузите его на любой файлообменник "
                "(Google Drive, Dropbox, Yandex.Disk и т.п.) и пришлите мне ссылку."
            )

        # Проверяем размер файла для транскрибации
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
            
            # Обновляем прогресс после скачивания
            await update_progress(message.chat.id, status_msg.message_id, 5)

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
                    # Извлекаем аудио из видео
                    audio_path = await transcriber.extract_audio(file_path)
                    logger.info(f"Audio extracted to {audio_path}")
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
                    # Показываем 100% после успешной отправки
                    await update_progress(message.chat.id, status_msg.message_id, 100)
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
