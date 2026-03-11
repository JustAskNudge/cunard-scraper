# Cunard Daily Programme Scraper

A Python-based scraper that logs into the Cunard My Voyage website, downloads the daily programme PDF, extracts events, and schedules Telegram reminders 15 minutes before each event starts.

## Features

- **Automated login** with fallback to manual login if automated fails
- **PDF detection** via multiple methods (network capture, JavaScript probing, API endpoints)
- **Event extraction** from PDF with categorization (Gala, Bingo, Theatre, Planetarium)
- **Smart reminders** that only schedule future events (past events are filtered out)
- **Local timezone support** — all times use the machine's local timezone
- **One-time cron jobs** — reminders fire once on the correct date, not daily

## Requirements

- Python 3.9+
- Playwright (browser automation)
- PyPDF2 (PDF text extraction)
- aiohttp (async HTTP requests)
- OpenClaw (for cron scheduling and Telegram messaging)

## Installation

```bash
# Clone the repository
git clone https://github.com/JustAskNudge/cunard-scraper.git
cd cunard-scraper

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

## Configuration

Create a `config.json` file or set environment variables:

```json
{
  "cunard_card_number": "YOUR_CARD_NUMBER",
  "cunard_first_name": "YOUR_FIRST_NAME",
  "cunard_last_name": "YOUR_LAST_NAME",
  "cunard_dob_day": "01",
  "cunard_dob_month": "01",
  "cunard_dob_year": "1980",
  "telegram_bot_token": "YOUR_BOT_TOKEN",
  "telegram_chat_id": "YOUR_CHAT_ID",
  "headless": false
}
```

Or use environment variables:

```bash
export CUNARD_CARD_NUMBER="..."
export CUNARD_FIRST_NAME="..."
export CUNARD_LAST_NAME="..."
export CUNARD_DOB_DAY="01"
export CUNARD_DOB_MONTH="01"
export CUNARD_DOB_YEAR="1980"
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## Usage

```bash
# Run with default config.json
python cunard_scraper.py

# Run with custom config
python cunard_scraper.py --config /path/to/config.json
```

## How It Works

1. **Login**: Opens a browser window and attempts automated login. If that fails, prompts for manual login.
2. **Navigation**: Navigates to the Daily Programme page, handling modals and sidebars.
3. **PDF Detection**: Captures the PDF URL via network monitoring, JavaScript probing, or API endpoint construction.
4. **Download**: Downloads the PDF using the authenticated browser context.
5. **Extraction**: Parses the PDF text to find events with times (e.g., "19:30 Gala Dinner").
6. **Scheduling**: For each future event, schedules a cron job to send a Telegram reminder 15 minutes before the event starts.

## Reminder Format

Reminders are sent via Telegram with emoji indicators:
- ⭐ Gala events
- 🎱 Bingo
- 🎭 Theatre
- 🚢 Everything else

Example:
```
⭐ Starting in 15 mins: Gala Dinner
🕐 19:30
```

## File Structure

```
cunard-scraper/
├── cunard_scraper.py      # Main scraper script
├── requirements.txt       # Python dependencies
├── config.json           # Your configuration (gitignored)
├── downloads/            # Downloaded PDFs and state
│   ├── daily-programme-YYYY-MM-DD.pdf
│   ├── scraper_state.json
│   ├── browser_state.json
│   └── events_YYYY-MM-DD.json
└── README.md             # This file
```

## State Management

The scraper maintains state in `downloads/scraper_state.json`:
- Tracks which PDFs have already been processed (by hash)
- Prevents duplicate processing of the same programme

Browser session state is saved to `downloads/browser_state.json` to avoid re-logging in on subsequent runs.

## Troubleshooting

### Login Issues
- Check your card number and DOB are correct
- The scraper will prompt for manual login if automated login fails
- Session is saved after first successful login

### PDF Not Found
- Check the debug files in `downloads/`:
  - `debug.png` — screenshot of the page
  - `debug_page.html` — page source
- The scraper tries multiple detection methods; logs show which succeeded

### Reminders Not Sending
- Verify `telegram_bot_token` and `telegram_chat_id` are correct
- Check OpenClaw is installed and configured
- Check cron jobs with `openclaw cron list`

## Cron Job Format

Reminders are scheduled as one-time cron jobs using OpenClaw:

```bash
openclaw cron add \
  --name "cunard_2026-03-11_Gala_Dinner" \
  --schedule "15 19 11 3 *" \
  --command "openclaw message send --target CHAT_ID --channel telegram --message '...'"
```

The cron expression `15 19 11 3 *` means: at 19:15 on March 11th (one-time, not recurring).

## License

MIT License — feel free to modify for your own cruises.

## Credits

Built by Nudge for Paul. Enjoy your cruise! 🚢
