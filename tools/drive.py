"""
tools/drive.py
Google Drive reader — fetches document content to use as research context.
"""

import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_PATH = "token.json"
CREDENTIALS_PATH = "credentials.json"


def _get_service():
    """Authenticate and return a Drive API service instance."""
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def read_document(file_id: str) -> str:
    """
    Export a Google Doc as plain text and return its content.
    Works for Google Docs only (not Sheets, Slides, etc.)
    """
    service = _get_service()
    content = (
        service.files()
        .export(fileId=file_id, mimeType="text/plain")
        .execute()
    )
    return content.decode("utf-8")


def list_files(folder_id: str = None, query: str = None, max_results: int = 10) -> list[dict]:
    """
    List files in Drive, optionally filtered by folder or a search query.

    Returns a list of dicts with: id, name, mimeType.
    """
    service = _get_service()

    q_parts = ["mimeType='application/vnd.google-apps.document'"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    if query:
        q_parts.append(f"name contains '{query}'")

    q = " and ".join(q_parts)
    result = (
        service.files()
        .list(q=q, pageSize=max_results, fields="files(id, name, mimeType)")
        .execute()
    )
    return result.get("files", [])


# Tool definitions for Anthropic tool use

READ_TOOL_DEFINITION = {
    "name": "drive_read_document",
    "description": (
        "Read the full text content of a Google Doc by its file ID. "
        "Use this when the user provides a Drive link or file ID to use as context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "The Google Drive file ID (found in the document URL).",
            }
        },
        "required": ["file_id"],
    },
}

LIST_TOOL_DEFINITION = {
    "name": "drive_list_files",
    "description": (
        "List Google Docs in Drive, optionally filtered by folder ID or name search. "
        "Use this to find relevant documents before reading them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "folder_id": {
                "type": "string",
                "description": "Optional Drive folder ID to restrict the search.",
            },
            "query": {
                "type": "string",
                "description": "Optional filename search string.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of files to return (default 10).",
                "default": 10,
            },
        },
        "required": [],
    },
}
