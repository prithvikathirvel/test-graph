"""
Run this once to clear stale checkpoints that were created before the
compiler was updated. These checkpoints reference node IDs (like
AgentFlow_node-1024) that no longer exist in the compiled graph.

Usage:
    python clear_stale_checkpoint.py --thread_id thread_b37d9c...
    python clear_stale_checkpoint.py --agent_id smart_multi_issue_resolver_001
    python clear_stale_checkpoint.py --all   # clears ALL checkpoints (nuclear)
"""

import argparse
from pymongo import MongoClient
from app.core.config import settings

def clear(thread_id=None, agent_id=None, clear_all=False):
    client = MongoClient(settings.MONGO_URI)
    db     = client[getattr(settings, "MONGO_DB_NAME", "agent_studio")]
    col    = db[getattr(settings, "MONGO_CHECKPOINTER_COLLECTION_NAME", "checkpoints")]

    if clear_all:
        result = col.delete_many({})
        print(f"Cleared ALL {result.deleted_count} checkpoints.")

    elif thread_id:
        result = col.delete_many({"thread_id": thread_id})
        print(f"Cleared {result.deleted_count} checkpoint(s) for thread '{thread_id}'.")

    elif agent_id:
        # thread_ids follow pattern: {agent_id}_{hex} or contain the agent_id
        result = col.delete_many({"thread_id": {"$regex": agent_id}})
        print(f"Cleared {result.deleted_count} checkpoint(s) matching agent '{agent_id}'.")

    else:
        print("Specify --thread_id, --agent_id, or --all")

    client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread_id", default=None)
    parser.add_argument("--agent_id",  default=None)
    parser.add_argument("--all",       action="store_true")
    args = parser.parse_args()
    clear(args.thread_id, args.agent_id, args.all)