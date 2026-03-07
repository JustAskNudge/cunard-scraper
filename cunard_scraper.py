#!/usr/bin/env python3
"""Cunard My Voyage Daily Programme Scraper"""
import os
import json
import hashlib
import logging
import asyncio
import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Optional

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
    import aiohttp
    from PyPDF2 import PdfReader
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install -r requirements.txt && playwright install chromium")
    raise

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class Event:
    time: str
    title: str
    venue: str
    category: str
    is_gala: bool = False


class CunardScraper:
    def __init__(self, config_path: str = "config.json"):
        self.config = self._load_config(config_path)
        self.download_dir = Path(self.config.get('download_dir', 'downloads'))
        self.download_dir.mkdir(exist_ok=True)
        self.state_file = self.download_dir / 'scraper_state.json'
        self.storage_state_file = self.download_dir / 'browser_state.json'
        self.state = self._load_json(self.state_file, {'sent_pdfs': {}, 'processed_dates': []})
        
    def _load_config(self, config_path: str) -> dict:
        config = {
            'cunard_card_number': os.getenv('CUNARD_CARD_NUMBER'),
            'cunard_first_name': os.getenv('CUNARD_FIRST_NAME'),
            'cunard_last_name': os.getenv('CUNARD_LAST_NAME'),
            'cunard_dob_day': os.getenv('CUNARD_DOB_DAY'),
            'cunard_dob_month': os.getenv('CUNARD_DOB_MONTH'),
            'cunard_dob_year': os.getenv('CUNARD_DOB_YEAR'),
            'telegram_bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID'),
            'download_dir': os.getenv('DOWNLOAD_DIR', 'downloads'),
            'headless': os.getenv('HEADLESS', 'false').lower() == 'true',
        }
        if Path(config_path).exists():
            with open(config_path) as f:
                file_config = json.load(f)
                config.update({k: v for k, v in file_config.items() if v is not None})
        return config
    
    def _load_json(self, path: Path, default: dict) -> dict:
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return default
    
    def _save_json(self, path: Path, data: dict):
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    
    def _get_pdf_hash(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()[:16]
    
    async def _check_login_required(self, page: Page) -> bool:
        try:
            card_input = await page.wait_for_selector(
                'input[placeholder*="number" i], input[name*="card" i]',
                timeout=5000
            )
            return card_input is not None
        except:
            return False
    
    async def _perform_login(self, page: Page) -> bool:
        logger.info("Performing automated login...")
        try:
            await page.wait_for_selector('input[placeholder*="number" i]', timeout=10000)
            await asyncio.sleep(2)
            await page.fill('input[placeholder*="number" i]', self.config['cunard_card_number'])
            await page.fill('input[placeholder*="first" i]', self.config['cunard_first_name'])
            await page.fill('input[placeholder*="last" i]', self.config['cunard_last_name'])
            selects = await page.query_selector_all('select')
            if len(selects) >= 3:
                await selects[0].select_option(self.config['cunard_dob_day'])
                await selects[1].select_option(self.config['cunard_dob_month'])
                await selects[2].select_option(self.config['cunard_dob_year'])
            await page.click('button:has-text("OK"), button[type="submit"]')
            await page.wait_for_load_state('networkidle')
            await asyncio.sleep(3)
            return not await self._check_login_required(page)
        except Exception as e:
            logger.error(f"Automated login failed: {e}")
            return False
    
    async def _manual_login(self, page: Page, context: BrowserContext) -> bool:
        logger.info("=" * 60)
        logger.info("MANUAL LOGIN REQUIRED")
        logger.info("=" * 60)
        logger.info("A browser window has opened. Please:")
        logger.info("1. Accept cookies if prompted")
        logger.info("2. Enter your credentials and click OK/Login")
        logger.info("3. Wait for the daily programme to load")
        logger.info("=" * 60)
        input("\nPress ENTER once you're logged in and can see the daily programme...")
        storage = await context.storage_state()
        self._save_json(self.storage_state_file, storage)
        logger.info("Session saved for future runs!")
        return True
    
    async def _extract_pdf_url(self, page: Page) -> Optional[str]:
        logger.info("Looking for PDF URL...")
        await asyncio.sleep(5)
        links = await page.query_selector_all('a[href*=".pdf"]')
        for link in links:
            href = await link.get_attribute('href')
            if href:
                return urljoin('https://myvoyage.cunard.com', href)
        for selector in ['iframe[src*=".pdf"]', 'embed[src*=".pdf"]', 'object[data*=".pdf"]']:
            elem = await page.query_selector(selector)
            if elem:
                src = await elem.get_attribute('src') or await elem.get_attribute('data')
                if src:
                    return urljoin('https://myvoyage.cunard.com', src)
        html = await page.content()
        pdf_urls = re.findall(r'https?://[^\s\"\'<>]+\.pdf', html)
        if pdf_urls:
            return pdf_urls[0]
        return None
    
    async def _download_pdf(self, context: BrowserContext, url: str) -> Optional[bytes]:
        logger.info(f"Downloading PDF from {url}")
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until='networkidle')
            content = await response.body()
            await page.close()
            if len(content) > 1000 and content[:4] == b'%PDF':
                return content
            logger.warning(f"Downloaded content is not a valid PDF")
            return None
        except Exception as e:
            logger.error(f"Error downloading PDF: {e}")
            await page.close()
            return None
    
    def _extract_events_from_pdf(self, pdf_path: Path) -> List[Event]:
        logger.info(f"Extracting events from {pdf_path}")
        events = []
        try:
            reader = PdfReader(str(pdf_path))
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            lines = text.split('\n')
            for line in lines:
                time_match = re.search(r'(\d{1,2}):(\d{2})', line)
                if time_match:
                    time_str = time_match.group(0)
                    rest = line[time_match.end():].strip()
                    is_gala = any(word in line.lower() for word in ['gala', 'ball', '⭐'])
                    category = 'Other'
                    if is_gala:
                        category = 'Gala'
                    elif 'bingo' in line.lower():
                        category = 'Bingo'
                    elif any(word in line.lower() for word in ['show', 'theatre']):
                        category = 'Theatre'
                    elif 'planetarium' in line.lower():
                        category = 'Planetarium'
                    events.append(Event(time=time_str, title=rest[:80], venue='', category=category, is_gala=is_gala))
        except Exception as e:
            logger.error(f"Error extracting events: {e}")
        logger.info(f"Extracted {len(events)} events")
        return events
    
    def _schedule_reminders(self, events: List[Event], date_str: str):
        logger.info(f"Scheduling reminders for {len(events)} events")
        for event in events:
            try:
                hour, minute = map(int, event.time.split(':'))
                reminder_min = minute - 15
                reminder_hour = hour
                if reminder_min < 0:
                    reminder_min += 60
                    reminder_hour -= 1
                    if reminder_hour < 0:
                        reminder_hour = 23
                emoji = "⭐" if event.is_gala else "🚢"
                if event.category == 'Bingo':
                    emoji = "🎱"
                elif event.category == 'Theatre':
                    emoji = "🎭"
                reminder_text = f"{emoji} Starting in 15 mins: {event.title}\n🕐 {event.time}"
                safe_title = re.sub(r'[^\w]', '_', event.title[:20])
                job_name = f"cunard_{date_str}_{safe_title}"
                cron_expr = f"{reminder_min} {reminder_hour} * * *"
                cmd = [
                    'openclaw', 'message', 'send',
                    '--target', self.config['telegram_chat_id'],
                    '--channel', 'telegram',
                    '--message', reminder_text
                ]
                subprocess.run([
                    'openclaw', 'cron', 'add',
                    '--name', job_name,
                    '--schedule', cron_expr,
                    '--command', ' '.join(cmd)
                ], capture_output=True)
                logger.info(f"Scheduled reminder for {event.time} - {event.title[:40]}")
            except Exception as e:
                logger.error(f"Error scheduling reminder: {e}")
    
    async def _send_pdf_to_telegram(self, filename: str, content: bytes):
        logger.info(f"Sending PDF to Telegram: {filename}")
        bot_token = self.config['telegram_bot_token']
        chat_id = self.config['telegram_chat_id']
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        data = aiohttp.FormData()
        data.add_field('chat_id', chat_id)
        data.add_field('document', content, filename=filename, content_type='application/pdf')
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as response:
                result = await response.json()
                if result.get('ok'):
                    logger.info("PDF sent successfully")
                    return True
                logger.error(f"Telegram error: {result}")
                return False
    
    async def run(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.get('headless', False))
            context_args = {'viewport': {'width': 1280, 'height': 800}}
            if self.storage_state_file.exists():
                logger.info("Loading saved session...")
                context_args['storage_state'] = self._load_json(self.storage_state_file, {})
            context = await browser.new_context(**context_args)
            try:
                page = await context.new_page()
                logger.info("Navigating to pdfviewer...")
                await page.goto('https://myvoyage.cunard.com/pdfviewer', wait_until='networkidle')
                await asyncio.sleep(3)
                
                login_attempts = 0
                while await self._check_login_required(page) and login_attempts < 2:
                    login_attempts += 1
                    logger.info(f"Login required (attempt {login_attempts})")
                    if login_attempts == 1:
                        # Try automated login first
                        if await self._perform_login(page):
                            logger.info("Automated login succeeded")
                            break
                        logger.warning("Automated login failed, switching to manual")
                    # Fall back to manual login
                    if await self._manual_login(page, context):
                        logger.info("Manual login completed")
                        break
                    logger.error("Manual login failed")
                    return
                
                if await self._check_login_required(page):
                    logger.error("Still on login page after all attempts")
                    return
                
                pdf_url = await self._extract_pdf_url(page)
                if not pdf_url:
                    logger.error("Could not find PDF URL")
                    await page.screenshot(path=str(self.download_dir / 'debug.png'))
                    return
                
                logger.info(f"Found PDF: {pdf_url}")
                content = await self._download_pdf(context, pdf_url)
                if not content:
                    logger.error("Failed to download PDF")
                    return
                
                today = datetime.now().strftime("%Y-%m-%d")
                pdf_hash = self._get_pdf_hash(content)
                if pdf_hash in self.state['sent_pdfs']:
                    logger.info("PDF already processed today")
                    return
                
                filename = f"daily-programme-{today}.pdf"
                pdf_path = self.download_dir / filename
                with open(pdf_path, 'wb') as f:
                    f.write(content)
                
                await self._send_pdf_to_telegram(filename, content)
                
                events = self._extract_events_from_pdf(pdf_path)
                if events:
                    self._schedule_reminders(events, today)
                    self._save_json(self.download_dir / f'events_{today}.json', 
                                   {'date': today, 'events': [e.__dict__ for e in events]})
                
                self.state['sent_pdfs'][pdf_hash] = {
                    'filename': filename,
                    'date': today,
                    'url': pdf_url
                }
                self._save_json(self.state_file, self.state)
                logger.info(f"Successfully processed daily programme for {today}")
                
            finally:
                await browser.close()


async def main():
    import argparse
    parser = argparse.ArgumentParser(description='Cunard Daily Programme Scraper')
    parser.add_argument('--config', '-c', default='config.json', help='Config file path')
    args = parser.parse_args()
    scraper = CunardScraper(config_path=args.config)
    await scraper.run()


if __name__ == '__main__':
    asyncio.run(main())
