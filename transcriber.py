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

    @classmethod
    async def create(cls):
        """Создает и инициализирует новый экземпляр Transcriber"""
        instance = cls()
        await instance.__aenter__()
        return instance

    async def __aenter__(self):
        if self.session is None:
            self.session = aiohttp.ClientSession(headers=self.headers)
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session is not None:
            await self.session.close()
            self.session = None
        if self._stop_event is not None:
            self._stop_event.set()
            self._stop_event = None

    async def _retry_request(self, func, max_retries=3, delay=1):
        """Выполняет функцию с повторными попытками при ошибках сети"""
        for attempt in range(max_retries):
            try:
                logger.info(f"Attempt {attempt + 1} of {max_retries}")
                return await func()
            except aiohttp.ClientError as e:
                logger.error(f"Network error on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:
                    raise TranscriptionError(f"Ошибка сети после {max_retries} попыток: {str(e)}")
                await asyncio.sleep(delay * (attempt + 1))
                continue

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
        if self.session is None:
            logger.error("Session is None in upload_file")
            raise TranscriptionError("Internal error: session not initialized")
            
        size = os.path.getsize(file_path)
        if size > MAX_FILE_SIZE:
            raise TranscriptionError("Файл слишком большой для загрузки", 0)
        
        async def _upload():
            logger.info("Starting file upload")
            async with self.session.post(
                f"{self.base_url}/upload",
                data=open(file_path, 'rb')
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        
        data = await self._retry_request(_upload)
        url = data.get('upload_url')
        logger.info(f"File uploaded: {url}")
        return url

    async def _simulate_progress(self, start: int, end: int, duration: int, callback):
        """Симулирует прогресс запроса"""
        if not callback:
            return
            
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
            except Exception as e:
                logger.error(f"Progress callback error: {e}")
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

    def get_progress_bar(self, percentage: int, error_message: str = None) -> str:
        """Создает строку прогресс-бара с процентом выполнения"""
        if error_message:
            return f"❌ {error_message}"
        
        cfg = PROGRESS_BAR
        filled_dots = int(percentage / 100 * cfg["total_dots"])
        empty_dots = cfg["total_dots"] - filled_dots
        
        progress_bar = cfg["filled_dot"] * filled_dots + cfg["empty_dot"] * empty_dots
        return cfg["format"].format(
            progress_bar=progress_bar,
            percentage=percentage
        )

    async def transcribe_audio(
        self,
        audio_url: str,
        callback=None,
        start_progress: int = 0
    ) -> str:
        """Отправляет аудио на транскрибацию и возвращает текст"""
        logger.info(f"Starting transcribe_audio for URL: {audio_url}")
        if self.session is None:
            logger.error("Session is None in transcribe_audio")
            raise TranscriptionError("Internal error: session not initialized")
            
        self._stop_event.clear()
        payload = {"audio_url": audio_url, "language_code": "ru"}
        
        async def _create_transcript():
            logger.info("Creating transcript request")
            async with self.session.post(
                f"{self.base_url}/transcript",
                json=payload
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        
        try:
            result = await self._retry_request(_create_transcript)
            transcript_id = result.get("id")
            logger.info(f"Got transcript ID: {transcript_id}")
            
            task = None
            if callback and asyncio.iscoroutinefunction(callback):
                task = asyncio.create_task(
                    self._simulate_progress(start_progress, 95, 30, callback)
                )
            
            while True:
                async def _check_status():
                    logger.info(f"Checking status for transcript {transcript_id}")
                    async with self.session.get(
                        f"{self.base_url}/transcript/{transcript_id}"
                    ) as status_resp:
                        status_resp.raise_for_status()
                        return await status_resp.json()
                
                info = await self._retry_request(_check_status)
                status = info.get('status')
                logger.info(f"Transcript status: {status}")
                
                if status == 'completed':
                    self._stop_event.set()
                    if task:
                        await task
                    if callback and asyncio.iscoroutinefunction(callback):
                        await callback(100)
                    elif callback:
                        callback(100)
                    text = info.get('text', '')
                    if not text:
                        raise TranscriptionError("Получен пустой текст транскрипции", 100)
                    return text
                if status == 'error':
                    self._stop_event.set()
                    code = info.get('error_code', 'unknown')
                    msg = self.get_error_message(code)
                    if task:
                        await task
                    if callback and asyncio.iscoroutinefunction(callback):
                        await callback(95, msg)
                    elif callback:
                        callback(95, msg)
                    raise TranscriptionError(msg, 95)
                await asyncio.sleep(3)
        finally:
            if task and not task.done():
                self._stop_event.set()
                await task

    async def process_file(self, file_path: str, callback=None) -> str:
        """Обрабатывает аудио или видео файл и возвращает транскрипцию"""
        logger.info(f"Starting process_file for {file_path}")
        ext = os.path.splitext(file_path)[1].lstrip('.')
        try:
            # Видео
            if ext in SUPPORTED_FORMATS.get('video', []):
                logger.info("Processing video file")
                if callback and asyncio.iscoroutinefunction(callback):
                    await callback(0)
                elif callback:
                    callback(0)
                audio = await self.extract_audio(file_path)
                if os.path.getsize(audio) > MAX_FILE_SIZE:
                    raise TranscriptionError("Аудио слишком большое после извлечения", 20)
                if callback and asyncio.iscoroutinefunction(callback):
                    await callback(20)
                elif callback:
                    callback(20)
                url = await self.upload_file(audio)
                if callback and asyncio.iscoroutinefunction(callback):
                    await callback(30)
                elif callback:
                    callback(30)
                text = await self.transcribe_audio(url, callback, start_progress=30)
                return text
            # Аудио
            elif ext in SUPPORTED_FORMATS.get('audio', []):
                logger.info("Processing audio file")
                if callback and asyncio.iscoroutinefunction(callback):
                    await callback(0)
                elif callback:
                    callback(0)
                url = await self.upload_file(file_path)
                if callback and asyncio.iscoroutinefunction(callback):
                    await callback(20)
                elif callback:
                    callback(20)
                return await self.transcribe_audio(url, callback, start_progress=20)
            else:
                logger.warning(f"Unsupported format: {ext}")
                return MESSAGES.get('unsupported_format', '')
        except TranscriptionError as e:
            logger.error(f"Transcription error: {e}")
            return f"{MESSAGES.get('processing_error','')}\n❌ {e.message}"  
        finally:
            if ext in SUPPORTED_FORMATS.get('video', []) and 'audio' in locals() and os.path.exists(audio):
                os.remove(audio)
                logger.info(f"Removed temp audio: {audio}")
