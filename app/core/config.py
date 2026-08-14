import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "agent_studio"
    MONGO_CHECKPOINTER_COLLECTION_NAME: str = "langgraph_checkpoints"
    SCHEMA_API_URL: str = "https://apidev.sifymodernization.digital/ai/api/agent-studio/agent-flow/"
    DICTIONARY_API_URL: str = "https://apidev.sifymodernization.digital/ai/api/agent-studio/dictionary/name/"
    SMTP_USER : str = "aakashchandha1@gmail.com"
    SMTP_PASSWORD : str = "wzie abbf cigm qida"
    SMTP_SERVER : str = "smtp.gmail.com"
    SMTP_PORT : int = 587
    GCP_PROJECT_ID: str = "prj-contentportal-dev-389901"
    GCP_CREDENTIALS_PATH: str = "service-account.json"
    OPENAI_API_KEY: str = "sk-gw-qbsgi2Q6Tft2xzGcNqgVfFnxJZcfPLrE-hUo6i5Tz1o"
    OPENAI_BASE_URL: str = "https://aigateway-uat.sifymdp.digital/api/v1/maas"
    GOOGLE_APPLICATION_CREDENTIALS: str = "service-account.json"
    LANGSMITH_TRACING: str = "true"
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_API_KEY: str = "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"
    LANGSMITH_PROJECT: str = "Sify_Agent_Studio"
    TAVILY_API_KEY: str
    SENDGRID_API_KEY: str = "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"
    
    class Config:
        env_file = ".env"

settings = Settings()

os.environ["LANGSMITH_TRACING"] = settings.LANGSMITH_TRACING
os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT
os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
os.environ["TAVILY_API_KEY"] = settings.TAVILY_API_KEY
os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
os.environ["OPENAI_BASE_URL"] = settings.OPENAI_BASE_URL


DEFAULT_VOICE_CONFIG = {
    "stt_provider": "whisper",
    "tts_provider": "piper",
    "voice_name": "alloy",
    "speed": 1.0,
    "mode": "auto"
}