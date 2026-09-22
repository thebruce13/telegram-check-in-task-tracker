"""Thin wrapper around gspread for logging one row per task session.

A session's row is appended when the task goes Active, then edited in place
(status + duration + stopped-at) as it moves through Paused/Active/Inactive,
rather than appending a new row for every status change.

Columns: Date | Time | Task | Stopped At | Duration | Activity (Status)
"""

import logging
import re
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("telcheck.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Date", "Time", "Task", "Stopped At", "Duration", "Activity"]


class SheetsClient:
    def __init__(self, credentials_file: str, sheet_id: str, worksheet_name: str = "Sheet1"):
        creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheet_id)

        try:
            self.worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            logger.info("Worksheet %s not found, creating it", worksheet_name)
            self.worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=6)
            self.worksheet.append_row(HEADER)
            return

        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Bring an older sheet layout up to Date/Time/Task/Status/Duration/Stopped At.

        Older layouts this handles: 3 cols (Timestamp,Task,Status), 4 cols
        (+Duration), or already-6-col (nothing to do). Existing historical rows
        keep their combined timestamp in the Time column - there's no way to
        retroactively split out a Date for them, so that cell is just left blank.
        """
        header = self.worksheet.row_values(1)
        if len(header) >= 6 and header[0] == "Date":
            return  # already migrated

        logger.info("Migrating sheet schema to Date/Time/Task/Status/Duration/Stopped At")

        # Ensure a Duration column exists at position 4 first, so the layout is a
        # known shape (Timestamp,Task,Status,Duration) before shifting anything.
        if len(header) < 4 or header[3] != "Duration":
            self.worksheet.update_cell(1, 4, "Duration")

        # Insert Date at the front; the old Timestamp column shifts right to become Time.
        self.worksheet.insert_cols([["Date"]], col=1, value_input_option="USER_ENTERED")

        # Duration is now one column further right (was col 4, now col 5).
        if self.worksheet.cell(1, 5).value != "Duration":
            self.worksheet.update_cell(1, 5, "Duration")

        # Append Stopped At as a new column at the right edge.
        self.worksheet.insert_cols(
            [["Stopped At"]], col=6, value_input_option="USER_ENTERED", inherit_from_before=True
        )

    def append_active(self, task: str, date_str: str, time_str: str) -> Optional[int]:
        """Start a new session row (Active, blank duration/stopped-at). Returns its row number."""
        response = self.worksheet.append_row(
            [date_str, time_str, task or "", "", "", "Active"],
            value_input_option="USER_ENTERED",
            table_range="A:F",
        )
        return self._row_from_append_response(response)

    def update_status(self, row: int, status: str, duration: str = "", stopped_at: str = "") -> None:
        """Edit an existing session row's Stopped At/Duration/Activity cells (D:F) in place."""
        self.worksheet.update(
            f"D{row}:F{row}", [[stopped_at, duration, status]], value_input_option="USER_ENTERED"
        )

    def update_task_name(self, row: int, task_name: str) -> None:
        """Relabel an existing session row's Task cell (column C) in place."""
        self.worksheet.update(f"C{row}", [[task_name]], value_input_option="USER_ENTERED")

    def log_entry(
        self,
        task: str,
        status: str,
        date_str: str,
        time_str: str,
        duration: str = "",
        stopped_at: str = "",
    ) -> None:
        """Fallback: append a standalone row when there's no session row to edit."""
        self.worksheet.append_row(
            [date_str, time_str, task or "", stopped_at, duration, status],
            value_input_option="USER_ENTERED",
            table_range="A:F",
        )

    def delete_zero_duration_rows(self) -> int:
        """Delete every Inactive row with a Duration of exactly 0:00:00.

        Scoped to Inactive only: a Paused row can be zero-duration too, but it's
        still the live session_row for a chat (kept around for /resume) - deleting
        it would leave that chat's row reference pointing at the wrong row.
        Inactive rows are never referenced after the fact, so they're safe to
        remove outright. Returns the number of rows deleted.
        """
        all_values = self.worksheet.get_all_values()
        rows_to_delete = [
            row_num
            for row_num, row in enumerate(all_values[1:], start=2)
            if len(row) >= 6 and row[5] == "Inactive" and row[4] == "0:00:00"
        ]
        for row_num in sorted(rows_to_delete, reverse=True):
            self.worksheet.delete_rows(row_num)
        return len(rows_to_delete)

    @staticmethod
    def _row_from_append_response(response: dict) -> Optional[int]:
        try:
            updated_range = response["updates"]["updatedRange"]  # e.g. "Sheet1!A12:F12"
            match = re.search(r"![A-Za-z]+(\d+)", updated_range)
            return int(match.group(1)) if match else None
        except Exception:
            logger.exception("Could not parse row number from append response: %r", response)
            return None
