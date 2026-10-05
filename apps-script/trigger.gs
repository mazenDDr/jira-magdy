/**
 * Starts the GitHub "Sync Jira to Google Sheets" workflow.
 *
 * Why: GitHub's own cron is best-effort and silently skips runs. A Google time trigger is reliable.
 * Setup is in the README ("Reliable refresh"). The GitHub token is kept in Script Properties
 * (key GITHUB_TOKEN), never in this file.
 *
 * Day (09:00-22:59 Cairo): runs on every trigger, i.e. every 30 minutes.
 * Night (23:00-08:59 Cairo): runs every 2 hours (23:00, 01:00, 03:00, 05:00, 07:00). Google handles daylight saving.
 */
const REPO = 'mazenDDr/jira-magdy';
const WORKFLOW = 'sync.yml';
const BRANCH = 'main';
const TIME_ZONE = 'Africa/Cairo';

function triggerSync() {
  const now = new Date();
  const hour = Number(Utilities.formatDate(now, TIME_ZONE, 'H'));
  const minute = Number(Utilities.formatDate(now, TIME_ZONE, 'm'));
  const night = hour >= 23 || hour < 9;
  // overnight, only the first trigger of every second hour (odd hours: 23, 1, 3, 5, 7)
  if (night && (hour % 2 === 0 || minute >= 30)) return;

  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add GITHUB_TOKEN under Project Settings > Script properties');

  const response = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPO + '/actions/workflows/' + WORKFLOW + '/dispatches',
    {
      method: 'post',
      contentType: 'application/json',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      payload: JSON.stringify({ ref: BRANCH }),
      muteHttpExceptions: true,
    });
  if (response.getResponseCode() !== 204) {
    throw new Error('GitHub answered ' + response.getResponseCode() + ': ' + response.getContentText());
  }
}
