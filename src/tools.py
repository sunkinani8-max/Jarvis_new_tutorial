import asyncio
import logging

from langchain_community.tools import DuckDuckGoSearchRun
from livekit.agents import RunContext, ToolError, function_tool

logger = logging.getLogger("tools")


@function_tool
async def search_web(context: RunContext, query: str) -> str:
    """
    Use this tool to search the web for information related to the given query.
    """

    try:
        search = DuckDuckGoSearchRun()
        result = await asyncio.to_thread(search.run, tool_input=query)
        logger.info(f"web search result for query '{query}': {result}")
        return str(result)
    except Exception as e:
        logger.error(f"Error occurred while searching the web for query '{query}': {e}")
        raise ToolError(f"Failed to search the web for '{query}': {e}") from e

