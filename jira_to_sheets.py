#!/usr/bin/env python3
"""Export Jira tickets into Google Sheets.

Writes every ticket matching a JQL query into:
  * a PRIVATE spreadsheet (all columns), shared only with you, and
  * optionally a SHARED/team spreadsheet (a configurable subset of columns).

All configuration comes from environment variables (see .env.example).
Never hard-code or commit API tokens.
"""

import json
import os
import sys
from datetime import datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

JIRA_FIELDS = [
    "summary", "status", "issuetype", "priority", "assignee", "reporter",
    "created", "updated", "duedate", "resolution", "labels", "project",
    "description",
]

# (column header, function(issue) -> cell value)
ALL_COLUMNS = {
    "Key": lambda i, base: i["key"],
    "Link": lambda i, base: f"{base}/browse/{i['key']}",
    "Project": lambda i, base: _name(i["fields"].get("project")),
    "Type": lambda i, base: _name(i["fields"].get("issuetype")),
    "Summary": lambda i, base: i["fields"].get("summary") or "",
    "Status": lambda i, base: _name(i["fields"].get("status")),
    "Priority": lambda i, base: _name(i["fields"].get("priority")),
    "Assignee": lambda i, base: _person(i["fields"].get("assignee")),
    "Reporter": lambda i, base: _person(i["fields"].get("reporter")),
    "Resolution": lambda i, base: _name(i["fields"].get("resolution")),
    "Labels": lambda i, base: ", ".join(i["fields"].get("labels") or []),
    "Created": lambda i, base: _date(i["fields"].get("created")),
    "Updated": lambda i, base: _date(i["fields"].get("updated")),
    "Due Date": lambda i, base: i["fields"].get("duedate") or "",
    "Description": lambda i, base: _adf_to_text(i["fields"].get("description"))[:5000],
}

DEFAULT_SHARED_COLUMNS = [
    "Key", "Link", "Type", "Summary", "Status", "Priority", "Assignee", "Updated", "Due Date",
]


def _name(obj):
    return (obj or {}).get("name", "")


def _person(obj):
    return (obj or {}).get("displayName", "") or "Unassigned"


def _date(value):
    if not value:
        return ""
    # Jira format: 2024-05-01T10:20:30.000+0000
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _adf_to_text(node):
    """Flatten an Atlassian Document Format description into plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [_adf_to_text(child) for child in node.get("content", [])]
    sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
    text = sep.join(parts)
    if node.get("type") in ("paragraph", "heading", "listItem"):
        text += "\n"
    return text.strip("\n") if node.get("type") == "doc" else text


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def fetch_jira_issues(base_url, email, token, jql):
    """Fetch all issues for a JQL query using the Jira Cloud search API."""
    url = f"{base_url}/rest/api/3/search/jql"
    session = requests.Session()
    session.auth = (email, token)
    session.headers["Accept"] = "application/json"

    issues, next_token = [], None
    while True:
        params = {"jql": jql, "fields": ",".join(JIRA_FIELDS), "maxResults": 100}
        if next_token:
            params["nextPageToken"] = next_token
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            sys.exit(f"Jira API error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if data.get("isLast", True) or not next_token:
            return issues


def build_rows(issues, columns, base_url):
    rows = [columns]
    for issue in issues:
        rows.append([ALL_COLUMNS[c](issue, base_url) for c in columns])
    return rows


def google_client():
    raw = require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    # Accept either the JSON contents or a path to the key file.
    info = json.loads(raw) if raw.lstrip().startswith("{") else json.load(open(raw))
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds), info.get("client_email")


def write_sheet(client, sheet_id, tab_name, rows, label):
    try:
        spreadsheet = client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        sys.exit(f"Could not open {label} spreadsheet {sheet_id}: {exc}")
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(tab_name, rows=len(rows) + 10, cols=len(rows[0]))

    ws.clear()
    ws.resize(rows=max(len(rows), 2), cols=len(rows[0]))
    ws.update(rows, "A1", value_input_option="RAW")
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.freeze(rows=1)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{label}] wrote {len(rows) - 1} tickets to '{tab_name}' "
          f"({spreadsheet.url}) at {stamp}")


def parse_columns(env_name, default):
    raw = os.environ.get(env_name, "").strip()
    columns = [c.strip() for c in raw.split(",") if c.strip()] if raw else default
    unknown = [c for c in columns if c not in ALL_COLUMNS]
    if unknown:
        sys.exit(f"{env_name} has unknown columns {unknown}. Valid: {list(ALL_COLUMNS)}")
    return columns


def main():
    base_url = require_env("JIRA_BASE_URL").rstrip("/")
    email = require_env("JIRA_EMAIL")
    token = require_env("JIRA_API_TOKEN")
    jql = os.environ.get("JIRA_JQL", "").strip() or "created >= -3650d ORDER BY updated DESC"
    tab = os.environ.get("SHEET_TAB_NAME", "").strip() or "Jira Tickets"

    private_id = require_env("PRIVATE_SHEET_ID")
    shared_id = os.environ.get("SHARED_SHEET_ID", "").strip()

    private_cols = parse_columns("PRIVATE_COLUMNS", list(ALL_COLUMNS))
    shared_cols = parse_columns("SHARED_COLUMNS", DEFAULT_SHARED_COLUMNS)

    print(f"Fetching Jira issues: {jql}")
    issues = fetch_jira_issues(base_url, email, token, jql)
    print(f"Fetched {len(issues)} issues")

    client, sa_email = google_client()
    print(f"Using Google service account: {sa_email}")

    write_sheet(client, private_id, tab, build_rows(issues, private_cols, base_url), "private")
    if shared_id:
        write_sheet(client, shared_id, tab, build_rows(issues, shared_cols, base_url), "shared")
    else:
        print("SHARED_SHEET_ID not set; skipping shared sheet")


if __name__ == "__main__":
    main()
