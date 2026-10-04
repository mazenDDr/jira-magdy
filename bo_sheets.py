#!/usr/bin/env python3
"""Sync the BO team's (Jira project L1) tickets into two Google Sheets.

Sheet 1 (BO_SHEET_ID)        tabs "MDB", "Installment", "Product", "Others": the L1 board's "To Do" and
                             "Clients To Do" tickets created since BO_FROM, sorted into the tab
                             whose source sheet (see SOURCES) mentions the ticket.
Sheet 2 (INPROGRESS_SHEET_ID) tabs:
    "In Progress"               open tickets currently with the BO team, time in status
    "Pending Customer Response" tickets sent back to the customer, time waiting
    "Pending Other Teams"       open tickets waiting on other teams
    "Productivity"              tickets resolved per person per day

Config comes from environment variables; see .env.example. Run it again to refresh.
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials

PROJECT = os.environ.get("JIRA_PROJECT", "L1")
TZ = ZoneInfo(os.environ.get("SHEET_TIMEZONE", "Africa/Cairo"))
BO_FROM = os.environ.get("BO_FROM", "2026-10-01")
BO_TODO_STATUSES = [x.strip() for x in os.environ.get("BO_TODO_STATUSES", "To Do,Clients To Do").split(",") if x.strip()]
# Words in the Request Type that route a ticket straight to a tab (checked before the source sheets).
KEYWORDS = {
    "Installment": ["installment", "unable to pay due"],
    "MDB": ["refund transaction", "purchase failed", "deposit didn't reflect", "pos transfer",
            "unable to deposit", "unable to withdraw"],
    "Product": ["different balance"],
}
# Team dropdown for the Assignee column: name -> words that identify them in a Jira display name.
TEAM = {
    "Mego": ["magdy"], "Helana": ["helana"], "Abdallah": ["abdalla", "abdullah"], "Heba": ["heba"],
    "Reem": ["reem"], "Samy": ["samy"], "Bassem": ["bassem"], "Mayar": ["mayar"],
}
RESOLVED_CHOICES = ["Solved", "Pending for customer response", "On hold"]
# tab name -> (source spreadsheet id, tab title to read, or "" for every tab)
SOURCES = {
    "MDB": (os.environ.get("MDB_SOURCE_ID", "1zkHFJxgOLRZXEFXcGV_LSjshLmLhXwxOvHLe9MspsV4"), ""),
    "Installment": (os.environ.get("INSTALLMENT_SOURCE_ID", "11j8dl3PmN4dvW7ye6FigjMo2xxXLmBN3E9xkl996tKM"),
                    os.environ.get("INSTALLMENT_SOURCE_TAB", "Callcenter Cases 2026 V2")),
    "Product": (os.environ.get("PRODUCT_SOURCE_ID", "1TnX3FVx1zAEq2wPh_oXyUSHIPYvOJ1DTrQKekfFWfew"), ""),
}
PRODUCTIVITY_DAYS = int(os.environ.get("PRODUCTIVITY_DAYS", "30"))
SLA_HOURS = int(os.environ.get("SLA_HOURS", "24"))
CUSTOMER_STATUS = os.environ.get("CUSTOMER_STATUS", "Pending Customer Response")
# Statuses where the ticket is being worked by the BO team itself.
BO_STATUSES = [s.strip() for s in os.environ.get(
    "BO_STATUSES", "In Progress,To Do,Open,Reopened,Work in progress").split(",") if s.strip()]
# A ticket whose request type or summary contains one of these words belongs to the queue.
INSTALLMENT_WORDS = [w.strip().lower() for w in os.environ.get("INSTALLMENT_KEYWORDS", "installment").split(",") if w.strip()]
MDB_WORDS = [w.strip().lower() for w in os.environ.get("MDB_KEYWORDS", "").split(",") if w.strip()]

REQUEST_TYPE_FIELD = "customfield_11163"
FIELDS = ["summary", "status", "assignee", "reporter", "created", "updated",
          "resolutiondate", REQUEST_TYPE_FIELD]


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


# ---------------------------------------------------------------- Jira

class Jira:
    def __init__(self):
        self.base = env("JIRA_BASE_URL").rstrip("/")
        self.s = requests.Session()
        self.s.auth = (env("JIRA_EMAIL"), env("JIRA_API_TOKEN"))
        self.s.headers["Accept"] = "application/json"

    def search(self, jql, fields, expand=None):
        out, token = [], None
        while True:
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
            if expand:
                params["expand"] = expand
            if token:
                params["nextPageToken"] = token
            r = self.s.get(f"{self.base}/rest/api/3/search/jql", params=params, timeout=90)
            if r.status_code != 200:
                sys.exit(f"Jira error {r.status_code}: {r.text[:400]}")
            data = r.json()
            out += data.get("issues", [])
            token = data.get("nextPageToken")
            if not token:
                return out


def parse_dt(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(TZ)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def status_since(issue):
    """When the ticket last moved into its current status (from the changelog)."""
    current = issue["fields"]["status"]["name"]
    best = None
    for history in (issue.get("changelog") or {}).get("histories", []):
        for item in history.get("items", []):
            if item.get("field") == "status" and item.get("toString") == current:
                when = parse_dt(history["created"])
                if best is None or when > best:
                    best = when
    return best or parse_dt(issue["fields"]["created"])


def queue_of(issue):
    f = issue["fields"]
    text = f"{f.get(REQUEST_TYPE_FIELD) or ''} {f.get('summary') or ''}".lower()
    if any(w in text for w in INSTALLMENT_WORDS):
        return "Installment"
    if MDB_WORDS and any(w in text for w in MDB_WORDS):
        return "MDB"
    return "Other"


def person(obj):
    return (obj or {}).get("displayName") or "Unassigned"


def request_type(f):
    value = f.get(REQUEST_TYPE_FIELD)
    if isinstance(value, dict):
        return value.get("value") or value.get("name") or ""
    return value or ""


def customer_ref(summary):
    m = re.search(r"\b([A-Z]{2,6}-\d+)\s*$", summary or "")
    return m.group(1) if m else ""


# -------------------------------------------------------------- Sheets

def google():
    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(raw) if raw.lstrip().startswith("{") else json.load(open(raw))
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


class Formula(str):
    """A cell value that really is a spreadsheet formula."""


def safe(cell):
    """Stop ticket text from being read as a formula."""
    if isinstance(cell, str) and not isinstance(cell, Formula) and cell[:1] in ("=", "+", "-", "@"):
        return "'" + cell
    return cell


def write_tab(spreadsheet, title, rows, widths=None, filter_range=True):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title, rows=2, cols=len(rows[0]))
    ws.clear()
    ws.resize(rows=max(len(rows), 2), cols=len(rows[0]))
    body = [rows[0]] + [[safe(c) for c in r] for r in rows[1:]]
    for start in range(0, len(body), 5000):
        ws.update(body[start:start + 5000], f"A{start + 1}", value_input_option="USER_ENTERED")
    ws.format("1:1", {"textFormat": {"bold": True},
                      "backgroundColor": {"red": 0.58, "green": 0.77, "blue": 0.71}})
    ws.freeze(rows=1)
    if filter_range and len(rows) > 1:
        ws.set_basic_filter()
    print(f"  '{title}': {len(rows) - 1} rows")
    return ws


def set_timezone(spreadsheet):
    spreadsheet.batch_update({"requests": [{"updateSpreadsheetProperties": {
        "properties": {"timeZone": TZ.key}, "fields": "timeZone"}}]})


def drop_default_tab(spreadsheet):
    """Remove the empty 'Sheet1' tab that new spreadsheets start with."""
    try:
        default = spreadsheet.worksheet("Sheet1")
    except gspread.exceptions.WorksheetNotFound:
        return
    if len(spreadsheet.worksheets()) > 1:
        spreadsheet.del_worksheet(default)


# ------------------------------------------------------------- builders

TICKET_ID = re.compile(r"\b((?:L1|CCS|CC2?)-\d+)\b", re.I)


def source_ticket_ids(gc, sheet_id, tab):
    """Every ticket number (L1-1234, CCS-1234, CC2-1234) mentioned in a source sheet."""
    book = gc.open_by_key(sheet_id)
    found = set()
    for ws in book.worksheets():
        if tab and ws.title.strip().lower() != tab.strip().lower():
            continue
        for row in ws.get_all_values():
            for cell in row:
                if len(cell) <= 40:
                    found.update(m.upper() for m in TICKET_ID.findall(cell))
    return found


def team_member(jira_name):
    """Map a Jira display name to a team dropdown name ("" if not on the team)."""
    low = (jira_name or "").lower()
    for name, words in TEAM.items():
        if any(w in low for w in words):
            return name
    return ""


def existing_choices(spreadsheet, title, column="Resolved"):
    """Dropdown picks already made in a tab, so a refresh doesn't wipe them."""
    try:
        values = spreadsheet.worksheet(title).get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return {}
    if not values or column not in values[0]:
        return {}
    col = values[0].index(column)
    return {r[0]: r[col] for r in values[1:] if len(r) > col and r[col]}


def add_dropdown(spreadsheet, ws, header, choices, rows):
    col = ws.row_values(1).index(header)
    spreadsheet.batch_update({"requests": [{"setDataValidation": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": max(rows, 1) + 1,
                  "startColumnIndex": col, "endColumnIndex": col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": c} for c in choices]},
                 "showCustomUi": True, "strict": True}}}]})


def route(issue, ids_by_tab):
    """Tab for a ticket: request-type keywords first, then the source sheets, else Others."""
    f = issue["fields"]
    text = (request_type(f) or f["summary"]).lower().replace("\u2019", "'")
    for tab, words in KEYWORDS.items():
        if any(w in text for w in words):
            return tab
    ref = customer_ref(f["summary"])
    for tab, ids in ids_by_tab.items():
        if issue["key"] in ids or (ref and ref in ids):
            return tab
    return "Others"


def todo_tabs(jira, gc, book):
    """Build the MDB / Installment / Product / Others tabs from the L1 board's to-do tickets."""
    statuses = ", ".join(f'"{x}"' for x in BO_TODO_STATUSES)
    issues = jira.search(
        f'project = {PROJECT} AND status in ({statuses}) AND created >= "{BO_FROM}" ORDER BY created DESC',
        FIELDS)
    print(f"  {len(issues)} to-do tickets since {BO_FROM}")
    ids_by_tab = {tab: source_ticket_ids(gc, sid, source_tab) for tab, (sid, source_tab) in SOURCES.items()}
    header = ["Key", "Link", "Request Type", "Customer Ticket", "Status",
              "Assignee", "Created", "Updated", "Resolved"]
    tabs = {name: [header] for name in [*SOURCES, "Others"]}
    choices = {}  # Resolved picks made earlier, kept even if a ticket moves to another tab
    assignees = {}
    for name in tabs:
        choices.update(existing_choices(book, name))
        assignees.update({k: v for k, v in existing_choices(book, name, "Assignee").items() if v in TEAM})
    conflicts = 0
    for i in issues:
        f = i["fields"]
        tab = route(i, ids_by_tab)
        in_sheets = [t for t, ids in ids_by_tab.items()
                     if i["key"] in ids or customer_ref(f["summary"]) in ids]
        conflicts += bool(in_sheets) and tab not in in_sheets
        # A pick made in the sheet wins; otherwise start from the Jira assignee.
        who = assignees.get(i["key"]) or team_member((f.get("assignee") or {}).get("displayName"))
        tabs[tab].append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f),
                          customer_ref(f["summary"]), f["status"]["name"], who,
                          fmt(parse_dt(f["created"])), fmt(parse_dt(f["updated"])),
                          choices.get(i["key"], "")])
    for tab, rows in tabs.items():
        ws = write_tab(book, tab, rows)
        add_dropdown(book, ws, "Resolved", RESOLVED_CHOICES, len(rows) - 1)
        add_dropdown(book, ws, "Assignee", list(TEAM), len(rows) - 1)
    if conflicts:
        print(f"  {conflicts} ticket(s) were routed by keyword to a different tab than a source sheet lists")
    for old in ("BO Tickets",):
        try:
            book.del_worksheet(book.worksheet(old))
        except gspread.exceptions.WorksheetNotFound:
            pass


def open_ticket_rows(jira, issues, hours_label):
    rows = [["Key", "Link", "Queue", "Request Type", "Summary", "Assignee", "Status",
             f"{hours_label} Since", "Hours In Status", "Created", "Hours Since Created",
             f"Over {SLA_HOURS}h?"]]
    issues = sorted(issues, key=lambda i: (person(i["fields"].get("assignee")), status_since(i)))
    for n, i in enumerate(issues, start=2):
        f = i["fields"]
        rows.append([i["key"], f"{jira.base}/browse/{i['key']}", queue_of(i), request_type(f),
                     f["summary"], person(f.get("assignee")), f["status"]["name"],
                     fmt(status_since(i)),
                     Formula(f"=ROUND((NOW()-H{n})*24,1)"),
                     fmt(parse_dt(f["created"])),
                     Formula(f"=ROUND((NOW()-J{n})*24,1)"),
                     Formula(f'=IF(I{n}>{SLA_HOURS},"OVERDUE","")')])
    return rows


def done_statuses(jira):
    r = jira.s.get(f"{jira.base}/rest/api/3/status", timeout=60)
    r.raise_for_status()
    return {st["name"] for st in r.json() if st["statusCategory"]["key"] == "done"}


def productivity_rows(jira):
    """Tickets moved into a Done-type status, credited to whoever moved them."""
    done = done_statuses(jira)
    since = datetime.now(TZ) - timedelta(days=PRODUCTIVITY_DAYS)
    issues = jira.search(
        f"project = {PROJECT} AND status changed DURING (-{PRODUCTIVITY_DAYS}d, now()) "
        "AND statusCategory = Done",
        FIELDS, expand="changelog")
    per = defaultdict(lambda: {"n": 0, "hours": 0.0, "inst": 0})
    for i in issues:
        f = i["fields"]
        moves = [(parse_dt(h["created"]), person(h.get("author")))
                 for h in (i.get("changelog") or {}).get("histories", [])
                 for it in h.get("items", [])
                 if it.get("field") == "status" and it.get("toString") in done]
        moves = [m for m in moves if m[0] >= since]
        if not moves:
            continue
        when, who = max(moves)
        key = (when.strftime("%Y-%m-%d"), who)
        per[key]["n"] += 1
        per[key]["hours"] += (when - parse_dt(f["created"])).total_seconds() / 3600
        per[key]["inst"] += queue_of(i) == "Installment"
    rows = [["Date", "Person", "Tickets Resolved", "Of Which Installment", "Avg Hours To Resolve"]]
    for (day, who), v in sorted(per.items(), reverse=True):
        rows.append([day, who, v["n"], v["inst"], round(v["hours"] / v["n"], 1)])
    return rows


# ----------------------------------------------------------------- main

def main():
    jira = Jira()
    gc = google()

    print("Sheet 1: BO to-do tickets by team")
    book1 = gc.open_by_key(env("BO_SHEET_ID"))
    set_timezone(book1)
    todo_tabs(jira, gc, book1)
    drop_default_tab(book1)
    print("  ", book1.url)

    print("Sheet 2: In Progress / Pending / Productivity")
    book2 = gc.open_by_key(env("INPROGRESS_SHEET_ID"))
    set_timezone(book2)
    open_issues = jira.search(f"project = {PROJECT} AND statusCategory != Done",
                              FIELDS, expand="changelog")
    mine = [i for i in open_issues if i["fields"]["status"]["name"] in BO_STATUSES]
    customer = [i for i in open_issues if i["fields"]["status"]["name"] == CUSTOMER_STATUS]
    other = [i for i in open_issues
             if i["fields"]["status"]["name"] not in BO_STATUSES + [CUSTOMER_STATUS]]
    write_tab(book2, "In Progress", open_ticket_rows(jira, mine, "In Status"))
    write_tab(book2, "Pending Customer Response", open_ticket_rows(jira, customer, "Sent To Customer"))
    write_tab(book2, "Pending Other Teams", open_ticket_rows(jira, other, "In Status"))
    write_tab(book2, "Productivity", productivity_rows(jira))
    drop_default_tab(book2)
    print("  ", book2.url)
    print("Done at", datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z"))


def main_with_retry(attempts=4):
    """Google allows ~60 reads a minute; if we hit that, wait for the window to reset and retry."""
    for attempt in range(1, attempts + 1):
        try:
            return main()
        except gspread.exceptions.APIError as exc:
            if exc.response.status_code != 429 or attempt == attempts:
                raise
            print(f"Google rate limit hit, waiting 70s (attempt {attempt}/{attempts})")
            time.sleep(70)


if __name__ == "__main__":
    main_with_retry()
