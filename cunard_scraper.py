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
import socket
import platform
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict
from typing import List, Optional

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
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
        self.config_path = Path(config_path).expanduser().resolve()
        self.config = self._load_config(config_path)
        self.download_dir = Path(self.config.get('download_dir', 'downloads'))
        self.download_dir.mkdir(exist_ok=True)
        self.state_file = self.download_dir / 'scraper_state.json'
        self.storage_state_file = self.download_dir / 'browser_state.json'
        self.state = self._load_json(self.state_file, {'sent_pdfs': {}, 'processed_dates': []})
        self._log_runtime_fingerprint()

    def _log_runtime_fingerprint(self):
        """Log runtime identity so we can confirm where scraper is running."""
        local_now = datetime.now().astimezone()
        utc_now = datetime.now(timezone.utc)
        ship_tz = timezone(timedelta(hours=10))
        ship_now = datetime.now(ship_tz)
        logger.info("Runtime fingerprint:")
        logger.info(f"  host={socket.gethostname()} platform={platform.platform()}")
        logger.info(f"  script={Path(__file__).resolve()}")
        logger.info(f"  cwd={Path.cwd()}")
        logger.info(f"  config={self.config_path}")
        logger.info(f"  tz_env={os.getenv('TZ', '(unset)')} tzname={time.tzname}")
        logger.info(f"  now_local={local_now.isoformat()}")
        logger.info(f"  now_utc={utc_now.isoformat()}")
        logger.info(f"  now_ship_aest={ship_now.isoformat()}")
        
    def _load_config(self, config_path: str) -> dict:
        config = {
            'cunard_card_number': os.getenv('CUNARD_CARD_NUMBER'),
            'cunard_first_name': os.getenv('CUNARD_FIRST_NAME'),
            'cunard_last_name': os.getenv('CUNARD_LAST_NAME'),
            'cunard_dob_day': os.getenv('CUNARD_DOB_DAY'),
            'cunard_dob_month': os.getenv('CUNARD_DOB_MONTH'),
            'cunard_dob_year': os.getenv('CUNARD_DOB_YEAR'),
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

    def _is_excluded_title(self, title: str) -> bool:
        normalized = re.sub(r'\s+', ' ', (title or '').lower()).strip()
        patterns = [
            r'\bline\s+dancing\b',
            r'\bjewel+l?ery\b',
            r'\bballroom\s+dancing\b',
            r'\bjazz\b',
            r'\bpianist\b',
            r'\bharpist\b',
            r'\bsolo\s+travellers?\b',
            r'\bcarat\b',
        ]
        return any(re.search(pattern, normalized) for pattern in patterns)
    
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
    
    def _extract_date_from_pdf_url(self, pdf_url: str) -> Optional[str]:
        """Extract date from PDF URL query parameters.
        
        Cunard PDF URLs can contain:
        - query param: ?date=YYYY-MM-DD
        - path segment: /.../YYYY-MM-DD...
        - compact path segment: /.../YYYYMMDD/... (e.g. digidocs DAILYPROGRAM)

        Returns the date string or None if not found.
        """
        try:
            parsed = urlparse(pdf_url)
            query_params = parse_qs(parsed.query)
            
            # Look for 'date' parameter
            if 'date' in query_params:
                date_str = query_params['date'][0]
                # Validate format (YYYY-MM-DD)
                datetime.strptime(date_str, "%Y-%m-%d")
                return date_str
            
            # Try to extract date from URL path (e.g., /dailyprogramme/2026-03-11.pdf)
            path_date_match = re.search(r'(\d{4}-\d{2}-\d{2})', parsed.path)
            if path_date_match:
                date_str = path_date_match.group(1)
                datetime.strptime(date_str, "%Y-%m-%d")
                return date_str

            # Try compact date format in path (e.g., /DAILYPROGRAM/20260311/)
            path_compact_match = re.search(r'(?<!\d)(\d{8})(?!\d)', parsed.path)
            if path_compact_match:
                compact = path_compact_match.group(1)
                parsed_date = datetime.strptime(compact, "%Y%m%d")
                return parsed_date.strftime("%Y-%m-%d")
                
        except (ValueError, IndexError) as e:
            logger.debug(f"Could not extract date from PDF URL: {e}")
        
        return None
    
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
                # Capture times like 6.00pm, 6.00 pm, 6.00a.m.; if a line has multiple times, create one event per time.
                time_matches = list(re.finditer(r'(\d{1,2})\.(\d{2})(?:\s*([ap])\.?\s*m\.?)?', line, re.IGNORECASE))
                if time_matches:
                    times = []
                    for time_match in time_matches:
                        hour_str = time_match.group(1)
                        minute_str = time_match.group(2)
                        meridiem = (time_match.group(3) or "").lower()
                        times.append(f"{int(hour_str)}.{minute_str}{meridiem + 'm' if meridiem else ''}")

                    # Keep existing title extraction behavior based on text after first time token.
                    rest = line[time_matches[0].end():].strip()
                    # If the extractor separated "am/pm" from time, consume it from the title prefix.
                    rest = re.sub(r'^(?:[ap])\.?\s*m\.?\b\s*', '', rest, flags=re.IGNORECASE)
                    # Parse title and venue from rest: "11.00am Event Name – Venue"
                    # Split on en-dash '–' to separate title from venue
                    if '–' in rest:
                        title_part, venue_part = rest.split('–', 1)
                        title = title_part.strip()
                        venue = venue_part.strip()
                    else:
                        title = rest[:80]
                        venue = ''

                    # Normalize common extraction artifacts at start of title.
                    title = re.sub(r'^[^\w]+', '', title).strip()
                    venue = venue.strip()

                    # Drop obvious non-event schedule/opening-hour fragments.
                    title_lower = title.lower()
                    non_event_prefixes = ('-', 'to ', 'and ', '&', ',')
                    non_event_phrases = [
                        'bar and lounge times',
                        'breakfast',
                        'lunch',
                        'dinner',
                        'day menu',
                        'late night menu',
                        'first sitting',
                        'second sitting',
                        'dress code',
                        'tonight:',
                        'this evening, we ask that you wear',
                        'by dialling',
                        'dialling',
                        'late',
                        'venue not listed',
                    ]
                    if (
                        not title
                        or len(title) < 4
                        or not re.search(r'[A-Za-z]', title)
                        or title_lower.startswith(non_event_prefixes)
                        or any(phrase in title_lower for phrase in non_event_phrases)
                    ):
                        continue

                    # Drop lines that are mostly time ranges (e.g. "1.30pm 6.00pm - 9.00pm").
                    title_no_times = re.sub(
                        r'\b\d{1,2}\.\d{2}\s*(?:[ap]\.?\s*m\.?)?\b',
                        ' ',
                        title_lower,
                        flags=re.IGNORECASE,
                    )
                    title_semantic = re.sub(r'\b(?:am|pm|to|and)\b', ' ', title_no_times, flags=re.IGNORECASE)
                    title_semantic = re.sub(r'[^a-z]+', ' ', title_semantic).strip()
                    if len(title_semantic) < 3:
                        continue

                    # If title still begins with another time token, it's usually a schedule range fragment.
                    if re.match(r'^\d{1,2}\.\d{2}\s*(?:[ap]\.?\s*m\.?)?', title_lower, flags=re.IGNORECASE):
                        continue
                    
                    # Skip excluded events
                    if self._is_excluded_title(title):
                        logger.debug(f"Excluding event: {title}")
                        continue
                    
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
                    for time_str in times:
                        events.append(Event(time=time_str, title=title, venue=venue, category=category, is_gala=is_gala))
        except Exception as e:
            logger.error(f"Error extracting events: {e}")
        logger.info(f"Extracted {len(events)} events")
        return events
    
    def _schedule_reminders(self, events: List[Event], date_str: str):
        """Schedule Apple Reminders for events, filtering out past events and excluded categories.
        
        Args:
            events: List of Event objects
            date_str: Date string in YYYY-MM-DD format (from PDF)
        """
        logger.info(f"Scheduling Apple Reminders for {len(events)} events on {date_str}")
        
        # Parse the PDF date and get current local time
        try:
            pdf_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Invalid date format: {date_str}")
            return
        
        ship_tz = timezone(timedelta(hours=10))
        now = datetime.now(ship_tz)

        scheduled_count = 0
        skipped_count = 0
        excluded_count = 0
        invalid_count = 0
        
        for event in events:
            try:
                # Skip excluded events
                if self._is_excluded_title(event.title):
                    logger.info(f"Skipping excluded event: {event.title}")
                    excluded_count += 1
                    continue
                
                # Handle both colon and dot formats, and strip am/pm
                time_clean = event.time.lower().replace('am', '').replace('pm', '')
                is_pm = 'pm' in event.time.lower()
                is_am = 'am' in event.time.lower()
                
                if ':' in time_clean:
                    hour, minute = map(int, time_clean.split(':'))
                elif '.' in time_clean:
                    hour, minute = map(int, time_clean.split('.'))
                else:
                    raise ValueError(f"Cannot parse time: {event.time}")
                
                # Convert 12-hour to 24-hour format
                if is_pm and hour != 12:
                    hour += 12
                elif is_am and hour == 12:
                    hour = 0
                
                # Create datetime for the event (using PDF date, ship timezone)
                event_datetime = datetime.combine(pdf_date, datetime.min.time().replace(hour=hour, minute=minute))
                event_datetime = event_datetime.replace(tzinfo=ship_tz)
                
                # Skip if event is in the past
                if event_datetime < now:
                    logger.info(f"Skipping past event: {event.title} at {event.time}")
                    skipped_count += 1
                    continue
                
                # Build reminder title and notes
                emoji = "⭐" if event.is_gala else "🚢"
                if event.category == 'Bingo':
                    emoji = "🎱"
                elif event.category == 'Theatre':
                    emoji = "🎭"

                clean_title = re.sub(r'\s+', ' ', event.title).strip(" -–,\t")
                clean_venue = re.sub(r'\s+', ' ', event.venue or "").strip(" -–,\t")
                if not clean_title:
                    invalid_count += 1
                    logger.debug(f"Skipping invalid event (empty title) at {event.time}")
                    continue
                if not clean_venue:
                    clean_venue = "Venue not listed in programme"

                reminder_title = f"{emoji} {clean_title}"
                reminder_notes = f"🕐 {event.time}\n📍 {clean_venue}\n📅 {date_str}"
                reminder_title = reminder_title.replace('"', '\\"')
                reminder_notes = reminder_notes.replace('"', '\\"')

                due_year = event_datetime.year
                due_month = event_datetime.month
                due_day = event_datetime.day
                due_hour = event_datetime.hour
                due_minute = event_datetime.minute
                due_second = event_datetime.second

                applescript = f'''
tell application "Reminders"
    tell list "Ship Reminders"
        set dueDate to (current date)
        set year of dueDate to {due_year}
        set month of dueDate to {due_month}
        set day of dueDate to {due_day}
        set time of dueDate to (({due_hour} * hours) + ({due_minute} * minutes) + {due_second})
        set remindDate to dueDate - (15 * minutes)
        set newReminder to make new reminder with properties {{name:"{reminder_title}", body:"{reminder_notes}", due date:dueDate}}
        set newReminderId to id of newReminder
        set newReminderRef to (first reminder whose id is newReminderId)
        set remind me date of newReminderRef to remindDate
    end tell
end tell
'''
                
                # Run osascript to create the reminder
                result = subprocess.run(
                    ['osascript', '-e', applescript],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode == 0:
                    logger.info(f"Created reminder for {event.time} - {event.title[:40]}")
                    scheduled_count += 1
                else:
                    logger.error(f"Failed to create reminder: {result.stderr}")
                    
            except Exception as e:
                logger.error(f"Error scheduling reminder: {e}")
        
        logger.info(f"Reminder scheduling complete: {scheduled_count} created, {skipped_count} skipped (past), {excluded_count} excluded, {invalid_count} invalid")
    

    
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
                    close_btn = await page.query_selector('button:has-text("Close")', timeout=750)
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
                    
                    logger.info("No direct PDF URL in page HTML; using extractor fallback")
                
                pdf_url = await self._extract_pdf_url(page)
                if not pdf_url:
                    logger.error("Could not find PDF URL")
                    return
                
                logger.info(f"Found PDF: {pdf_url}")
                content = await self._download_pdf(context, pdf_url)
                if not content:
                    logger.error("Failed to download PDF")
                    return
                
                # Extract date from PDF URL (e.g., ?date=2026-03-11) or use current date as fallback
                pdf_date_str = self._extract_date_from_pdf_url(pdf_url)
                if not pdf_date_str:
                    pdf_date_str = datetime.now().strftime("%Y-%m-%d")
                    logger.warning(f"Could not extract date from PDF URL, using today: {pdf_date_str}")
                else:
                    logger.info(f"Extracted date from PDF URL: {pdf_date_str}")
                
                pdf_hash = self._get_pdf_hash(content)
                if pdf_hash in self.state['sent_pdfs']:
                    logger.info("PDF already processed today")
                    return
                
                filename = f"daily-programme-{pdf_date_str}.pdf"
                pdf_path = self.download_dir / filename
                with open(pdf_path, 'wb') as f:
                    f.write(content)

                events = self._extract_events_from_pdf(pdf_path)
                if events:
                    self._schedule_reminders(events, pdf_date_str)
                    self._save_json(self.download_dir / f'events_{pdf_date_str}.json', 
                                   {'date': pdf_date_str, 'events': [e.__dict__ for e in events]})
                
                self.state['sent_pdfs'][pdf_hash] = {
                    'filename': filename,
                    'date': pdf_date_str,
                    'url': pdf_url
                }
                self._save_json(self.state_file, self.state)
                logger.info(f"Successfully processed daily programme for {pdf_date_str}")
                
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
