#!/usr/bin/env python3
"""Cunard My Voyage Daily Programme Scraper"""
import os
import json
import hashlib
import logging
import asyncio
import base64
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
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
                # Day
                await selects[0].select_option(self.config['cunard_dob_day'])
                # Month - handle both 1-based (1-12) and 0-based (0-11) dropdown values.
                month_num = str(self.config['cunard_dob_month']).strip()
                month_names = {
                    '1': 'January', '2': 'February', '3': 'March', '4': 'April',
                    '5': 'May', '6': 'June', '7': 'July', '8': 'August',
                    '9': 'September', '10': 'October', '11': 'November', '12': 'December'
                }
                month_name = month_names.get(month_num, month_num)
                month_candidates = []
                option_values = await selects[1].evaluate(
                    "el => Array.from(el.options).map(o => String(o.value || '').trim())"
                )

                if month_num.isdigit():
                    month_int = int(month_num)
                    if "0" in option_values and "11" in option_values and 1 <= month_int <= 12:
                        # Cunard uses 0-based month values: Jan=0 ... Oct=9 ... Dec=11.
                        month_candidates.append(str(month_int - 1))
                    month_candidates.append(month_num)

                for candidate in month_candidates:
                    if candidate in option_values:
                        await selects[1].select_option(value=candidate)
                        logger.info("Selected DOB month via value=%s", candidate)
                        break
                else:
                    try:
                        await selects[1].select_option(label=month_name)
                        logger.info("Selected DOB month via label=%s", month_name)
                    except Exception:
                        # Last fallback: previous behavior.
                        await selects[1].select_option(month_num)
                        logger.info("Selected DOB month via fallback=%s", month_num)
                # Year
                await selects[2].select_option(self.config['cunard_dob_year'])
            submitted = False
            submit_selectors = [
                'button:has-text("OK")',
                'button:has-text("Login")',
                'button:has-text("Log in")',
                '#webapp-login-form button[type="submit"]',
                'form button[type="submit"]',
                'button[type="submit"]',
                'input[type="submit"]',
            ]
            for selector in submit_selectors:
                try:
                    button = page.locator(selector).first
                    if await button.count() > 0:
                        await button.click(timeout=3000)
                        logger.info("Submitted login using selector: %s", selector)
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                try:
                    await page.evaluate(
                        """() => {
                            const form = document.querySelector('#webapp-login-form') || document.querySelector('form');
                            if (!form) return false;
                            if (typeof form.requestSubmit === 'function') {
                                form.requestSubmit();
                            } else {
                                form.submit();
                            }
                            return true;
                        }"""
                    )
                    logger.info("Submitted login via form requestSubmit() fallback")
                    submitted = True
                except Exception:
                    pass

            if not submitted:
                await page.keyboard.press("Enter")
                logger.info("Submitted login via Enter key fallback")

            # The login form is SPA-driven; avoid relying only on navigation events.
            for _ in range(12):
                if not await self._check_login_required(page):
                    return True
                await asyncio.sleep(1)

            return False
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

        def looks_like_pdf_url(candidate_url: str, content_type: str = "") -> bool:
            lower_url = (candidate_url or "").lower()
            lower_content_type = (content_type or "").lower()
            return (
                ".pdf" in lower_url
                or "getdailyprogrampdf" in lower_url
                or "application/pdf" in lower_content_type
            )

        found_urls = []

        def handle_response(response) -> None:
            try:
                headers = getattr(response, "headers", {}) or {}
                content_type = headers.get("content-type") or headers.get("Content-Type") or ""
                status = getattr(response, "status", None)
                resp_url = getattr(response, "url", "")
                if status == 200 and looks_like_pdf_url(resp_url, content_type):
                    normalized = urljoin(page.url, resp_url)
                    if normalized not in found_urls:
                        found_urls.append(normalized)
                        logger.info("Captured PDF URL from network: %s", normalized)
            except Exception as exc:
                logger.debug("Failed to inspect response: %s", exc)

        page.on("response", handle_response)
        try:
            try:
                await page.wait_for_selector(".mobile__pdf__container", timeout=5000)
            except Exception:
                logger.debug("PDF container selector not found yet")

            js_probe = """() => {
                const urls = new Set();
                const push = (value) => {
                    if (!value || typeof value !== 'string') return;
                    const trimmed = value.trim();
                    if (!trimmed) return;
                    const lower = trimmed.toLowerCase();
                    if (lower.includes('.pdf') || lower.includes('getdailyprogrampdf')) urls.add(trimmed);
                };

                for (const entry of performance.getEntriesByType('resource')) push(entry.name);

                for (const key of ['pdfUrl', 'dailyProgrammePdf', 'dailyProgramPdf', 'programmePdfUrl']) {
                    push(window[key]);
                }

                const viewer = document.querySelector('.mobile__pdf__container');
                if (viewer) {
                    const reactKey = Object.keys(viewer).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'));
                    if (reactKey) {
                        const node = viewer[reactKey];
                        push(node?.memoizedProps?.pdfUrl);
                        push(node?.return?.memoizedProps?.pdfUrl);
                        push(node?.memoizedProps?.src);
                        push(node?.return?.memoizedProps?.src);
                    }
                }

                for (const script of document.querySelectorAll('script')) {
                    const txt = script.textContent || '';
                    const matches = txt.match(/https?:\\/\\/[^"'\\s]+(?:\\.pdf|getDailyProgramPdf[^"'\\s]*)/gi) || [];
                    for (const m of matches) push(m);
                }

                return Array.from(urls);
            }"""

            timeout_seconds = float(self.config.get('pdf_capture_timeout_seconds', 12.0))
            poll_seconds = float(self.config.get('pdf_capture_poll_interval_seconds', 0.5))
            poll_count = max(1, int(timeout_seconds / max(poll_seconds, 0.05)))

            for _ in range(poll_count):
                if found_urls:
                    return found_urls[0]

                js_urls = await page.evaluate(js_probe)
                if js_urls:
                    for js_url in js_urls:
                        if looks_like_pdf_url(js_url):
                            normalized = urljoin(page.url, js_url)
                            logger.info("Found PDF URL via JavaScript probe: %s", normalized)
                            return normalized

                await asyncio.sleep(poll_seconds)
        finally:
            try:
                page.remove_listener("response", handle_response)
            except Exception:
                pass

        if found_urls:
            return found_urls[0]

        parsed = urlparse(page.url)
        query = parse_qs(parsed.query)
        cruise_id = query.get("cruiseId", [None])[0] or query.get("cruiseid", [None])[0]
        program_date = query.get("date", [None])[0] or datetime.now().strftime("%Y-%m-%d")

        if cruise_id and program_date:
            base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://myvoyage.cunard.com"
            fallback = f"{base_url}/dailyprogram/getDailyProgramPdf?{urlencode({'cruiseId': cruise_id, 'date': program_date})}"
            logger.info("Constructed fallback PDF URL: %s", fallback)
            return fallback

        logger.warning("Unable to determine PDF URL from network, JavaScript, or fallback construction")
        return None
    
    async def _download_pdf(self, context: BrowserContext, url: str) -> Optional[bytes]:
        logger.info("Downloading PDF from %s", url)

        pdf_bytes = None
        try:
            response = await context.request.get(url, timeout=30000)
            if response.ok:
                pdf_bytes = await response.body()
                logger.info("Downloaded %s bytes via context.request", len(pdf_bytes))
            else:
                logger.warning("context.request download failed with status %s for %s", response.status, url)
        except Exception as exc:
            logger.warning("context.request download raised %s", exc)

        if not pdf_bytes:
            page = await context.new_page()
            try:
                data_url = await page.evaluate(
                    """async (pdfUrl) => {
                        const response = await fetch(pdfUrl, { credentials: 'include' });
                        if (!response.ok) return null;
                        const buffer = await response.arrayBuffer();
                        let binary = '';
                        const bytes = new Uint8Array(buffer);
                        const chunkSize = 0x8000;
                        for (let i = 0; i < bytes.length; i += chunkSize) {
                            binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
                        }
                        return 'data:application/pdf;base64,' + btoa(binary);
                    }""",
                    url,
                )
                if data_url and "," in data_url:
                    _, encoded = data_url.split(",", 1)
                    pdf_bytes = base64.b64decode(encoded)
                    logger.info("Downloaded %s bytes via browser fetch fallback", len(pdf_bytes))
            except Exception as exc:
                logger.error("Browser fetch fallback failed: %s", exc)
            finally:
                await page.close()

        if not pdf_bytes:
            logger.error("Failed to download PDF bytes from %s", url)
            return None

        if b"%PDF" not in pdf_bytes[:1024]:
            logger.error("Downloaded bytes do not appear to be a PDF")
            return None

        return pdf_bytes
    
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
            # Try WebKit (Safari) first as it may work better with Cunard site
            try:
                logger.info("Launching WebKit browser (Safari)...")
                browser = await p.webkit.launch(headless=False)
            except Exception as e:
                logger.warning(f"WebKit not available ({e}), falling back to Chromium")
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
                
                # Check if we're on landing page and need to click Daily Programme
                current_url = page.url
                logger.info(f"Current URL: {current_url}")
                
                if '/landing' in current_url or current_url == 'https://myvoyage.cunard.com/':
                    logger.info("On landing page, looking for Daily Programme button...")
                    
                    # Wait for page to fully load
                    await asyncio.sleep(5)
                    
                    # Try to find and click the hamburger menu to open sidebar
                    try:
                        menu_btn = await page.query_selector('button[aria-label="menu"], button svg[id="webapp-menu-navigation"]')
                        if menu_btn:
                            await menu_btn.click()
                            logger.info("Clicked menu button to open sidebar")
                            await asyncio.sleep(2)
                    except Exception as e:
                        logger.info(f"Menu button not found or error: {e}")
                    
                    # Try to click Daily Programme button - look for the sidebar link
                    dp_selectors = [
                        'a[href="/dailyProgramme"]',
                        'nav a[href*="dailyProgramme"]',
                        '.sidebar_left_view a[href*="dailyProgramme"]',
                        'a:has-text("Daily Programme")',
                        'a:has-text("Daily programme")',
                        'button:has-text("Daily Programme")',
                        'button:has-text("Daily programme")',
                        '[href*="pdfviewer"]'
                    ]
                    clicked = False
                    for selector in dp_selectors:
                        try:
                            btn = await page.query_selector(selector)
                            if btn:
                                await btn.click()
                                logger.info(f"Clicked Daily Programme button: {selector}")
                                await page.wait_for_load_state('networkidle')
                                await asyncio.sleep(3)
                                clicked = True
                                break
                        except Exception as e:
                            logger.debug(f"Selector {selector} failed: {e}")
                            continue
                    
                    if not clicked:
                        # Try navigating directly to pdfviewer
                        logger.info("Button not found, navigating to pdfviewer directly...")
                        await page.goto('https://myvoyage.cunard.com/pdfviewer', wait_until='networkidle')
                        await asyncio.sleep(3)
                
                # Debug: save page content
                html = await page.content()
                with open(self.download_dir / 'debug_page.html', 'w') as f:
                    f.write(html)
                await page.screenshot(path=str(self.download_dir / 'debug.png'))
                logger.info(f"Saved debug screenshot and HTML to {self.download_dir}")
                
                # Debug: list all buttons
                buttons = await page.query_selector_all('button')
                logger.info(f"Found {len(buttons)} buttons on page:")
                for i, btn in enumerate(buttons[:10]):
                    text = await btn.inner_text()
                    logger.info(f"  Button {i}: {text[:50]}")
                
                # Debug: list all links
                links = await page.query_selector_all('a[href]')
                logger.info(f"Found {len(links)} links on page:")
                for i, link in enumerate(links[:20]):
                    href = await link.get_attribute('href')
                    text = await link.inner_text()
                    logger.info(f"  Link {i}: {text[:30]} -> {href}")
                
                # Try to close any modal/dialog first
                try:
                    close_btn = await page.query_selector('button:has-text("Close")')
                    if close_btn:
                        await close_btn.click()
                        logger.info("Clicked Close button on modal")
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.debug(f"No close button or error: {e}")
                
                # Check for PDF viewer iframe
                iframes = await page.query_selector_all('iframe')
                logger.info(f"Found {len(iframes)} iframes")
                for i, iframe in enumerate(iframes):
                    src = await iframe.get_attribute('src')
                    logger.info(f"  Iframe {i}: {src}")
                    if src and '.pdf' in src:
                        return src
                
                # Check for embed/object
                embeds = await page.query_selector_all('embed, object')
                logger.info(f"Found {len(embeds)} embeds/objects")
                for i, embed in enumerate(embeds):
                    src = await embed.get_attribute('src') or await embed.get_attribute('data')
                    logger.info(f"  Embed {i}: {src}")
                    if src and '.pdf' in src:
                        return src
                
                # Look for mobile PDF container header
                pdf_header = await page.query_selector('.mobile__pdf__container__header')
                if pdf_header:
                    header_text = await pdf_header.inner_text()
                    logger.info(f"Found PDF header: {header_text}")
                    
                    # Try to find PDF URL in page source
                    html = await page.content()
                    
                    # Look for PDF URLs in the HTML
                    import re
                    pdf_matches = re.findall(r'https?://[^\s"\'<>]+\.pdf', html)
                    if pdf_matches:
                        logger.info(f"Found PDF URLs in HTML: {pdf_matches}")
                        return pdf_matches[0]
                    
                    # Try to find PDF reference in JavaScript variables or data attributes
                    # Look for common patterns in React apps
                    js_pdf_matches = re.findall(r'"([^"]*\.pdf)"', html)
                    if js_pdf_matches:
                        logger.info(f"Found potential PDF refs in JS: {js_pdf_matches}")
                        for match in js_pdf_matches:
                            if match.startswith('http'):
                                return match
                            elif match.startswith('/'):
                                return f"https://myvoyage.cunard.com{match}"
                    
                    # Try API endpoints that might return the PDF
                    # Common patterns for ship daily programmes
                    today = datetime.now()
                    date_str = today.strftime("%Y-%m-%d")
                    potential_urls = [
                        f"https://myvoyage.cunard.com/api/dailyProgramme/pdf",
                        f"https://myvoyage.cunard.com/api/pdf/daily",
                        f"https://ship-cms.cunard.com/content/dailyprogramme/{date_str}.pdf",
                        f"https://myvoyage.cunard.com/pdfviewer",
                    ]
                    
                    for url in potential_urls:
                        logger.info(f"Trying potential URL: {url}")
                        try:
                            # Try to fetch the URL
                            response = await context.new_page()
                            resp = await response.goto(url, wait_until='networkidle', timeout=10000)
                            content_type = resp.headers.get('content-type', '')
                            if 'pdf' in content_type.lower():
                                logger.info(f"Found PDF at: {url}")
                                return url
                            await response.close()
                        except Exception as e:
                            logger.debug(f"URL {url} failed: {e}")
                            continue
                    
                    logger.warning("Could not find PDF URL through direct URL probing; using extractor fallback")
                
                pdf_url = await self._extract_pdf_url(page)
                if not pdf_url:
                    logger.error("Could not find PDF URL")
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
