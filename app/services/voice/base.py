import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Type

logger = logging.getLogger(__name__)

class VoiceCapability(Enum):
    STT = "speech_to_text"
    TTS = "text_to_speech"
    BOTH = "both"

class ProviderRegistry:
    """Central registry for all voice providers."""
    _providers: Dict[str, Type['VoiceProvider']] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(provider_cls: Type['VoiceProvider']):
            cls._providers[name] = provider_cls
            return provider_cls
        return decorator

    @classmethod
    def get_all(cls) -> Dict[str, Type['VoiceProvider']]:
        return cls._providers

class VoiceProvider(ABC):
    """Abstract Base Class for Voice Providers."""
    
    @property
    @abstractmethod
    def capabilities(self) -> VoiceCapability:
        pass

    async def transcribe(self, audio_data: bytes, **kwargs) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} does not support STT")

    async def synthesize(self, text: str, **kwargs) -> bytes:
        raise NotImplementedError(f"{self.__class__.__name__} does not support TTS")