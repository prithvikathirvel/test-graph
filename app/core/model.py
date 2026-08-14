import uuid
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, List
from enum import Enum

class UserInput(BaseModel):
    message: Optional[str] = ""
    voiceInput: Optional[str] = None
    uploadedFiles: Optional[List[Any]] = []

class VoiceConfig(BaseModel):
    tts_provider: str = "piper"
    stt_provider: str = "whisper"
    mode: str = "voice_in_voice_out"

class InvokeReq(BaseModel):
    agent_id: str
    userInput: Optional[UserInput] = None
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    user_id: str = "default_user"
    session_id: str = Field(default_factory=lambda: f"session_{uuid.uuid4().hex}")
    thread_id: str = Field(default_factory=lambda: f"thread_{uuid.uuid4().hex}")

class ResumeReq(BaseModel):
    agent_id: str
    user_id: str
    session_id: str
    thread_id: str
    node_id: str
    user_response: Optional[Any] = None
    # Backward-compat for clients sending voice payload at top-level.
    voiceInput: Optional[str] = None
    input_type: str = "text"
    voice_config: Optional[VoiceConfig] = None
    voice_enabled: bool = False
    userInput: Optional[UserInput] = None

class DynamicFlowReq(BaseModel):
    query: str = Field(description="Natural language description of the workflow to build.")
    session_id: str = Field(default_factory=lambda: f"arch_session_{uuid.uuid4().hex}")
    thread_id: str = Field(default_factory=lambda: f"arch_thread_{uuid.uuid4().hex}")

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

class NodeTestRequest(BaseModel):
    node_name: str = Field(description="Exact registered name, e.g. 'API caller'")
    node_config: Dict[str, Any] = Field(description="inputParameters, outputParameters, and any extra node fields")
    variables: Dict[str, Any] = Field(default_factory=dict, description="Pre-populated FlowState.variables")
    messages: List[Any] = Field(default_factory=list, description="Optional chat history for LLM nodes")