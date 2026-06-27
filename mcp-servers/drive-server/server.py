"""
MCP server wrapping the Google Drive API, low-level Server class.

Replaces the drive-server skeleton's hardcoded returns with real Drive
calls. Same OAuth pattern as the original research.py: token.json holds
the user's access/refresh tokens and is reused + auto-refreshed across
runs; credentials.json holds the OAuth client ID/secret and is only
needed if token.json doesn't exist yet or the refresh token is dead.

CRITICAL: never print() in this process. stdout is the JSON-RPC channel
to the client. All logging goes to stderr via the `logging` module.
"""

import asyncio
import io
import logging
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# Resolved relative to this file, not cwd, for the same reason as the
# .env lookup in search-server: Claude Code's subprocess cwd isn't
# guaranteed to be the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TOKEN_PATH = PROJECT_ROOT / "token.json"
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Google Docs/Sheets/Slides have no raw byte content of their own;
# Drive has to export them to a real format on request. Anything with
# this MIME prefix needs export_media, not get_media.
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("drive-server")


def _load_drive_service():
    """
    Loads credentials from token.json, refreshing if expired. Falls
    back to the interactive InstalledAppFlow only if no usable token
    exists at all, mirroring the original research.py setup. In normal
    operation this should never hit the interactive branch, since
    you've already done that flow once for the original tool.
    """
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"No valid token.json and no credentials.json found "
                    f"at {CREDENTIALS_PATH}. Can't authenticate."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist the refreshed/new token so next run doesn't need to
        # refresh or re-auth again.
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def _list_files_sync(folder_id: str | None) -> list[dict]:
    service = _load_drive_service()
    query = f"'{folder_id}' in parents" if folder_id else None
    results = (
        service.files()
        .list(
            q=query,
            pageSize=20,
            fields="files(id, name, mimeType)",
        )
        .execute()
    )
    return results.get("files", [])


def _read_doc_sync(doc_id: str) -> str:
    service = _load_drive_service()

    # Check the type first so we know whether to export or download.
    meta = service.files().get(fileId=doc_id, fields="name, mimeType").execute()
    mime_type = meta.get("mimeType", "")

    if mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
        # Native Google Docs/Sheets/Slides: ask Drive to export as
        # plain text. This won't preserve formatting, tables, or
        # images, which is fine for feeding into Claude's context but
        # worth knowing if you ever need richer structure later.
        request = service.files().export_media(
            fileId=doc_id, mimeType="text/plain"
        )
    else:
        # Regular uploaded file (pdf, txt, etc): fetch raw bytes.
        request = service.files().get_media(fileId=doc_id)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    raw = buffer.getvalue()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file (e.g. an actual PDF's raw bytes, not exported
        # text). Returning a clear note beats returning garbage bytes
        # as if they were text.
        return (
            f"[Binary content, {len(raw)} bytes, mimeType={mime_type}. "
            f"Not decodable as text. Real binary handling would need a "
            f"separate extraction step, not implemented in this server.]"
        )


server = Server("drive-server")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    logger.info("list_tools() called by client")
    return [
        types.Tool(
            name="list_files",
            description=(
                "List files in a Google Drive folder. If no folder_id "
                "is given, lists files Drive returns by default (not "
                "scoped to a specific folder)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "string",
                        "description": "The Drive folder ID to list files from",
                    },
                },
            },
        ),
        types.Tool(
            name="read_doc",
            description=(
                "Read the contents of a Google Drive file by its file "
                "ID. Handles both native Google Docs (exported as "
                "plain text) and regular uploaded files."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "The Drive file ID to read",
                    },
                },
                "required": ["doc_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.info("call_tool() invoked: name=%s arguments=%s", name, arguments)

    try:
        if name == "list_files":
            folder_id = arguments.get("folder_id")
            files = await asyncio.to_thread(_list_files_sync, folder_id)

            if not files:
                return [
                    types.TextContent(
                        type="text",
                        text="No files found.",
                    )
                ]

            lines = [f"Files{f' in folder {folder_id}' if folder_id else ''}:"]
            for f in files:
                lines.append(f"- {f['name']} (id: {f['id']}, type: {f['mimeType']})")
            return [types.TextContent(type="text", text="\n".join(lines))]

        elif name == "read_doc":
            doc_id = arguments.get("doc_id", "").strip()
            if not doc_id:
                return [
                    types.TextContent(
                        type="text",
                        text="Read failed: doc_id argument was empty.",
                    )
                ]
            content = await asyncio.to_thread(_read_doc_sync, doc_id)
            return [types.TextContent(type="text", text=content)]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except HttpError as exc:
        # Drive API returned an error response (bad ID, no permission,
        # rate limit, etc). Surface it as a readable tool result.
        logger.exception("Drive API error for tool=%s args=%s", name, arguments)
        return [
            types.TextContent(
                type="text",
                text=f"Drive API error: {exc}",
            )
        ]
    except FileNotFoundError as exc:
        # Missing token.json/credentials.json. Distinct from an API
        # error, since it means auth setup itself is incomplete.
        logger.error(str(exc))
        return [types.TextContent(type="text", text=f"Auth error: {exc}")]
    except Exception as exc:
        # Same broad-catch principle as search-server: this handler
        # must always return a TextContent, never raise, so any other
        # unexpected failure becomes a readable error instead of a
        # crashed call.
        logger.exception("Unexpected error for tool=%s args=%s", name, arguments)
        return [types.TextContent(type="text", text=f"Unexpected error: {exc}")]


async def main():
    logger.info("Starting drive-server over stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
