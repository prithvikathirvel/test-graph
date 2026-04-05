# app/services/voice/__init__.py

# Importing this triggers the @ProviderRegistry.register decorators
import app.services.voice.providers 

# Expose the main service and helpers so main.py can import them cleanly
from app.services.voice.service import UniversalVoiceService, should_run_stt, should_run_tts

__all__ = ["UniversalVoiceService", "should_run_stt", "should_run_tts"]