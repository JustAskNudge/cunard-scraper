# Cunard Daily Programme Reminders

Automatically scrape the Cunard My Voyage daily programme PDF and schedule event reminders.

## Overview

This skill logs into the Cunard My Voyage portal, downloads the daily programme PDF, extracts events, and schedules Telegram reminders 15 minutes before each event.

## Installation

```bash
cd /Users/nudge/clawd/skills/cunard-reminders
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Copy `config.json.example` to `config.json` and fill in your details:

```json
{
  "cunard_card_number": "269501",
  "cunard_first_name": "Paul",
  "cunard_last_name": "Kingham",
  "cunard_dob_day": "15",
  "cunard_dob_month": "10",
  "cunard_dob_year": "1971",
  "telegram_bot_token": "YOUR_BOT_TOKEN",
  "telegram_chat_id": "YOUR_CHAT_ID"
}
```

## Usage

### Manual run
```bash
python3 cunard_scraper.py --config config.json
```

### Daily cron job
```bash
# Run every morning at 7 AM
0 7 * * * cd /Users/nudge/clawd/skills/cunard-reminders && python3 cunard_scraper.py --config config.json
```

## How it works

1. **Login**: Uses Playwright to automate login to myvoyage.cunard.com
2. **Session persistence**: Saves browser storage state to avoid re-logging in
3. **PDF extraction**: Navigates to pdfviewer page and extracts PDF URL
4. **Event parsing**: Uses PyPDF2 to extract text and parse event times/titles
5. **Reminder scheduling**: Creates OpenClaw cron jobs for 15-min-before reminders

## Troubleshooting

### Login issues
If automated login fails, the script will open a visible browser for manual login. Complete the login and press ENTER to save the session.

### PDF not found
Check `downloads/debug_screenshot.png` to see what the page looks like.

### Session expired
Delete `downloads/storage_state.json` to force re-login.

## Files

- `cunard_scraper.py` - Main scraper script
- `pdf_parser.py` - PDF event extraction
- `reminder_scheduler.py` - OpenClaw cron integration
- `config.json` - Your credentials (gitignored)
- `downloads/` - Downloaded PDFs and debug files
