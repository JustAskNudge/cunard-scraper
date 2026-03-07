#!/bin/bash
# Setup script for Cunard scraper - run this on your MacBook Air

set -e

echo "Setting up Cunard scraper..."

# Create directory
mkdir -p ~/ships-scraper-v2
cd ~/ships-scraper-v2

# Download files
echo "Downloading files..."
curl -sO http://100.115.36.101:8000/cunard_scraper.py || scp nudge@100.115.36.101:/Users/nudge/clawd/skills/cunard-reminders/cunard_scraper.py .
curl -sO http://100.115.36.101:8000/requirements.txt || scp nudge@100.115.36.101:/Users/nudge/clawd/skills/cunard-reminders/requirements.txt .

# Create config
echo "Creating config..."
cat > config.json << 'CONFIGEOF'
{
  "cunard_card_number": "269501",
  "cunard_first_name": "Paul",
  "cunard_last_name": "Kingham",
  "cunard_dob_day": "15",
  "cunard_dob_month": "10",
  "cunard_dob_year": "1971",
  "telegram_bot_token": "8159429593:AAEfRif0Eky5ZtCg7TBQMVYfe4SoCvyWe38",
  "telegram_chat_id": "1665068053",
  "headless": false
}
CONFIGEOF

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt
playwright install chromium

echo "Setup complete!"
echo "Run: cd ~/ships-scraper-v2 && python3 cunard_scraper.py"
