import os
import asyncio
import aiohttp
import logging
from config import ASSEMBLYAI_CONFIG, SUPPORTED_FORMATS, MESSAGES, MAX_FILE_SIZE, PROGRESS_BAR

logger = logging.getLogger(__name__)

class TranscriptionError(Exception):
    """Исключение для ошибок транскрибации"""
    def __init__(self, message: str, progress: int = 0):
        super().__init__(message)
        self.message = message
        self.progress = progress

class Transcriber:
    """Асинхронный транскрайбер для AssemblyAI"""
    def __init__(self):
        cfg = ASSEMBLYAI_CONFIG
        self.base_url = cfg["base_url"]
        self.headers = cfg["headers"]
        self.session: aiohttp.ClientSession = None
        self._stop_event: asyncio.Event = None
        logger.info(f"Transcriber initialized with base_url: {self.base_url}")

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers=self.headers)
        self._stop_event = asyncio.Event()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def extract_audio(self, video_path: str) -> str:
        """Извлекает аудио из видео с помощью ffmpeg"""
        audio_path = os.path.splitext(video_path)[0] + '.mp3'
        cmd = ['ffmpeg', '-i', video_path, '-vn', '-acodec', 'libmp3lame', '-y', audio_path]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode() or 'ffmpeg error'
            logger.error(f"ffmpeg extraction error: {err}")
            raise TranscriptionError("Не удалось извлечь аудио", 0)
        return audio_path

    async def upload_file(self, file_path: str) -> str:
        """Загружает файл на AssemblyAI и возвращает URL"""
        logger.info(f"Uploading file: {file_path}")
        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE:
            raise TranscriptionError("Файл слишком большой для загрузки", 0)
        async with self.session.post(
            f"{self.base_url}/upload",
            data=open(file_path, 'rb')
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        url = data.get('upload_url')
        logger.info(f"File uploaded: {url}")
        return url

    async def _simulate_progress(self, start: int, end: int, duration: int, callback):
        """Симулирует прогресс запроса"""
        start_time = asyncio.get_event_loop().time()
        while not self._stop_event.is_set():
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= duration:
                break
            pct = start + int((elapsed / duration) * (end - start))
            pct = min(pct, end)
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(pct)
                else:
                    callback(pct)
            except Exception:
                logger.exception("Progress callback error")
            await asyncio.sleep(0.5)

    def get_error_message(self, code: str) -> str:
        """Преобразует код ошибки в сообщение"""
        mapping = {
            "audio_too_long": "слишком долгое аудио",
            "audio_too_short": "слишком короткое аудио",
            "invalid_audio": "неверный формат аудио",
            "file_too_large": "слишком большой файл",
            "rate_limit_exceeded": "превышен лимит запросов",
            "insufficient_credits": "недостаточно кредитов",
            "internal_error": "внутренняя ошибка сервиса"
        }
        return mapping.get(code, "неизвестная ошибка")

    async def transcribe_audio(
        self,
        audio_url: str,
        callback=None,
        start_progress: int = 0
    ) -> str:
        """Отправляет аудио на транскрибацию и возвращает текст"""
        self._stop_event.clear()
        payload = {"audio_url": audio_url, "language_code": "ru"}
        async with self.session.post(
            f"{self.base_url}/transcript",
            json=payload
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
        transcript_id = result.get("id")
        task = None
        if callback:
            task = asyncio.create_task(
                self._simulate_progress(start_progress, 95, 30, callback)
            )
        try:
            while True:
                async with self.session.get(
                    f"{self.base_url}/transcript/{transcript_id}"
                ) as status_resp:
                    status_resp.raise_for_status()
                    info = await status_resp.json()
                status = info.get('status')
                if status == 'completed':
                    self._stop_event.set()
                    if task:
                        await task
                        await callback(100)
                    return info.get('text', '')
                if status == 'error':
                    self._stop_event.set()
                    code = info.get('error_code', 'unknown')
                    msg = self.get_error_message(code)
                    if task:
                        await task
                        await callback(95, msg)
                    raise TranscriptionError(msg, 95)
                await asyncio.sleep(3)
        finally:
            if task and not task.done():
                self._stop_event.set()
                await task

    async def process_file(self, file_path: str, callback=None) -> str:
        """Обрабатывает аудио или видео файл и возвращает транскрипцию"""
        ext = os.path.splitext(file_path)[1].lstrip('.')
        try:
            # Видео
            if ext in SUPPORTED_FORMATS.get('video', []):
                if callback:
                    await callback(0)
                audio = await self.extract_audio(file_path)
                if os.path.getsize(audio) > MAX_FILE_SIZE:
                    raise TranscriptionError("Аудио слишком большое после извлечения", 20)
                if callback:
                    await callback(20)
                url = await self.upload_file(audio)
                if callback:
                    await callback(30)
                text = await self.transcribe_audio(url, callback, start_progress=30)
                return text
            # Аудио
            elif ext in SUPPORTED_FORMATS.get('audio', []):
                if callback:
                    await callback(0)
                url = await self.upload_file(file_path)
                if callback:
                    await callback(20)
                return await self.transcribe_audio(url, callback, start_progress=20)
            else:
                return MESSAGES.get('unsupported_format', '')
        except TranscriptionError as e:
            return f"{MESSAGES.get('processing_error','')}\n❌ {e.message}"  
        finally:
            if ext in SUPPORTED_FORMATS.get('video', []) and 'audio' in locals() and os.path.exists(audio):
                os.remove(audio)
                logger.info(f"Removed temp audio: {audio}")
