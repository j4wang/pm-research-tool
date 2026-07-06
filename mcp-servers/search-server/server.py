"""
MCP server wrapping Tavily search, using the low-level Server class.

This replaces the hardcoded skeleton response with a real Tavily call.
The MCP plumbing (list_tools, call_tool, stdio transport) was already
validated against Claude Code with the skeleton version, so anything
that breaks from here is isolated to the Tavily integration itself.

CRITICAL: never print() in this process. stdout is the JSON-RPC channel
to the client. All logging goes to stderr via the `logging` module.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from tavily import TavilyClient
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# Load .env relative to this file's location, not the process cwd.
# Claude Code launches this as a subprocess and the cwd it starts in
# isn't guaranteed to be the project root.
ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("search-server")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
if not TAVILY_API_KEY:
    # Don't crash at import time. Log it and let call_tool report a
    # clear error to the client instead of the server failing to start
    # with no explanation visible from the Claude Code side.
    logger.warning("TAVILY_API_KEY not found. Checked: %s", ENV_PATH)

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

server = Server("search-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    logger.info("list_tools() called by client")
    return [
        types.Tool(
            name="web_search",
            description=(
                "Search the web for a query and return relevant results "
                "with titles, URLs, and content snippets. Backed by Tavily."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        )
    ]


def _run_tavily_search(query: str, max_results: int) -> dict:
    """Sync call, run off the event loop via asyncio.to_thread."""
    return tavily_client.search(query=query, max_results=max_results)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.info("call_tool() invoked: name=%s arguments=%s", name, arguments)

    try:
        if name != "web_search":
            # Moved inside the try block. Every other failure path in
            # this file (and in drive-server) returns a TextContent
            # instead of raising, so the client always gets a readable
            # result. This branch used to be the one exception: it
            # raised outside any try/except, which meant an unknown
            # tool name would crash the handler instead of producing
            # a clean error, and it never got logged like every other
            # failure here does.
            logger.error("Unknown tool requested: %s", name)
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: unknown tool '{name}'. This server only exposes 'web_search'.",
                )
            ]

        if tavily_client is None:
            return [
                types.TextContent(
                    type="text",
                    text=(
                        "Search failed: TAVILY_API_KEY is not set. "
                        f"Checked for .env at {ENV_PATH}. Set the key and "
                        "restart the server."
                    ),
                )
            ]

        query = arguments.get("query", "").strip()
        max_results = arguments.get("max_results", 5)

        if not query:
            return [
                types.TextContent(
                    type="text",
                    text="Search failed: query argument was empty.",
                )
            ]

        # Tavily's client is synchronous. Run it in a thread so it
        # doesn't block the event loop while waiting on the HTTP call.
        response = await asyncio.to_thread(
            _run_tavily_search, query, max_results
        )

    except Exception as exc:
        # Catch broadly here on purpose. Whatever Tavily throws
        # (timeout, rate limit, auth error, network failure), the
        # client should get a readable explanation back as a tool
        # result, not an unhandled exception killing the call.
        logger.exception("Tavily search failed for query=%r", arguments.get("query"))
        return [
            types.TextContent(
                type="text",
                text=f"Search failed: {exc}",
            )
        ]

    results = response.get("results", [])
    if not results:
        return [
            types.TextContent(
                type="text",
                text=f"No results found for query: '{query}'",
            )
        ]

    formatted = [f"Search results for: '{query}'\n"]
    for i, r in enumerate(results, start=1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        content = r.get("content", "")
        formatted.append(f"{i}. {title}\n   {url}\n   {content}\n")

    return [types.TextContent(type="text", text="\n".join(formatted))]


async def main():
    logger.info("Starting search-server over stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
