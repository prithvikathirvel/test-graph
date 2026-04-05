from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, List
from enum import Enum

class UserInput(BaseModel):
    message: Optional[str] = ""
    voiceInput: Optional[str] = None
    uploadedFiles: Optional[List[Any]] = []

class VoiceConfig(BaseModel):
    stt_provider: str = "whisper"
    tts_provider: str = "piper"
    mode: str = "voice_in_voice_out"

class InvokeRequest(BaseModel):
    agent_id: str
    userInput: Optional[UserInput] = None
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    
    # Optional overrides
    user_id: str = "default_user"
    session_id: str = "session_default"
    thread_id: str = "thread_default"

class ResumeRequest(BaseModel):
    agent_id: str
    user_id: str
    session_id: str
    thread_id: str
    node_id: str
    user_response: Any
    input_type: str = "text"
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    userInput: Optional[UserInput] = None # For voice input on resume

class MCPTransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"

class MCPServerConfig(BaseModel):
    server_id: str
    name: str
    transport_type: MCPTransportType
    connection_params: Dict[str, Any]
    enabled: bool = True
    timeout: int = 30
    max_retries: int = 3

class MCPToolCall(BaseModel):
    server_id: str
    tool_name: str
    arguments: Dict[str, Any]