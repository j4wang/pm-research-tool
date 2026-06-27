# PM Research Assistant

A research tool for product managers. Point it at a set of questions, optionally give it a Google Doc for context, and it searches the web, reads your documents, and writes a structured brief, saving it to Notion if you want.

Web search and Google Drive access run as real MCP servers, communicating with the agent loop over stdio. `research.py` is an MCP client: it discovers each server's tools at startup and routes Claude's tool calls to the correct server, with no hardcoded knowledge of which server provides which tool. Notion output stays a direct SDK call rather than a third MCP server (see [Engineering notes](#engineering-notes) for why).

---

## What it does

- Takes a markdown file of research questions as input
- Optionally reads a Google Doc as context before starting
- Searches the web via Tavily, through a dedicated MCP server
- Reads Google Drive documents through a dedicated MCP server
- Writes a structured research brief
- Optionally saves output directly to a Notion page
- Traces every run in Phoenix (spans for each API call and tool dispatch)
- Scores output quality automatically using LLM-as-judge evals, logged to Langfuse
- Drops a tool from the run if it fails repeatedly, rather than retrying a dead tool indefinitely

## When to use it

- Competitive landscape research before writing a PRD
- Market research for a new feature area
- Background on a customer segment or vertical
- Getting up to speed before a strategy review

---

## Setup

### What you need

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- A [Tavily API key](https://tavily.com): read by the search MCP server, not by `research.py` directly
- A [Notion integration token](https://developers.notion.com): optional, only needed if you want Notion output
- Google Drive API credentials: optional, only needed if you want to feed in a Drive doc as context
- A [Langfuse account](https://langfuse.com): optional, only needed for eval logging and prompt versioning
- [Phoenix](https://phoenix.arize.com) running locally: optional, only needed for distributed tracing

### Install

```bash
pip install -r requirements.txt
```

This now also installs the `mcp` SDK and the Google Drive client libraries, since those are used by `mcp-servers/drive-server`, which runs under the same virtual environment as `research.py`.

### Configure

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here

# Notion output (optional)
NOTION_API_KEY=your_integration_token_here
NOTION_PAGE_ID=your_page_id_here

# Observability (optional, tool works without these)
LANGFUSE_PUBLIC_KEY=your_key_here
LANGFUSE_SECRET_KEY=your_key_here
LANGFUSE_HOST=https://cloud.langfuse.com

# Model (optional, defaults to claude-sonnet-4-6)
ANTHROPIC_MODEL=claude-sonnet-4-6
```

`TAVILY_API_KEY` is read by `mcp-servers/search-server/server.py`, not by `research.py`. If it's missing, the failure surfaces mid-run as a tool result Claude has to work around, not as an immediate startup error. That's an intentional tradeoff: the search server owns its own dependency rather than the client knowing about it.

Sonnet is the right default for this use case. Opus's improvements are concentrated in complex coding and long-horizon engineering tasks, but for research synthesis, the quality difference isn't meaningful and Sonnet runs about 20% cheaper.

### Google Drive setup (optional)

1. Create a Google Cloud project and enable the Drive API
2. Create an OAuth 2.0 Desktop App credential and save it as `credentials.json` in the project root
3. The first time the Drive MCP server needs to authenticate, a browser window opens for authorization, just once: this writes `token.json` in the project root, which gets reused and silently refreshed on every later run
4. Both `credentials.json` and `token.json` are gitignored

If you see a Drive authentication error after the integration has worked before, see the OAuth testing-mode note under [Engineering notes](#engineering-notes) before assuming something's broken.

### Observability setup (optional)

Start Phoenix before running the tool if you want distributed traces:

```bash
phoenix serve
```

This opens a local UI at `http://localhost:6006`. Traces appear there automatically after each run.

For Langfuse, create a free account at [langfuse.com](https://langfuse.com), create a project, and copy the three keys into your `.env`. The tool will register the system prompt in Langfuse on first run and log eval scores after each research session.

---

## Usage

**Basic:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps"
```

**With a Google Drive doc as context:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps" --context-doc <drive-file-id>
```

The file ID is the long string in the document URL: `https://docs.google.com/document/d/FILE_ID_IS_HERE/edit`

**With Notion output:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps" --notion-page <notion-page-id>
```

**Skip the eval suite** (useful when iterating on the research prompt):
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps" --skip-evals
```

**Re-run evals against a previous run** (without re-running the research):
```bash
python -m evals.run_evals --run-dir runs/20260608_001254
```

---

## Project structure

```
pm-research-assistant/
├── research.py              # Agentic loop, MCP client, entry point
├── observability.py         # Phoenix and Langfuse initialization
├── mcp-servers/
│   ├── search-server/
│   │   └── server.py        # MCP server wrapping Tavily web search
│   └── drive-server/
│       └── server.py        # MCP server wrapping Google Drive (list_files, read_doc)
├── tools/
│   └── notion.py             # Notion page writer (direct SDK call, not an MCP server)
├── evals/
│   ├── run_evals.py         # Eval runner (question coverage, groundedness, synthesis)
│   └── prompts/
│       ├── question_coverage.md
│       ├── groundedness.md
│       └── synthesis_quality.md
├── questions/
│   └── competitive.md       # Example question template
├── runs/                    # Run artifacts (gitignored)
├── token.json               # Drive OAuth token (gitignored)
├── credentials.json         # Drive OAuth client credentials (gitignored)
├── requirements.txt
└── .env                     # API keys (not committed)
```

---

## How it works

`research.py` runs an agentic loop, now as an MCP client rather than calling tool implementations directly:

1. At startup, `research.py` spawns `search-server` and `drive-server` as subprocesses over stdio and calls `list_tools()` on each. The tool schemas sent to Claude are built from these discovery responses, not hardcoded in this file.
2. Research questions (and any context doc) are sent to Claude along with the discovered tool schemas and the Notion tool definition.
3. Claude decides which tools to call and in what order: web searches, Drive reads, or both.
4. Each tool call is routed to whichever MCP session reported owning that tool name, except `notion_create_page`, which is still called directly via the Notion SDK.
5. Tool results are added to the conversation history. If a tool fails the same way several turns in a row, it's dropped from the tools offered to Claude for the rest of the run (see the circuit breaker note below).
6. Claude keeps going until it has enough to write the brief.
7. If a Notion page ID is set, Claude calls the Notion tool to save the output.
8. The full run is written to `runs/<timestamp>/result.json`: tool calls (with per-call error flags), token counts, trace IDs, any tools dropped during the run, and the brief itself.
9. The eval suite runs automatically, scoring the brief on coverage, groundedness, and synthesis quality, with results logged to Langfuse.

### Circuit breaker on repeated tool failures

If a single tool fails `MAX_CONSECUTIVE_TOOL_FAILURES` times in a row (default 3, set in `research.py`), it's removed from the tools list offered to Claude for the rest of that run, and a notice prints to the console. The counter is per tool, not global: a dead Drive token doesn't affect the web search tool's availability, and any success resets a tool's own counter to zero. This exists so a transient or permanent upstream failure (an expired token, a missing API key) degrades the run gracefully instead of either crashing it or burning turns retrying something that's already proven dead.

---

## Observability

Each research run produces:

**A Phoenix trace** showing the full execution graph, including every Anthropic API call as a span (with token counts, latency, stop reason) and every tool dispatch as a child span. Useful for diagnosing slow runs or understanding which tool calls contributed to the brief. Tool dispatch spans now wrap MCP client calls rather than direct function calls, but the span structure itself didn't need to change.

**Three Langfuse eval scores:**
- *Question coverage*: did the brief actually answer the research questions?
- *Groundedness*: are the claims traceable to retrieved sources?
- *Synthesis quality*: does the brief draw conclusions, or just summarize?

The system prompt is versioned in Langfuse, so prompt changes are tracked and you can compare eval scores across versions.

---

## Customizing questions

The `questions/` directory holds markdown templates. Copy and edit `competitive.md` for different research types: user research, market sizing, feature benchmarking, etc. The format is flexible; Claude reads the whole file.

---

## Engineering notes

**Why search and Drive are MCP servers and Notion isn't.** Converting all three tools to MCP servers wasn't worth doing. Two servers already exercise multi-server discovery and routing; a third teaches nothing the first two didn't, and Notion's write API is the least interesting one to convert. `notion_create_page` stays a direct SDK call, deliberately.

**MCP servers use the low-level `Server` class, not `FastMCP`.** `FastMCP`'s decorators hide the `list_tools`/`call_tool` protocol handlers, which is good for shipping fast and bad for understanding what's actually happening on the wire. Both servers here implement those handlers directly.

**Async client refactor.** The agent loop in `research.py` is async end to end: `AsyncAnthropic` instead of the sync client, and an `AsyncExitStack` that holds both MCP server subprocesses open for the life of a run, tearing them down together regardless of which one fails first. The 5-second inter-call delay and exponential backoff from the original sync version both carry over unchanged, just on `asyncio.sleep`.

**Two layers of failure handling.** Each MCP server catches its own failures internally (a missing API key, a dead OAuth token, a malformed file ID) and returns readable error text instead of raising. Separately, `research.py` catches failures at the transport level, around the `session.call_tool()` call itself, since a crashed subprocess or a broken pipe is a different failure mode than a tool that ran and reported its own error. Without that second catch, a dead MCP server would crash the whole research run instead of producing a recoverable error message.

**Circuit breaker on repeated tool failures.** See the dedicated section above. Implemented as a per-tool consecutive-failure counter checked against `MAX_CONSECUTIVE_TOOL_FAILURES`, a plain module constant rather than an environment variable, since it's a behavior-tuning constant, not a secret or environment-specific value.

**Google OAuth in testing mode.** Apps using External OAuth in "Testing" publishing status have their refresh tokens revoked after 7 days, regardless of usage. This is different from a normal expiry and can't be fixed by refreshing; it shows up as `invalid_grant: Bad Request` on the next `creds.refresh()` call. The fix is deleting `token.json` and re-running the interactive auth flow. If you have a Google Workspace account, switching the OAuth consent screen's user type to "Internal" removes this 7-day limit and the verification requirement entirely; personal Gmail accounts don't have that option and will hit this periodically.

**Agentic loop and rate limits.** Conversation history grows with every tool call round-trip. At 30,000 input tokens/minute (the Sonnet default), a run with 8-10 tool calls can hit the ceiling mid-session. The fix was a 5-second sleep between calls plus exponential backoff on rate limit errors.

**Notion's block API.** Two non-obvious constraints: 100-block limit per API call, and table rows must be passed inline with the parent table block in the same request, since they can't be appended separately afterward.

**Markdown to Notion conversion.** Claude outputs markdown; Notion expects structured block objects. Edge cases worth knowing: table cells need `rich_text` objects (not plain strings), consecutive plain-text lines need buffering to form a single paragraph block, and heading patterns must be matched longest-prefix-first.

**Observability and the v4 SDK.** The Langfuse Python SDK was rewritten in v4 (released March 2026) with a new observation-centric data model. `lf.trace()` is gone; traces are created via `lf.start_observation()`. Scores use `lf.create_score()`. Worth knowing if you're referencing older docs or examples.

---

## Notes

- Google Drive access uses `drive.readonly` scope, so the tool can read but not modify your Drive
- The Notion integration only has access to pages you explicitly share with it via Connections
- A typical run makes 8-18 web searches and uses roughly 100K-150K input tokens
- `mcp-servers/search-server` and `mcp-servers/drive-server` are spawned as subprocesses by `research.py` itself; they aren't separately registered with any external MCP client unless you choose to do so (e.g. for testing against Claude Code or Claude Desktop during development)
