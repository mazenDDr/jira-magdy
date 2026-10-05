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
# Names in the Assignee dropdown, used exactly as given. Picked by hand in the sheet, never matched to Jira.
TEAM = ["Mego", "Helana", "Abdallah", "Heba", "Reem", "Samy", "Bassem", "Mayar"]
RESOLVED_CHOICES = ["Solved", "Pending for customer response", "On hold"]
# tab name -> (source spreadsheet id, tab title to read, or "" for every tab)
SOURCES = {
    "MDB": (os.environ.get("MDB_SOURCE_ID", "1zkHFJxgOLRZXEFXcGV_LSjshLmLhXwxOvHLe9MspsV4"), ""),
    "Installment": (os.environ.get("INSTALLMENT_SOURCE_ID", "11j8dl3PmN4dvW7ye6FigjMo2xxXLmBN3E9xkl996tKM"),
                    os.environ.get("INSTALLMENT_SOURCE_TAB", "Callcenter Cases 2026 V2")),
    "Product": (os.environ.get("PRODUCT_SOURCE_ID", "1TnX3FVx1zAEq2wPh_oXyUSHIPYvOJ1DTrQKekfFWfew"), ""),
}
# People left out of the In Progress sheet (matched on the Jira display name, case-insensitive).
EXCLUDED_PEOPLE = {x.strip().lower() for x in os.environ.get(
    "EXCLUDED_PEOPLE", "loay.khaled,nourhan.khaled,mohanad.maher,Abdullah Mubark").split(",") if x.strip()}
# Jira statuses that count as "In Progress" in sheet 2 (the ticket is being worked by the BO team).
INPROGRESS_STATUSES = [x.strip() for x in os.environ.get(
    "INPROGRESS_STATUSES", "In Progress,Open,Reopened,Work in progress").split(",") if x.strip()]
PRODUCTIVITY_DAYS = int(os.environ.get("PRODUCTIVITY_DAYS", "30"))
SLA_HOURS = int(os.environ.get("SLA_HOURS", "24"))
CUSTOMER_STATUS = os.environ.get("CUSTOMER_STATUS", "Pending Customer Response")
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


def write_tab(spreadsheet, title, rows, number_formats=None, filter_range=True):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title, rows=2, cols=len(rows[0]))
    ws.clear()
    ws.resize(rows=max(len(rows), 2), cols=len(rows[0]))
    # clear() keeps old cell formats; reset them so a changed layout can't show numbers as dates.
    reqs = [{"repeatCell": {"range": {"sheetId": ws.id}, "cell": {"userEnteredFormat": {}},
                            "fields": "userEnteredFormat.numberFormat"}}]
    for header, pattern in (number_formats or {}).items():
        col = rows[0].index(header)
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}})
    spreadsheet.batch_update({"requests": reqs})
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


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


RESOLVED_COLORS = {"Solved": "#b7e1cd", "Pending for customer response": "#fce8b2", "On hold": "#f4c7c3"}
TEAM_COLORS = dict(zip(TEAM, ["#c9daf8", "#d9d2e9", "#d0e0e3", "#ffe5d0", "#fff2cc",
                              "#d9ead3", "#ead1dc", "#cfe2f3"]))
STATUS_COLORS = {"Done": "#b7e1cd", "To Do": "#e8eaed", "Clients To Do": "#e1d5f0", "In Progress": "#c9daf8",
                 "Open": "#fff2cc", "Reopened": "#f4c7c3", "Pending Customer Response": "#fce8b2"}


def apply_colors(spreadsheet, specs):
    """Colour cells by value. specs: [(worksheet, column index, {value: hex colour})].

    Old rules are deleted first so repeated runs don't pile up duplicates."""
    meta = spreadsheet.fetch_sheet_metadata()
    requests_ = []
    for sheet in meta["sheets"]:
        for n in reversed(range(len(sheet.get("conditionalFormats", [])))):
            requests_.append({"deleteConditionalFormatRule": {"sheetId": sheet["properties"]["sheetId"], "index": n}})
    for ws, col, colors in specs:
        for value, colour in colors.items():
            requests_.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [{"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1}],
                "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                                "format": {"backgroundColor": rgb(colour)}}}}})
    if requests_:
        spreadsheet.batch_update({"requests": requests_})


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
        who = assignees.get(i["key"], "")
        tabs[tab].append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f),
                          customer_ref(f["summary"]), f["status"]["name"], who,
                          fmt(parse_dt(f["created"])), fmt(parse_dt(f["updated"])),
                          choices.get(i["key"], "")])
    specs = []
    for tab, rows in tabs.items():
        ws = write_tab(book, tab, rows)
        add_dropdown(book, ws, "Resolved", RESOLVED_CHOICES, len(rows) - 1)
        add_dropdown(book, ws, "Assignee", TEAM, len(rows) - 1)
        specs += [(ws, header.index("Resolved"), RESOLVED_COLORS),
                  (ws, header.index("Assignee"), TEAM_COLORS),
                  (ws, header.index("Status"), STATUS_COLORS)]
    apply_colors(book, specs)
    if conflicts:
        print(f"  {conflicts} ticket(s) were routed by keyword to a different tab than a source sheet lists")
    for old in ("BO Tickets",):
        try:
            book.del_worksheet(book.worksheet(old))
        except gspread.exceptions.WorksheetNotFound:
            pass


def done_statuses(jira):
    r = jira.s.get(f"{jira.base}/rest/api/3/status", timeout=60)
    r.raise_for_status()
    return {st["name"] for st in r.json() if st["statusCategory"]["key"] == "done"}


def excluded(name):
    return (name or "").strip().lower() in EXCLUDED_PEOPLE


def done_info(issue, done, since):
    """(when, who) of the latest move into a Done-type status after `since`, or None."""
    moves = [(parse_dt(h["created"]), person(h.get("author")))
             for h in (issue.get("changelog") or {}).get("histories", [])
             for it in h.get("items", [])
             if it.get("field") == "status" and it.get("toString") in done]
    moves = [m for m in moves if m[0] >= since]
    return max(moves) if moves else None


STATE_ORDER = {"In Progress": 0, "Pending Customer Response": 0, "Done": 1}
TICKET_HEADER = ["Key", "Link", "Queue", "Request Type", "Summary", "Person", "Status", "Jira Status",
                 "Since", "Hours", f"Over {SLA_HOURS}h?", "Created", "Days Since Arrived", "Days In Progress"]


def progress_start(issue, before):
    """When the ticket last moved into an In Progress status before `before` (else when it was created)."""
    starts = [parse_dt(h["created"])
              for h in (issue.get("changelog") or {}).get("histories", [])
              for it in h.get("items", [])
              if it.get("field") == "status" and it.get("toString") in INPROGRESS_STATUSES
              and parse_dt(h["created"]) <= before]
    return max(starts) if starts else parse_dt(issue["fields"]["created"])


def ticket_rows(jira, items, since_label, progress_label="Days In Progress"):
    """items: [(issue, state, since datetime, done datetime or None)].

    Hours is live (counts up) for open tickets and fixed (created -> done) for done ones."""
    rows = [[*TICKET_HEADER[:8], since_label, *TICKET_HEADER[9:13], progress_label]]
    items = sorted(items, key=lambda x: (person(x[0]["fields"].get("assignee")),
                                         STATE_ORDER[x[1]], x[2]))
    for n, (i, state, since, done_at) in enumerate(items, start=2):
        f = i["fields"]
        created = parse_dt(f["created"])
        if done_at:
            # Finished: the counters stop at the moment it was done.
            hours, over = round((done_at - created).total_seconds() / 3600, 1), ""
            arrived = round((done_at - created).total_seconds() / 86400, 1)
            in_progress = round((done_at - progress_start(i, done_at)).total_seconds() / 86400, 1)
        else:
            hours = Formula(f"=ROUND((NOW()-I{n})*24,1)")
            over = Formula(f'=IF(J{n}>{SLA_HOURS},"OVERDUE","")')
            arrived = Formula(f"=ROUND(NOW()-L{n},1)")      # keeps counting up
            in_progress = Formula(f"=ROUND(NOW()-I{n},1)")
        rows.append([i["key"], f"{jira.base}/browse/{i['key']}", queue_of(i), request_type(f),
                     f["summary"], person(f.get("assignee")), state, f["status"]["name"],
                     fmt(since), hours, over, fmt(created), arrived, in_progress])
    return rows


def productivity_rows(done_issues, done, since):
    """Tickets moved into a Done-type status, credited to whoever moved them."""
    per = defaultdict(lambda: {"n": 0, "hours": 0.0, "inst": 0})
    for i in done_issues:
        info = done_info(i, done, since)
        if not info or excluded(info[1]):
            continue
        when, who = info
        key = (when.strftime("%Y-%m-%d"), who)
        per[key]["n"] += 1
        per[key]["hours"] += (when - parse_dt(i["fields"]["created"])).total_seconds() / 3600
        per[key]["inst"] += queue_of(i) == "Installment"
    rows = [["Date", "Person", "Tickets Resolved", "Of Which Installment", "Avg Hours To Resolve"]]
    for (day, who), v in sorted(per.items(), reverse=True):
        rows.append([day, who, v["n"], v["inst"], round(v["hours"] / v["n"], 1)])
    return rows


def progress_book(jira, book):
    """Sheet 2: In Progress (with Done), Pending Customer Response, Productivity."""
    done = done_statuses(jira)
    window = datetime.now(TZ) - timedelta(days=PRODUCTIVITY_DAYS)
    quoted = ", ".join(f'"{x}"' for x in INPROGRESS_STATUSES + [CUSTOMER_STATUS])
    open_issues = jira.search(f"project = {PROJECT} AND status in ({quoted})", FIELDS, expand="changelog")
    done_issues = jira.search(
        f"project = {PROJECT} AND status changed DURING (-{PRODUCTIVITY_DAYS}d, now()) AND statusCategory = Done",
        FIELDS, expand="changelog")

    first = datetime.fromisoformat(BO_FROM).replace(tzinfo=TZ)
    mine, customer = [], []
    for i in open_issues:
        if excluded(person(i["fields"].get("assignee"))):
            continue
        name = i["fields"]["status"]["name"]
        if name == CUSTOMER_STATUS:
            customer.append((i, CUSTOMER_STATUS, status_since(i), None))
        elif name in INPROGRESS_STATUSES:
            mine.append((i, "In Progress", status_since(i), None))
    for i in done_issues:
        info = done_info(i, done, first)
        if info and not excluded(person(i["fields"].get("assignee"))):
            mine.append((i, "Done", info[0], info[0]))

    specs = []
    for title, items, label, days_label in [
            ("In Progress", mine, "In Status / Done Since", "Days In Progress"),
            ("Pending Customer Response", customer, "Sent To Customer", "Days Waiting For Customer")]:
        rows = ticket_rows(jira, items, label, days_label)
        ws = write_tab(book, title, rows, {"Hours": "0.0", "Days Since Arrived": "0.0", days_label: "0.0"})
        specs += [(ws, rows[0].index("Status"), STATUS_COLORS),
                  (ws, rows[0].index(f"Over {SLA_HOURS}h?"), {"OVERDUE": "#ea9999"})]
    write_tab(book, "Productivity", productivity_rows(done_issues, done, window))
    apply_colors(book, specs)
    for old in ("Pending Other Teams",):
        try:
            book.del_worksheet(book.worksheet(old))
        except gspread.exceptions.WorksheetNotFound:
            pass


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

    print("Sheet 2: In Progress / Pending Customer / Productivity")
    book2 = gc.open_by_key(env("INPROGRESS_SHEET_ID"))
    set_timezone(book2)
    progress_book(jira, book2)
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
