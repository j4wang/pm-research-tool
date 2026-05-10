"""
tools/notion.py
Notion writer — creates or updates a page with research output.

The markdown_to_blocks() function converts Claude's markdown output into
native Notion blocks so the saved page is properly formatted rather than
displaying raw markdown syntax.
"""

import re
import os
from notion_client import Client


def _get_client() -> Client:
    return Client(auth=os.environ["NOTION_API_KEY"])


def _parse_inline(text: str) -> list[dict]:
    """
    Parse inline markdown and return a list of Notion rich_text objects.
    Handles bold (**text**), italic (*text*), and plain text.
    Inline elements can be nested within any block type.
    """
    rich_text = []
    # Match **bold**, *italic*, or plain text spans between them
    pattern = re.compile(r'\*\*(.+?)\*\*|\*(.+?)\*|([^*]+)')

    for match in pattern.finditer(text):
        bold_text, italic_text, plain_text = match.groups()
        if bold_text:
            rich_text.append({
                "type": "text",
                "text": {"content": bold_text},
                "annotations": {"bold": True},
            })
        elif italic_text:
            rich_text.append({
                "type": "text",
                "text": {"content": italic_text},
                "annotations": {"italic": True},
            })
        elif plain_text:
            rich_text.append({
                "type": "text",
                "text": {"content": plain_text},
            })

    # Return a plain text object if nothing matched
    return rich_text or [{"type": "text", "text": {"content": text}}]


def markdown_to_blocks(content: str) -> list[dict]:
    """
    Convert a markdown string into a list of Notion block objects.

    Supported elements:
    - # / ## / ###         Headings (h1, h2, h3)
    - - item or * item     Bulleted list
    - 1. item              Numbered list
    - ---                  Divider
    - > text               Quote block
    - | col | col |        Table (with header row)
    - Plain paragraphs     Paragraph block
    - **bold** / *italic*  Inline formatting within any block

    Tables are returned as a special {"_table": True, "rows": [...]} dict
    rather than a standard Notion block, because Notion requires table rows
    to be appended as nested children after the parent table block is created.
    create_page() handles these entries separately.
    """
    blocks = []

    # Process line by line so list items and other single-line
    # elements are handled correctly alongside paragraph blocks
    lines = content.split("\n")
    paragraph_buffer = []
    table_buffer = []  # Accumulates rows for the current markdown table

    def flush_paragraph():
        """Flush any buffered paragraph lines as a single paragraph block."""
        if paragraph_buffer:
            text = " ".join(paragraph_buffer).strip()
            if text:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _parse_inline(text)},
                })
            paragraph_buffer.clear()

    def flush_table():
        """
        Flush the table buffer as a special _table sentinel dict.
        The first row is treated as the header; separator rows (---|---) are skipped.
        create_page() handles converting this into actual Notion table blocks.
        """
        if table_buffer:
            blocks.append({"_table": True, "rows": list(table_buffer)})
            table_buffer.clear()

    def parse_table_row(line: str) -> list[str] | None:
        """
        Parse a markdown table row into a list of cell strings.
        Returns None if the row is a separator (|---|---|).
        """
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Separator rows contain only dashes and colons
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            return None
        return cells

    for line in lines:
        stripped = line.strip()

        # Detect table rows — any line that starts and ends with |
        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_paragraph()
            row = parse_table_row(stripped)
            if row is not None:  # Skip separator rows
                table_buffer.append(row)
            continue

        # A non-table line flushes any in-progress table
        if table_buffer:
            flush_table()

        # Skip blank lines but use them to flush the paragraph buffer
        if not stripped:
            flush_paragraph()
            continue

        # Headings — check deepest level first to avoid prefix conflicts.
        # Notion only supports h1-h3, so h4+ maps to heading_3.
        if stripped.startswith("#### ") or stripped.startswith("##### ") or stripped.startswith("###### "):
            flush_paragraph()
            text = stripped.lstrip("#").strip()
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _parse_inline(text)},
            })

        elif stripped.startswith("### "):
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _parse_inline(stripped[4:])},
            })

        elif stripped.startswith("## "):
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _parse_inline(stripped[3:])},
            })

        elif stripped.startswith("# "):
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "heading_1",
                "heading_1": {"rich_text": _parse_inline(stripped[2:])},
            })

        # Divider (--- or *** or ___)
        elif stripped in ("---", "***", "___"):
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {},
            })

        # Bulleted list item (- or *)
        elif re.match(r'^[-*]\s+', stripped):
            flush_paragraph()
            item_text = re.sub(r'^[-*]\s+', '', stripped)
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_inline(item_text)},
            })

        # Numbered list item (1. 2. etc.)
        elif re.match(r'^\d+\.\s+', stripped):
            flush_paragraph()
            item_text = re.sub(r'^\d+\.\s+', '', stripped)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _parse_inline(item_text)},
            })

        # Blockquote
        elif stripped.startswith("> "):
            flush_paragraph()
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": _parse_inline(stripped[2:])},
            })

        # Plain text — buffer consecutive lines so they form one paragraph block
        else:
            paragraph_buffer.append(stripped)

    # Flush any remaining buffered content at end of input
    flush_paragraph()
    flush_table()

    return blocks


def create_page(parent_page_id: str, title: str, content: str) -> str:
    """
    Create a new Notion page under parent_page_id with the given title and content.
    Content is converted from markdown to native Notion blocks before saving.

    Block handling:
    - Regular blocks are batched in groups of 100 (Notion API limit) and appended
    - Table blocks (returned as {"_table": True, "rows": [...]}) are handled
      separately: a table parent block is created first, then rows are appended
      as its children via a second API call

    Returns the URL of the created page.
    """
    client = _get_client()
    all_blocks = markdown_to_blocks(content)

    # Create the page with no initial children so we can process
    # all blocks uniformly in order below
    response = client.pages.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": title}}]
            }
        },
        children=[],
    )
    page_id = response["id"]

    BATCH_SIZE = 100
    regular_buffer = []

    def flush_regular():
        """Append any buffered regular blocks to the page in batches of 100."""
        for i in range(0, len(regular_buffer), BATCH_SIZE):
            client.blocks.children.append(
                block_id=page_id,
                children=regular_buffer[i:i + BATCH_SIZE],
            )
        regular_buffer.clear()

    for block in all_blocks:
        if block.get("_table"):
            # Flush any preceding regular blocks first to preserve order
            flush_regular()

            rows = block["rows"]
            if not rows:
                continue

            table_width = max(len(r) for r in rows)

            # Build table row blocks
            table_rows = []
            for row in rows:
                # Pad short rows to match table width
                padded = row + [""] * (table_width - len(row))
                table_rows.append({
                    "object": "block",
                    "type": "table_row",
                    "table_row": {
                        "cells": [_parse_inline(cell) for cell in padded]
                    },
                })

            # Notion requires table rows to be included inline with the table
            # parent block in the same API call — they cannot be appended separately
            client.blocks.children.append(
                block_id=page_id,
                children=[{
                    "object": "block",
                    "type": "table",
                    "table": {
                        "table_width": table_width,
                        "has_column_header": True,
                        "has_row_header": False,
                        "children": table_rows,
                    },
                }],
            )
        else:
            regular_buffer.append(block)

    # Flush any remaining regular blocks
    flush_regular()

    return response.get("url", "")


# Tool definition for Anthropic tool use
TOOL_DEFINITION = {
    "name": "notion_create_page",
    "description": (
        "Create a new Notion page with the research output. "
        "Use this at the end of the research task to save the final brief."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "parent_page_id": {
                "type": "string",
                "description": "The Notion page ID to create the new page under.",
            },
            "title": {
                "type": "string",
                "description": "Title of the new page.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Full content of the research brief in markdown format. "
                    "Use # ## ### for headings, - for bullets, 1. for numbered lists, "
                    "--- for dividers, **text** for bold, and *text* for italic."
                ),
            },
        },
        "required": ["parent_page_id", "title", "content"],
    },
}
