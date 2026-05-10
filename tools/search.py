"""
tools/search.py
Tavily web search — used for competitive research and market context.
"""

import os
from tavily import TavilyClient


def search(query: str, max_results: int = 5) -> list[dict]:
    """
    Run a web search and return a list of results.

    Each result contains:
      - title
      - url
      - content (snippet)
    """
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    response = client.search(query, max_results=max_results)
    results = response.get("results", [])
    
    # Trim content snippets to reduce token usage in conversation history
    for r in results:
        r["content"] = r["content"][:500]
    
    return results


# Tool definition for Anthropic tool use
TOOL_DEFINITION = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this for competitive research, "
        "market context, recent news, or any topic that benefits from up-to-date sources."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to run.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 10).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}
