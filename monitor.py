"""
MCAT seat monitor (runs on GitHub Actions).

Signs in to AAMC (reading the emailed one-time code from Gmail when asked),
opens the Pearson VUE reschedule search, checks each target date, and sends a
phone alert via ntfy when an appointment time shows up.

It NEVER selects or books an appointment. It only searches and reads results.

Secrets (GitHub Actions secrets):
  AAMC_USERNAME, AAMC_PASSWORD      your AAMC sign-in
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD Gmail + a Google "app password" (used only to read AAMC's code email)
  NTFY_TOPIC                        your private ntfy topic name

Optional variables:
  TARGET_DATES     default 02/12/2027,03/06/2027,03/19/2027
  INCLUDE_CENTERS  name fragments to alert on (default: NYC + Brooklyn centers)
"""

import email
import imaplib
import json
import os
import re
import sys
import time
import urllib.request
from email.header import decode_header
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

USERNAME = os.environ.get("AAMC_USERNAME", "")
PASSWORD = os.environ.get("AAMC_PASSWORD", "")
GMAIL = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_PW = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

START_URL = "https://auth.aamc.org/account/#/login?goto=https:%2F%2Fmcat.aamc.org%2Fmrs"
SCHEDULER_HOME = "https://mcat.aamc.org/mrs"
TARGET_DATES = [d.strip() for d in (os.environ.get("TARGET_DATES") or
                "02/12/2027,03/06/2027,03/19/2027").split(",") if d.strip()]
INCLUDE = [c.strip().lower() for c in (os.environ.get("INCLUDE_CENTERS") or
           "255 Greenwich,Herald Square,W 44th,Brooklyn NY,New York City NY TBD").split(",") if c.strip()]
# Clicks from the AAMC dashboard to the "Find a new appointment" search (mapped from the live site)
NAV_STEPS = ["Reschedule or Cancel Appointment", "MCAT: Medical College Admission Test", "Reschedule"]

SEL_ADDRESS = "#testCentersNearAddress"
SEL_DATE = "#preferredDateShown"
SEL_SEARCH = "#testCenterSearch"
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b", re.I)
STATE_FILE = Path("state/state.json")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def notify(title, message, priority="high", tags="rotating_light"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": SCHEDULER_HOME})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log(f"Notified: {title}")
    except Exception as e:
        log(f"Notification failed: {e}")


def settle(page):
    for state, ms in (("load", 30000), ("networkidle", 8000)):
        try:
            page.wait_for_load_state(state, timeout=ms)
        except PWTimeout:
            pass


def visible(page, selector, timeout=1500):
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except PWTimeout:
        return False


def body_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


# ---------- Gmail: fetch AAMC's one-time code ----------
def fetch_code(since_ts, wait_s=180):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(8)
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com")
            M.login(GMAIL, GMAIL_PW)
            M.select("INBOX")
            _, ids = M.search(None, '(FROM "aamc")')
            for mid in reversed(ids[0].split()[-10:]):
                _, data = M.fetch(mid, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                when = email.utils.parsedate_to_datetime(msg["Date"]).timestamp()
                if when < since_ts - 60:
                    continue
                subj = "".join(s.decode(c or "utf-8") if isinstance(s, bytes) else s
                               for s, c in decode_header(msg.get("Subject", "")))
                text = ""
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        text += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                text = re.sub(r"<[^>]+>", " ", text)
                m = (re.search(r"code is\s*(\d{4,8})", text, re.I)
                     or re.search(r"\b(\d{6})\b", subj + " " + text))
                if m:
                    M.store(mid, "+X-GM-LABELS", "\\Trash")  # tidy up the code email
                    M.logout()
                    return m.group(1)
            M.logout()
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"Couldn't sign in to Gmail — check GMAIL_ADDRESS / GMAIL_APP_PASSWORD ({e}).")
    raise RuntimeError("AAMC's code email didn't arrive in Gmail within 3 minutes.")


# ---------- sign in ----------
def sign_in(page):
    page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
    if not visible(page, "input[type=password]", timeout=30000):
        raise RuntimeError("AAMC sign-in page didn't load.")
    page.locator("input[placeholder*='user' i], input[type=text]").first.fill(USERNAME)
    page.locator("input[type=password]").first.fill(PASSWORD)
    page.wait_for_timeout(500)
    page.get_by_role("button", name=re.compile(r"^\s*sign\s*in\s*$", re.I)).first.click()
    settle(page)
    page.wait_for_timeout(3000)

    if visible(page, "input[type=password]", timeout=2000):
        raise RuntimeError("Still on the sign-in page — check AAMC_USERNAME / AAMC_PASSWORD.")

    if re.search(r"two-factor|one-time code", body_text(page), re.I):
        log("AAMC asked for the emailed code")
        requested = time.time()
        page.get_by_role("button", name=re.compile(r"^\s*continue\s*$", re.I)).first.click()
        settle(page)
        code = fetch_code(requested)
        log("Got the code from Gmail")
        box = page.locator("input:visible:not([type=hidden]):not([type=checkbox]):not([type=radio])").first
        box.wait_for(state="visible", timeout=20000)
        digits = page.locator("input[maxlength='1']:visible")
        if digits.count() >= len(code):  # AAMC uses six one-digit boxes
            for i, d in enumerate(code):
                digits.nth(i).fill(d)
        else:
            box.click()
            page.keyboard.type(code, delay=120)
        page.wait_for_timeout(800)
        btn = page.get_by_role("button", name=re.compile(r"authenticate|verify|submit|continue|sign\s*in|confirm", re.I))
        if btn.count() and btn.first.is_enabled():
            btn.first.click()
        else:
            page.keyboard.press("Enter")
        for _ in range(10):  # wait up to ~20s to leave the sign-in site
            page.wait_for_timeout(2000)
            if "auth.aamc.org" not in page.url:
                break
        settle(page)
        if "auth.aamc.org" in page.url:
            inputs = page.locator("input:visible").evaluate_all(
                "els => els.map(e => `${e.type}/${e.maxLength}/${e.placeholder||''}`)")
            buttons = page.locator("button:visible").all_inner_texts()
            snippet = re.sub(r"\S+@\S+|\d{3,}", "[hidden]", " ".join(body_text(page).split()))[:400]
            log(f"Code page inputs: {inputs} | buttons: {buttons}")
            log(f"Page says: {snippet}")
            raise RuntimeError("AAMC didn't accept the emailed code.")
    log("Signed in")


def dismiss_cookies(page):
    for pattern in (r"reject|decline|necessary only", r"allow and continue|accept all|^\s*accept\s*$"):
        btn = page.get_by_role("button", name=re.compile(pattern, re.I))
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                page.wait_for_timeout(1000)
                return
        except Exception:
            pass


def click_step(page, text):
    dismiss_cookies(page)
    exact = page.get_by_role("link", name=text, exact=True).or_(page.get_by_role("button", name=text, exact=True))
    loose = page.get_by_role("link", name=re.compile(re.escape(text), re.I)).or_(
        page.get_by_role("button", name=re.compile(re.escape(text), re.I)))
    el = exact.first if exact.count() else loose.first
    el.wait_for(state="visible", timeout=20000)
    el.click()
    settle(page)
    page.wait_for_timeout(2000)


def get_to_search(page):
    if "mcat.aamc.org" not in page.url:
        page.goto(SCHEDULER_HOME, wait_until="domcontentloaded", timeout=60000)
        settle(page)
    for step in NAV_STEPS:
        if visible(page, SEL_ADDRESS, timeout=2000):
            return
        log(f"Clicking '{step}'")
        try:
            click_step(page, step)
        except PWTimeout:
            labels = page.locator("a:visible, button:visible").all_inner_texts()
            # logs are public: skip anything multi-line (profile name) and hide numbers
            safe = {re.sub(r"\d+", "#", l.strip()[:40]) for l in labels if l.strip() and "\n" not in l.strip()}
            log("Clickable here: " + " | ".join(sorted(x for x in safe if "ID" not in x))[:1200])
            raise RuntimeError(f"Couldn't find '{step}' (on {page.url.split('?')[0]}).")
    if not visible(page, SEL_ADDRESS, timeout=15000):
        raise RuntimeError(f"Didn't reach the appointment search (on {page.url.split('?')[0]}).")


# ---------- search ----------
def search_date(page, date):
    page.fill(SEL_DATE, date)
    page.keyboard.press("Escape")
    page.click(SEL_SEARCH)
    settle(page)
    page.wait_for_timeout(2000)
    if page.get_by_text(re.compile(r"Available Appointments\s*-\s*" + re.escape(date))).count() == 0:
        raise RuntimeError(f"Search for {date} didn't load results for that date.")
    found = []
    rows = page.locator("table tbody tr")
    for i in range(rows.count()):
        text = rows.nth(i).inner_text()
        m = re.search(r"Pearson Professional Centers-([^\n]+)", text)
        if not m:
            continue
        name = m.group(1).strip()
        if not any(x in name.lower() for x in INCLUDE):
            continue
        times = TIME_RE.findall(text)
        if times:
            found.append(f"{date} – {name}: {', '.join(sorted(set(t.strip() for t in times))[:6])}")
    log(f"{date}: {len(found)} matching center(s) with times")
    return found


def main():
    missing = [k for k in ("AAMC_USERNAME", "AAMC_PASSWORD", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NTFY_TOPIC")
               if not os.environ.get(k)]
    if missing:
        log("Missing secrets: " + ", ".join(missing))
        sys.exit(1)
    try:
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        state = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900}, locale="en-US", timezone_id="America/New_York",
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
            page = ctx.new_page()
            sign_in(page)
            get_to_search(page)
            openings = []
            for date in TARGET_DATES:
                openings += search_date(page, date)
            browser.close()

        msg = "\n".join(openings)
        if openings and msg != state.get("last_alert"):
            notify("MCAT seat open!", msg + "\nOpen the scheduler and book it now.", priority="urgent")
        elif not openings:
            log("No openings this run.")
        if state.get("error"):
            notify("MCAT monitor working again", "Checks are running normally.", priority="low", tags="white_check_mark")
        state.update(error="", last_alert=msg)
    except Exception as e:
        err = str(e).splitlines()[0][:300]
        log(f"ERROR: {err}")
        if state.get("error") != err:
            notify("MCAT monitor needs attention", err, priority="default", tags="warning")
        state["error"] = err
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))
        sys.exit(1)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
