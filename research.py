"""
research.py
PM Research Assistant — an agentic research tool for product managers.

Usage:
  python research.py --questions questions/competitive.md --topic "AI note-taking apps"
  python research.py --questions questions/competitive.md --topic "Figma competitors" --context-doc <google-drive-file-id>
  python research.py --questions questions/competitive.md --topic "CRM tools for SMBs" --notion-page <notion-page-id>

Environment variables required (set in .env):
  ANTHROPIC_API_KEY
  TAVILY_API_KEY
  NOTION_API_KEY     (only needed if writing to Notion)
  NOTION_PAGE_ID     (optional default page; overridden by --notion-page)
  ANTHROPIC_MODEL    (optional; overridden by --model)

Observability (optional — tool works without these):
  LANGFUSE_PUBLIC_KEY
  LANGFUSE_SECRET_KEY
  LANGFUSE_HOST                 (optional; defaults to cloud.langfuse.com)
  PHOENIX_COLLECTOR_ENDPOINT    (optional; defaults to http://localhost:6006/v1/traces)
"""

import argparse
import json
import os
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from tools.search import search as tavily_search, TOOL_DEFINITION as SEARCH_TOOL
from tools.drive import read_document, list_files, READ_TOOL_DEFINITION, LIST_TOOL_DEFINITION
from tools.notion import create_page, TOOL_DEFINITION as NOTION_TOOL
from observability import get_tracer, get_langfuse, get_system_prompt, try_init_observability

load_dotenv()

MODEL_DEFAULT = "claude-sonnet-4-6"
MAX_TOKENS = 16500

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


def dispatch_tool(tool_name: str, tool_input: dict, notion_page_id: str = None) -> str:
    """
    Route a tool call to the appropriate implementation and return a string result.

    Each call is wrapped as a Phoenix child span (tool_dispatch) when observability
    is initialized. The span is automatically parented to the current research_run
    span via OTel context propagation, so tool latency shows up nested under the
    correct LLM turn in the trace.
    """
    # Build a Phoenix span for this tool call if observability is active.
    # nullcontext() is a no-op stand-in when it isn't.
    try:
        tracer = get_tracer()
        span_ctx = tracer.start_as_current_span(
            "tool_dispatch",
            attributes={
                "tool.name": tool_name,
                # Truncate large inputs — spans aren't a storage layer.
                "tool.input_summary": json.dumps(tool_input)[:500],
            },
        )
    except RuntimeError:
        span_ctx = nullcontext()

    with span_ctx as span:
        if tool_name == "web_search":
            results = tavily_search(
                query=tool_input["query"],
                max_results=tool_input.get("max_results", 5),
            )
            result = json.dumps(results, indent=2)

        elif tool_name == "drive_read_document":
            result = read_document(tool_input["file_id"])

        elif tool_name == "drive_list_files":
            files = list_files(
                folder_id=tool_input.get("folder_id"),
                query=tool_input.get("query"),
                max_results=tool_input.get("max_results", 10),
            )
            result = json.dumps(files, indent=2)

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

        # Record result size on the span so the Phoenix UI shows it.
        if span is not None:
            span.set_attribute("tool.result_length_chars", len(result))

        return result


def create_with_retry(client, max_retries: int = 3, **kwargs):
    """
    Call client.messages.create with exponential backoff on rate limit errors.
    Retries up to max_retries times, waiting 10s, 20s, 30s between attempts.
    Raises RuntimeError if all retries are exhausted.
    """
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise RuntimeError("Rate limit retries exhausted — try again in a minute.")
            wait = 10 * (attempt + 1)
            print(f"\n[Rate limit hit — retrying in {wait}s]\n")
            time.sleep(wait)


def run_research(
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
        # Capture the Phoenix trace ID from the active span context.
        # This links the run artifact back to the Phoenix UI.
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

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

        tools = [SEARCH_TOOL, READ_TOOL_DEFINITION, LIST_TOOL_DEFINITION]
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
            response = create_with_retry(
                client,
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            # Brief pause between calls to stay within the token-per-minute rate limit.
            # The conversation history grows with each tool use round-trip, so without
            # this delay rapid successive calls can exceed 30,000 input tokens/minute.
            time.sleep(5)
            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            # Collect text blocks for printing. On end_turn, also save as the brief.
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

                        # Time the dispatch separately from the Phoenix span latency so
                        # the artifact has the data independently of the tracing backend.
                        t0 = time.time()
                        result = dispatch_tool(block.name, block.input, notion_page_id)
                        latency_ms = int((time.time() - t0) * 1000)

                        tool_calls_log.append({
                            "turn": turn,
                            "tool": block.name,
                            "input": block.input,
                            # 200-char preview is enough for the groundedness eval
                            # without bloating the artifact with full search results.
                            "result_preview": result[:1000],
                            "result_length_chars": len(result),
                            "latency_ms": latency_ms,
                        })

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # Append assistant response and tool results to message history
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                print(f"Unexpected stop reason: {response.stop_reason}")
                break

        print(f"\nTotal tokens — input: {total_input_tokens}, output: {total_output_tokens}")

        # Attach final token totals to the root span for Phoenix dashboards.
        if root_span is not None:
            root_span.set_attribute("research.total_input_tokens", total_input_tokens)
            root_span.set_attribute("research.total_output_tokens", total_output_tokens)
            root_span.set_attribute("research.tool_call_count", len(tool_calls_log))
            
        if lf_trace is not None:
            lf_trace.end()
            lf.flush()

    # --- Write run artifact -------------------------------------------------
    # Written outside the span context so the completed_at timestamp reflects
    # the true end of the run including span teardown.

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

    for key in ["ANTHROPIC_API_KEY", "TAVILY_API_KEY"]:
        if not os.environ.get(key):
            print(f"Error: {key} is not set. Add it to your .env file.")
            sys.exit(1)

    notion_page = args.notion_page or os.environ.get("NOTION_PAGE_ID")

    if notion_page and not os.environ.get("NOTION_API_KEY"):
        print("Error: NOTION_API_KEY is not set but a Notion page ID was provided.")
        sys.exit(1)

    # Initialize observability before doing anything else so the Anthropic
    # auto-instrumentor is patched before the first API call.
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

    run_dir = run_research(
        questions=questions,
        topic=args.topic,
        context_doc_id=args.context_doc,
        notion_page_id=notion_page,
        model=model,
        question_template=question_template,
    )

    if not args.skip_evals:
        from evals.run_evals import run_evals
        print()
        run_evals(run_dir)
    else:
        print("\nEvals skipped (--skip-evals). To run manually:")
        print(f"  python -m evals.run_evals --run-dir {run_dir}")


if __name__ == "__main__":
    main()
