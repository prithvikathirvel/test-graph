import time
import logging
import asyncio
from typing import Any, List
from langchain_community.utilities import SQLDatabase
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState
from app.nodes.agents import _get_llm

logger = logging.getLogger(__name__)

def _sync_db_chat(uri: str, user_query: str, chat_history: List[BaseMessage], llm: Any) -> str:
    """Synchronous CPU/DB bound task with Conversation Memory and Performance Profiling."""
    total_start = time.time()
    try:
        # ---------------------------------------------------------
        # STEP 1: Connect to DB and Fetch Schema
        # ---------------------------------------------------------
        logger.info(f"🛢️ [Step 1/5] Connecting to DB and fetching schema...")
        step_start = time.time()
        
        # NOTE: sample_rows_in_table_info=3 runs a SELECT * LIMIT 3 on EVERY table.
        # If your DB has hundreds of tables, THIS is why it is slow!
        db = SQLDatabase.from_uri(uri, sample_rows_in_table_info=3)
        schema_info = db.get_table_info()
        
        logger.info(f"✅ [Step 1] Schema fetched in {time.time() - step_start:.2f}s. (Schema size: {len(schema_info)} chars)")

        # ---------------------------------------------------------
        # STEP 2: Reformulate Question (Memory)
        # ---------------------------------------------------------
        logger.info(f"🧠 [Step 2/5] Checking memory and reformulating question...")
        step_start = time.time()
        
        if chat_history:
            reformulate_prompt = ChatPromptTemplate.from_messages([
                ("system", "Given the following conversation and a follow up question, rephrase the follow up question to be a standalone question. Do not answer the question, just reformulate it."),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{question}")
            ])
            reformulate_chain = reformulate_prompt | llm
            standalone_query = reformulate_chain.invoke({
                "chat_history": chat_history,
                "question": user_query
            }).content
            logger.info(f"✅ [Step 2] Question reformulated in {time.time() - step_start:.2f}s. New Query: '{standalone_query}'")
        else:
            standalone_query = user_query
            logger.info(f"⏭️ [Step 2] No chat history. Skipped reformulation. Query: '{standalone_query}'")

        # ---------------------------------------------------------
        # STEP 3: Generate SQL via LLM
        # ---------------------------------------------------------
        logger.info(f"🤖 [Step 3/5] Asking LLM to generate SQL query...")
        step_start = time.time()
        
        sql_prompt = ChatPromptTemplate.from_template(
            """You are a PostgreSQL/MySQL expert. Given an input question, create a syntactically correct SQL query to run.
            Never query for all columns from a table. You must query only the columns that are needed.
            DO NOT execute DML statements (INSERT, UPDATE, DELETE, DROP).
            
            Here is the schema:
            {schema}
            
            Question: {question}
            SQL Query:"""
        )
        
        sql_chain = sql_prompt | llm
        sql_response = sql_chain.invoke({"schema": schema_info, "question": standalone_query})
        
        raw_sql = sql_response.content.replace("```sql", "").replace("```", "").strip()
        logger.info(f"✅ [Step 3] SQL generated in {time.time() - step_start:.2f}s. SQL: {raw_sql}")

        # ---------------------------------------------------------
        # STEP 4: Execute SQL on Database
        # ---------------------------------------------------------
        logger.info(f"⚡ [Step 4/5] Executing SQL on Database...")
        step_start = time.time()
        
        db_results = db.run(raw_sql)
        
        logger.info(f"✅ [Step 4] SQL executed in {time.time() - step_start:.2f}s. Results returned: {len(str(db_results))} chars")

        # ---------------------------------------------------------
        # STEP 5: Synthesize Final Answer
        # ---------------------------------------------------------
        logger.info(f"🗣️ [Step 5/5] Asking LLM to synthesize final natural language answer...")
        step_start = time.time()
        
        answer_prompt = ChatPromptTemplate.from_messages([
            ("system", """Given the user's question, the SQL query, and the SQL result, answer the user question directly in natural language.
            SQL Query: {query}
            SQL Result: {result}"""),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}")
        ])
        
        answer_chain = answer_prompt | llm
        final_response = answer_chain.invoke({
            "question": standalone_query,
            "query": raw_sql,
            "result": db_results,
            "chat_history": chat_history
        })
        
        logger.info(f"✅ [Step 5] Answer synthesized in {time.time() - step_start:.2f}s.")
        logger.info(f"🎉 Total DB Chat Pipeline completed in {time.time() - total_start:.2f}s")
        
        return final_response.content

    except Exception as e:
        error_msg = f"SQL Chat Error: {str(e)}"
        logger.error(f"❌ {error_msg}. Failed after {time.time() - total_start:.2f}s")
        return f"I encountered an error querying the database: {str(e)}"


@NodeRegistry.register("Chat with DB")
async def db_chat_node(state: FlowState, node_config: dict) -> dict:
    """Production Text-to-SQL Chat Node with Memory Support."""
    logger.info("🟢 Entering Chat with DB Node...")
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    db_uri = resolve_placeholders(inputs.get("db_uri", ""), state["variables"])
    user_query = resolve_placeholders(inputs.get("user_query", "{{CHAT_QUERY}}"), state["variables"])
    model_choice = resolve_placeholders(inputs.get("model", "gemini-2.5-pro"), state["variables"])
    memory_window = int(resolve_placeholders(inputs.get("memory_window", 10), state["variables"]))
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "db_chat_response"

    if not db_uri or not user_query:
        logger.warning("Missing Database URI or User Query. Aborting DB Chat.")
        return {"variables": {output_key: "Missing Database URI or User Query."}}

    # Fetch Memory from LangGraph State
    all_messages = state.get("messages", [])
    recent_chat_history = all_messages[-(memory_window * 2):] if memory_window > 0 and len(all_messages) > 0 else []

    try:
        logger.info(f"Initializing LLM '{model_choice}' for DB Chat...")
        llm = _get_llm(model_choice)
        
        # Pass the chat history into the background thread!
        final_answer = await asyncio.to_thread(_sync_db_chat, db_uri, user_query, recent_chat_history, llm)
        
        return {
            "variables": {output_key: final_answer},
            "messages": [HumanMessage(content=user_query), AIMessage(content=final_answer)]
        }
        
    except Exception as e:
        logger.error(f"❌ Chat with DB Node Critical Error: {e}", exc_info=True)
        return {"variables": {output_key: f"Database Chat Error: {str(e)}"}}