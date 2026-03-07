#!/bin/bash
# Test script for Cunard scraper

cd "$(dirname "$0")"

echo "Cunard Daily Programme Scraper - Test Script"
echo "============================================"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

# Check config
if [ ! -f "config.json" ]; then
    echo "Creating config.json from template..."
    cp config.json.example config.json
    echo "Please edit config.json with your credentials"
    exit 1
fi

# Check dependencies
echo "Checking dependencies..."
python3 -c "import playwright, aiohttp, PyPDF2" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    playwright install chromium
fi

echo ""
echo "Starting scraper..."
python3 cunard_scraper.py --config config.json
