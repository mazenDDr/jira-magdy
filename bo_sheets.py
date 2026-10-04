#!/usr/bin/env python3
"""Sync the BO team's (Jira project L1) tickets into two Google Sheets.

Sheet 1 (BO_SHEET_ID)        tab "BO Tickets": every ticket created in the last BO_DAYS days.
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
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials

PROJECT = os.environ.get("JIRA_PROJECT", "L1")
TZ = ZoneInfo(os.environ.get("SHEET_TIMEZONE", "Africa/Cairo"))
BO_DAYS = int(os.environ.get("BO_DAYS", "90"))
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

def sheet1_rows(jira):
    issues = jira.search(f"project = {PROJECT} AND created >= -{BO_DAYS}d ORDER BY created DESC", FIELDS)
    rows = [["Key", "Link", "Request Type", "Queue", "Summary", "Customer Ticket", "Status",
             "Assignee", "Reporter", "Created", "Updated", "Resolved"]]
    for i in issues:
        f = i["fields"]
        rows.append([i["key"], f"{jira.base}/browse/{i['key']}", request_type(f), queue_of(i),
                     f["summary"], customer_ref(f["summary"]), f["status"]["name"],
                     person(f.get("assignee")), person(f.get("reporter")),
                     fmt(parse_dt(f["created"])), fmt(parse_dt(f["updated"])),
                     fmt(parse_dt(f.get("resolutiondate")))])
    return rows


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

    print("Sheet 1: all BO tickets")
    book1 = gc.open_by_key(env("BO_SHEET_ID"))
    set_timezone(book1)
    write_tab(book1, "BO Tickets", sheet1_rows(jira))
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


if __name__ == "__main__":
    main()
