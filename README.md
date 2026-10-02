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
