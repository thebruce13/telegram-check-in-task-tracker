# Telegram Check-In

A self-hosted Telegram bot that periodically asks whether you're still working
on your current task, and logs your answers to a Google Sheet. Runs in Docker
on your home server. Everything was vibe-coded even this readme, fight me.

## How it works

- Every `CHECKIN_INTERVAL_MINUTES` (default 30) the bot sends: *"Are you still
  working on **&lt;task&gt;**?"* with **Yes** / **No** buttons.
- **No response within `CHECKIN_INTERVAL_MINUTES - 1` minutes** (29 by
  default) → the prompt is auto-resolved as if you'd answered No → No: the
  task is paused, and the original message is edited to say so. This also
  means a second check-in can never stack on an unanswered one — the stale
  one always resolves first.
- **Yes** → just replies with a short confirmation and the loop continues on
  schedule. Nothing is written to the sheet — the task's row is already
  `Active`.
- **No** → asks *"Do you have anything else you want to track?"*
  - **Yes** → asks you to type the new task name. Once you send it, the bot
    logs `(timestamp, previous_task, "Inactive")` (the old task is done for
    good), then `(timestamp, new_task, "Active")`, makes the new task
    current, and restarts the 30-minute loop from now.
  - **No** → logs `(timestamp, task, "Paused")`, cancels all reminders, and
    stays silent until you send `/resume`, `/start`, or `/task <name>`.
- Running `/task <name>` while a different task is currently **active** first
  asks *"Are you done with **&lt;current task&gt;**?"* — **Yes** logs the old
  task `Inactive`, logs the new one `Active`, and restarts the loop; **No**
  cancels the switch and keeps the current task running. If the current task
  is merely paused (or nothing is set), `/task <name>` switches immediately
  with no confirmation, closing out the old one as `Inactive`.
- **`Paused` is resumable, `Inactive` is not.** A paused task picks back up
  as `Active` via `/resume`/`/start`. Once a task is logged `Inactive` (by
  switching tasks, or by the midnight sweep below), it's done — start a new
  one with `/task <name>`.
- **End-of-day sweep:** every night at midnight (in the `TZ` you configure),
  any task still sitting `Paused` gets automatically logged `Inactive` — a
  pause is meant to be resumed the same day, not carried over indefinitely.
  The same sweep then deletes every `Inactive` row whose `Duration` is
  exactly `0:00:00` — cleanup for tasks started and immediately
  switched/stopped with no real time logged against them. `Paused` rows are
  never deleted, even at `0:00:00`, since a paused task is still resumable.
- Every row logged to the sheet has one of three statuses: `Active`,
  `Paused`, or `Inactive`.
- Every row that closes out a task (`Paused` or `Inactive`) also gets a
  **`Duration`** column: total active time worked on the task across the
  session, formatted `H:MM:SS`, cumulative across any pause/resume cycles.
  `Active` rows leave it blank since the session is still ongoing. Time
  spent while paused doesn't count toward it.
- A **`Stopped At`** column is filled in with a full date+time only when a
  task is logged `Inactive` — i.e. genuinely done, not just paused — whether
  that's via `/stop`, switching tasks with `/task <name>`, or the midnight
  sweep. `Active` and `Paused` rows leave it blank.
- State (current task, active/paused/inactive, and the active-since
  timestamp) is persisted to disk, so a container restart resumes exactly
  where it left off, duration tracking included.
- Only the Telegram user ID you configure can interact with the bot — anyone
  else's messages are ignored.

## 0. Prerequisites

- A server (or any always-on machine) with **Docker** and the **Docker
  Compose plugin** installed (`docker compose version` should work).
- A **Telegram account** — you'll create a bot through Telegram itself, no
  separate signup needed.
- A **Google account** with access to
  [Google Cloud Console](https://console.cloud.google.com/) — free tier is
  fine, no billing needs to be enabled for the Sheets API.

## 1. Required credentials

You need two things before touching any code: a Telegram bot token, and a
Google service account that's allowed to write to a specific sheet.

### 1a. Create the Telegram bot and get your token

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather)
   — this is Telegram's official bot for creating other bots.
2. Send `/newbot`.
3. BotFather asks for a **display name** (shown in chats, can have spaces,
   e.g. `My Activity Tracker`).
4. Then it asks for a **username**, which must be unique and end in `bot`
   (e.g. `my_activity_tracker_bot`). If it's taken, try another.
5. BotFather replies with a message containing your bot's **API token** —
   a string like `123456789:AAExampleTokenFromBotFatherXXXXXXXXXXXXXXX`.
   Copy this; it's your `TELEGRAM_BOT_TOKEN`. Treat it like a password —
   anyone with it can control your bot.
6. Open a chat with your new bot (search its username in Telegram) and send
   it any message, e.g. `hi`. This is required so Telegram has a record of
   you talking to it, which the next step depends on.

### 1b. Find your Telegram user ID

The bot only responds to one specific person (you) — every other message it
receives is silently ignored. To set that up, it needs your numeric
Telegram user ID as `TELEGRAM_CHAT_ID`.

Easiest way: message [@userinfobot](https://t.me/userinfobot) — it replies
instantly with your numeric ID.

Alternative (no third-party bot): after messaging your own bot (step 1a.6),
visit this URL in a browser, with `<TOKEN>` replaced by your bot token:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Look for `"from":{"id":`**`123456789`**`,...}` in the JSON response — that
number is your `TELEGRAM_CHAT_ID`.

### 1c. Create a Google Sheet to log to

1. Go to [Google Sheets](https://sheets.google.com) and create a new blank
   spreadsheet (or reuse an existing one).
2. Give it any name you like — the bot doesn't care about the spreadsheet's
   name, only the sheet ID (next step) and the worksheet tab name.
3. Copy the **spreadsheet ID** out of the URL. It's the long string between
   `/d/` and `/edit`:

   ```
   https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit
   ```

   This is your `GOOGLE_SHEET_ID`.
4. Note the name of the tab (bottom-left) you want logged to — this is
   `GOOGLE_WORKSHEET_NAME` (defaults to `Sheet1`, Google Sheets' default tab
   name). You don't need to set up any headers yourself — the bot creates
   `Date | Time | Task | Stopped At | Duration | Activity` headers
   automatically the first time it writes to a brand-new tab (and will
   migrate an older layout in place if you're upgrading from a previous
   version of this bot).

### 1d. Create a Google service account and give it access to the sheet

The bot authenticates to Google Sheets as a service account, not as you —
this avoids ever putting your personal Google password anywhere near the
bot.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a new project (top-left project dropdown → **New Project**), or
   select an existing one you're happy to use.
2. Enable the Sheets API: in the top search bar, search **"Google Sheets
   API"**, open it, and click **Enable**.
3. Create the service account: **IAM & Admin → Service Accounts → Create
   Service Account**. Give it any name (e.g. `telcheck-bot`). You can skip
   the optional "grant access" and "grant users access" steps — click
   **Done**.
4. Open the service account you just created, go to the **Keys** tab, click
   **Add Key → Create new key**, choose **JSON**, and confirm. This
   downloads a `.json` file to your computer — this is your credentials
   file. **Keep it private**; it grants write access to anything you share
   with it.
5. Open that downloaded JSON file in a text editor and find the
   `"client_email"` field — it looks like
   `telcheck-bot@your-project-id.iam.gserviceaccount.com`.
6. Back in your Google Sheet, click **Share** (top-right), paste that email
   address in, set its permission to **Editor**, uncheck "notify people"
   (it's a service account, it won't read the email), and click **Share**.
   **This step is easy to miss and is the #1 cause of "the bot doesn't
   write anything" — the sheet must be explicitly shared with that exact
   email address.**
7. Move the downloaded JSON file into this project at
   `credentials/service_account.json` (created in the next section).

## 2. Project layout

```
TelCheck/
├── bot.py                  # Main application
├── sheets_client.py        # Google Sheets wrapper
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example            # Copy to .env and fill in
├── credentials/            # Put service_account.json here (mounted read-only)
└── data/                   # Bot's persisted state (created automatically)
```

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`GOOGLE_SHEET_ID`, and `TZ` (an IANA timezone like `America/New_York`,
used for the timestamps written to the sheet).

Place your downloaded service account key at:

```
credentials/service_account.json
```

## 4. Build and run

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

You should see a log line like:

```
telcheck | ... TelCheck starting (interval=30.0 min, tz=America/New_York)
```

## 5. Usage

- `/task <task name>` — set (or switch) the current task. If you're already
  actively tracking something else, first asks *"Are you done with
  &lt;current task&gt;?"*; answering **Yes** logs it `Inactive`, logs the new
  task `Active`, and (re)starts the 30-minute check-in loop counting from now;
  answering **No** cancels the switch. If nothing is currently active (or the
  current task is only paused), sets the new task directly with no
  confirmation, closing out whatever was current as `Inactive`.
- `/rename <new name>` — relabel the current task in place. Unlike `/task`,
  this doesn't close anything out or ask for confirmation: it's the same
  session, same row, same running duration — just a new name (useful for
  correcting a typo or clarifying what you're calling something mid-session).
- `/pause` — pause tracking without setting a new task. Logs the current task
  as `Paused` (with its duration so far) and cancels reminders. Resumable the
  same day via `/resume` or `/start`; if left paused past midnight, the
  end-of-day sweep logs it `Inactive` automatically. Equivalent to answering
  No → No to a check-in prompt.
- `/stop` — hard stop: ends tracking for good, not just a pause. Logs the
  current task `Inactive` (with its duration) and cancels reminders. There's
  no automatic way back — start something new with `/task <name>`.
- `/resume` (alias: `/start`) — if a task is currently `Paused`, logs it
  `Active` again (duration keeps accumulating from where it left off, not
  reset) and restarts the check-in loop; otherwise tells you to use `/task`.
- `/status` — shows the current task, whether it's active, paused, or
  inactive, when it started, and how long you've been on it so far
  (cumulative across any pause/resume cycles, still ticking while active).

From there, just respond to the periodic Yes/No prompts. Every task gets one
row in your Google Sheet that's updated in place as it changes state:
`Date | Time | Task | Stopped At | Duration | Activity` (`Activity` is
`Active`, `Paused`, or `Inactive`; `Duration` is cumulative `H:MM:SS`, filled
in on `Paused`/`Inactive` rows; `Stopped At` is filled in only on `Inactive`
rows). Column *position* is what the bot actually relies on, not the header
labels — relabeling a header is safe, but reordering columns in the sheet
requires a matching code change to the column letters in `sheets_client.py`.

## 6. Operational notes

- **State survives restarts**: `data/bot_persistence.pickle` holds per-chat
  state (current task, active/paused). Don't delete it if you want continuity
  across `docker compose restart` / host reboots.
- **Changing the interval**: edit `CHECKIN_INTERVAL_MINUTES` in `.env` and
  `docker compose up -d` again (this restarts the container). Existing loops
  pick up the new interval next time they're rescheduled (any `/task` call,
  or on container start via the automatic restore-on-startup logic).
- **Multiple chats**: the bot is scoped to `TELEGRAM_CHAT_ID`; every other
  chat's updates are ignored. If you want to track multiple people, you'd
  need to extend the allow-list logic in `bot.py` — not implemented here
  since this is designed as a personal tool.
- **Logs**: `docker compose logs -f telcheck`. Sheets write failures are
  logged and also reported to you in Telegram (they don't crash the bot).

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Bot never responds | Wrong `TELEGRAM_BOT_TOKEN`, or `TELEGRAM_CHAT_ID` doesn't match your actual user ID |
| `Failed to initialize Google Sheets client` at startup | Bad `GOOGLE_SHEET_ID`, missing/misnamed `credentials/service_account.json`, or Sheets API not enabled |
| Bot replies but Sheets rows never appear | Sheet not shared with the service account's `client_email` as Editor |
| State resets after restart | `data/` volume not mounted, or its contents were deleted |
| Old rows' Time column loses its year/format after upgrading from a pre-Date-column sheet | The one-time schema migration inserts columns via the Sheets API, which can reset a pre-existing column's custom number format as a side effect. The underlying values are untouched — only the display format needs restoring. Select the affected cells → Format → Number → Custom date and time, and re-add whatever pattern you want (e.g. `dddd, mmmm d, yyyy "at" h:mm:ss am/pm`). |
| Sheet rows land in the wrong columns after upgrading from a very old (3–4 column) sheet | The one-time schema migration builds the new columns in the order `Date, Time, Task, Stopped At, Duration, Activity`, but does so via simple column inserts, which historically produced `Date, Time, Task, Activity, Duration, Stopped At` instead (`Activity`/`Stopped At` swapped) for a from-scratch legacy migration. If your migrated sheet's columns don't match what the bot is writing, check `sheets_client.py`'s column-letter assumptions (`update_status`, `append_active`, `log_entry`) against your sheet's actual header row and adjust one or the other to match. |
| Reordering columns in the sheet (not just relabeling a header) breaks writes | The bot relies on column *position*, not header text — relabeling `Status` to `Activity` is safe, but dragging a column to a new position in the Sheets UI is not: the code still writes to the old letters. Update the column letters in `sheets_client.py` to match before using the bot again, and check the rows written between the reorder and the fix for anything that landed in the wrong cell. |
| `Invalid TZ=... - must be an IANA timezone name` at startup | Your `TZ` in `.env` isn't a valid [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (e.g. `America/Chicago`, `Europe/London`, `UTC`) — check for typos. If `TZ` is left unset entirely, the bot defaults to `UTC`. |
