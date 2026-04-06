from datetime import datetime
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

def save_conversation_turn(mongo_client, session_id: str, thread_id: str, user_message: str, agent_message: str):
    """
    Appends the user and agent messages to the session's message array in MongoDB.
    Creates the document if it doesn't exist.
    """
    if not user_message and not agent_message:
        return # Nothing to save
        
    try:
        db = mongo_client[settings.MONGO_DB_NAME]
        collection = db["conversation_memory"]
        
        # 1. Use actual datetime objects so MongoDB stores them as ISODate ({"$date": "..."})
        now = datetime.utcnow()
        
        # 2. Build the messages to append
        messages_to_push = []
        
        if user_message:
            messages_to_push.append({
                "role": "user",
                "content": user_message,
                "timestamp": now
            })
            
        if agent_message:
            messages_to_push.append({
                "role": "assistant",
                "content": agent_message,
                "timestamp": now
            })

        # 3. Perform an Upsert (Update if exists, Insert if not)
        collection.update_one(
            {"session_id": session_id},  # Find by session_id
            {
                # $setOnInsert ONLY runs the very first time the session is created
                "$setOnInsert": {
                    "session_id": session_id,
                    "created_at": now
                },
                # $push appends the new messages to the end of the array
                "$push": {
                    "messages": {
                        "$each": messages_to_push
                    }
                }
            },
            upsert=True  # Create it if it doesn't exist!
        )
        
        # logger.info(f"✅ Appended memory to session: {session_id}")
        
    except Exception as e:
        logger.error(f"❌ Failed to save conversation memory: {e}")