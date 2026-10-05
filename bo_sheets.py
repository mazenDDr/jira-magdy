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
from concurrent.futures import ThreadPoolExecutor
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
PENDING_SLA_HOURS = int(os.environ.get("PENDING_SLA_HOURS", "48"))  # flag shown on the Pending Customer tab only
PROGRESS_CHOICES = ["In Progress", "Done"]
CUSTOMER_STATUS = os.environ.get("CUSTOMER_STATUS", "Pending Customer Response")
# A ticket whose request type or summary contains one of these words belongs to the queue.
INSTALLMENT_WORDS = [w.strip().lower() for w in os.environ.get("INSTALLMENT_KEYWORDS", "installment").split(",") if w.strip()]
MDB_WORDS = [w.strip().lower() for w in os.environ.get("MDB_KEYWORDS", "").split(",") if w.strip()]

REQUEST_TYPE_FIELD = "customfield_11163"
RELATED_FIELD = "customfield_11301"  # Jira "Related Tickets Status", e.g. "Operations (OPS-1934): Pending CS |"
# Columns copied from each source sheet into the BO tabs: {column on our tab: header in the source sheet}.
EXTRA_ROWS, MIN_ROWS, EXTRA_COLS, MIN_COLS = 500, 1000, 6, 26  # free space left around every table
PROJECT_CHOICES = ["Lucky", "Chat", "Bsata", "Hyper", "NTG", "Mozare3", "Basata"]  # MDB "Project" dropdown
KEEP_PICKS = ["Comment", "Project"]  # typed or picked in the sheet; kept when the source sheet has nothing
SOURCE_DROPDOWNS = {"MDB": ["Validation"]}  # new columns that keep a dropdown, using the source sheet's own list
SOURCE_COLUMNS = {
    "MDB": {"Card #": "card #", "Acc No": "acc no", "Description": "descrebtion", "Amount": "amount",
            "Disputed Amount If Partial": "disputed amount if partial", "Project": "project",
            "BO Owner": "bo owner", "Validation": "validation"},
    "Installment": {"Comment": "first comment"},
    "Product": {"Comment": "comment"},
}
TEXT_COLUMNS = {"Card #", "Acc No"}  # keep leading zeros
FIELDS = ["summary", "status", "assignee", "reporter", "created", "updated",
          "resolutiondate", REQUEST_TYPE_FIELD]


def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


# ---------------------------------------------------------------- Jira

class Jira:
    """Jira Cloud search. prefetch() starts a query in the background; search() returns its result."""

    def __init__(self):
        self.base = env("JIRA_BASE_URL").rstrip("/")
        self.auth = (env("JIRA_EMAIL"), env("JIRA_API_TOKEN"))
        self._pool = ThreadPoolExecutor(6)
        self._futures = {}

    def _fetch(self, jql, fields, expand):
        s = requests.Session()  # one session per query so queries can run side by side
        s.auth = self.auth
        s.headers["Accept"] = "application/json"
        out, token = [], None
        while True:
            params = {"jql": jql, "fields": ",".join(fields), "maxResults": 100}
            if expand:
                params["expand"] = expand
            if token:
                params["nextPageToken"] = token
            r = s.get(f"{self.base}/rest/api/3/search/jql", params=params, timeout=90)
            if r.status_code != 200:
                raise RuntimeError(f"Jira error {r.status_code}: {r.text[:400]}")
            data = r.json()
            out += data.get("issues", [])
            token = data.get("nextPageToken")
            if not token:
                return out

    def prefetch(self, jql, fields, expand=None):
        key = (jql, tuple(fields), expand)
        if key not in self._futures:
            self._futures[key] = self._pool.submit(self._fetch, jql, fields, expand)

    def search(self, jql, fields, expand=None):
        key = (jql, tuple(fields), expand)
        if key in self._futures:
            return self._futures[key].result()
        return self._fetch(jql, fields, expand)

    def get(self, path):
        s = requests.Session()
        s.auth = self.auth
        r = s.get(f"{self.base}{path}", headers={"Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        return r.json()


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


def get_ws(book, title, ncols):
    """Worksheet by title, created if missing. Titles are read once per spreadsheet and cached."""
    cache = book.__dict__.setdefault("_ws_cache", {w.title: w for w in book.worksheets()})
    if title not in cache:
        cache[title] = book.add_worksheet(title, rows=2, cols=ncols)
    return cache[title]


def delete_tab(book, title):
    cache = book.__dict__.setdefault("_ws_cache", {w.title: w for w in book.worksheets()})
    if title in cache and len(cache) > 1:
        book.del_worksheet(cache.pop(title))


def set_order(book, titles):
    cache = book.__dict__["_ws_cache"]
    book.batch_update({"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": cache[t].id, "index": n}, "fields": "index"}} for n, t in enumerate(titles)]})


def write_tab(book, title, rows, number_formats=None, dropdowns=None, hidden=False, filter_cols=None, loose=()):
    """Queue a tab's new contents. flush() sends every queued tab to Google in three calls."""
    ncols, nrows = len(rows[0]), len(rows)
    ws = get_ws(book, title, ncols)
    sheet = {"sheetId": ws.id}
    cell_range = lambda col: {**sheet, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1}
    reqs = [
        # Leave free rows and columns like a normal sheet, so people can type below and beside the table.
        {"updateSheetProperties": {"properties": {**sheet, "hidden": hidden, "gridProperties": {
            "rowCount": max(nrows + EXTRA_ROWS, MIN_ROWS), "columnCount": max(ncols + EXTRA_COLS, MIN_COLS),
            "frozenRowCount": 1}},
            "fields": "hidden,gridProperties(rowCount,columnCount,frozenRowCount)"}},
        # Google copies a column's dropdown onto columns added beside it, and old layouts leave theirs behind:
        # remove every dropdown first, then set only the ones this tab is meant to have.
        {"setDataValidation": {"range": sheet}},
        # clearing values keeps old cell formats; reset them so a changed layout can't show numbers as dates.
        {"repeatCell": {"range": sheet, "cell": {"userEnteredFormat": {}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {**sheet, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0,
                                  "endColumnIndex": ncols},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                       "backgroundColor": rgb("#94c4b5")}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
    ]
    if filter_cols != 0:  # filter only the ticket columns, never the counters beside them
        reqs.append({"setBasicFilter": {"filter": {"range": {**sheet, "startRowIndex": 0, "startColumnIndex": 0,
                                                            "endColumnIndex": filter_cols or ncols}}}})
    else:
        reqs.append({"clearBasicFilter": {"sheetId": ws.id}})
    for header, pattern in (number_formats or {}).items():
        reqs.append({"repeatCell": {"range": cell_range(rows[0].index(header)),
                                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                                    "fields": "userEnteredFormat.numberFormat"}})
    for header, choices in (dropdowns or {}).items():
        reqs.append({"setDataValidation": {"range": cell_range(rows[0].index(header)), "rule": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": c} for c in choices]},
            "showCustomUi": True, "strict": header not in loose}}})
    body = [rows[0]] + [[safe(c) for c in r] for r in rows[1:]]
    book.__dict__.setdefault("_pending", []).append({"title": title, "reqs": reqs, "rows": body})
    return ws


def flush(book):
    """Clear, lay out and fill every queued tab: three Google calls however many tabs there are."""
    pending = book.__dict__.pop("_pending", [])
    if not pending:
        return
    book.values_batch_clear(body={"ranges": [f"'{p['title']}'" for p in pending]})
    book.batch_update({"requests": [r for p in pending for r in p["reqs"]]})
    book.values_batch_update({"valueInputOption": "USER_ENTERED", "data": [
        {"range": f"'{p['title']}'!A1", "values": p["rows"]} for p in pending]})
    for p in pending:
        print(f"  '{p['title']}': {len(p['rows']) - 1} rows")


def set_timezone(spreadsheet):
    spreadsheet.batch_update({"requests": [{"updateSpreadsheetProperties": {
        "properties": {"timeZone": TZ.key}, "fields": "timeZone"}}]})


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


RESOLVED_COLORS = {"Solved": "#b7e1cd", "Pending for customer response": "#fce8b2", "On hold": "#f4c7c3"}
TEAM_COLORS = dict(zip(TEAM, ["#c9daf8", "#d9d2e9", "#d0e0e3", "#ffe5d0", "#fff2cc",
                              "#d9ead3", "#ead1dc", "#cfe2f3"]))
DONE_COLOR = "#b7e1cd"
STATUS_COLORS = {"Done": DONE_COLOR, "To Do": "#e8eaed", "Clients To Do": "#e1d5f0", "In Progress": "#c9daf8",
                 "Open": "#fff2cc", "Reopened": "#f4c7c3", "Pending Customer Response": "#fce8b2"}


def apply_colors(spreadsheet, specs):
    """Colour cells by value. specs: [(worksheet, column index, {value: hex colour} or one hex colour for a heat scale)].

    Old rules are deleted first so repeated runs don't pile up duplicates."""
    meta = spreadsheet.fetch_sheet_metadata()
    requests_ = []
    for sheet in meta["sheets"]:
        for n in reversed(range(len(sheet.get("conditionalFormats", [])))):
            requests_.append({"deleteConditionalFormatRule": {"sheetId": sheet["properties"]["sheetId"], "index": n}})
    for ws, col, colors in specs:
        if isinstance(colors, str):  # one colour -> white-to-colour heat scale, like a backlog table
            requests_.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [{"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1}],
                "gradientRule": {"minpoint": {"type": "MIN", "color": rgb("#ffffff")},
                                 "maxpoint": {"type": "MAX", "color": rgb(colors)}}}}})
            continue
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


def norm(text):
    return " ".join((text or "").lower().split())


def read_source(gc, sheet_id, tab, columns, dropdown_labels=()):
    """Read a source sheet once.

    Returns (every ticket number mentioned, {ticket: {our column: value}}, {our column: dropdown options})."""
    book = gc.open_by_key(sheet_id)
    found, data, dropdowns = set(), {}, {}
    for ws in book.worksheets():
        if tab and ws.title.strip().lower() != tab.strip().lower():
            continue
        values = ws.get_all_values()
        counts = {}
        for row in values:
            for c, cell in enumerate(row):
                if len(cell) <= 40:
                    found.update(m.upper() for m in TICKET_ID.findall(cell))
                    if TICKET_ID.fullmatch(cell.strip()):
                        counts[c] = counts.get(c, 0) + 1
        if not columns or not values:
            continue
        header = [norm(h) for h in values[0]]
        wanted = {label: header.index(h) for label, h in columns.items() if h in header}
        for label in dropdown_labels:  # the dropdown list the source sheet itself uses on that column
            if label in wanted:
                dropdowns[label] = source_dropdown(book, ws, wanted[label], values)
        top = max(counts.values(), default=0)
        key_cols = [c for c, n in counts.items() if n >= 50 and n >= 0.1 * top]
        for row in values[1:]:
            cells = {label: row[c].strip() for label, c in wanted.items() if c < len(row) and row[c].strip()}
            if not cells:
                continue
            for c in key_cols:
                if c < len(row) and TICKET_ID.fullmatch(row[c].strip()):
                    data.setdefault(row[c].strip().upper(), {}).update(cells)  # a later row fills or updates
    return found, data, dropdowns


def source_dropdown(book, ws, col, values):
    """Options of the dropdown on a source column, read from its most recent filled row (rules change over time)."""
    row = next((n for n in range(len(values), 1, -1) if col < len(values[n - 1]) and values[n - 1][col].strip()), 2)
    letter_ = chr(ord("A") + col) if col < 26 else "A"
    r = book.client.request("get", f"https://sheets.googleapis.com/v4/spreadsheets/{book.id}", params={
        "fields": "sheets.data.rowData.values.dataValidation", "includeGridData": "true",
        "ranges": f"'{ws.title}'!{letter_}{row}:{letter_}{row}"}).json()
    try:
        rule = r["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"]["condition"]
        return [v["userEnteredValue"] for v in rule["values"]] if rule["type"] == "ONE_OF_LIST" else []
    except (KeyError, IndexError):
        return []


def read_picks(book, titles, columns):
    """Dropdown picks already made in the sheet, {column: {ticket key: pick}}, so a refresh doesn't wipe them."""
    cache = book.__dict__.setdefault("_ws_cache", {w.title: w for w in book.worksheets()})
    found = [t for t in titles if t in cache]
    picks = {c: {} for c in columns}
    if not found:
        return picks
    ranges = book.values_batch_get([f"'{t}'" for t in found]).get("valueRanges", [])
    for vr in ranges:
        values = vr.get("values", [])
        if not values:
            continue
        for c in columns:
            if c in values[0]:
                idx = values[0].index(c)
                picks[c].update({r[0]: r[idx] for r in values[1:] if len(r) > idx and r[idx]})
    return picks


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


def q_todo():
    statuses = ", ".join(f'"{x}"' for x in BO_TODO_STATUSES)
    return (f'project = {PROJECT} AND status in ({statuses}) AND created >= "{BO_FROM}" ORDER BY created DESC', FIELDS)


def q_backlog():
    return (f'project = {PROJECT} AND created >= "{BO_FROM}"', FIELDS)


def q_open():
    quoted = ", ".join(f'"{x}"' for x in INPROGRESS_STATUSES + [CUSTOMER_STATUS])
    return (f"project = {PROJECT} AND status in ({quoted})", FIELDS + [RELATED_FIELD], "changelog")


def backlog_tab(jira, book):
    """Contact-type table (Closed / Pending / Untouched) over everything that arrived since BO_FROM."""
    done = done_statuses(jira)
    issues = jira.search(*q_backlog())
    per = defaultdict(lambda: {"Closed": 0, "Pending": 0, "Untouched": 0})
    for i in issues:
        f = i["fields"]
        name = f["status"]["name"]
        group = "Untouched" if name in BO_TODO_STATUSES else "Closed" if name in done else "Pending"
        per[request_type(f) or f["summary"]][group] += 1
    header = ["Contact Type", "Closed", "Pending", "Untouched", "Total"]
    rows = [header] + [[k, v["Closed"], v["Pending"], v["Untouched"], sum(v.values())]
                       for k, v in sorted(per.items(), key=lambda kv: (-kv[1]["Closed"], kv[0]))]
    ws = write_tab(book, "Backlog", rows)
    return [(ws, 1, "#57bb8a"), (ws, 2, "#f6b26b"), (ws, 3, "#e06666")]


def bo_productivity_rows(header, tab_names):
    """Live counts straight from the team tabs, so they follow the Resolved and Assignee dropdowns."""
    res, asg = letter(header, "Resolved"), letter(header, "Assignee")
    count = lambda tab, col, value: f"=COUNTIF('{tab}'!{col}2:{col},\"{value}\")"
    team = [["Team", "Total Tickets", *RESOLVED_CHOICES, "Not Set Yet"]]
    for n, tab in enumerate(tab_names, start=2):
        team.append([tab, Formula(f"=COUNTA('{tab}'!A2:A)"),
                     *[Formula(count(tab, res, c)) for c in RESOLVED_CHOICES],
                     Formula(f"=B{n}-SUM(C{n}:E{n})")])
    last = len(team)
    team.append(["Total", *[Formula(f"=SUM({chr(ord('B') + k)}2:{chr(ord('B') + k)}{last})") for k in range(5)]])

    people = [["Person", *tab_names, "Total"]]
    for n, name in enumerate(TEAM, start=2):
        people.append([name, *[Formula(count(tab, asg, name)) for tab in tab_names],
                       Formula(f"=SUM(I{n}:{chr(ord('H') + len(tab_names))}{n})")])
    first, end = 2, len(people)
    people.append(["No one picked yet", *[
        Formula(f"=COUNTA('{tab}'!A2:A)-SUM({chr(ord('I') + k)}{first}:{chr(ord('I') + k)}{end})")
        for k, tab in enumerate(tab_names)],
        Formula(f"=SUM(I{end + 1}:{chr(ord('H') + len(tab_names))}{end + 1})")])

    width_t, width_p = len(team[0]), len(people[0])
    rows = []
    for n in range(max(len(team), len(people))):
        left = team[n] if n < len(team) else [""] * width_t
        right = people[n] if n < len(people) else [""] * width_p
        rows.append([*left, "", *right])
    return rows


def todo_tabs(jira, book, ids_by_tab, data_by_tab, lists_by_tab):
    """Build the MDB / Installment / Product / Others tabs from the L1 board's to-do tickets."""
    header = ["Key", "Link", "Request Type", "Customer Ticket", "Status",
              "Assignee", "Created", "Updated", "Resolved"]
    # MDB gets the MDP sheet's case columns; every other tab gets a Comment column.
    extras = {name: list(SOURCE_COLUMNS["MDB"]) if name == "MDB" else ["Comment"] for name in [*SOURCES, "Others"]}
    tabs = {name: [header + extras[name]] for name in extras}
    picks = read_picks(book, list(tabs), ["Key", "Resolved", "Assignee", *KEEP_PICKS])  # kept even if a ticket moves tab
    choices = picks["Resolved"]
    assignees = {k: v for k, v in picks["Assignee"].items() if v in TEAM}
    # A ticket stays once it has been listed, even after Jira moves it out of To Do or closes it, so it can be tracked
    # again later. Its Status column keeps following Jira.
    listed = set(picks["Key"])
    everything = jira.search(*q_backlog())
    issues = sorted((i for i in everything if i["fields"]["status"]["name"] in BO_TODO_STATUSES or i["key"] in listed),
                    key=lambda i: i["fields"]["created"], reverse=True)
    todo = sum(i["fields"]["status"]["name"] in BO_TODO_STATUSES for i in everything)
    print(f"  {todo} to-do tickets since {BO_FROM}, {len(issues) - todo} kept from earlier runs")
    conflicts = 0
    for i in issues:
        f = i["fields"]
        tab = route(i, ids_by_tab)
        in_sheets = [t for t, ids in ids_by_tab.items()
                     if i["key"] in ids or customer_ref(f["summary"]) in ids]
        conflicts += bool(in_sheets) and tab not in in_sheets
        who = assignees.get(i["key"], "")
        ref = customer_ref(f["summary"])
        found = data_by_tab.get(tab, {})
        info = found.get(i["key"].upper()) or found.get(ref.upper()) or {}
        extra = []
        for label in extras[tab]:
            value = info.get(label) or (picks[label].get(i["key"], "") if label in KEEP_PICKS else "")
            extra.append("'" + value if value and label in TEXT_COLUMNS else value)
        tabs[tab].append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f),
                          ref, f["status"]["name"], who,
                          fmt(parse_dt(f["created"])), fmt(parse_dt(f["updated"])),
                          choices.get(i["key"], ""), *extra])
    specs = []
    for tab, rows in tabs.items():
        dropdowns = {"Resolved": RESOLVED_CHOICES, "Assignee": TEAM}
        for label, options in lists_by_tab.get(tab, {}).items():
            if options:  # also allow what is already in the cells, so no cell is flagged as invalid
                at = rows[0].index(label)
                present = {r[at] for r in rows[1:] if len(r) > at}
                dropdowns[label] = options + sorted(v for v in present if v and v not in options)
        loose = tuple(SOURCE_DROPDOWNS.get(tab, ()))
        if tab == "MDB":
            dropdowns["Project"], loose = PROJECT_CHOICES, loose + ("Project",)
        ws = write_tab(book, tab, rows, dropdowns=dropdowns, loose=loose)
        shown = {r[header.index("Status")] for r in rows[1:]}
        colours = {name: DONE_COLOR for name in done_statuses(jira)}
        colours.update(STATUS_COLORS)
        specs += [(ws, header.index("Resolved"), RESOLVED_COLORS),
                  (ws, header.index("Assignee"), TEAM_COLORS),
                  (ws, header.index("Status"), {k: v for k, v in colours.items() if k in shown})]
    specs += backlog_tab(jira, book)
    write_tab(book, "Productivity", bo_productivity_rows(header, list(tabs)), filter_cols=0)
    flush(book)
    apply_colors(book, specs)
    if conflicts:
        print(f"  {conflicts} ticket(s) were routed by keyword to a different tab than a source sheet lists")
    delete_tab(book, "BO Tickets")
    set_order(book, ["Backlog", "Productivity", *tabs])


def done_statuses(jira):
    if not hasattr(jira, "_done"):
        jira._done = {st["name"] for st in jira.get("/rest/api/3/status") if st["statusCategory"]["key"] == "done"}
    return jira._done


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


def adf_text(node):
    """Plain text of an Atlassian rich-text field."""
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(adf_text(c) for c in node.get("content", [])) + ("\n" if node.get("type") == "paragraph" else "")


def related_tickets(f):
    """[(team, ticket, status)] from Jira's Related Tickets Status field."""
    found = []
    for part in re.split(r"\||\n", adf_text(f.get(RELATED_FIELD))):
        m = re.match(r"\s*(.+?)\s*\(([A-Z][A-Z0-9]*-\d+)\):\s*(.+?)\s*$", part)
        if m:
            found.append((m.group(1), m.group(2), m.group(3)))
    return found


def with_team(f, own):
    """Which team has the problem now, and the related tickets Jira lists.

    A related ticket whose status mentions CS (Pending CS, PENDING RESPONSE (CS)) is waiting for us, so the
    problem is back with `own` (BO, or Customer on the pending tab); otherwise it is with that ticket's team."""
    related = related_tickets(f)
    if not related:
        return own, ""
    detail = "; ".join(f"{team} {key}: {status}" for team, key, status in related)
    active = list(dict.fromkeys(team for team, _, status in related if not re.search(r"\bCS\b", status)))
    if active:
        return ", ".join(active), detail
    answered = ", ".join(dict.fromkeys(team for team, _, _ in related))
    return f"{own} (reply from {answered})", detail


def letter(header, name):
    return chr(ord("A") + header.index(name))


def team_rows(jira, items, choices):
    """In-progress tickets for one team tab. items: [(issue, in-status-since)].

    Status is a dropdown the team sets (In Progress / Done); picks made earlier are kept."""
    header = ["Key", "Link", "Request Type", "Summary", "Person", "Status", "Jira Status",
              "In Status Since", "Hours", "Created", "Days Since Arrived", "Days In Progress",
              "With Team", "Related Tickets (Jira)"]
    since_col, created_col = letter(header, "In Status Since"), letter(header, "Created")
    status_col = letter(header, "Status")
    # Live counters in the always-visible header row, beside the filtered ticket columns.
    counters = ["", Formula('="Total: "&COUNTA(A2:A)'),
                Formula(f'="In Progress: "&COUNTIF({status_col}2:{status_col},"In Progress")'),
                Formula(f'="Done: "&COUNTIF({status_col}2:{status_col},"Done")')]
    rows = [header + counters]
    oldest = datetime.min.replace(tzinfo=TZ)
    items = sorted(items, key=lambda x: (person(x[0]["fields"].get("assignee")), len(x) > 2, x[1] or oldest))
    for n, item in enumerate(items, start=2):
        i, since = item[0], item[1]
        f = i["fields"]
        if len(item) > 2:  # closed in Jira after the team marked it Done: kept, with the counters stopped
            created = parse_dt(f["created"])
            span = (lambda days: round((since - created).total_seconds() / days, 1) if since else "")
            rows.append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f), f["summary"],
                         person(f.get("assignee")), "Done", f["status"]["name"], fmt(since), span(3600),
                         fmt(created), span(86400), "", "Closed in Jira", ""])
            continue
        rows.append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f), f["summary"],
                     person(f.get("assignee")), choices.get(i["key"], "In Progress"), f["status"]["name"],
                     fmt(since), Formula(f"=ROUND((NOW()-{since_col}{n})*24,1)"), fmt(parse_dt(f["created"])),
                     Formula(f"=ROUND(NOW()-{created_col}{n},1)"),   # keeps counting up
                     Formula(f"=ROUND(NOW()-{since_col}{n},1)"), *with_team(f, "BO")])
    return rows


def pending_rows(jira, items):
    """Tickets sent back to the customer. The overdue flag lives only here (PENDING_SLA_HOURS)."""
    over_label = f"Over {PENDING_SLA_HOURS}h?"
    header = ["Key", "Link", "Request Type", "Summary", "Person", "Status", "Sent To Customer",
              "Hours", over_label, "Created", "Days Since Arrived", "Days Waiting For Customer",
              "With Team", "Related Tickets (Jira)"]
    since_col, created_col = letter(header, "Sent To Customer"), letter(header, "Created")
    hours_col, over_col = letter(header, "Hours"), letter(header, over_label)
    rows = [header + ["", Formula('="Total: "&COUNTA(A2:A)'),
                      Formula(f'="Overdue: "&COUNTIF({over_col}2:{over_col},"OVERDUE")')]]
    items = sorted(items, key=lambda x: (person(x[0]["fields"].get("assignee")), x[1]))
    for n, (i, since) in enumerate(items, start=2):
        f = i["fields"]
        rows.append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f), f["summary"],
                     person(f.get("assignee")), f["status"]["name"], fmt(since),
                     Formula(f"=ROUND((NOW()-{since_col}{n})*24,1)"),
                     Formula(f'=IF({hours_col}{n}>{PENDING_SLA_HOURS},"OVERDUE","")'),
                     fmt(parse_dt(f["created"])),
                     Formula(f"=ROUND(NOW()-{created_col}{n},1)"),
                     Formula(f"=ROUND(NOW()-{since_col}{n},1)"), *with_team(f, "Customer")])
    return rows


def productivity_rows(counts):
    """One row per person: total cases and how many are in progress / done / waiting on the customer."""
    header = ["Person", "Total Cases", "In Progress", f"Done (last {PRODUCTIVITY_DAYS} days)", "Pending Customer"]
    rows = [header + ["", Formula('="All people: "&SUM(B2:B)')]]
    for who, c in sorted(counts.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        rows.append([who, sum(c.values()), c["progress"], c["done"], c["customer"]])
    return rows


DONE_LOG_TAB = "Done Log"


def done_plan(last_sync):
    """None = rebuild the log from scratch, else how many hours back to look for changes."""
    if last_sync is None:
        return None
    age = datetime.now(TZ) - last_sync
    if age > timedelta(days=PRODUCTIVITY_DAYS - 2):
        return None
    return max(6, int(age.total_seconds() // 3600) + 4)


def q_done(window):
    return (f"project = {PROJECT} AND status changed DURING ({window}, now()) AND statusCategory = Done",
            FIELDS, "changelog")


def q_reopened(hours):
    return (f"project = {PROJECT} AND status changed DURING (-{hours}h, now()) AND statusCategory != Done", ["status"])


def load_done_log(book):
    """The saved history of who closed what: ({ticket: (when, who)}, time of last sync)."""
    cache = book.__dict__.setdefault("_ws_cache", {w.title: w for w in book.worksheets()})
    if DONE_LOG_TAB not in cache:
        return {}, None
    values = book.values_batch_get([f"'{DONE_LOG_TAB}'"])["valueRanges"][0].get("values", [])
    entries, last_sync = {}, None
    for row in values[1:]:
        if len(row) > 4 and row[4]:
            last_sync = datetime.fromtimestamp(int(row[4]), TZ)
        if row and row[0] and len(row) > 2 and row[1]:
            entries[row[0]] = (datetime.fromtimestamp(int(row[1]), TZ), row[2])
    return entries, last_sync


def refresh_done_log(jira, entries, last_sync, window):
    """Add what was closed since the last sync. Only the first run needs the full 30 days from Jira."""
    done = done_statuses(jira)
    hours = done_plan(last_sync)
    if hours is None:
        entries, closed = {}, jira.search(*q_done(f"-{PRODUCTIVITY_DAYS}d"))
    else:
        closed = jira.search(*q_done(f"-{hours}h"))
        for i in jira.search(*q_reopened(hours)):  # opened again since: no longer counts as done
            entries.pop(i["key"], None)
    for i in closed:
        info = done_info(i, done, window)
        if info:
            entries[i["key"]] = info
    return {k: v for k, v in entries.items() if v[0] >= window}


def done_log_rows(entries):
    now = int(datetime.now(TZ).timestamp())
    rows = [["Key", "Done At", "By", "", "Last Sync"]]
    for n, (key, (when, who)) in enumerate(sorted(entries.items())):
        rows.append([key, int(when.timestamp()), who, "", now if n == 0 else ""])
    if len(rows) == 1:
        rows.append(["", "", "", "", now])
    return rows


def progress_book(jira, book, ids_by_tab, log):
    """Sheet 2: MDB / Installment / Product / Others (in-progress tickets), Pending Customer Response, Productivity."""
    window = datetime.now(TZ) - timedelta(days=PRODUCTIVITY_DAYS)
    open_issues = jira.search(*q_open())
    entries = refresh_done_log(jira, *log, window)

    team_tabs = [*SOURCES, "Others"]
    # In Progress / Done picks made in the sheet earlier
    choices = {k: v for k, v in read_picks(book, team_tabs, ["Status"])["Status"].items() if v in PROGRESS_CHOICES}

    by_tab = {name: [] for name in team_tabs}
    customer = []
    counts = defaultdict(lambda: {"progress": 0, "done": 0, "customer": 0})
    for i in open_issues:
        who = person(i["fields"].get("assignee"))
        if excluded(who):
            continue
        name = i["fields"]["status"]["name"]
        if name == CUSTOMER_STATUS:
            customer.append((i, status_since(i)))
            counts[who]["customer"] += 1
        elif name in INPROGRESS_STATUSES:
            by_tab[route(i, ids_by_tab)].append((i, status_since(i)))
            counts[who]["done" if choices.get(i["key"]) == "Done" else "progress"] += 1
    # Tickets the team marked Done stay on the sheet after Jira closes them. They are already counted as Done
    # through the Done Log below, so they are not counted again here.
    open_keys = {i["key"] for i in open_issues}
    kept = sorted(k for k, v in choices.items() if v == "Done" and k not in open_keys)
    for start in range(0, len(kept), 100):
        for i in jira.search(f"key in ({', '.join(kept[start:start + 100])})", FIELDS):
            if not excluded(person(i["fields"].get("assignee"))):
                closed = entries.get(i["key"])
                by_tab[route(i, ids_by_tab)].append((i, closed[0] if closed else None, True))
    for when, who in entries.values():  # closed in Jira: credited to whoever moved the ticket
        if not excluded(who):
            counts[who]["done"] += 1
    counts.pop("Unassigned", None)

    specs = []
    for tab in team_tabs:
        rows = team_rows(jira, by_tab[tab], choices)
        ws = write_tab(book, tab, rows, {"Hours": "0.0", "Days Since Arrived": "0.0", "Days In Progress": "0.0"},
                       dropdowns={"Status": PROGRESS_CHOICES}, filter_cols=rows[0].index(""))
        specs.append((ws, rows[0].index("Status"), STATUS_COLORS))
    rows = pending_rows(jira, customer)
    ws = write_tab(book, "Pending Customer Response", rows,
                   {"Hours": "0.0", "Days Since Arrived": "0.0", "Days Waiting For Customer": "0.0"},
                   filter_cols=rows[0].index(""))
    specs += [(ws, rows[0].index("Status"), STATUS_COLORS),
              (ws, rows[0].index(f"Over {PENDING_SLA_HOURS}h?"), {"OVERDUE": "#ea9999"})]
    rows = productivity_rows(counts)
    ws = write_tab(book, "Productivity", rows, filter_cols=5)
    specs += [(ws, 2, "#f6b26b"), (ws, 3, "#57bb8a"), (ws, 4, "#ffd966")]
    write_tab(book, DONE_LOG_TAB, done_log_rows(entries), hidden=True)  # kept for the next run, hidden from view
    flush(book)
    apply_colors(book, specs)
    for old in ("In Progress", "Pending Other Teams"):
        delete_tab(book, old)
    set_order(book, [*team_tabs, "Pending Customer Response", "Productivity", DONE_LOG_TAB])


# ----------------------------------------------------------------- main

def main():
    started = time.time()
    jira = Jira()
    gc = google()
    book1 = gc.open_by_key(env("BO_SHEET_ID"))
    book2 = gc.open_by_key(env("INPROGRESS_SHEET_ID"))
    for book in (book1, book2):
        if book.timezone != TZ.key:
            set_timezone(book)
    log = load_done_log(book2)

    # Start every Jira query now so they run while the source sheets are being read.
    for query in (q_backlog(), q_open()):
        jira.prefetch(*query)
    hours = done_plan(log[1])
    jira.prefetch(*q_done(f"-{PRODUCTIVITY_DAYS}d" if hours is None else f"-{hours}h"))
    if hours is not None:
        jira.prefetch(*q_reopened(hours))
    jira._pool.submit(done_statuses, jira)
    with ThreadPoolExecutor(len(SOURCES)) as pool:  # each source sheet gets its own Google client
        futures = {tab: pool.submit(read_source, google(), sid, st, SOURCE_COLUMNS.get(tab, {}),
                                    SOURCE_DROPDOWNS.get(tab, ()))
                   for tab, (sid, st) in SOURCES.items()}
        sources = {tab: f.result() for tab, f in futures.items()}
    ids_by_tab = {tab: ids for tab, (ids, _, _) in sources.items()}
    data_by_tab = {tab: data for tab, (_, data, _) in sources.items()}
    lists_by_tab = {tab: lists for tab, (_, _, lists) in sources.items()}

    print("Sheet 1: BO to-do tickets by team")
    todo_tabs(jira, book1, ids_by_tab, data_by_tab, lists_by_tab)
    drop_default_tab(book1)
    print("  ", book1.url)

    print("Sheet 2: team tabs / Pending Customer / Productivity")
    progress_book(jira, book2, ids_by_tab, log)
    drop_default_tab(book2)
    print("  ", book2.url)
    print(f"Done at {datetime.now(TZ).strftime('%Y-%m-%d %H:%M %Z')} in {time.time() - started:.0f}s")


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
