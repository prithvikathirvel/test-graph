import os
import logging
from typing import Optional
from pydantic import BaseModel, Field

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_google_vertexai import ChatVertexAI
from langchain_openai import ChatOpenAI

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from google.oauth2 import service_account

logger = logging.getLogger(__name__)

# --- 1. Strictly Typed Chatty Output ---

# --- 2. LLM Model Factory ---
credentials = service_account.Credentials.from_service_account_file(
    "service-account.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

class ChattyResponse(BaseModel):
    message: str = Field(description="The actual text response to show to the user.")
    intent: str = Field(description="MUST be one of: 'continue', 'close', 'escalate', 'tool_call'.")
    tool_name: Optional[str] = Field(None, description="If intent is 'tool_call', the exact name of the tool to execute.")

# --- 2. LLM Model Factory ---
def _get_llm(model_name: str, temperature: float = 0.0):
    """Initializes the correct production LLM based on UI Dropdown."""
    if "Gemini" in model_name:
        # Uses your Google Service Account JSON seamlessly
        return ChatVertexAI(
                model=model_name,              # gemini-1.5-pro / gemini-1.5-flash
                credentials=credentials,
                temperature=temperature,
               # convert_system_message_to_human=True
            )
    else:
        # Fallback to OpenAI (e.g., Llama 3 via proxy or GPT-4)
        # return ChatOpenAI(
        #     model="meta/llama-3.3-70b-instruct",
        #     api_key="sk-Fm3dP1vX7qYt6uJzZbL5Kr2HgS8oWnCxEjQaRfNiGpTl",
        #     base_url="https://infinitai.sifymdp.digital/maas/v1"
        # )
        return ChatVertexAI(
                model=model_name,              # gemini-1.5-pro / gemini-1.5-flash
                credentials=credentials,
                temperature=temperature,
                # convert_system_message_to_human=True
            )


# --- 3. The Node Executor ---
@NodeRegistry.register("LLM invoker")
async def llm_invoker_node(state: FlowState, node_config: dict) -> dict:
    """Executes modern LangChain LLM Calls with native Structured Output."""
    logger.info("🟢 Entering LLM Invoker Node...")
    
    # 1. Parse JSON configuration
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    model_choice = inputs.get("Model", "Gemini 2.5 pro")
    chatty_mode = str(inputs.get("chatty_mode", "false")).lower() == "true"
    user_msg_key = inputs.get("user_message_key", "CHAT_QUERY")
    memory_window = int(inputs.get("memory_window", 20))
    
    # 2. Get the Output Key (THIS IS WHAT WAS MISSING IN YOUR ERROR!)
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "response"

    # 3. Resolve dynamic variables in the System Prompt
    raw_prompt = inputs.get("Prompt", "You are a helpful assistant.")
    system_prompt = resolve_placeholders(raw_prompt, state["variables"])

    # 4. Get the latest user query from the variables
    user_query = str(state["variables"].get(user_msg_key, "")).strip()

    # 5. Trim History based on memory_window
    all_messages = state.get("messages", [])
    if memory_window > 0:
        recent_chat_history = all_messages[-(memory_window * 2):] if len(all_messages) > 0 else []
    else:
        recent_chat_history = []

    # 6. Initialize LLM
    try:
        llm = _get_llm("gemini-2.5-flash")
    except Exception as e:
        logger.error(f"❌ Failed to initialize LLM: {e}")
        return {"variables": {output_key: {"message": f"Initialization Error: {e}", "intent": "continue"}}}

    # 7. Execute LangChain
    try:
        if chatty_mode:
            # Inject Tool Results
            tool_context = [f"[{k}]: {v}" for k, v in state["variables"].items() if k.endswith("_output") or k.endswith("Status") or k == "context"]
            if tool_context:
                system_prompt += "\n\n### TOOL RESULTS (DO NOT CALL THESE TOOLS AGAIN) ###\n" + "\n".join(tool_context)
            
            # Use {system_prompt_var} to prevent LangChain from crashing on curly braces {}
            prompt = ChatPromptTemplate.from_messages([
                ("system", "{system_prompt_var}"), 
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{user_query}")
            ])
            
            chain = prompt | llm.with_structured_output(ChattyResponse)
            
            # Pass ONLY the trimmed history
            response_obj: ChattyResponse = await chain.ainvoke({
                "system_prompt_var": system_prompt, 
                "chat_history": recent_chat_history, 
                "user_query": user_query
            })

            # Format to perfectly match your UI expectations
            result_dict = {
                "message": response_obj.message,
                "intent": response_obj.intent if response_obj.intent in ["continue", "close", "escalate", "tool_call"] else "continue",
                "is_chatty": True,
                "user_text": user_query
            }
            if response_obj.tool_name: result_dict["tool_name"] = response_obj.tool_name

            return {
                "variables": {output_key: result_dict},
                "messages": [HumanMessage(content=user_query), AIMessage(content=response_obj.message)]
            }

        else:
            # STANDARD MODE
            prompt = ChatPromptTemplate.from_messages([
                ("system", "{system_prompt_var}"),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{user_query}")
            ])
            
            chain = prompt | llm
            
            response = await chain.ainvoke({
                "system_prompt_var": system_prompt,
                "chat_history": recent_chat_history,
                "user_query": user_query
            })
            
            return {
                "variables": {output_key: response.content},
                "messages": [HumanMessage(content=user_query), AIMessage(content=response.content)]
            }

    except Exception as e:
        logger.exception("❌ LLM Invoker Error")
        error_msg = f"I'm sorry, an error occurred: {str(e)}"
        
        # Fallback gracefully using output_key
        if chatty_mode:
            return {"variables": {output_key: {"message": error_msg, "intent": "continue", "error": str(e)}}}
        return {"variables": {output_key: error_msg}}