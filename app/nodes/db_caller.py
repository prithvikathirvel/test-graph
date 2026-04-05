from pymongo import AsyncMongoClient
from bson import ObjectId
import json
import logging

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

# Keep a module-level connection pool to avoid opening/closing connections per node execution
_mongo_clients = {}

def get_async_mongo_client(uri: str) -> AsyncMongoClient:
    """Reuses connection pools for the same MongoDB URIs."""
    if uri not in _mongo_clients:
        _mongo_clients[uri] = AsyncMongoClient(uri)
    return _mongo_clients[uri]

@NodeRegistry.register("Mongo DB caller")
async def mongo_db_caller(state: FlowState, node_config: dict) -> dict:
    """Production MongoDB Caller. Executes native Async PyMongo queries."""
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    # 1. Resolve configurations and credentials
    mongo_uri = resolve_placeholders(inputs.get("mongoUri"), state["variables"])
    db_name = resolve_placeholders(inputs.get("dbName"), state["variables"])
    collection_name = resolve_placeholders(inputs.get("collectionName"), state["variables"])
    
    # 2. Resolve query parameters
    task = resolve_placeholders(inputs.get("task", "fetch"), state["variables"]).lower()
    conditions = resolve_placeholders(inputs.get("conditions", {}), state["variables"])
    document = resolve_placeholders(inputs.get("document", {}), state["variables"])
    doc_id = resolve_placeholders(inputs.get("docId"), state["variables"])
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "DbOperationStatus"

    if not mongo_uri or not db_name or not collection_name:
        return {"variables": {output_key: {"error": "Missing MongoDB URI, DB Name, or Collection Name"}}}

    # If conditions came in as a JSON string from the UI, parse them
    if isinstance(conditions, str):
        try: conditions = json.loads(conditions)
        except json.JSONDecodeError: conditions = {}
        
    if isinstance(document, str):
        try: document = json.loads(document)
        except json.JSONDecodeError: document = {}

    try:
        client = get_async_mongo_client(mongo_uri)
        collection = client[db_name][collection_name]
        
        result_data = None
        
        # --- Execute Task ---
        if "fetch" in task or "find" in task or "get" in task:
            # Handle specific ID lookup
            if doc_id:
                conditions["_id"] = ObjectId(doc_id) if len(doc_id) == 24 else doc_id
            
            # Execute async find
            cursor = collection.find(conditions).limit(100) # Limit to protect memory
            docs = await cursor.to_list(length=100)
            
            # Serialize ObjectIds for JSON compatibility
            for doc in docs:
                if "_id" in doc: doc["_id"] = str(doc["_id"])
                
            result_data = docs

        elif "insert" in task or "create" in task or "add" in task:
            if not document:
                raise ValueError("No document provided for insertion.")
            
            res = await collection.insert_one(document)
            result_data = {"inserted_id": str(res.inserted_id)}

        elif "update" in task or "modify" in task:
            if doc_id:
                conditions["_id"] = ObjectId(doc_id) if len(doc_id) == 24 else doc_id
            
            if not conditions or not document:
                raise ValueError("Conditions and document are required for update.")
                
            # Assume document is the $set payload if it doesn't contain mongo operators
            update_payload = document if any(k.startswith('$') for k in document.keys()) else {"$set": document}
            
            res = await collection.update_many(conditions, update_payload)
            result_data = {"matched_count": res.matched_count, "modified_count": res.modified_count}

        elif "delete" in task or "remove" in task:
            if doc_id:
                conditions["_id"] = ObjectId(doc_id) if len(doc_id) == 24 else doc_id
                
            res = await collection.delete_many(conditions)
            result_data = {"deleted_count": res.deleted_count}

        else:
            result_data = {"error": f"Unknown task type: {task}"}

        return {"variables": {output_key: result_data}}

    except Exception as e:
        logger.error(f"MongoDB Tool Error: {str(e)}")
        return {"variables": {output_key: {"error": str(e)}}}