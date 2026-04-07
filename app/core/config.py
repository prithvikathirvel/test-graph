from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "agent_studio"
    MONGO_CHECKPOINTER_COLLECTION_NAME: str = "langgraph_checkpoints"
    SCHEMA_API_URL: str = "https://apidev.sifymodernization.digital/ai/api/agent-studio/agent-flow/"
    SMTP_USER : str = "aakashchandha1@gmail.com"
    SMTP_PASSWORD : str = "wzie abbf cigm qida"
    SMTP_SERVER : str = "smtp.gmail.com"
    SMTP_PORT : int = 587
    GCP_PROJECT_ID: str = "prj-contentportal-dev-389901"
    GCP_CREDENTIALS_PATH: str = "service-account.json"
    OPENAI_API_KEY: str = "sk-Fm3dP1vX7qYt6uJzZbL5Kr2HgS8oWnCxEjQaRfNiGpTl"
    OPENAI_BASE_URL: str = "https://infinitai.sifymdp.digital/maas/v1"
    GOOGLE_APPLICATION_CREDENTIALS: str = "service-account.json"
    
    class Config:
        env_file = ".env"

settings = Settings()

# Default Voice Config fallback
DEFAULT_VOICE_CONFIG = {
    "stt_provider": "whisper",
    "tts_provider": "piper",
    "voice_name": "alloy",
    "speed": 1.0,
    "mode": "auto"
}