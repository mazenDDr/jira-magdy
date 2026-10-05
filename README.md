# jira-magdy

Exports Jira tickets into Google Sheets:

- **Private sheet**: every column, including descriptions and reporter. Share it only with yourself.
- **Shared sheet** (optional): a smaller set of columns for the team.

Each run replaces the `Jira Tickets` tab in each sheet with the latest data.

## Setup

1. **Jira API token**: create one at https://id.atlassian.com/manage-profile/security/api-tokens.
2. **Google service account**:
   - In Google Cloud Console, enable the *Google Sheets API*.
   - Create a service account and download a JSON key.
   - Create two Google Sheets (private + shared). Share **both** with the service account's
     `client_email` as *Editor*. Share the shared sheet with your team, and keep the private one to yourself.
3. Set the environment variables listed in `.env.example`. Use environment secrets and don't commit them.

## Run

```bash
pip install -r requirements.txt
python jira_to_sheets.py
```

Available columns: Key, Link, Project, Type, Summary, Status, Priority, Assignee, Reporter,
Resolution, Labels, Created, Updated, Due Date, Description.

## BO team sheets (`bo_sheets.py`)

`python bo_sheets.py` refreshes two sheets from Jira project `L1`:

- **BO Tickets (Auto)**: the L1 board's *To Do* and *Clients To Do* tickets created since `BO_FROM`, split into
  tabs **MDB**, **Installment** and **Product** by looking each ticket up in the matching source sheet.
  The *Resolved* column is a dropdown (Solved / Pending for customer response / On hold); picks survive refreshes.
- **In Progress** sheet: tabs **MDB**, **Installment**, **Product** and **Others** (open tickets with the BO team, routed like
  sheet 1; *Status* is an In Progress / Done dropdown the team sets), **Pending Customer Response** (flagged OVERDUE after
  `PENDING_SLA_HOURS`, 48 by default) and **Productivity** (one row per person: total cases, in progress, done, pending
  customer). People in `EXCLUDED_PEOPLE` are left out. Hours and day counters are live formulas.
- **BO Tickets** also has a **Backlog** tab: Closed / Pending / Untouched counts per contact type since `BO_FROM`.

## Automatic refresh

`.github/workflows/sync.yml` runs `bo_sheets.py`. It must be on the repo's default branch, and the repo needs two
secrets (Settings -> Secrets and variables -> Actions): `JIRA_API_TOKEN` and `GOOGLE_SERVICE_ACCOUNT_JSON` (the full
contents of the key file).

### Reliable refresh (recommended)

GitHub's own `schedule:` is best-effort and skips runs, so the sheets can go hours without updating. Use a Google time
trigger to start the workflow instead:

1. Create a fine-grained GitHub token: Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens.
   Repository access: only `jira-magdy`. Permissions: **Actions: Read and write** (nothing else).
2. Open the BO sheet -> Extensions -> Apps Script. Paste in `apps-script/trigger.gs`.
3. Project Settings (gear) -> Script properties -> add `GITHUB_TOKEN` with the token.
4. Triggers (clock icon) -> Add trigger -> function `triggerSync`, time-driven, minutes timer, **every 30 minutes**.
5. Run `triggerSync` once by hand to authorise it and check the Actions tab shows a new run.

The script runs every 30 minutes by day (09:00-22:59 Cairo) and once an hour overnight, and Google handles daylight saving.
Once this works, delete the `schedule:` block in the workflow so runs are not doubled.
