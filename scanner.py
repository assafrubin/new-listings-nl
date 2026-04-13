#!/usr/bin/env python3
"""
Dutch Real Estate Scanner — Pararius + Funda
Scrapes all listings with no price filter, then notifies each subscriber
only about listings that match their personal filters.

Pararius: headless Playwright/Chromium (no login needed).
Funda:    real Chrome via persistent profile (~/.../Chrome/Profile 1)
          to bypass bot detection. Deduplication against DB is done locally.

Email setup (Gmail app password):
  - Go to https://myaccount.google.com/apppasswords
  - Create an app password → paste it as email_password in config.json
"""

import json
import os
import re
import smtplib
import logging
import urllib.parse
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import anthropic
import db

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE    = os.path.join(BASE_DIR, "scanner.log")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "notifications": {
        "whatsapp_number": "+310624463594",
        "whatsapp_apikey": "",
        "email_from": "assafrubin@gmail.com",
        "email_password": "",           # Gmail App Password
    },
}

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log.info("Created config.json — please fill in notification credentials.")
    return json.load(open(CONFIG_FILE))

# ── Browser helper ─────────────────────────────────────────────────────────────
def make_browser_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="nl-NL",
        extra_http_headers={"Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8"},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context

# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_pararius(city: str, page) -> List[dict]:
    """Scrape Pararius for a city with no price/rooms filter."""
    url = f"https://www.pararius.nl/huurwoningen/{city}"
    log.info(f"Pararius → {url}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)
    except PwTimeoutError:
        log.warning(f"Pararius page load timed out for {city}.")
        return []

    soup  = BeautifulSoup(page.content(), "lxml")
    cards = soup.select("li.search-list__item--listing")
    log.info(f"Pararius [{city}]: {len(cards)} listing cards found")

    listings = []
    for card in cards:
        a_link    = card.select_one("a.listing-search-item__link--title") or card.select_one("a[href]")
        href      = a_link["href"] if a_link else ""
        lid_match = re.search(r"/([0-9a-f]{8})/", href)
        lid       = lid_match.group(1) if lid_match else href.strip("/").replace("/", "-")
        title     = (a_link.get("aria-label") or a_link.get_text(strip=True)) if a_link else "?"

        price_el  = card.select_one(".listing-search-item__price")
        price     = price_el.get_text(strip=True) if price_el else "?"

        size_el   = card.select_one(".illustrated-features__item--surface-area")
        rooms_el  = card.select_one(".illustrated-features__item--number-of-rooms")
        energy_el = card.select_one(".illustrated-features__item--energy-label")
        size      = size_el.get_text(strip=True)  if size_el  else ""
        rooms_raw = rooms_el.get_text(strip=True) if rooms_el else ""
        rooms     = re.search(r"\d+", rooms_raw).group() if re.search(r"\d+", rooms_raw) else ""
        energy    = energy_el.get_text(strip=True) if energy_el else ""

        agency_el   = card.select_one(".listing-search-item__broker-name, [class*='broker-name']")
        agency_text = agency_el.get_text(strip=True) if agency_el else ""

        detail_parts = [p for p in [size, (rooms + " bedrooms") if rooms else "", energy] if p]
        detail_str   = "  ·  ".join(detail_parts)
        if agency_text:
            detail_str += f"  |  {agency_text}"

        link = f"https://www.pararius.nl{href}" if href.startswith("/") else href
        if not lid:
            continue

        listings.append({
            "id":      f"pararius-{lid}",
            "source":  "Pararius",
            "city":    city,
            "title":   title,
            "price":   price,
            "size":    size,
            "rooms":   rooms,
            "energy":  energy,
            "agency":  agency_text,
            "phone":   "",
            "details": detail_str,
            "url":     link,
        })

    return listings


def _chrome_major_version() -> int:
    """Detect installed Chrome major version so undetected_chromedriver matches."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
            stderr=subprocess.DEVNULL,
        ).decode()
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def scrape_funda_all_cities(cities: List[str]) -> List[dict]:
    """Scrape Funda for all cities in one browser session using undetected_chromedriver."""
    version = _chrome_major_version()
    driver  = uc.Chrome(headless=False, **({} if not version else {"version_main": version}))
    all_listings = []
    try:
        for city in cities:
            slug = city.lower().replace(" ", "-")
            url  = f"https://www.funda.nl/zoeken/huur/?selected_area=%5B%22{slug}%22%5D&availability=%5B%22available%22%5D"
            log.info(f"Funda → {url}")
            driver.get(url)
            try:
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/detail/huur/']"))
                )
            except Exception:
                log.warning(f"Funda [{city}]: timed out waiting for listings")
                continue
            time.sleep(2)

            soup = BeautifulSoup(driver.page_source, "lxml")
            seen_ids = set()
            listings = []

            for a in soup.select("a[href*='/detail/huur/']"):
                href  = a.get("href", "")
                lid_m = re.search(r"/(\d{7,9})/?$", href)
                if not lid_m or lid_m.group(1) in seen_ids:
                    continue
                lid = lid_m.group(1)
                seen_ids.add(lid)

                link = f"https://www.funda.nl{href}" if href.startswith("/") else href
                card = a.find_parent("li") or a.find_parent("div")
                text = card.get_text(" ", strip=True) if card else ""

                # Price
                price_m = re.search(r"€\s*([\d.,]+)\s*/mnd", text)
                price   = f"€ {price_m.group(1)} /mnd" if price_m else ""

                # Title: text before the price, or the <p> tag inside the card
                if price_m:
                    raw_title = text[:price_m.start()].strip().rstrip(",").strip()
                else:
                    p_el = card.select_one("p") if card else None
                    raw_title = p_el.get_text(strip=True) if p_el else ""
                # Clean up noise tokens
                for noise in ["Nieuw", "Blikvanger", "New", "Top"]:
                    raw_title = raw_title.replace(noise, "").strip()
                title = raw_title[:80].strip() or link

                # Size: "XX m²"
                size_m = re.search(r"(\d{2,4})\s*m[²2]", text)
                size   = f"{size_m.group(1)} m²" if size_m else ""

                # Rooms: Dutch "X kamers/slaapkamers" or English "X bedrooms"
                rooms_m = re.search(r"(\d+)\s*(?:slaap)?kamers?|(\d+)\s*bedrooms?", text, re.I)
                rooms   = (rooms_m.group(1) or rooms_m.group(2)) if rooms_m else ""

                # Agency
                agency = ""
                if card:
                    agency_a = card.select_one("a[href*='/makelaar/']")
                    if agency_a:
                        agency = agency_a.get_text(strip=True)

                detail_parts = [p for p in [size, (rooms + " bedrooms") if rooms else ""] if p]
                detail_str   = "  ·  ".join(detail_parts)
                if agency:
                    detail_str += f"  |  {agency}"

                listings.append({
                    "id":      f"funda-{lid}",
                    "source":  "Funda",
                    "city":    city,
                    "title":   title,
                    "price":   price,
                    "size":    size,
                    "rooms":   rooms,
                    "energy":  "",
                    "agency":  agency,
                    "phone":   "",
                    "details": detail_str,
                    "url":     link,
                })

            log.info(f"Funda [{city}]: {len(listings)} listings found")
            all_listings.extend(listings)
    finally:
        driver.quit()
    return all_listings


def scrape_all(cities: List[str]) -> tuple:
    """Returns (pararius_listings, funda_listings)."""
    pararius, funda = [], []

    # ── Pararius: headless Playwright/Chromium ─────────────────────────────────
    with sync_playwright() as pw:
        browser, ctx = make_browser_context(pw)
        page = ctx.new_page()
        try:
            for city in cities:
                pararius.extend(scrape_pararius(city, page))
        finally:
            browser.close()

    # ── Funda: undetected_chromedriver (bypasses Datadome) ────────────────────
    try:
        funda = scrape_funda_all_cities(cities)
    except Exception as e:
        log.warning(f"Funda scrape failed: {e}")

    return pararius, funda


# ── Student classification ─────────────────────────────────────────────────────

_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8",
}


def fetch_listing_description(url: str, source: str) -> str:
    """
    Fetch the description text from a listing detail page.
    Returns an empty string if the page cannot be retrieved or parsed.
    """
    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=12)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        # Pararius detail page description selectors
        if source == "Pararius":
            desc = (
                soup.select_one(".listing-detail-description__additional")
                or soup.select_one(".listing-detail-description")
                or soup.select_one("[class*='description']")
            )
        else:  # Funda
            desc = (
                soup.select_one(".object-description-body")
                or soup.select_one(".object-description")
                or soup.select_one("[class*='description']")
            )
        return desc.get_text(" ", strip=True)[:3000] if desc else ""
    except Exception as e:
        log.debug(f"Description fetch failed for {url}: {e}")
        return ""


def classify_student_listing(text: str, api_key: str) -> bool:
    """
    Use Claude to determine whether this listing is exclusively for students.
    Returns True only if the listing explicitly restricts applicants to students.
    """
    if not text.strip() or not api_key:
        return False
    try:
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "You are analyzing a Dutch rental listing description.\n"
            "Determine whether this listing is EXCLUSIVELY meant for students "
            "(i.e. it explicitly states only students may apply, requires proof of enrollment, "
            "uses terms like 'alleen voor studenten', 'studentenwoning', 'student only', etc.).\n\n"
            f"Listing text:\n{text}\n\n"
            "Reply with exactly one word: YES if it is student-only, NO otherwise. "
            "When in doubt, answer NO."
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        log.warning(f"Student classification API error: {e}")
        return False


def enrich_with_student_flag(listings: List[dict], api_key: str):
    """
    For each listing (in-place), fetch its description and classify whether it
    is a student-only listing.  Sets listing['student'] = True | False.
    """
    if not api_key:
        log.warning("No Anthropic API key — skipping student classification (defaulting to False).")
        for l in listings:
            l["student"] = False
        return

    for l in listings:
        desc = fetch_listing_description(l.get("url", ""), l.get("source", ""))
        is_student = classify_student_listing(desc, api_key)
        l["student"] = is_student
        if is_student:
            log.info(f"  STUDENT listing detected: {l.get('title')} ({l.get('url')})")


# ── Subscriber matching ────────────────────────────────────────────────────────

def matches_query(listing: dict, query: dict) -> bool:
    cities = query.get("cities") or []
    if cities and listing.get("city", "").lower() not in [c.lower() for c in cities]:
        return False
    price_num = db._parse_price(listing.get("price", ""))
    if price_num is not None:
        if query.get("min_price") and price_num < query["min_price"]:
            return False
        if query.get("max_price") and price_num > query["max_price"]:
            return False
    rooms_num = db._parse_rooms(listing.get("rooms", ""))
    if rooms_num is not None and query.get("min_rooms") and rooms_num < query["min_rooms"]:
        return False
    # Student filter: if listing is student-only and the query is NOT marked as
    # student-friendly, exclude the listing.
    if listing.get("student") and not query.get("student"):
        return False
    return True


# ── Notifications ──────────────────────────────────────────────────────────────

def send_whatsapp(message: str, cfg: dict):
    apikey = cfg.get("whatsapp_apikey", "").strip()
    phone  = cfg.get("whatsapp_number", "").strip()
    if not apikey:
        log.warning("WhatsApp skipped — no CallMeBot API key in config.json")
        return
    encoded = urllib.parse.quote_plus(message)
    url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded}&apikey={apikey}"
    try:
        r = requests.get(url, timeout=15)
        if "Message queued" in r.text or r.status_code == 200:
            log.info("WhatsApp sent.")
        else:
            log.warning(f"WhatsApp API {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.error(f"WhatsApp error: {e}")
    time.sleep(1)


def send_email(subject: str, html_body: str, to_addr: str, notify_cfg: dict):
    password  = notify_cfg.get("email_password", "").strip()
    from_addr = notify_cfg.get("email_from", "").strip()
    if not password:
        log.warning("Email skipped — no Gmail app password in config.json")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(from_addr, password)
            srv.sendmail(from_addr, [to_addr], msg.as_string())
        log.info(f"Email sent to {to_addr}.")
    except Exception as e:
        log.error(f"Email error: {e}")


def _query_section_html(query: dict, listings: List[dict]) -> str:
    name  = query.get("customer_name", "")
    cities = " / ".join(c.title() for c in (query.get("cities") or []))
    min_p = query.get("min_price")
    max_p = query.get("max_price")
    min_r = query.get("min_rooms")
    filter_parts = [cities]
    if min_p or max_p:
        filter_parts.append(f"€{int(min_p) if min_p else '?'}–€{int(max_p) if max_p else '?'}/mo")
    if min_r:
        filter_parts.append(f"{min_r}+ room(s)")
    if query.get("student"):
        filter_parts.append("Student listings")
    filter_str = "  ·  ".join(p for p in filter_parts if p)

    rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px'>{l['source']}</td>"
        f"<td style='padding:6px 10px'>{l['title']}</td>"
        f"<td style='padding:6px 10px'>{l['price']}</td>"
        f"<td style='padding:6px 10px'>{l.get('details','')}</td>"
        f"<td style='padding:6px 10px'><a href='{l['url']}'>View →</a></td>"
        f"</tr>"
        for l in listings
    )
    return f"""
    <div style="margin-bottom:28px">
      <h3 style="color:#e84e1b;margin-bottom:4px">
        🏠 {len(listings)} new rental{'s' if len(listings)>1 else ''} for {name}
      </h3>
      <p style="color:#888;font-size:13px;margin-bottom:10px">{filter_str}</p>
      <table border="1" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;width:100%;border-color:#ddd">
        <tr style="background:#f5f5f5;font-weight:bold">
          <th style="padding:6px 10px">Source</th>
          <th style="padding:6px 10px">Address</th>
          <th style="padding:6px 10px">Price</th>
          <th style="padding:6px 10px">Details</th>
          <th style="padding:6px 10px">Link</th>
        </tr>
        {rows}
      </table>
    </div>
    """


def notify_subscribers(new_listings: List[dict], notify_cfg: dict):
    subscribers = db.get_subscribers_with_queries()
    if not subscribers:
        log.info("No active subscribers — skipping notifications.")
        return

    for sub in subscribers:
        if not sub.get("queries"):
            continue
        sections = []
        customer_names = []
        for query in sub["queries"]:
            matching = [l for l in new_listings if matches_query(l, query)]
            if matching:
                sections.append(_query_section_html(query, matching))
                customer_names.append(query.get("customer_name", ""))
                log.info(f"  → {sub['email']} / {query.get('customer_name')}: {len(matching)} match(es)")
        if not sections:
            continue

        now  = datetime.now().strftime("%d %b %Y %H:%M")
        names_str = ", ".join(customer_names)
        html = f"""
        <div style="font-family:sans-serif;max-width:740px">
          <p style="color:#888;font-size:12px;margin-bottom:20px">{now}</p>
          {''.join(sections)}
        </div>
        """
        send_email(
            f"[Listings] New rentals for {names_str}",
            html,
            sub["email"],
            notify_cfg,
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    cfg  = load_config()
    ntfy = cfg.get("notifications", {})
    anthropic_key = cfg.get("anthropic_api_key", "").strip()

    log.info("=== Scan started ===")
    seen   = db.get_seen_ids()
    cities = cfg.get("scrape", {}).get("cities") or ["amsterdam"]
    log.info(f"Scraping cities: {cities}")

    pararius_listings, funda_listings = scrape_all(cities)
    log.info(f"Pararius fetched: {len(pararius_listings)}, Funda fetched: {len(funda_listings)}")

    new_pararius = [l for l in pararius_listings if l["id"] not in seen]
    new_funda    = [l for l in funda_listings    if l["id"] not in seen]

    new = new_pararius + new_funda
    log.info(f"New listings: {len(new)} ({len(new_pararius)} Pararius, {len(new_funda)} Funda)")

    if new:
        for l in new:
            log.info(f"  NEW  [{l['source']}] {l['title']} — {l['price']}  {l['url']}")
        log.info(f"Classifying {len(new)} new listings for student-only content…")
        enrich_with_student_flag(new, anthropic_key)
        db.upsert_listings(new)
        notify_subscribers(new, ntfy)
    else:
        log.info("No new listings.")

    db.upsert_listings(pararius_listings)
    db.upsert_listings(funda_listings)
    db.record_scan_run([l["id"] for l in new])
    log.info("=== Scan complete ===\n")


if __name__ == "__main__":
    main()
