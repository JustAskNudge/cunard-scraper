# Cunard Daily Programme Reminders

Automatically scrape the Cunard My Voyage daily programme PDF and schedule event reminders.

## Overview

This skill logs into the Cunard My Voyage portal, downloads the daily programme PDF, extracts events, and schedules iCloud Reminders 15 minutes before each event.

## Architecture

```
MacBook Air (on ship)
├── cunard_scraper.py → Downloads PDF from myvoyage.cunard.com
└── process_pdf.py → Parses PDF, creates iCloud Reminders
```

## Installation (MacBook Air)

```bash
cd ~/ship-scraper-v2
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Create `config.json`:

```json
{
  "cunard_card_number": "YOUR_CARD_NUMBER",
  "cunard_first_name": "Paul",
  "cunard_last_name": "Kingham",
  "cunard_dob_day": "15",
  "cunard_dob_month": "10",
  "cunard_dob_year": "1971"
}
```

## Usage

### One-click run
```bash
./get-reminders.sh
```

This will:
1. Scrape the latest daily programme PDF
2. Parse events matching your interests
3. Create iCloud Reminders with 15-minute alerts

### Manual steps
```bash
# Download PDF
python3 cunard_scraper.py

# Process and create reminders
python3 process_pdf.py downloads/daily-programme-YYYY-MM-DD.pdf
```

## Event Categories

Reminders are created for:
- **Bingo** 🎱
- **Cunard Insights** (lectures, captain's talks)
- **Planetarium** shows
- **Theatre** performances
- **Trivia** sessions
- **Movies**

Excluded: Line dancing, EHNY, Designer Showcase

## How It Works

1. **Login**: Uses Playwright to automate login to myvoyage.cunard.com
2. **Session persistence**: Saves browser storage state to avoid re-logging in
3. **PDF extraction**: Navigates to pdfviewer page and extracts PDF URL
4. **Event parsing**: Uses PyPDF2 to extract text and parse event times/titles
5. **Reminder creation**: Creates iCloud Reminders via AppleScript with explicit date components (avoids year/timezone bugs)

## Troubleshooting

### Login issues
If automated login fails, the script opens a visible browser for manual login. Complete the login and press ENTER to save the session.

### Wrong year (2014 instead of 2026)
Fixed in current version. Uses explicit date components (year, month, day, hour, minute) instead of string date parsing.

### GMT timezone showing in reminders
Fixed by running script on MBA (ship time) instead of remote Mac. Both MBA and iPhone are in ship timezone, so no conversion needed.

### Session expired
Delete `downloads/browser_state.json` to force re-login.

## Files

- `cunard_scraper.py` - Main scraper script (downloads PDF)
- `process_pdf.py` - PDF parser + reminder creator
- `get-reminders.sh` - One-click wrapper script
- `config.json` - Your credentials (gitignored)
- `downloads/` - Downloaded PDFs and debug files

## Last Updated

2026-03-09 - Fixed year bug (2014→2026) and timezone display issues
