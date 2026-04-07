from datetime import datetime
import logging
from app.core.config import settings
from langchain_core.messages import HumanMessage, AIMessage

logger = logging.getLogger(__name__)

def save_conversation_turn(mongo_client, session_id: str, thread_id: str, user_message: str, agent_message: str):
    """Appends user and agent messages to MongoDB."""
    if not user_message and not agent_message:
        return
        
    try:
        db = mongo_client[settings.MONGO_DB_NAME]
        collection = db["conversation_memory"]
        now = datetime.utcnow()
        messages_to_push = []
        
        if user_message:
            messages_to_push.append({"role": "user", "content": user_message, "timestamp": now})
        if agent_message:
            messages_to_push.append({"role": "assistant", "content": agent_message, "timestamp": now})

        collection.update_one(
            {"session_id": session_id},
            {
                "$setOnInsert": {"session_id": session_id, "created_at": now},
                "$push": {"messages": {"$each": messages_to_push}}
            },
            upsert=True
        )
    except Exception as e:
        logger.error(f"❌ Failed to save conversation memory: {e}")

def get_conversation_history(mongo_client, session_id: str, memory_window: int = 10) -> list:
    """Fetches past messages from MongoDB and converts them to LangChain format."""
    try:
        db = mongo_client[settings.MONGO_DB_NAME]
        collection = db["conversation_memory"]
        
        record = collection.find_one({"session_id": session_id})
        if not record or "messages" not in record:
            return []
            
        raw_msgs = record["messages"][-(memory_window * 2):]
        
        chat_history = []
        for msg in raw_msgs:
            if msg.get("role") == "user":
                chat_history.append(HumanMessage(content=msg.get("content", "")))
            else:
                chat_history.append(AIMessage(content=msg.get("content", "")))
                
        return chat_history
    except Exception as e:
        logger.error(f"Failed to fetch conversation memory: {e}")
        return []