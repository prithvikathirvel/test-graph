import httpx
import aiosmtplib
from email.message import EmailMessage
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.core.config import settings
import os
import json
import logging
import asyncio
from app.utils.tools import _perform_sync_web_search

logger = logging.getLogger(__name__)

@NodeRegistry.register("API caller")
async def api_caller_node(state: FlowState, node_config: dict) -> dict:
    """Production API Caller."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    url = resolve_placeholders(inputs.get("url"), state["variables"])
    method = resolve_placeholders(inputs.get("method", "GET"), state["variables"]).upper()
    data = resolve_placeholders(inputs.get("data", {}), state["variables"])
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            pass  # Use as it is if it's not a valid JSON string

    headers = resolve_placeholders(inputs.get("headers", {}), state["variables"])
    timeout = float(inputs.get("timeout", 10.0) or 10.0)
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "api_output"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                json=data if method in ["POST", "PUT", "PATCH"] else None,
                headers=headers,
                timeout=timeout
            )
            response.raise_for_status()
            
            try: result = response.json()
            except: result = response.text
            
            return {"variables": {output_key: result}}
            
    except httpx.HTTPStatusError as e:
        # Capture the actual error body from the API (especially useful for 422 validation errors)
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        logger.error(f"❌ API Caller HTTP Error {e.response.status_code}: {detail}")
        return {"variables": {output_key: {"error": f"HTTP {e.response.status_code}", "detail": detail}}}
    except Exception as e:
        logger.exception("❌ API Caller Unexpected Error")
        return {"variables": {output_key: {"error": str(e)}}}


@NodeRegistry.register("Send Email")
async def send_email_node(state: FlowState, node_config: dict) -> dict:
    """Production Email Sender using async SMTP."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    sender = resolve_placeholders(inputs.get("from"), state["variables"])
    recipient = resolve_placeholders(inputs.get("to"), state["variables"])
    subject = resolve_placeholders(inputs.get("subject"), state["variables"])
    body = resolve_placeholders(inputs.get("body"), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "emailStatus"

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        await aiosmtplib.send(
            message,
            hostname=getattr(settings, "SMTP_SERVER"),
            port=int(getattr(settings, "SMTP_PORT")),
            #start_tls=True,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
        )
        return {"variables": {output_key: "Email sent successfully."}}
    except Exception as e:
        return {"variables": {output_key: f"Email failed: {str(e)}"}}


@NodeRegistry.register("Knowledge Retrieval Node")
async def kb_retrieval_node(state: FlowState, node_config: dict) -> dict:
    """Queries the Vector Knowledge Base Microservice and formats context for LLMs."""
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    # 1. Resolve inputs from FlowState
    kb_name = resolve_placeholders(inputs.get("knowledge_base_name", ""), state["variables"])
    query = resolve_placeholders(inputs.get("user_prompt", ""), state["variables"])
    
    # Extract advanced parameters (with safe fallbacks based on your OpenAPI spec)
    limit = resolve_placeholders(inputs.get("limit", 3), state["variables"])
    max_distance = resolve_placeholders(inputs.get("max_distance", 0.7), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "kb_context"

    # Validation
    if not kb_name or not query:
        logger.warning("KB Node missing kb_name or query.")
        return {"variables": {output_key: "System Error: Missing Knowledge Base Name or Query."}}

    # Enforce Types for the API Payload
    try: limit = int(limit)
    except (ValueError, TypeError): limit = 1
    
    try: max_distance = float(max_distance)
    except (ValueError, TypeError): max_distance = 0.7

    # 2. Prepare API Call
    url = f"https://apidev.sifymodernization.digital/kb/api/v1/knowledge-base/{kb_name}/search"
    payload = {
        "query": str(query),
        "limit": limit,
        "max_distance": max_distance
    }

    # 3. Execute Async Request
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            print(f"🚀 DEBUG: Firing KB Search to URL: {url}")
            print(f"🚀 DEBUG: Payload: {payload}")
            response = await client.post(url, json=payload)
            response.raise_for_status()
            result_json = response.json()
            
            if result_json.get("success"):
                data = result_json.get("data", [])
                
                if not data:
                    return {"variables": {output_key: "No relevant information found in the knowledge base."}}
                
                # 🚀 THE POWERFUL PART: Formatting for the LLM
                # We don't just dump raw JSON. We build a clean, readable text block 
                # so the downstream "LLM Invoker" understands exactly what it's reading.
                formatted_chunks = []
                for i, item in enumerate(data):
                    # Safely extract text. If it's a deeply nested JSON document from your Unified Schema,
                    # we dump it as nicely indented JSON. Otherwise, we use the "content" text.
                    content = item.get("content", item.get("text", json.dumps(item, indent=2)))
                    
                    formatted_chunks.append(f"--- Document {i+1} ---\n{content}")
                
                final_context = "\n\n".join(formatted_chunks)
                
                logger.info(f"✅ KB Retrieval Success: Found {len(data)} chunks for '{query}'")
                return {"variables": {output_key: final_context}}
                
            else:
                error_msg = result_json.get("message", "Unknown KB Error")
                return {"variables": {output_key: f"Knowledge Base Error: {error_msg}"}}
                
    except httpx.HTTPStatusError as e:
        logger.error(f"KB HTTP Error: {e.response.text}")
        return {"variables": {output_key: f"Vector Search API Error: {e.response.status_code}"}}
    except Exception as e:
        logger.error(f"KB Retrieval Node Error: {str(e)}")
        return {"variables": {output_key: f"Internal Search Error: {str(e)}"}}



@NodeRegistry.register("Web Search")
async def web_search_node(state: FlowState, node_config: dict) -> dict:
    """
    Performs a web search using Tavily and returns the raw results as a
    formatted text block.
    """
    logger.info("🟢 Entering Web Search Node (Raw Results Mode)...")

    # 1. Parse and resolve inputs from the node configuration
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    search_query_template = inputs.get("search_query", "{{CHAT_QUERY}}")
    max_results = int(inputs.get("search_count", 3))

    search_query = resolve_placeholders(search_query_template, state["variables"])

    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "search_results"

    if not search_query:
        logger.warning("Web Search Node skipped: search_query is empty.")
        return {"variables": {output_key: "Search query was empty."}}

    try:
        # 2. Perform the web search in a background thread
        search_results = await asyncio.to_thread(
            _perform_sync_web_search, search_query, max_results
        )

        if not search_results:
            return {"variables": {output_key: "No results found from the web search."}}

        formatted_results = "<hr>".join(
    [
        f"""
        <div class="search-result">
            <h3>{result.get('title', 'N/A')}</h3>
            <p><a href="{result.get('url', '#')}" target="_blank">
                {result.get('url', 'N/A')}
            </a></p>
            <p>{result.get('content', 'N/A')}</p>
        </div>
        """
        for result in search_results
    ]
)
        
        logger.info("✅ Web Search Node completed successfully (raw results).")
        
        # 4. Return the formatted text block directly
        return {"variables": {output_key: formatted_results}}

    except Exception as e:
        logger.error(f"❌ Error in Web Search Node: {e}", exc_info=True)
        return {"variables": {output_key: f"An error occurred during the web search: {e}"}}