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
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from tools.search import search as tavily_search, TOOL_DEFINITION as SEARCH_TOOL
from tools.drive import read_document, list_files, READ_TOOL_DEFINITION, LIST_TOOL_DEFINITION
from tools.notion import create_page, TOOL_DEFINITION as NOTION_TOOL

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


def dispatch_tool(tool_name: str, tool_input: dict, notion_page_id: str = None) -> str:
    """Route a tool call to the appropriate implementation and return a string result."""
    if tool_name == "web_search":
        results = tavily_search(
            query=tool_input["query"],
            max_results=tool_input.get("max_results", 5),
        )
        return json.dumps(results, indent=2)

    elif tool_name == "drive_read_document":
        return read_document(tool_input["file_id"])

    elif tool_name == "drive_list_files":
        files = list_files(
            folder_id=tool_input.get("folder_id"),
            query=tool_input.get("query"),
            max_results=tool_input.get("max_results", 10),
        )
        return json.dumps(files, indent=2)

    elif tool_name == "notion_create_page":
        page_id = tool_input.get("parent_page_id") or notion_page_id
        if not page_id:
            return "Error: no Notion page ID provided. Pass --notion-page or include it in the tool call."
        url = create_page(
            parent_page_id=page_id,
            title=tool_input["title"],
            content=tool_input["content"],
        )
        return f"Page created successfully: {url}"

    else:
        return f"Error: unknown tool '{tool_name}'"


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


def run_research(questions: str, topic: str = None, context_doc_id: str = None, notion_page_id: str = None, model: str = MODEL_DEFAULT):
    """
    Main agentic loop. Sends the research questions to Claude, handles tool calls,
    and loops until Claude produces a final response.
    """
    total_input_tokens = 0
    total_output_tokens = 0

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
        response = create_with_retry(
            client,
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        # Brief pause between calls to stay within the token-per-minute rate limit.
        # The conversation history grows with each tool use round-trip, so without
        # this delay rapid successive calls can exceed 30,000 input tokens/minute.
        time.sleep(5)
        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Collect any text output and print it
        for block in response.content:
            if hasattr(block, "text"):
                print(block.text)

        # If Claude is done, exit the loop
        if response.stop_reason == "end_turn":
            break

        # If Claude wants to use tools, handle them and continue
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"\n[Tool call: {block.name}({json.dumps(block.input, indent=2)})]\n")
                    result = dispatch_tool(block.name, block.input, notion_page_id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            # Append assistant response and tool results to message history
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Unexpected stop reason
            print(f"Unexpected stop reason: {response.stop_reason}")
            break

    print(f"\nTotal tokens — input: {total_input_tokens}, output: {total_output_tokens}")


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

    args = parser.parse_args()

    for key in ["ANTHROPIC_API_KEY", "TAVILY_API_KEY"]:
        if not os.environ.get(key):
            print(f"Error: {key} is not set. Add it to your .env file.")
            sys.exit(1)

    notion_page = args.notion_page or os.environ.get("NOTION_PAGE_ID")

    if notion_page and not os.environ.get("NOTION_API_KEY"):
        print("Error: NOTION_API_KEY is not set but a Notion page ID was provided.")
        sys.exit(1)

    questions = load_questions(args.questions)
    model = args.model or os.environ.get("ANTHROPIC_MODEL", MODEL_DEFAULT)

    print(f"Questions : {args.questions}")
    if args.topic:
        print(f"Topic     : {args.topic}")
    if args.context_doc:
        print(f"Drive doc : {args.context_doc}")
    if notion_page:
        print(f"Notion    : {notion_page}")
    print(f"Model     : {model}")
    print()

    run_research(
        questions=questions,
        topic=args.topic,
        context_doc_id=args.context_doc,
        notion_page_id=notion_page,
        model=model,
    )


if __name__ == "__main__":
    main()
