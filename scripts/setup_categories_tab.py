"""(Re)create the 'Categories' tab: a live per-category time summary.

Not run by the bot itself - this is a one-off/rerunnable setup script for the
spreadsheet side of TelCheck. Run it whenever you want to (re)create the tab
(e.g. a fresh sheet, or after someone deletes it), via:

    docker compose run --rm -v $(pwd)/scripts/setup_categories_tab.py:/app/setup_categories_tab.py \
        telcheck python3 setup_categories_tab.py

It reads .env for GOOGLE_SHEET_ID etc. the same way the bot does. Writes
nothing to Sheet1 - the Categories tab is entirely formula-driven and reads
Sheet1 live, so it never needs to be rerun just because new rows/categories
get added; only rerun it if the tab itself gets deleted or you want to reset
its formatting.

How the categorization works: a task's category is whatever comes before the
first ":" in its name (e.g. "work: Web Team meeting" -> "Work"), normalized
with PROPER(TRIM(LOWER(...))) so "Work:", "work:", " Work :" etc. all group
together regardless of how it was typed. A task with no ":" at all falls
into "Uncategorized". There's no fixed category list anywhere - typing a new
prefix like "health: brush teeth" makes "Health" show up in this tab on its
own, no code change or redeploy needed.
"""

import os

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDS_FILE = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", "/app/credentials/service_account.json")
SOURCE_TAB = os.environ.get("GOOGLE_WORKSHEET_NAME", "Sheet1")
TAB_NAME = "Categories"
ROW_START, ROW_END = 2, 5000  # generous headroom past current data, cheap either way

creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
client = gspread.authorize(creds)
spreadsheet = client.open_by_key(SHEET_ID)

try:
    ws = spreadsheet.worksheet(TAB_NAME)
    print(f"'{TAB_NAME}' tab already exists (id {ws.id}) - resetting its contents.")
    ws.clear()
    ws.resize(rows=30, cols=6)
except gspread.WorksheetNotFound:
    ws = spreadsheet.add_worksheet(title=TAB_NAME, rows=30, cols=6)
    print(f"Created '{TAB_NAME}' tab (id {ws.id}).")

task_range = f"{SOURCE_TAB}!C{ROW_START}:C{ROW_END}"
duration_range = f"{SOURCE_TAB}!E{ROW_START}:E{ROW_END}"

category_col = (
    f'ARRAYFORMULA(IF({task_range}="","",'
    f'IFERROR(PROPER(TRIM(LOWER(LEFT({task_range},FIND(":",{task_range})-1)))),"Uncategorized")))'
)
duration_col = f"ARRAYFORMULA(IFERROR(N({duration_range}),0))"

summary_formula = (
    f"=QUERY({{{category_col},{duration_col}}},"
    "\"select Col1, sum(Col2) where Col1 <> '' group by Col1 order by Col1 "
    "label sum(Col2) 'Total Time', Col1 'Category'\",0)"
)
grand_total_formula = f"=SUM({SOURCE_TAB}!E{ROW_START}:E{ROW_END})"

ws.update(range_name="A1", values=[[summary_formula]], value_input_option="USER_ENTERED")
ws.update(
    range_name="D1:E1", values=[["Grand Total", grand_total_formula]], value_input_option="USER_ENTERED"
)

ws.format("A1:B1", {"textFormat": {"bold": True}})
ws.format("D1", {"textFormat": {"bold": True}})
# Pre-format ahead of the dynamic spill's current size so newly-appearing
# categories inherit the right display format without rerunning this script.
ws.format("B2:B30", {"numberFormat": {"type": "TIME", "pattern": "[h]:mm:ss"}})
ws.format("E1", {"numberFormat": {"type": "TIME", "pattern": "[h]:mm:ss"}})

computed = ws.get("A1:E10")
print("Computed values:")
for row in computed:
    print(" ", row)
errors = [r for r in computed if any(isinstance(c, str) and c.startswith("#") for c in r)]
if errors:
    raise SystemExit(f"Formula errors detected: {errors}")
print("OK - no formula errors.")
