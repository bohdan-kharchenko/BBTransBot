# Бот для транскрибации аудио и видео

Telegram бот для автоматической транскрибации аудио и видео файлов в текст.

## Возможности

- Поддержка аудио файлов (MP3, WAV, OGG)
- Поддержка видео файлов (MP4, AVI, MOV)
- Распознавание русской речи
- Простой интерфейс через Telegram

## Установка

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd <repository-name>
```

2. Установите зависимости:
```bash
pip install -r requirements.txt
```

3. Создайте файл `.env` и добавьте в него токен вашего бота:
```
BOT_TOKEN=your_bot_token_here
```

## Запуск

```bash
python bot.py
```

## Использование

1. Найдите бота в Telegram по его имени
2. Отправьте команду `/start`
3. Отправьте аудио или видео файл
4. Дождитесь результата транскрибации

## Требования

- Python 3.8+
- FFmpeg (для обработки видео файлов) 