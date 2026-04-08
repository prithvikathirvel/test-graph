import os
import io
import json
import base64
import asyncio
import logging
from typing import Optional
from pydantic import BaseModel, Field

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_google_vertexai import ChatVertexAI
from langchain_openai import ChatOpenAI
from google.oauth2 import service_account

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

# --- 1. Strictly Typed Chatty Output ---
class ChattyResponse(BaseModel):
    message: str = Field(description="The actual text response to show to the user.")
    intent: str = Field(description="MUST be one of: 'continue', 'close', 'escalate', 'tool_call'.")
    tool_name: Optional[str] = Field(None, description="If intent is 'tool_call', the exact name of the tool to execute.")

# --- 2. CPU-Bound Document Converters ---
def _convert_to_pdf_b64(text: str) -> str:
    """Generates a PDF in memory and returns Base64."""
    try:
        from fpdf import FPDF
    except ImportError:
        raise ImportError("Please install fpdf2: pip install fpdf2")
        
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font("Helvetica", size=12)
    # Handle unicode safely for standard PDF fonts
    clean_text = text.encode('latin-1', 'replace').decode('latin-1')
    pdf.multi_cell(0, 8, clean_text)
    
    pdf_bytes = pdf.output() # fpdf2 outputs a bytearray natively
    return base64.b64encode(pdf_bytes).decode('utf-8')

def _convert_to_docx_b64(text: str) -> str:
    """Generates a DOCX in memory and returns Base64."""
    try:
        from docx import Document
    except ImportError:
        raise ImportError("Please install python-docx: pip install python-docx")
        
    doc = Document()
    doc.add_paragraph(text)
    
    buf = io.BytesIO()
    doc.save(buf)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

# --- 3. LLM Model Factory ---
try:
    credentials = service_account.Credentials.from_service_account_file(
        "service-account.json",
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
except Exception as e:
    logger.warning(f"Could not load GCP credentials: {e}")
    credentials = None

def _get_llm(model_name: str, temperature: float = 0.0):
    """Initializes the correct production LLM."""
    if "Gemini" in model_name or "gemini" in model_name.lower():
        return ChatVertexAI(
            model=model_name,
            credentials=credentials,
            temperature=temperature,
        )
    else:
        # Fallback for OpenAI / proxy endpoints if needed
        return ChatVertexAI(
            model=model_name,
            credentials=credentials,
            temperature=temperature,
        )


# --- 4. The Node Executor ---
@NodeRegistry.register("LLM invoker")
async def llm_invoker_node(state: FlowState, node_config: dict) -> dict:
    """Executes modern LangChain LLM Calls with structured output & file formatting."""
    logger.info("🟢 Entering LLM Invoker Node...")
    
    # 1. Parse JSON configuration
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    model_choice = inputs.get("Model", "gemini-2.5-flash")
    chatty_mode = str(inputs.get("chatty_mode", "false")).lower() == "true"
    user_msg_key = inputs.get("user_message_key", "CHAT_QUERY")
    memory_window = int(inputs.get("memory_window", 20))
    response_format = str(inputs.get("Response Format", "text")).lower()
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "response"

    # 2. Resolve dynamic variables
    raw_prompt = inputs.get("Prompt", "You are a helpful assistant.")
    system_prompt = resolve_placeholders(raw_prompt, state["variables"])
    user_query = str(state["variables"].get(user_msg_key, "")).strip()
    
    logger.debug(f"Resolved System Prompt (len={len(system_prompt)})")
    logger.debug(f"User Query: '{user_query[:50]}...'")

    # 3. Trim History
    all_messages = state.get("messages", [])
    recent_chat_history = all_messages[-(memory_window * 2):] if memory_window > 0 and len(all_messages) > 0 else []
    logger.debug(f"Context: {len(recent_chat_history)} messages in history window.")

    # 4. Initialize LLM
    try:
        logger.info(f"Model selection: {model_choice} (Format: {response_format})")
        llm = _get_llm("gemini-2.5-flash")
    except Exception as e:
        logger.error(f"❌ Failed to initialize LLM: {e}")
        return {"variables": {output_key: {"message": f"Initialization Error: {e}", "intent": "continue"}}}

    # 5. Execute LangChain
    try:
        if chatty_mode:
            # --- CHATTY MODE (Agentic Loops) ---
            tool_context = [f"[{k}]: {v}" for k, v in state["variables"].items() if k.endswith("_output") or k.endswith("Status") or k == "context" or k == "kb_context"]
            if tool_context:
                system_prompt += "\n\n### TOOL RESULTS (DO NOT CALL THESE TOOLS AGAIN) ###\n" + "\n".join(tool_context)
            
            prompt = ChatPromptTemplate.from_messages([
                ("system", "{system_prompt_var}"), 
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{user_query}")
            ])
            
            chain = prompt | llm.with_structured_output(ChattyResponse)
            
            logger.debug("Executing structured LLM call (Chatty Mode)...")
            response_obj: ChattyResponse = await chain.ainvoke({
                "system_prompt_var": system_prompt, 
                "chat_history": recent_chat_history, 
                "user_query": user_query
            })
            logger.info(f"LLM Response Intention: {response_obj.intent}")

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
            # --- STANDARD MODE (With Strict File/JSON Generation) ---
            
            # 🔥 STRICT JSON ENFORCEMENT via Prompt Hacking
            if response_format == "json":
                system_prompt += "\n\nCRITICAL INSTRUCTION: You must respond with raw, valid JSON only. Do not wrap the response in markdown blocks (e.g., ```json). Do not include any conversational text or explanations before or after the JSON."

            prompt = ChatPromptTemplate.from_messages([
                ("system", "{system_prompt_var}"),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{user_query}")
            ])
            
            logger.debug(f"Executing standard LLM call (Format: {response_format})...")
            response = await chain.ainvoke({
                "system_prompt_var": system_prompt,
                "chat_history": recent_chat_history,
                "user_query": user_query
            })
            
            final_text = response.content  
            logger.debug(f"Received LLM response (len={len(final_text)})")
            new_vars = {}
            
            try:
                if response_format == "pdf":
                    encoded = await asyncio.to_thread(_convert_to_pdf_b64, final_text)
                    new_vars[output_key] = encoded
                    new_vars["fileType"] = "pdf"
                    new_vars["fileName"] = "output.pdf"
                    
                elif response_format in ["doc", "docx", "document", "docs"]:
                    encoded = await asyncio.to_thread(_convert_to_docx_b64, final_text)
                    new_vars[output_key] = encoded
                    new_vars["fileType"] = "doc"
                    new_vars["fileName"] = "output.docx"
                    
                elif response_format == "json":
                    # 🔥 MARKDOWN STRIPPER (Bulletproof JSON parsing)
                    clean_text = final_text.strip()
                    
                    if clean_text.startswith("```json"):
                        clean_text = clean_text[7:]
                    elif clean_text.startswith("```"):
                        clean_text = clean_text[3:]
                        
                    if clean_text.endswith("```"):
                        clean_text = clean_text[:-3]
                        
                    clean_text = clean_text.strip()
                    
                    try:
                        new_vars[output_key] = json.loads(clean_text)
                    except json.JSONDecodeError as je:
                        logger.error(f"LLM failed to produce valid JSON: {je}. Raw output: {clean_text}")
                        # Provide a structured error object so downstream nodes don't crash
                        new_vars[output_key] = {"error": "LLM did not return valid JSON", "raw_response": clean_text}
                        
                else:
                    # Default "text" fallback
                    new_vars[output_key] = final_text

            except Exception as format_err:
                logger.error(f"Failed to convert format to {response_format}: {format_err}")
                new_vars[output_key] = final_text # Safe fallback to raw text
            
            return {
                "variables": new_vars,
                "messages": [HumanMessage(content=user_query), AIMessage(content=final_text)]
            }

    except Exception as e:
        logger.exception("❌ LLM Invoker Error")
        error_msg = f"I'm sorry, an error occurred: {str(e)}"
        
        if chatty_mode:
            return {"variables": {output_key: {"message": error_msg, "intent": "continue", "error": str(e)}}}
        return {"variables": {output_key: error_msg}}