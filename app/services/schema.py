import httpx
from app.core.config import settings
import logging
logger = logging.getLogger(__name__)

async def fetch_schema_by_agent_id(agent_id: str) -> dict:
    """Dynamically fetches the UI JSON schema from your external API."""
    url = settings.SCHEMA_API_URL.format(agent_id)
    logger.info(f"Fetching schema from: {url}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            logger.info("Schema fetched successfully.")
            return response.json()
    except Exception as e:
        logger.error(f"Schema Fetch Error: {e}")
        raise ValueError(f"Failed to fetch schema for {agent_id}: {str(e)}")