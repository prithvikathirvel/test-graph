import logging
from typing import List, Dict, Any
from langchain_community.tools import TavilySearchResults
from app.core.config import settings
from app.utils.templating import resolve_placeholders 
from app.core.state import FlowState 
import asyncio


logger = logging.getLogger(__name__)

def _perform_sync_web_search(query: str, max_results: int) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper for the Tavily search tool to be run in a separate thread.
    This remains unchanged.
    """
    try:
        search_tool = TavilySearchResults(
            k=max_results,
            api_key=settings.TAVILY_API_KEY,
        )
        logger.info(f"Performing web search for query: '{query}' with {max_results} results.")
        search_results = search_tool.invoke(query)
        logger.info(f"Retrieved {len(search_results)} search results.")
        return search_results
    except Exception as e:
        logger.error(f"Web search failed: {e}", exc_info=True)
        return []