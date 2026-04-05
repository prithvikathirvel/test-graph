import logging
import base64
from app.services.voice.base import ProviderRegistry, VoiceCapability

logger = logging.getLogger(__name__)

class UniversalVoiceService:
    """Manages routing STT and TTS requests to the correct registered provider."""
    def __init__(self):
        self.providers = {}
        
        # Instantiate all registered providers
        for name, cls in ProviderRegistry.get_all().items():
            try:
                self.providers[name] = cls()
            except Exception as e:
                logger.error(f"Failed to load Voice Provider '{name}': {e}")
                
        logger.info(f"🎧 Active Voice Providers: {list(self.providers.keys())}")

    async def process_stt(self, voice_base64: str, provider_name: str) -> str:
        """Decodes base64 and routes to the selected STT provider."""
        if not voice_base64: return ""
        
        # 1. Strip Data URI prefix if present (e.g., "data:audio/webm;base64,")
        if "," in voice_base64:
            voice_base64 = voice_base64.split(",")[1]
            
        provider = self.providers.get(provider_name)
        if not provider or provider.capabilities not in (VoiceCapability.STT, VoiceCapability.BOTH):
            logger.warning(f"Provider '{provider_name}' not capable of STT. Falling back to empty string.")
            return ""
            
        try:
            audio_bytes = base64.b64decode(voice_base64)
            return await provider.transcribe(audio_bytes)
        except Exception as e:
            logger.error(f"STT Decoding/Processing Error: {e}")
            return ""

    async def process_tts(self, text: str, provider_name: str) -> str:
        """Routes to TTS provider and encodes the result to base64."""
        if not text: return ""
        
        provider = self.providers.get(provider_name)
        if not provider or provider.capabilities not in (VoiceCapability.TTS, VoiceCapability.BOTH):
            logger.warning(f"Provider '{provider_name}' not capable of TTS. Falling back to empty string.")
            return ""
            
        audio_bytes = await provider.synthesize(text)
        return base64.b64encode(audio_bytes).decode("utf-8")

def should_run_stt(config: dict, has_voice: bool) -> bool:
    mode = config.get("mode", "auto")
    return has_voice and mode not in ["text", "text_in_voice_out"]

def should_run_tts(config: dict, has_voice: bool) -> bool:
    mode = config.get("mode", "auto")
    if mode in ["voice_in_voice_out", "text_in_voice_out"]: return True
    if mode == "text": return False
    return has_voice