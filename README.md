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
- **In Progress**: tabs *In Progress* (with the BO team), *Pending Customer Response*,
  *Pending Other Teams* and *Productivity* (tickets moved to a Done status per person per day).
  The hours columns are live formulas, so they keep counting between runs.

## Automatic refresh (every 10 minutes)

`.github/workflows/sync.yml` runs `bo_sheets.py` every 10 minutes. It must be on the repo's default
branch, and the repo needs two secrets (Settings → Secrets and variables → Actions):
`JIRA_API_TOKEN` and `GOOGLE_SERVICE_ACCOUNT_JSON` (the full contents of the key file).
