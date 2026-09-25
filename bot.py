"""
TelCheck - a self-hosted Telegram activity-logging bot.

Every CHECKIN_INTERVAL_MINUTES it asks "Are you still working on <task>?".
Responses are logged to Google Sheets. See README.md for setup.
"""

import asyncio
import functools
import logging
import os
import re
import sys
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

from sheets_client import SheetsClient

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("telcheck")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        logger.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def _resolve_timezone(name: str, default: str = "UTC") -> ZoneInfo:
    value = os.environ.get(name, default)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        logger.error(
            "Invalid %s=%r - must be an IANA timezone name, e.g. America/Chicago, "
            "Europe/London, Asia/Tokyo, or UTC. Full list: "
            "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
            name,
            value,
        )
        sys.exit(1)


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(_require_env("TELEGRAM_CHAT_ID"))
GOOGLE_SHEET_ID = _require_env("GOOGLE_SHEET_ID")

CHECKIN_INTERVAL_MINUTES = float(os.environ.get("CHECKIN_INTERVAL_MINUTES", "30"))
# If a check-in prompt goes unanswered this long, auto-resolve it as "No" (pause).
# Kept a hair under the interval so a stale prompt always resolves before the next one fires.
CHECKIN_TIMEOUT_MINUTES = max(CHECKIN_INTERVAL_MINUTES - 1, 0.5)
TIMEZONE = _resolve_timezone("TZ")
PERSISTENCE_PATH = os.environ.get("PERSISTENCE_PATH", "/app/data/bot_persistence.pickle")
GOOGLE_CREDS_FILE = os.environ.get(
    "GOOGLE_SHEETS_CREDENTIALS_FILE", "/app/credentials/service_account.json"
)
GOOGLE_WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")

# Set once in main() before polling starts. Kept out of bot_data/chat_data since
# PicklePersistence replaces bot_data wholesale on startup and can't pickle a
# live gspread client anyway.
SHEETS: "SheetsClient | None" = None

# --------------------------------------------------------------------------
# Callback data / job naming
# --------------------------------------------------------------------------

CB_CHECKIN_YES = "checkin:yes"
CB_CHECKIN_NO = "checkin:no"
CB_TRACK_YES = "track:yes"
CB_TRACK_NO = "track:no"
CB_WAS_YES = "was:yes"
CB_WAS_NO = "was:no"

JOB_NAME_PREFIX = "checkin_"
TIMEOUT_JOB_NAME_PREFIX = "checkin_timeout_"


def job_name(chat_id: int) -> str:
    return f"{JOB_NAME_PREFIX}{chat_id}"


def checkin_timeout_job_name(chat_id: int) -> str:
    return f"{TIMEOUT_JOB_NAME_PREFIX}{chat_id}"


def format_timestamp(dt: datetime) -> str:
    # Plain "YYYY-MM-DD HH:MM:SS" (no offset) so Google Sheets parses it as a
    # real datetime value under USER_ENTERED, instead of storing it as text.
    # Used for the Stopped At column, which needs a full date+time.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def now_str() -> str:
    return format_timestamp(datetime.now(TIMEZONE))


def now_date_str() -> str:
    return format_date(datetime.now(TIMEZONE))


def now_time_str() -> str:
    return format_time(datetime.now(TIMEZONE))


def format_duration(total_seconds: float) -> str:
    return str(timedelta(seconds=int(total_seconds)))


# Requires a colon or an am/pm marker - a bare number like "5" is too likely
# to be the start of a task name (e.g. "/task 5 minute break") to treat as a time.
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", re.IGNORECASE)
_MERIDIEM_ONLY_RE = re.compile(r"^[ap]\.?m\.?$", re.IGNORECASE)


def parse_clock_time(token: str, tz: ZoneInfo) -> "datetime | None":
    """Parse a HH:MM[am/pm] time-of-day token as today's date in tz. None if it doesn't look like one."""
    match = _TIME_RE.match(token.strip())
    if not match:
        return None
    hour_str, minute_str, meridiem = match.groups()
    if minute_str is None and not meridiem:
        return None
    hour = int(hour_str)
    minute = int(minute_str or 0)
    meridiem = (meridiem or "").lower().replace(".", "")
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    if not 0 <= minute <= 59:
        return None
    return datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)


def extract_leading_time(args: list, tz: ZoneInfo) -> tuple:
    """Pull a leading time-of-day off a command's args (e.g. /task 1:32pm digging a hole).

    Handles both "1:32pm" as one token and "1:32 pm" split across two by the
    normal whitespace tokenizing. Returns (start_time_or_None, remaining_args).
    """
    if not args:
        return None, args
    # If the second token is a bare am/pm marker, it belongs with the first -
    # check that combination before treating the first token alone as 24-hour.
    if len(args) >= 2 and _MERIDIEM_ONLY_RE.match(args[1].strip()):
        start_time = parse_clock_time(args[0] + args[1], tz)
        if start_time is not None:
            return start_time, args[2:]
    start_time = parse_clock_time(args[0], tz)
    if start_time is not None:
        return start_time, args[1:]
    if len(args) >= 2:
        start_time = parse_clock_time(args[0] + args[1], tz)
        if start_time is not None:
            return start_time, args[2:]
    return None, args


def _accumulate_and_pop_active(chat_data: dict) -> float:
    """Fold any in-progress active stretch into accumulated_seconds; clear active_since."""
    active_since = chat_data.pop("active_since", None)
    accumulated = chat_data.get("accumulated_seconds", 0.0)
    if active_since:
        accumulated += (datetime.now(TIMEZONE) - active_since).total_seconds()
    chat_data["accumulated_seconds"] = accumulated
    return chat_data["accumulated_seconds"]


def _current_elapsed_seconds(chat_data: dict) -> float:
    """Read-only: total active time accrued so far, including any in-progress stretch."""
    accumulated = chat_data.get("accumulated_seconds", 0.0)
    active_since = chat_data.get("active_since")
    if active_since:
        accumulated += (datetime.now(TIMEZONE) - active_since).total_seconds()
    return accumulated


def restricted(func):
    """Ignore updates from anyone but the configured user (token leaks happen)."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if user is None or user.id != ALLOWED_USER_ID:
            logger.warning("Ignoring update from unauthorized user %s", user.id if user else None)
            return
        return await func(update, context, *a, **kw)

    return wrapper


# --------------------------------------------------------------------------
# Job scheduling helpers
# --------------------------------------------------------------------------


def _clear_pending_checkin(application: Application, chat_id: int) -> None:
    """Cancel any outstanding check-in timeout job and forget its pending state."""
    for job in application.job_queue.get_jobs_by_name(checkin_timeout_job_name(chat_id)):
        job.schedule_removal()
    chat_data = application.chat_data.get(chat_id)
    if chat_data is not None:
        chat_data["awaiting_checkin_response"] = False
        chat_data.pop("checkin_message_id", None)


def schedule_checkin_loop(application: Application, chat_id: int) -> None:
    """(Re)start the check-in loop for chat_id, cancelling any existing job first."""
    for job in application.job_queue.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    _clear_pending_checkin(application, chat_id)
    interval_seconds = CHECKIN_INTERVAL_MINUTES * 60
    application.job_queue.run_repeating(
        send_checkin_prompt,
        interval=interval_seconds,
        first=interval_seconds,
        chat_id=chat_id,
        name=job_name(chat_id),
    )
    logger.info(
        "Scheduled check-in loop for chat %s every %.1f minutes", chat_id, CHECKIN_INTERVAL_MINUTES
    )


def cancel_checkin_loop(application: Application, chat_id: int) -> None:
    for job in application.job_queue.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    _clear_pending_checkin(application, chat_id)
    logger.info("Cancelled check-in loop for chat %s", chat_id)


async def _notify_sheet_error(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task: str, status: str) -> None:
    logger.exception("Failed to write to Google Sheets (task=%r, status=%r)", task, status)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Couldn't write that entry to Google Sheets. Check the container logs.",
        )
    except Exception:
        logger.exception("Also failed to notify the user about the Sheets error")


async def log_to_sheet(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    task: str,
    status: str,
    stopped_at: str = "",
) -> None:
    """Fallback: append a standalone row. Used only when there's no session row to edit."""
    try:
        await asyncio.to_thread(
            SHEETS.log_entry, task, status, now_date_str(), now_time_str(), stopped_at
        )
    except Exception:
        await _notify_sheet_error(context, chat_id, task, status)


async def update_session_status(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, task: str, row: "int | None", status: str
) -> None:
    # Stamped whenever the row stops accumulating time - Paused (still resumable)
    # as well as Inactive (done for good). Active leaves it blank (still ongoing).
    stopped_at = now_str() if status in ("Inactive", "Paused") else ""
    if row is not None:
        try:
            await asyncio.to_thread(SHEETS.update_status, row, status, stopped_at)
            return
        except Exception:
            await _notify_sheet_error(context, chat_id, task, status)
            return
    # No row reference (e.g. a previous append failed) - don't lose the event.
    await log_to_sheet(context, chat_id, task, status, stopped_at)


async def start_session(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_name: str, start_time: "datetime | None" = None
) -> None:
    """Append a new Active row for task_name and initialize its session tracking.

    start_time backdates the row's Date/Time and the active-since clock (e.g.
    from /task 1:32pm <name>); defaults to now. The check-in loop itself
    always counts CHECKIN_INTERVAL_MINUTES from now, regardless.
    """
    chat_data = context.chat_data
    start_time = start_time or datetime.now(TIMEZONE)
    chat_data["current_task"] = task_name
    chat_data["status"] = "active"
    chat_data["accumulated_seconds"] = 0.0
    chat_data["active_since"] = start_time
    chat_data["session_started_at"] = start_time
    chat_data.pop("awaiting_new_task_name", None)
    try:
        row = await asyncio.to_thread(
            SHEETS.append_active, task_name, format_date(start_time), format_time(start_time)
        )
    except Exception:
        row = None
        await _notify_sheet_error(context, chat_id, task_name, "Active")
    chat_data["session_row"] = row
    schedule_checkin_loop(context.application, chat_id)


async def close_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int, final_status: str) -> None:
    """Fold in any in-progress active time and set the session row to its final status."""
    chat_data = context.chat_data
    task_name = chat_data.get("current_task")
    _accumulate_and_pop_active(chat_data)
    row = chat_data.pop("session_row", None)
    chat_data.pop("accumulated_seconds", None)
    chat_data["last_stopped_at"] = datetime.now(TIMEZONE)
    await update_session_status(context, chat_id, task_name, row, final_status)


async def pause_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Set the session row to Paused; stays resumable. Sheet Duration keeps running (wall-clock)."""
    chat_data = context.chat_data
    task_name = chat_data.get("current_task")
    _accumulate_and_pop_active(chat_data)
    row = chat_data.get("session_row")
    chat_data["last_stopped_at"] = datetime.now(TIMEZONE)
    await update_session_status(context, chat_id, task_name, row, "Paused")


async def resume_session(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Flip the session row back to Active (clears Stopped At) and restart the active clock."""
    chat_data = context.chat_data
    task_name = chat_data.get("current_task")
    chat_data["active_since"] = datetime.now(TIMEZONE)
    row = chat_data.get("session_row")
    await update_session_status(context, chat_id, task_name, row, "Active")


async def set_new_task(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_name: str, start_time: "datetime | None" = None
) -> None:
    previous_task = context.chat_data.get("current_task")
    if previous_task and previous_task != task_name:
        await close_session(context, chat_id, "Inactive")
    await start_session(context, chat_id, task_name, start_time)


# --------------------------------------------------------------------------
# Job callback: the periodic prompt
# --------------------------------------------------------------------------


async def resolve_unanswered_checkin_as_no(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """No response to the last check-in - treat it like an unattended No -> No: pause."""
    chat_data = context.chat_data
    if not chat_data.get("awaiting_checkin_response"):
        return
    message_id = chat_data.get("checkin_message_id")
    task = chat_data.get("current_task")

    cancel_checkin_loop(context.application, chat_id)  # also clears the pending-checkin state
    await pause_session(context, chat_id)
    chat_data["status"] = "paused"

    text = (
        f"⏰ Didn't hear back, so I've paused tracking *{task}*. Send /resume or "
        "/task <name> when you're ready."
    )
    edited = False
    if message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
            edited = True
        except Exception:
            logger.exception("Could not edit the stale check-in prompt for chat %s", chat_id)
    if not edited:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def checkin_timeout_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    await resolve_unanswered_checkin_as_no(context, context.job.chat_id)


async def send_checkin_prompt(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    task = context.chat_data.get("current_task")
    if not task:
        # Shouldn't happen, but don't ask about nothing.
        cancel_checkin_loop(context.application, chat_id)
        return

    if context.chat_data.get("awaiting_checkin_response"):
        # A prior prompt was never answered - resolve it as No instead of stacking a new one.
        await resolve_unanswered_checkin_as_no(context, chat_id)
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes", callback_data=CB_CHECKIN_YES),
                InlineKeyboardButton("❌ No", callback_data=CB_CHECKIN_NO),
            ]
        ]
    )
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Are you still working on *{task}*?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )
    context.chat_data["awaiting_checkin_response"] = True
    context.chat_data["checkin_message_id"] = message.message_id

    for job in context.application.job_queue.get_jobs_by_name(checkin_timeout_job_name(chat_id)):
        job.schedule_removal()
    context.application.job_queue.run_once(
        checkin_timeout_callback,
        when=timedelta(minutes=CHECKIN_TIMEOUT_MINUTES),
        chat_id=chat_id,
        name=checkin_timeout_job_name(chat_id),
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


@restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    task = context.chat_data.get("current_task")
    status = context.chat_data.get("status")

    if task and status == "paused":
        await resume_session(context, chat_id)
        context.chat_data["status"] = "active"
        schedule_checkin_loop(context.application, chat_id)
        await update.message.reply_text(
            f"Resumed tracking *{task}*. I'll check in every {CHECKIN_INTERVAL_MINUTES:g} minutes.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif task and status == "active":
        await update.message.reply_text(
            f"Already tracking *{task}*. Use /task <name> to switch tasks.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "Nothing paused to resume. Use /task <task name> to start tracking."
        )


def _now_tracking_text(task_name: str, start_time: "datetime | None") -> str:
    when = f" (started at {start_time.strftime('%I:%M %p')})" if start_time else ""
    return f"Now tracking *{task_name}*{when}. I'll check in every {CHECKIN_INTERVAL_MINUTES:g} minutes."


@restricted
async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /task [HH:MMam/pm] <task name>")
        return

    start_time, remaining_args = extract_leading_time(context.args, TIMEZONE)
    if start_time is not None:
        if not remaining_args:
            await update.message.reply_text("Usage: /task [HH:MMam/pm] <task name>")
            return
        now = datetime.now(TIMEZONE)
        if start_time > now:
            await update.message.reply_text(
                f"{start_time.strftime('%I:%M %p')} is in the future — /task can only "
                "backdate to a time earlier today."
            )
            return
        last_stopped_at = context.chat_data.get("last_stopped_at")
        if last_stopped_at and start_time < last_stopped_at:
            await update.message.reply_text(
                f"That's earlier than when your last task stopped "
                f"({last_stopped_at.strftime('%I:%M %p')}) — it would overlap. Use a later "
                "time, or /was to backfill the gap instead."
            )
            return

    new_task_name = " ".join(remaining_args if start_time is not None else context.args).strip()

    await set_new_task(context, chat_id, new_task_name, start_time)
    await update.message.reply_text(_now_tracking_text(new_task_name, start_time), parse_mode=ParseMode.MARKDOWN)


@restricted
async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /rename <new task name>")
        return
    new_name = " ".join(context.args).strip()
    old_name = context.chat_data.get("current_task")
    status = context.chat_data.get("status")

    if not old_name or status == "inactive":
        await update.message.reply_text("Nothing is currently being tracked to rename.")
        return
    if new_name == old_name:
        await update.message.reply_text(f"Already tracking *{new_name}*.", parse_mode=ParseMode.MARKDOWN)
        return

    context.chat_data["current_task"] = new_name
    row = context.chat_data.get("session_row")
    if row is not None:
        try:
            await asyncio.to_thread(SHEETS.update_task_name, row, new_name)
        except Exception:
            await _notify_sheet_error(context, chat_id, new_name, status)
    await update.message.reply_text(
        f"Renamed *{old_name}* to *{new_name}*. Same session, same duration so far.",
        parse_mode=ParseMode.MARKDOWN,
    )


@restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    task = context.chat_data.get("current_task")
    if not task or context.chat_data.get("status") != "active":
        await update.message.reply_text("Nothing is currently active to pause.")
        return

    cancel_checkin_loop(context.application, chat_id)
    await pause_session(context, chat_id)
    context.chat_data["status"] = "paused"
    await update.message.reply_text(
        f"Paused tracking *{task}*. Send /resume to continue it, or /task <name> to "
        "switch to something else.",
        parse_mode=ParseMode.MARKDOWN,
    )


@restricted
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    task = context.chat_data.get("current_task")
    status = context.chat_data.get("status")
    if not task or status == "inactive":
        await update.message.reply_text("Nothing is currently being tracked.")
        return

    cancel_checkin_loop(context.application, chat_id)
    await close_session(context, chat_id, "Inactive")
    context.chat_data["status"] = "inactive"
    await update.message.reply_text(
        f"Stopped tracking *{task}* for good. Send /task <name> to start something new.",
        parse_mode=ParseMode.MARKDOWN,
    )


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_data = context.chat_data
    task = chat_data.get("current_task")
    status = chat_data.get("status", "inactive")
    if not task:
        await update.message.reply_text("No task set. Use /task <name> to start.")
        return

    lines = [f"Current task: *{task}*", f"Status: {status}"]

    # Older sessions (started before this field existed) fall back to active_since,
    # which is exact as long as the session hasn't been paused/resumed since.
    started_at = chat_data.get("session_started_at") or chat_data.get("active_since")
    if started_at:
        lines.append(f"Started: {started_at.strftime('%a %b %d, %I:%M %p')}")

    if status in ("active", "paused"):
        elapsed = format_duration(_current_elapsed_seconds(chat_data))
        suffix = " (still counting)" if status == "active" else " (paused)"
        lines.append(f"Time on task: {elapsed}{suffix}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@restricted
async def cmd_was(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /was <task name>")
        return
    task_name = " ".join(context.args).strip()

    if context.chat_data.get("status") == "active":
        await update.message.reply_text(
            "You're actively tracking something right now, so there's no gap to backfill. "
            "Use /pause or /stop first, then /was."
        )
        return

    now = datetime.now(TIMEZONE)
    cap_start = now - timedelta(minutes=CHECKIN_INTERVAL_MINUTES)
    last_stopped_at = context.chat_data.get("last_stopped_at")
    start = max(cap_start, last_stopped_at) if last_stopped_at else cap_start

    if start >= now:
        await update.message.reply_text("No time gap to backfill — you're all caught up.")
        return

    duration = format_duration((now - start).total_seconds())
    context.chat_data["pending_was"] = {"task": task_name, "start": start, "end": now, "duration": duration}

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data=CB_WAS_YES), InlineKeyboardButton("No", callback_data=CB_WAS_NO)]]
    )
    await update.message.reply_text(
        f"Log *{task_name}* from {start.strftime('%a %I:%M %p')} to "
        f"{now.strftime('%a %I:%M %p')} ({duration})?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )


# --------------------------------------------------------------------------
# Callback query handlers
# --------------------------------------------------------------------------


@restricted
async def on_checkin_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    task = context.chat_data.get("current_task")

    context.chat_data["awaiting_checkin_response"] = False
    context.chat_data.pop("checkin_message_id", None)
    for job in context.application.job_queue.get_jobs_by_name(checkin_timeout_job_name(chat_id)):
        job.schedule_removal()

    if query.data == CB_CHECKIN_YES:
        await query.edit_message_text(f"Got it! Staying on *{task}*.", parse_mode=ParseMode.MARKDOWN)
    elif query.data == CB_CHECKIN_NO:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes", callback_data=CB_TRACK_YES),
                    InlineKeyboardButton("No", callback_data=CB_TRACK_NO),
                ]
            ]
        )
        await query.edit_message_text(
            "Do you have anything else you want to track?", reply_markup=keyboard
        )


@restricted
async def on_track_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == CB_TRACK_YES:
        context.chat_data["awaiting_new_task_name"] = True
        await query.edit_message_text("What's the name of the new task? Just type it below.")
    elif query.data == CB_TRACK_NO:
        cancel_checkin_loop(context.application, chat_id)
        await pause_session(context, chat_id)
        context.chat_data["status"] = "paused"
        await query.edit_message_text(
            "Okay, pausing all check-ins. Send /resume (or /task <task name>) whenever "
            "you want to continue."
        )


@restricted
async def on_was_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    pending = context.chat_data.pop("pending_was", None)

    if query.data == CB_WAS_YES:
        if not pending:
            await query.edit_message_text("Something went wrong — please run /was again.")
            return
        task_name, start, end, duration = pending["task"], pending["start"], pending["end"], pending["duration"]
        try:
            await asyncio.to_thread(
                SHEETS.log_entry,
                task_name,
                "Inactive",
                format_date(start),
                format_time(start),
                format_timestamp(end),
            )
        except Exception:
            await _notify_sheet_error(context, chat_id, task_name, "Inactive")
            return
        context.chat_data["last_stopped_at"] = end
        await query.edit_message_text(
            f"Logged *{task_name}* from {start.strftime('%a %I:%M %p')} to "
            f"{end.strftime('%a %I:%M %p')} ({duration}).",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text("Okay, discarded.")


@restricted
async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if context.chat_data.get("awaiting_new_task_name"):
        task_name = update.message.text.strip()
        await set_new_task(context, chat_id, task_name)
        await update.message.reply_text(
            f"Switched to tracking *{task_name}*. Check-ins will resume every "
            f"{CHECKIN_INTERVAL_MINUTES:g} minutes.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "I didn't understand that. Use /task <task name> to set a task, or /status "
            "to check current status."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update %r", update, exc_info=context.error)


# --------------------------------------------------------------------------
# Startup recovery
# --------------------------------------------------------------------------


async def restore_jobs_on_startup(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    for chat_id, data in application.chat_data.items():
        if data.get("status") == "active" and data.get("current_task"):
            schedule_checkin_loop(application, chat_id)
            logger.info(
                "Restored check-in loop for chat %s (task=%r)", chat_id, data.get("current_task")
            )


async def end_of_day_sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any task still Paused at midnight is done for the day - close its row out in place."""
    application = context.application
    for chat_id, data in application.chat_data.items():
        if data.get("status") == "paused" and data.get("current_task"):
            task = data.get("current_task")
            row = data.pop("session_row", None)
            data.pop("accumulated_seconds", None)
            data["status"] = "inactive"
            await update_session_status(context, chat_id, task, row, "Inactive")
            logger.info("End-of-day sweep: marked %r inactive for chat %s", task, chat_id)

    try:
        deleted = await asyncio.to_thread(SHEETS.delete_zero_duration_rows)
        if deleted:
            logger.info("End-of-day sweep: removed %d zero-duration Inactive row(s)", deleted)
    except Exception:
        logger.exception("End-of-day sweep failed to remove zero-duration rows")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    global SHEETS
    try:
        SHEETS = SheetsClient(GOOGLE_CREDS_FILE, GOOGLE_SHEET_ID, GOOGLE_WORKSHEET_NAME)
    except Exception:
        logger.exception("Failed to initialize Google Sheets client - check credentials/sheet ID")
        sys.exit(1)

    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    application = ApplicationBuilder().token(BOT_TOKEN).persistence(persistence).build()

    application.add_handler(CommandHandler("start", cmd_resume))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("task", cmd_task))
    application.add_handler(CommandHandler("rename", cmd_rename))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("was", cmd_was))
    application.add_handler(
        CallbackQueryHandler(on_checkin_response, pattern=f"^(?:{CB_CHECKIN_YES}|{CB_CHECKIN_NO})$")
    )
    application.add_handler(
        CallbackQueryHandler(on_track_response, pattern=f"^(?:{CB_TRACK_YES}|{CB_TRACK_NO})$")
    )
    application.add_handler(
        CallbackQueryHandler(on_was_response, pattern=f"^(?:{CB_WAS_YES}|{CB_WAS_NO})$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    application.add_error_handler(on_error)

    application.job_queue.run_once(restore_jobs_on_startup, when=0)
    application.job_queue.run_daily(
        end_of_day_sweep,
        time=dt_time(hour=0, minute=0, second=0, tzinfo=TIMEZONE),
        name="end_of_day_sweep",
    )

    logger.info("TelCheck starting (interval=%.1f min, tz=%s)", CHECKIN_INTERVAL_MINUTES, TIMEZONE)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
