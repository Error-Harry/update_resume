import asyncio
import os
import shutil
import sys
import smtplib
import logging
from datetime import datetime
from email.message import EmailMessage

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ================== LOAD ENV ==================
load_dotenv()

EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
TO_EMAIL = os.getenv("TO_EMAIL")

BASE_RESUME = "naukari_bot/Harsh_Nargide.pdf"
MAX_RETRIES = 2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ================== UTIL ==================

async def dump_debug_artifacts(page, prefix: str):
    """
    Best-effort debug dump for CI failures (screenshots + HTML).
    Safe to call even when the page is mid-navigation.
    """
    try:
        os.makedirs("artifacts", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join("artifacts", f"{prefix}_{ts}")
        await page.screenshot(path=f"{base}.png", full_page=True)
        html = await page.content()
        with open(f"{base}.html", "w", encoding="utf-8") as f:
            f.write(html)
        with open(f"{base}.txt", "w", encoding="utf-8") as f:
            f.write(f"url={page.url}\n")
            try:
                f.write(f"title={await page.title()}\n")
            except Exception:
                pass
        logging.info("Saved debug artifacts: %s(.png/.html/.txt)", base)
    except Exception as e:
        logging.info("Debug artifact dump failed: %s", e)


async def wait_for_any(page, *, urls: list[str], selectors: list[str], timeout_ms: int):
    """
    Wait until either:
    - the page URL matches any glob in `urls`, or
    - any selector in `selectors` is visible
    """
    tasks = []
    for u in urls:
        tasks.append(asyncio.create_task(page.wait_for_url(u, wait_until="domcontentloaded", timeout=timeout_ms)))
    for s in selectors:
        tasks.append(asyncio.create_task(page.locator(s).first.wait_for(state="visible", timeout=timeout_ms)))

    done, pending = await asyncio.wait(tasks, timeout=timeout_ms / 1000, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    if not done:
        raise PlaywrightTimeoutError(f"Timeout {timeout_ms}ms exceeded waiting for any of urls={urls} selectors={selectors}")
    # propagate exceptions if the first completed task failed
    await list(done)[0]


def rename_resume():
    today = datetime.now().strftime("%d_%b_%Y")
    new_file = f"Harsh_Nargide_{today}.pdf"
    shutil.copy(BASE_RESUME, new_file)
    return os.path.abspath(new_file)


def cleanup_file(file_path):
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logging.info(f"Deleted file: {file_path}")
    except Exception as e:
        logging.error(f"Cleanup failed: {e}")


def send_email(subject, body, attachment_path=None):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_EMAIL
        msg["To"] = TO_EMAIL
        msg.set_content(body)

        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="application",
                    subtype="pdf",
                    filename=os.path.basename(attachment_path),
                )

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_EMAIL, SMTP_PASSWORD)
            smtp.send_message(msg)

        logging.info(f"Email sent successfully: '{subject}'")

    except Exception as e:
        logging.error(f"Email failed: {e}")


# ================== CORE ==================

async def login(page):
    # domcontentloaded avoids hanging on long-lived analytics requests (Naukri never reaches "networkidle").
    await page.goto(
        "https://www.naukri.com/nlogin/login",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    logging.info("Login page: url=%r title=%r", page.url, await page.title())

    # Naukri serves at least two login UIs (SPA vs login.naukri.com legacy). Waiting only for
    # #usernameField times out when the legacy form (#emailTxt / #pwd1) is shown instead.
    user = page.locator("#usernameField, #emailTxt, input[name='USERNAME']").first
    await user.wait_for(state="visible", timeout=45000)

    pwd_new = page.locator("#passwordField")
    if await pwd_new.is_visible():
        await page.locator("#usernameField").fill(EMAIL)
        await pwd_new.fill(PASSWORD)
        await page.locator("button[type='submit']").first.click()
    else:
        await page.locator("#emailTxt, input[name='USERNAME']").first.fill(EMAIL)
        await page.locator("#pwd1").fill(PASSWORD)
        await page.locator("#sbtLog[name='Login']").first.click()

    # Naukri sometimes does not fire the full 'load' event (long-lived requests), so do not wait for it.
    # Also, the post-login landing URL can vary. Accept any mnjuser page or presence of a logged-in header.
    try:
        await wait_for_any(
            page,
            urls=[
                "**/mnjuser/homepage**",
                "**/mnjuser/profile**",
                "**/mnjuser/**",
            ],
            selectors=[
                # Logged-in top navigation (best-effort; selector may vary)
                "a[href*='logout' i]",
                "a[href*='mnjuser/profile' i]",
            ],
            timeout_ms=60000,
        )
    except Exception:
        await dump_debug_artifacts(page, "login_post_submit")
        raise
    logging.info("Login successful — redirected (or logged-in UI detected)")

    await page.wait_for_timeout(2000)   # let any post-login popup render

    # Dismiss any overlay/popup (disability survey, notifications, etc.)
    # Escape works universally for Naukri modals; safe to call even if nothing is open
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)
    except Exception:
        pass



async def update_resume_headline(page):
    logging.info("Updating resume headline...")

    TEXTAREA_ID = "#resumeHeadlineTxt"
    # Save button: scoped to the form-actions row to avoid matching hidden "Save photo" button
    # Both the inline form and modal use div.form-actions > div.action > button[type=submit]
    SAVE_BTN    = ".form-actions button[type='submit']"

    async def scroll_and_open_editor():
        """Scroll to the Resume Headline section (triggers lazy load) then click the edit icon."""
        # Use JS to scroll the lazy container into view
        await page.evaluate("""
            const el = document.querySelector('#lazyResumeHead');
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        """)
        await page.wait_for_timeout(2000)   # wait for lazy content to render

        # Find the edit (pencil) icon that belongs specifically to the Resume Headline widget
        edit_icon = (
            page.locator(".widgetHead")
                .filter(has_text="Resume headline")
                .locator("span.edit.icon")
        )
        await edit_icon.wait_for(state="visible", timeout=15000)
        # Use JS click to bypass interception by any lingering modal overlays (e.g. .ltLayer.open)
        await edit_icon.evaluate("node => node.click()")

        # Wait for the modal / inline editor textarea to appear
        await page.wait_for_selector(TEXTAREA_ID, state="visible", timeout=20000)
        logging.info("Editor opened")

    async def save_and_close():
        """Click Save and wait for the editor/modal to close."""
        await page.locator(SAVE_BTN).first.click()
        await page.wait_for_selector(TEXTAREA_ID, state="hidden", timeout=20000)
        await page.wait_for_timeout(1500)   # let DOM settle

    # ── FIRST EDIT: open, append dot, save ───────────────────────────────────
    await scroll_and_open_editor()

    textarea = page.locator(TEXTAREA_ID)
    current_text = await textarea.input_value()
    logging.info(f"Current headline: {current_text!r}")

    await textarea.fill(current_text + ".")
    await save_and_close()
    logging.info("First save done (dot added)")

    # ── SECOND EDIT: re-open, restore original, save ──────────────────────────
    await scroll_and_open_editor()

    textarea = page.locator(TEXTAREA_ID)
    await textarea.fill(current_text)
    await save_and_close()
    logging.info("Second save done — headline update complete ✓")


async def upload_resume_once(resume_path):
    async with async_playwright() as p:
        # Local: headed (easier on Naukri). CI: true headless is often blocked (no login DOM);
        # run under Xvfb with PLAYWRIGHT_HEADED=1 (see .github/workflows) so Chromium is headed.
        is_ci = os.getenv("CI", "false").lower() == "true"
        headed = os.getenv("PLAYWRIGHT_HEADED", "").lower() in ("1", "true", "yes")
        headless = is_ci and not headed
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = await context.new_page()

        try:
            await login(page)

            await page.goto("https://www.naukri.com/mnjuser/profile", timeout=60000)
            await page.wait_for_selector("text=Resume", timeout=20000)

            # Upload resume
            await page.click("text=Update resume")
            await page.set_input_files("input[type='file']", resume_path)

            await page.wait_for_timeout(5000)
            logging.info("Resume uploaded")

            # Re-navigate to profile page so it's in a clean state after upload
            logging.info("Reloading profile page before headline update...")
            await page.goto("https://www.naukri.com/mnjuser/profile", timeout=60000)
            # Naukri fires analytics/widget requests endlessly — networkidle never fires.
            # Use domcontentloaded + wait for a key element instead.
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await page.wait_for_selector("text=Resume headline", timeout=20000)
            await page.wait_for_timeout(2000)   # brief pause for JS to wire up

            # Update headline
            await update_resume_headline(page)

        finally:
            await browser.close()


async def upload_with_retry():
    resume_path = rename_resume()
    today = datetime.now().strftime("%d-%b-%Y")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info(f"Attempt {attempt}")

            await upload_resume_once(resume_path)

            subject = f"Resume & Profile Updated - {today}"
            body = f"Your resume and profile were successfully updated on {today}."

            send_email(subject, body, resume_path)

            cleanup_file(resume_path)

            return

        except Exception as e:
            logging.error(f"Attempt {attempt} failed: {e}")
            await asyncio.sleep(5)

    send_email(
        f"Update Failed - {today}",
        f"Resume/Profile update failed after {MAX_RETRIES} attempts."
    )
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(upload_with_retry())
