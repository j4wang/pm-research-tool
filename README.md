# PM Research Assistant

An agentic research tool for product managers. Give it a set of research questions, point it at relevant documents in Google Drive, and it searches the web, reads your context, and produces a structured research brief — optionally saving it directly to Notion.

Built on the Anthropic API's tool use feature, which mirrors the patterns used in Model Context Protocol (MCP) agents: Claude decides which tools to call, in what order, based on the task at hand.

---

## What it does

- Accepts a markdown file of research questions as input
- Optionally reads a Google Doc as context before starting research
- Runs web searches via Tavily to gather current information
- Synthesizes findings into a structured brief
- Optionally writes the output directly to a Notion page

## Example use cases

- Competitive landscape analysis before writing a PRD
- Market research for a new feature area
- Background research on a customer segment or vertical
- Summarizing what's known before a strategy review

---

## Setup

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- A [Tavily API key](https://tavily.com)
- A [Notion integration token](https://developers.notion.com) (optional, for output)
- Google Drive API credentials (optional, for context documents)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configure environment variables

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here
NOTION_API_KEY=your_integration_token_here

# Optional — script will skip Notion output if not set
NOTION_PAGE_ID=your_page_id_here

# Optional — defaults to claude-sonnet-4-6 if not set
ANTHROPIC_MODEL=claude-sonnet-4-6
```

The default model is `claude-sonnet-4-6` (current as of May 2026). Check the 
[Anthropic deprecation docs](https://docs.anthropic.com/en/docs/resources/model-deprecations) 
for the latest recommended model and update `ANTHROPIC_MODEL` accordingly.

Note: Sonnet is recommended over Opus for this use case. Opus's improvements 
are focused on advanced coding and long-horizon engineering tasks — research 
synthesis and web search don't benefit meaningfully from the additional cost.

### Google Drive setup (optional)

If you want to feed Drive documents in as context:

1. Create a Google Cloud project and enable the Drive API
2. Create an OAuth 2.0 Desktop App credential and download it as `credentials.json` in the project root
3. The first time you run the script with `--context-doc`, a browser window will open asking you to authorize access — this only happens once

---

## Usage

**Basic research from questions only:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps"```

**With a Google Drive document as context:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps" --context-doc <drive-file-id>
```

The Drive file ID is the long string in the document URL:
`https://docs.google.com/document/d/FILE_ID_IS_HERE/edit`

**With Notion output:**
```bash
python research.py --questions questions/competitive.md --topic "AI note-taking apps" --notion-page <notion-page-id>
```

The Notion page ID is the string at the end of the page URL (without any query parameters).

Note: If NOTION_PAGE_ID is set in your .env file, you can omit --notion-page entirely 
and output will go there automatically.

**All options combined:**
```bash
python research.py \
  --questions questions/competitive.md \
  --topic <quoted string with company or industry> \
  --context-doc <drive-file-id> \
  --notion-page <notion-page-id>
```

---

## Project structure

```
pm-research-assistant/
├── research.py              # Main entry point and agentic loop
├── tools/
│   ├── search.py            # Tavily web search
│   ├── drive.py             # Google Drive reader
│   └── notion.py            # Notion page writer
├── questions/
│   └── competitive.md       # Example question template
├── requirements.txt
├── credentials.json         # Google OAuth credentials (not committed)
└── .env                     # API keys (not committed)
```

---

## How it works

`research.py` runs an agentic loop:

1. The research questions (and any context doc reference) are sent to Claude along with tool definitions
2. Claude decides which tools to call — web searches, Drive reads — and in what order
3. Tool results are fed back into the conversation
4. Claude continues until it has enough information to write the brief
5. If a Notion page ID was provided, Claude calls the Notion tool to save the output

This is the same pattern used in MCP-based agents: the model orchestrates tool calls rather than the developer hardcoding a sequence of steps.
---

## Customizing research questions

The `questions/` directory holds markdown templates. Copy and edit `competitive.md` to create new templates for different research types (user research, market sizing, feature benchmarking, etc.).

The template format is flexible — Claude reads the whole file, so plain prose questions work just as well as structured lists.

---

## Extending this project

Some natural next steps:

- **Add a Confluence writer** alongside Notion for teams on Atlassian
- **Add a questions generator** that takes a one-liner topic and produces a questions file automatically
- **Connect to true MCP servers** (Tavily, Google Drive, and Notion all publish MCP servers) to replace the Python tool implementations
- **Add output templates** the way the PRD generator uses markdown templates, to control the shape of the research brief

---

## Engineering notes This project started as a straightforward tool-chaining exercise but surfaced several non-obvious production API challenges worth documenting. 

**Agentic loop design.** The core pattern (Claude decides which tools to call rather than the developer hardcoding a sequence) means the conversation history grows with every tool use round-trip. At 30,000 input tokens per minute (the default Sonnet rate limit), a research run with 8-10 tool calls can hit the ceiling mid-session. The fix was a combination of a baseline sleep between calls and exponential backoff retry logic on rate limit errors. 

**Notion's block API.** Notion imposes a 100-block limit per API call and requires table rows to be passed inline with the parent table block in a single request; they cannot be appended separately. Both constraints required non-obvious workarounds that aren't prominently documented. 

**Markdown to Notion conversion.** Claude outputs markdown, but Notion's API expects structured block objects. Building the converter revealed several edge cases: table cells require rich_text objects (not plain strings) to support inline bold and italic, consecutive plain-text lines need buffering to form single paragraph blocks rather than rendering as separate elements, and headings must be matched longest-prefix-first to avoid ### being caught by the # handler.

**Model selection.** Opus 4.7 and Sonnet 4.6 produce comparable output quality for research synthesis tasks. Sonnet ran approximately 20% cheaper on identical inputs. Opus's advantages are concentrated in advanced coding and long-horizon engineering tasks, which don't apply here. 

**Google OAuth in testing mode.** Apps using External OAuth must explicitly allowlist test users in Google Cloud Console. The error message (403: access_denied) doesn't make this obvious.

**Application configuration.** The tool uses a three-layer config system (CLI flag > .env > hardcoded default) that mirrors production API patterns, making it easy to swap models or update credentials without touching code.

**Scope management.** The iteration path (build, run, hit real errors, fix them) reflects the core PM instinct of starting with the minimal viable tool and adding complexity (tables, batching, retries) only when real usage surfaced the need for it.

---
## Notes

- Google Drive access uses `drive.readonly` scope — the script can read but not modify your Drive
- The Notion integration only has access to pages you explicitly share with it (via Connections in the page settings)
- API calls to Anthropic and Tavily consume quota — a typical research run makes 3-8 web searches and 1-2 Claude calls
