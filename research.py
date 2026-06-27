"""
research.py
PM Research Assistant — an agentic research tool for product managers.

MCP CONVERSION NOTE:
web_search and the two drive_* tools are now served by real MCP servers
(mcp-servers/search-server, mcp-servers/drive-server) instead of direct
SDK calls. tools/search.py and tools/drive.py have been removed; their
logic now lives entirely inside the corresponding MCP server. This file
is now an MCP client: it spawns both servers as subprocesses over stdio,
discovers their tools via list_tools(), and routes Claude's tool_use
blocks to the correct session instead of calling dispatch functions
directly.

notion_create_page deliberately stays as a direct SDK call. Converting
all three tools to MCP servers wasn't worth doing for the learning goal
here — two servers already exercise multi-server routing, and a third
teaches nothing the first two didn't. See pm_research_tool_mcp_conversion.md
for the reasoning.

Usage:
  python research.py --questions questions/competitive.md --topic "AI note-taking apps"
  python research.py --questions questions/competitive.md --topic "Figma competitors" --context-doc <google-drive-file-id>
  python research.py --questions questions/competitive.md --topic "CRM tools for SMBs" --notion-page <notion-page-id>

Environment variables required (set in .env):
  ANTHROPIC_API_KEY
  TAVILY_API_KEY       (read by search-server, not this file, now)
  NOTION_API_KEY        (only needed if writing to Notion)
  NOTION_PAGE_ID         (optional default page; overridden by --notion-page)
  ANTHROPIC_MODEL        (optional; overridden by --model)

Observability (optional — tool works without these):
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST                 (optional; defaults to cloud.langfuse.com)
  PHOENIX_COLLECTOR_ENDPOINT    (optional; defaults to http://localhost:6006/v1/traces)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tools.notion import create_page, TOOL_DEFINITION as NOTION_TOOL
from observability import get_tracer, get_langfuse, get_system_prompt, try_init_observability

load_dotenv()

MODEL_DEFAULT = "claude-sonnet-4-6"
MAX_TOKENS = 16500

# Circuit breaker: if a single tool fails this many times in a row within
# one run, it gets dropped from the tools list offered to Claude for the
# rest of that run. Consecutive, not total — a success resets a tool's
# count to zero, so a few isolated failures spread across a long run
# won't trip this.
MAX_CONSECUTIVE_TOOL_FAILURES = 3

# Paths to the two MCP servers, resolved relative to this file so it
# doesn't matter what cwd research.py itself gets launched from.
SEARCH_SERVER_PATH = Path(__file__).resolve().parent / "mcp-servers" / "search-server" / "server.py"
DRIVE_SERVER_PATH = Path(__file__).resolve().parent / "mcp-servers" / "drive-server" / "server.py"

SYSTEM_PROMPT = """You are a senior product manager's research assistant. Your job is to 
produce thorough, structured research briefs by chaining together web searches, reading 
relevant documents from Google Drive, and writing clear output.

When given a set of research questions:
1. Break them into specific search queries and gather information from multiple sources.
2. If a context document is provided, read it first to ground your research.
3. Synthesize findings into a well-structured brief — don't just summarize individual 
   search results, draw conclusions and surface implications.
4. Use headings, bullet points, and clear sections to make output scannable.
5. Cite sources where relevant (include URLs).

Be thorough but focused. A good research brief surfaces what actually matters, 
not everything you found."""


def load_questions(path: str) -> str:
    """Load a questions template from a markdown file."""
    return Path(path).read_text(encoding="utf-8")


def _write_run_artifact(run_dir: Path, result: dict) -> None:
    """Write the run artifact JSON to run_dir/result.json."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


async def connect_mcp_server(stack: AsyncExitStack, server_path: Path) -> ClientSession:
    """
    Spawn one MCP server as a subprocess over stdio and return an
    initialized ClientSession for it. The subprocess and the session
    are both registered on `stack`, so they get torn down together
    when the AsyncExitStack closes, regardless of which one fails
    first or whether an exception is raised in between.

    Uses sys.executable so the spawned server runs under the same
    interpreter (and therefore the same venv) as this script, instead
    of whatever "python" happens to resolve to on PATH.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
    )
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


def _mcp_tool_to_anthropic_tool(mcp_tool) -> dict:
    """
    Convert one MCP Tool schema into the shape the Anthropic API
    expects. The two protocols use different field names for the same
    concept (inputSchema vs input_schema) but the schema content
    itself is identical JSON Schema, so this is just a rename.
    """
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description,
        "input_schema": mcp_tool.inputSchema,
    }


async def dispatch_tool(
    tool_name: str,
    tool_input: dict,
    tool_routing: dict,
    notion_page_id: str = None,
) -> str:
    """
    Route a tool call to the appropriate implementation and return a string result.

    Two dispatch paths now instead of one big if/elif:
      - If tool_name is in tool_routing, it came from list_tools() on one of
        the MCP sessions, and we call it on whichever session owns it. This
        is the "no hardcoded knowledge of which server provides which tool"
        requirement — tool_routing is built dynamically at startup, not
        written out here as literal tool names.
      - notion_create_page stays a direct SDK call, same as before the
        conversion, since it was deliberately not converted to MCP.

    Each call is still wrapped as a Phoenix child span (tool_dispatch) when
    observability is initialized, same as before.
    """
    try:
        tracer = get_tracer()
        span_ctx = tracer.start_as_current_span(
            "tool_dispatch",
            attributes={
                "tool.name": tool_name,
                "tool.input_summary": json.dumps(tool_input)[:500],
            },
        )
    except RuntimeError:
        span_ctx = nullcontext()

    with span_ctx as span:
        if tool_name in tool_routing:
            session = tool_routing[tool_name]
            try:
                mcp_result = await session.call_tool(tool_name, tool_input)
                # MCP tool results are a list of content blocks (usually
                # just one TextContent for our servers). Join any text
                # blocks into the same flat string shape dispatch_tool
                # always returned.
                result = "\n".join(
                    block.text for block in mcp_result.content if hasattr(block, "text")
                )
            except Exception as exc:
                # This is a transport-level failure, not a tool reporting
                # its own error. The server's own code already catches
                # everything it can and returns clean error text for
                # things like a missing API key (see search-server). This
                # catch is for the layer below that: the subprocess died,
                # the pipe broke, the session timed out. Without this, an
                # exception here would propagate up through the while
                # loop and crash the entire research run, losing the
                # brief in progress. A clear error result lets Claude
                # keep working with the tools that are still alive.
                result = (
                    f"Tool '{tool_name}' failed at the transport level: {exc}. "
                    f"The MCP server may have crashed or stopped responding."
                )

        elif tool_name == "notion_create_page":
            page_id = tool_input.get("parent_page_id") or notion_page_id
            if not page_id:
                result = "Error: no Notion page ID provided. Pass --notion-page or include it in the tool call."
            else:
                url = create_page(
                    parent_page_id=page_id,
                    title=tool_input["title"],
                    content=tool_input["content"],
                )
                result = f"Page created successfully: {url}"

        else:
            result = f"Error: unknown tool '{tool_name}'"

        if span is not None:
            span.set_attribute("tool.result_length_chars", len(result))

        return result


async def create_with_retry(client, max_retries: int = 3, **kwargs):
    """
    Call client.messages.create with exponential backoff on rate limit errors.
    Retries up to max_retries times, waiting 10s, 20s, 30s between attempts.
    Raises RuntimeError if all retries are exhausted.

    Same logic as before, just async: awaits the API call and uses
    asyncio.sleep instead of time.sleep so the backoff doesn't block
    the event loop (which matters now that there are MCP subprocess
    sessions alive concurrently).
    """
    for attempt in range(max_retries):
        try:
            return await client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise RuntimeError("Rate limit retries exhausted — try again in a minute.")
            wait = 10 * (attempt + 1)
            print(f"\n[Rate limit hit — retrying in {wait}s]\n")
            await asyncio.sleep(wait)


async def run_research(
    questions: str,
    topic: str = None,
    context_doc_id: str = None,
    notion_page_id: str = None,
    model: str = MODEL_DEFAULT,
    question_template: str = None,
) -> Path:
    """
    Main agentic loop. Sends the research questions to Claude, handles tool calls,
    and loops until Claude produces a final response.

    Now async end to end: the Anthropic client is AsyncAnthropic, the two
    MCP servers are connected and held open for the duration of the run via
    an AsyncExitStack, and dispatch_tool is awaited per tool call.

    When observability is initialized:
      - A root Phoenix span (research_run) wraps the entire loop. Child spans for
        each Anthropic API call are created automatically by AnthropicInstrumentor.
        Child spans for each tool dispatch are created manually in dispatch_tool().
      - A Langfuse trace is created at the start, carrying the prompt version and
        run metadata.

    Returns the Path to the run directory so the caller can pass it to run_evals().
    """
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    started_at = datetime.now(timezone.utc).isoformat()
    run_dir = Path("runs") / run_id

    # --- Observability setup ------------------------------------------------
    # Both are optional: if init_observability() wasn't called (e.g. no Langfuse
    # credentials), we fall back to nullcontext / hardcoded prompt gracefully.

    langfuse_trace_id = None
    phoenix_trace_id = None

    system_prompt, prompt_version = get_system_prompt(SYSTEM_PROMPT)

    try:
        lf = get_langfuse()
        lf_trace = lf.start_observation(
            as_type="span",
            name="research-run",
            input={"topic": topic or "", "questions": questions},
        )
        lf_trace.update(metadata={
            "question_template": question_template or "",
            "model": model,
            "prompt_version": prompt_version,
        })
        langfuse_trace_id = lf_trace.trace_id
    except RuntimeError:
        pass  # Langfuse not initialized — continue without trace

    try:
        tracer = get_tracer()
        root_span_ctx = tracer.start_as_current_span(
            "research_run",
            attributes={
                "research.topic": topic or "",
                "research.question_template": question_template or "",
                "research.model": model,
                "research.prompt_version": prompt_version,
            },
        )
    except RuntimeError:
        root_span_ctx = nullcontext()

    # --- Agentic loop -------------------------------------------------------

    with root_span_ctx as root_span:
        if root_span is not None:
            from opentelemetry import trace as otel_trace
            span_ctx = otel_trace.get_current_span().get_span_context()
            if span_ctx.is_valid:
                phoenix_trace_id = format(span_ctx.trace_id, "032x")

        total_input_tokens = 0
        total_output_tokens = 0
        tool_calls_log = []
        brief = ""
        turn = 0

        # Circuit breaker state: consecutive failure count per tool name,
        # and which tools we've already announced as dropped (so we only
        # print the notice once per tool, not every subsequent turn).
        tool_failure_counts = {}
        dropped_tools = set()

        client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        # --- MCP setup --------------------------------------------------
        # Both servers are connected once, up front, and held open for the
        # whole run via the AsyncExitStack. This is the part that didn't
        # exist before the conversion: tools used to be three Python
        # imports, now they're two live subprocess sessions we have to
        # manage the lifecycle of ourselves.
        async with AsyncExitStack() as mcp_stack:
            search_session = await connect_mcp_server(mcp_stack, SEARCH_SERVER_PATH)
            drive_session = await connect_mcp_server(mcp_stack, DRIVE_SERVER_PATH)

            search_tools = (await search_session.list_tools()).tools
            drive_tools = (await drive_session.list_tools()).tools

            # tool_routing maps a tool name to the session that owns it.
            # Built dynamically from what each server actually reports,
            # not hardcoded — this is what lets the agent loop discover
            # and use tools from both servers with no built-in knowledge
            # of which server provides which tool.
            tool_routing = {}
            tools = []
            for mcp_tool in search_tools:
                tool_routing[mcp_tool.name] = search_session
                tools.append(_mcp_tool_to_anthropic_tool(mcp_tool))
            for mcp_tool in drive_tools:
                tool_routing[mcp_tool.name] = drive_session
                tools.append(_mcp_tool_to_anthropic_tool(mcp_tool))

            if notion_page_id:
                tools.append(NOTION_TOOL)

            base_questions = f"Research topic: {topic}\n\n{questions}" if topic else questions
            if context_doc_id:
                user_message = (
                    f"Context document (Google Drive file ID): {context_doc_id}\n\n"
                    f"Please read this document first, then answer the following research questions:\n\n"
                    f"{base_questions}"
                )
            else:
                user_message = base_questions

            if notion_page_id:
                user_message += (
                    f"\n\nWhen you have completed the research brief, save it to Notion "
                    f"using page ID: {notion_page_id}"
                )

            messages = [{"role": "user", "content": user_message}]

            print("Starting research...\n")

            while True:
                turn += 1

                # Apply the circuit breaker: filter out any tool that's
                # crossed the failure threshold before sending the tools
                # list to Claude. Recomputed each turn (cheap, just a list
                # comprehension) rather than mutating `tools` in place, so
                # there's a single source of truth for "what's currently
                # offered" with no risk of it drifting from dropped_tools.
                active_tools = [t for t in tools if t["name"] not in dropped_tools]

                response = await create_with_retry(
                    client,
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=active_tools,
                    messages=messages,
                )

                # Same rate-limit-driven pause as before, just async now.
                await asyncio.sleep(5)
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

                text_parts = [
                    block.text for block in response.content if hasattr(block, "text")
                ]
                for text in text_parts:
                    print(text)

                if response.stop_reason == "end_turn":
                    brief = "\n\n".join(text_parts)
                    break

                if response.stop_reason == "tool_use":
                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            print(f"\n[Tool call: {block.name}({json.dumps(block.input, indent=2)})]\n")

                            t0 = time.time()
                            result = await dispatch_tool(
                                block.name, block.input, tool_routing, notion_page_id
                            )
                            latency_ms = int((time.time() - t0) * 1000)

                            is_error = result.startswith((
                                "Search failed:",
                                "Drive API error:",
                                "Auth error:",
                                "Unexpected error:",
                                "Read failed:",
                                "Tool '",  # transport-level failure message
                                "Error:",
                            ))

                            tool_calls_log.append({
                                "turn": turn,
                                "tool": block.name,
                                "input": block.input,
                                "result_preview": result[:1000],
                                "result_length_chars": len(result),
                                "latency_ms": latency_ms,
                                "is_error": is_error,
                            })

                            # Circuit breaker bookkeeping: a success resets
                            # the count, a failure increments it. We check
                            # the threshold here rather than waiting until
                            # the next loop iteration so the drop notice
                            # appears right next to the failure that caused
                            # it, not detached from context.
                            if is_error:
                                tool_failure_counts[block.name] = (
                                    tool_failure_counts.get(block.name, 0) + 1
                                )
                                if (
                                    tool_failure_counts[block.name] >= MAX_CONSECUTIVE_TOOL_FAILURES
                                    and block.name not in dropped_tools
                                ):
                                    dropped_tools.add(block.name)
                                    print(
                                        f"\n[Circuit breaker: '{block.name}' failed "
                                        f"{tool_failure_counts[block.name]} times in a row — "
                                        f"removing it from available tools for the rest of "
                                        f"this run]\n"
                                    )
                            else:
                                tool_failure_counts[block.name] = 0

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            })

                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                else:
                    print(f"Unexpected stop reason: {response.stop_reason}")
                    break

            print(f"\nTotal tokens — input: {total_input_tokens}, output: {total_output_tokens}")

            if root_span is not None:
                root_span.set_attribute("research.total_input_tokens", total_input_tokens)
                root_span.set_attribute("research.total_output_tokens", total_output_tokens)
                root_span.set_attribute("research.tool_call_count", len(tool_calls_log))

            if lf_trace is not None:
                lf_trace.end()
                lf.flush()

        # AsyncExitStack closes here: both MCP subprocesses get torn down
        # together, cleanly, whether the loop above succeeded or raised.

    # --- Write run artifact -------------------------------------------------

    completed_at = datetime.now(timezone.utc).isoformat()

    artifact = {
        "run_id": run_id,
        "topic": topic or "",
        "question_template": question_template or "",
        "questions": questions,
        "model": model,
        "prompt_version": prompt_version,
        "phoenix_trace_id": phoenix_trace_id,
        "langfuse_trace_id": langfuse_trace_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "token_totals": {
            "input": total_input_tokens,
            "output": total_output_tokens,
        },
        "tool_calls": tool_calls_log,
        "dropped_tools": sorted(dropped_tools),
        "brief": brief,
    }

    _write_run_artifact(run_dir, artifact)
    print(f"\nRun artifact written to {run_dir}/result.json")

    return run_dir


def main():
    parser = argparse.ArgumentParser(
        description="PM Research Assistant — agentic research tool for product managers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--questions", "-q",
        required=True,
        help="Path to a markdown file containing your research questions.",
    )
    parser.add_argument(
        "--topic", "-t",
        default=None,
        help='The product, company, or space to research. E.g. "AI note-taking apps".',
    )
    parser.add_argument(
        "--context-doc", "-d",
        default=None,
        help="Optional Google Drive file ID to read as context before researching.",
    )
    parser.add_argument(
        "--notion-page", "-n",
        default=None,
        help="Notion page ID to write output to. Overrides NOTION_PAGE_ID env var.",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help=f'Anthropic model to use. Overrides ANTHROPIC_MODEL env var. (default: {MODEL_DEFAULT})',
    )
    parser.add_argument(
        "--skip-evals",
        action="store_true",
        help=(
            "Skip the eval suite after research completes. "
            "Useful during development to avoid extra API calls."
        ),
    )

    args = parser.parse_args()

    # TAVILY_API_KEY is intentionally not checked here. It's read and
    # validated by search-server's own .env lookup, not by this file. If
    # it's missing, the failure surfaces as a clear tool-result error
    # during the run, not as an early sys.exit — that's the cost of
    # search-server owning its own dependency instead of this client
    # knowing about it.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    notion_page = args.notion_page or os.environ.get("NOTION_PAGE_ID")

    if notion_page and not os.environ.get("NOTION_API_KEY"):
        print("Error: NOTION_API_KEY is not set but a Notion page ID was provided.")
        sys.exit(1)

    try_init_observability()

    questions = load_questions(args.questions)
    model = args.model or os.environ.get("ANTHROPIC_MODEL", MODEL_DEFAULT)
    question_template = Path(args.questions).name

    print(f"Questions : {args.questions}")
    if args.topic:
        print(f"Topic     : {args.topic}")
    if args.context_doc:
        print(f"Drive doc : {args.context_doc}")
    if notion_page:
        print(f"Notion    : {notion_page}")
    print(f"Model     : {model}")
    print()

    # run_research is now async — this is the one place sync code calls
    # into the async world, via asyncio.run(). Everything below this
    # point in the call stack is async.
    run_dir = asyncio.run(run_research(
        questions=questions,
        topic=args.topic,
        context_doc_id=args.context_doc,
        notion_page_id=notion_page,
        model=model,
        question_template=question_template,
    ))

    if not args.skip_evals:
        from evals.run_evals import run_evals
        print()
        run_evals(run_dir)
    else:
        print("\nEvals skipped (--skip-evals). To run manually:")
        print(f"  python -m evals.run_evals --run-dir {run_dir}")


if __name__ == "__main__":
    main()
