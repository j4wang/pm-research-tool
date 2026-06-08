# PM Research Assistant

A research tool for product managers. Point it at a set of questions, optionally give it a Google Doc for context, and it searches the web, reads your documents, and writes a structured brief, saving it to Notion if you want.

Built on Anthropic's tool use API, which is the same pattern that powers MCP agents. The model decides what to look up and in what order, rather than following a hardcoded sequence.

---

## What it does

- Takes a markdown file of research questions as input
- Optionally reads a Google Doc as context before starting
- Searches the web via Tavily
- Writes a structured research brief
- Optionally saves output directly to a Notion page
- Traces every run in Phoenix (spans for each API call and tool dispatch)
- Scores output quality automatically using LLM-as-judge evals, logged to Langfuse

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
- A [Tavily API key](https://tavily.com)
- A [Notion integration token](https://developers.notion.com): optional, only needed if you want Notion output
- Google Drive API credentials: optional, only needed if you want to feed in a Drive doc as context
- A [Langfuse account](https://langfuse.com): optional, only needed for eval logging and prompt versioning
- [Phoenix](https://phoenix.arize.com) running locally: optional, only needed for distributed tracing

### Install

```bash
pip install -r requirements.txt
```

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

Sonnet is the right default for this use case. Opus's improvements are concentrated in complex coding and long-horizon engineering tasks, but for research synthesis, the quality difference isn't meaningful and Sonnet runs about 20% cheaper.

### Google Drive setup (optional)

1. Create a Google Cloud project and enable the Drive API
2. Create an OAuth 2.0 Desktop App credential and save it as `credentials.json` in the project root
3. The first time you run with `--context-doc`, a browser window opens for authorization, just once

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
├── research.py              # Agentic loop and entry point
├── observability.py         # Phoenix and Langfuse initialization
├── tools/
│   ├── search.py            # Tavily web search
│   ├── drive.py             # Google Drive reader
│   └── notion.py            # Notion page writer
├── evals/
│   ├── run_evals.py         # Eval runner (question coverage, groundedness, synthesis)
│   └── prompts/
│       ├── question_coverage.md
│       ├── groundedness.md
│       └── synthesis_quality.md
├── questions/
│   └── competitive.md       # Example question template
├── runs/                    # Run artifacts (gitignored)
├── requirements.txt
└── .env                     # API keys (not committed)
```

---

## How it works

`research.py` runs an agentic loop:

1. Research questions (and any context doc) are sent to Claude with tool definitions
2. Claude decides which tools to call and in what order: web searches, Drive reads
3. Tool results are added to the conversation history
4. Claude keeps going until it has enough to write the brief
5. If a Notion page ID is set, Claude calls the Notion tool to save the output
6. The full run is written to `runs/<timestamp>/result.json`: tool calls, token counts, trace IDs, and the brief itself
7. The eval suite runs automatically, scoring the brief on coverage, groundedness, and synthesis quality, with results logged to Langfuse

---

## Observability

Each research run produces:

**A Phoenix trace** showing the full execution graph, including every Anthropic API call as a span (with token counts, latency, stop reason) and every tool dispatch as a child span. Useful for diagnosing slow runs or understanding which tool calls contributed to the brief.

**Three Langfuse eval scores:**
- *Question coverage*: did the brief actually answer the research questions?
- *Groundedness*: are the claims traceable to retrieved sources?
- *Synthesis quality*: does the brief draw conclusions, or just summarize?

The system prompt is versioned in Langfuse, so prompt changes are tracked and you can compare eval scores across versions.

---

## Customizing questions

The `questions/` directory holds markdown templates. Copy and edit `competitive.md` for different research types — user research, market sizing, feature benchmarking, etc. The format is flexible; Claude reads the whole file.

---

## Engineering notes

**Agentic loop and rate limits.** Conversation history grows with every tool call round-trip. At 30,000 input tokens/minute (the Sonnet default), a run with 8-10 tool calls can hit the ceiling mid-session. The fix was a 5-second sleep between calls plus exponential backoff on rate limit errors.

**Notion's block API.** Two non-obvious constraints: 100-block limit per API call, and table rows must be passed inline with the parent table block in the same request — they can't be appended separately afterward.

**Markdown to Notion conversion.** Claude outputs markdown; Notion expects structured block objects. Edge cases worth knowing: table cells need `rich_text` objects (not plain strings), consecutive plain-text lines need buffering to form a single paragraph block, and heading patterns must be matched longest-prefix-first.

**Observability and the v4 SDK.** The Langfuse Python SDK was rewritten in v4 (released March 2026) with a new observation-centric data model. `lf.trace()` is gone; traces are created via `lf.start_observation()`. Scores use `lf.create_score()`. Worth knowing if you're referencing older docs or examples.

**Google OAuth in testing mode.** Apps using External OAuth need test users explicitly allowlisted in Google Cloud Console. The error (`403: access_denied`) doesn't make this obvious.

---

## Notes

- Google Drive access uses `drive.readonly` scope, so the tool can read but not modify your Drive
- The Notion integration only has access to pages you explicitly share with it via Connections
- A typical run makes 8-18 web searches and uses roughly 100K-150K input tokens
