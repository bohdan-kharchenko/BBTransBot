import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройки бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")

# Настройки AssemblyAI
ASSEMBLYAI_CONFIG = {
    "base_url": "https://api.assemblyai.com/v2",
    "headers": {
        "authorization": ASSEMBLYAI_API_KEY,
        "content-type": "application/json"
    }
}

# Настройки прогресс-бара
PROGRESS_BAR = {
    "total_dots": 10,
    "empty_dot": "⚪",
    "filled_dot": "🟢",
    "format": "{progress_bar} {percentage}%"
}

# Настройки распознавания речи
SPEECH_RECOGNITION = {
    "language": "ru-RU",
    "timeout": 10,  # таймаут для распознавания в секундах
    "phrase_time_limit": 30  # максимальная длительность фразы в секундах
}

# Поддерживаемые форматы файлов
SUPPORTED_FORMATS = {
    "audio": ["mp3", "wav", "ogg", "m4a", "flac"],
    "video": ["mp4", "avi", "mov", "mkv", "wmv"]
}

# Настройки временных файлов
TEMP_DIR = "temp"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB в байтах (максимальный размер для AssemblyAI)

# Сообщения бота
MESSAGES = {
    "start": "👋 Привет! Я бот для транскрибации аудио и видео файлов.\n\n"
             "Отправьте мне аудио или видео файл, и я преобразую его в текст.\n"
             "Поддерживаемые форматы:\n"
             "🎵 Аудио: MP3, WAV, OGG, M4A, FLAC\n"
             "🎥 Видео: MP4, AVI, MOV, MKV, WMV\n\n"
             "Максимальный размер файла: 500 МБ",
    
    "help": "📝 Как пользоваться ботом:\n\n"
            "1. Отправьте аудио или видео файл\n"
            "2. Дождитесь обработки\n"
            "3. Получите текст транскрипции\n\n"
            "Поддерживаемые форматы:\n"
            "🎵 Аудио: MP3, WAV, OGG, M4A, FLAC\n"
            "🎥 Видео: MP4, AVI, MOV, MKV, WMV\n\n"
            "Максимальный размер файла: 500 МБ",
    
    "file_received": "✅ Файл получен! Начинаю обработку...",
    "processing_start": "⏳ Получил ваш файл! Начинаю обработку...",
    "uploading": "📤 Загружаю файл на сервер...",
    "processing": "🔄 Обрабатываю файл...",
    "file_too_large": "❌ Файл слишком большой. Максимальный размер: 500 МБ",
    "unsupported_format": "❌ Неподдерживаемый формат файла.\n\n"
                         "Поддерживаемые форматы:\n"
                         "🎵 Аудио: MP3, WAV, OGG, M4A, FLAC\n"
                         "🎥 Видео: MP4, AVI, MOV, MKV, WMV",
    "processing_error": "❌ Произошла ошибка при обработке файла. Попробуйте еще раз.",
    "transcription_complete": "Транскрибация завершена!",
    "api_error": "Ошибка сервиса транскрибации. Попробуйте позже."
} 