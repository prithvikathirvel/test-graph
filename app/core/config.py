import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB_NAME: str = "agent_studio"
    MONGO_CHECKPOINTER_COLLECTION_NAME: str = "langgraph_checkpoints"
    SCHEMA_API_URL: str = "https://apidev.sifymodernization.digital/ai/api/agent-studio/agent-flow/"
    DICTIONARY_API_URL: str = "https://apidev.sifymodernization.digital/ai/api/agent-studio/dictionary/name/"
    GRAPH_CACHE_TTL_SECONDS: int = 300

    # Optional compatibility-safe API authentication. Enable in production or
    # replace with an OIDC/JWT gateway that sets the same trusted auth context.
    AUTH_REQUIRED: bool = False
    AGENT_API_KEY: str = ""
    AGENT_API_SCOPES: str = "agent:access,agent:invoke,mcp:access"
    AGENT_API_ROLES: str = "service"
    AGENT_API_TENANT_ID: str = ""
    AGENT_API_USER_ID: str = "service"

    # Secrets intentionally have no source-controlled values. Deployments must
    # provide them through environment variables or their secret manager.
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    GCP_PROJECT_ID: str = ""
    GCP_CREDENTIALS_PATH: str = "service-account.json"
    GOOGLE_APPLICATION_CREDENTIALS: str = "service-account.json"
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://aigateway-uat.sifymdp.digital/api/v1/maas"
    TAVILY_API_KEY: str = ""
    SENDGRID_API_KEY: str = ""

    LANGSMITH_TRACING: str = "false"
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "Sify_Agent_Studio"


settings = Settings()


def _export_if_present(name: str, value: str) -> None:
    if value:
        os.environ[name] = value


_export_if_present("LANGSMITH_TRACING", settings.LANGSMITH_TRACING)
_export_if_present("LANGSMITH_ENDPOINT", settings.LANGSMITH_ENDPOINT)
_export_if_present("LANGSMITH_API_KEY", settings.LANGSMITH_API_KEY)
_export_if_present("LANGSMITH_PROJECT", settings.LANGSMITH_PROJECT)
_export_if_present("TAVILY_API_KEY", settings.TAVILY_API_KEY)
_export_if_present("OPENAI_API_KEY", settings.OPENAI_API_KEY)
_export_if_present("OPENAI_BASE_URL", settings.OPENAI_BASE_URL)


DEFAULT_VOICE_CONFIG = {
    "stt_provider": "whisper",
    "tts_provider": "piper",
    "voice_name": "alloy",
    "speed": 1.0,
    "mode": "auto",
}
