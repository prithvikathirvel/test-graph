import os
import io
import wave
import tempfile
import asyncio
import logging
from google.oauth2 import service_account
from google.cloud import speech, texttospeech

from app.services.voice.base import VoiceProvider, VoiceCapability, ProviderRegistry

logger = logging.getLogger(__name__)

# --- 1. LOCAL WHISPER (STT) ---
@ProviderRegistry.register("whisper")
class LocalWhisperProvider(VoiceProvider):
    """Runs the official Whisper 'base' model 100% locally."""
    def __init__(self):
        import whisper
        # 🧪 Thread-safety: Whisper's model object is NOT thread-safe for parallel inference.
        # We use a Lock to process one request at a time properly.
        self._lock = asyncio.Lock()
        
        try:
            logger.info("Loading Local Whisper 'base' model... this may take a moment.")
            self.model = whisper.load_model("base")
            logger.info("✓ Local Whisper model loaded successfully.")
        except Exception as e:
            logger.error(f"CRITICAL: Failed to load Whisper model: {e}")
            self.model = None

    @property
    def capabilities(self): return VoiceCapability.STT

    def _sync_transcribe(self, audio_data: bytes) -> str:
        """Synchronous CPU-bound transcription."""
        if not self.model: return ""
        
        # 1. Create a safe temporary file on Windows
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload") as f:
            f.write(audio_data)
            path = f.name
            
        try:
            # 2. Run the local model (fp16=False prevents warnings on standard CPUs)
            # Whisper will call FFmpeg internally to decode whatever format we saved.
            result = self.model.transcribe(path, fp16=False)
            return result.get("text", "").strip()
        except Exception as e:
            logger.error(f"Local Whisper Process Error (Ensure FFmpeg is installed): {e}")
            return ""
        finally:
            if os.path.exists(path):
                try: os.remove(path)
                except: pass

    async def transcribe(self, audio_data: bytes, **kwargs) -> str:
        """Async wrapper to prevent FastAPI from freezing during STT."""
        if not audio_data or not self.model: return ""
        
        # ⚡ Acquire lock: Only one thread can use the heavy Whisper model at a time.
        # This prevents the specific "NoneType" or "IndexError" crashes seen in parallel STT calls.
        async with self._lock:
            return await asyncio.to_thread(self._sync_transcribe, audio_data)


# --- 2. PIPER LOCAL (TTS) ---
@ProviderRegistry.register("piper")
class PiperProvider(VoiceProvider):
    """Runs Piper Text-to-Speech locally."""
    def __init__(self):
        from piper import PiperVoice
        model_path = os.getenv("PIPER_MODEL_PATH", "piper_models/en_US-ryan-medium.onnx")
        if os.path.exists(model_path):
            self.voice = PiperVoice.load(model_path)
            logger.info("✓ Local Piper TTS model loaded.")
        else:
            logger.warning(f"Piper model not found at {model_path}. TTS will fail.")

    @property
    def capabilities(self): return VoiceCapability.TTS

    def _sync_synthesize(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            self.voice.synthesize_wav(text, wf)
        return buf.getvalue()

    async def synthesize(self, text: str, **kwargs) -> bytes:
        if not text: return b""
        return await asyncio.to_thread(self._sync_synthesize, text)


# --- 3. GEMINI / GCP (STT & TTS) ---
@ProviderRegistry.register("gemini")
class GeminiVoiceProvider(VoiceProvider):
    """Uses your provided GCP Service Account credentials."""
    def __init__(self):
        creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service-account.json")
        if os.path.exists(creds_path):
            self.creds = service_account.Credentials.from_service_account_file(creds_path)
            self.stt_client = speech.SpeechAsyncClient(credentials=self.creds)
            self.tts_client = texttospeech.TextToSpeechAsyncClient(credentials=self.creds)
            logger.info("✓ Gemini/GCP Voice clients initialized.")
        else:
            logger.error("GCP credentials not found. Gemini Voice disabled.")
            self.stt_client = None

    @property
    def capabilities(self): return VoiceCapability.BOTH

    async def transcribe(self, audio_data: bytes, **kwargs) -> str:
        if not self.stt_client: raise ValueError("Gemini STT not configured.")
        
        audio = speech.RecognitionAudio(content=audio_data)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            language_code="en-US",
        )
        response = await self.stt_client.recognize(config=config, audio=audio)
        return " ".join([res.alternatives[0].transcript for res in response.results])

    async def synthesize(self, text: str, **kwargs) -> bytes:
        if not self.tts_client: raise ValueError("Gemini TTS not configured.")
        
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="en-US", name="en-US-Journey-F")
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
        
        response = await self.tts_client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        return response.audio_content