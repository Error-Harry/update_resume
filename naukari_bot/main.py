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

# Playwright stores cookies/session under this directory when not in CI (override with NAUKRI_USER_DATA_DIR).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_USER_DATA_DIR = os.path.join(_SCRIPT_DIR, ".pw-profile")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ================== UTIL ==================

def validate_env():
    missing = []
    for name, value in [
        ("EMAIL", EMAIL),
        ("PASSWORD", PASSWORD),
        ("SMTP_EMAIL", SMTP_EMAIL),
        ("SMTP_PASSWORD", SMTP_PASSWORD),
        ("SMTP_SERVER", SMTP_SERVER),
        ("TO_EMAIL", TO_EMAIL),
    ]:
        if not value:
            missing.append(name)
    if missing:
        logging.error("Missing required environment variables: %s", ", ".join(missing))
        logging.error("In GitHub Actions, ensure these repository secrets are set and mapped in the workflow.")
        sys.exit(2)


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


def resolve_user_data_dir() -> str | None:
    """
    Persistent Chromium profile path. None = fresh session every run (typical CI).
    Local default reuses naukari_bot/.pw-profile so Naukri stays logged in between days.
    """
    explicit = os.getenv("NAUKRI_USER_DATA_DIR", "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if os.getenv("CI", "false").lower() == "true":
        return None
    return _DEFAULT_USER_DATA_DIR


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


async def _session_valid_on_profile(page) -> bool:
    """After navigating to /mnjuser/profile, detect whether we are logged in."""
    if "nlogin" in page.url.lower():
        return False
    try:
        await page.wait_for_selector("text=Resume", timeout=15000)
        return True
    except PlaywrightTimeoutError:
        return False


async def ensure_logged_in(page):
    """
    Reuse cookies from a persistent user-data dir when possible; only run password login if needed.
    """
    await page.goto(
        "https://www.naukri.com/mnjuser/profile",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    if await _session_valid_on_profile(page):
        logging.info("Existing Naukri session is still valid — skipping credential login.")
        return
    logging.info("No usable session — performing login.")
    await login(page)
    await page.goto("https://www.naukri.com/mnjuser/profile", timeout=60000)
    await page.wait_for_selector("text=Resume", timeout=20000)


async def dismiss_overlays(page):
    """Close Naukri modals/toasts that can block profile edits."""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        for sel in (
            ".ltLayer.open .crossIcon",
            ".ltLayer.open [class*='close']",
            "button[aria-label='Close']",
        ):
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(500)
    except Exception:
        pass


async def wait_for_first_visible(page, selectors: list[str], *, timeout_ms: int) -> str:
    """Return the first selector that becomes visible within timeout_ms."""
    per_selector_ms = max(timeout_ms // len(selectors), 3000)
    last_err = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=per_selector_ms)
            return sel
        except Exception as e:
            last_err = e
    raise PlaywrightTimeoutError(
        f"None of {selectors} became visible within ~{timeout_ms}ms (last error: {last_err})"
    )


async def click_first_visible(page, selectors: list[str], *, timeout_ms: int) -> str:
    """Click the first visible locator from candidates; fall back to force/JS click."""
    sel = await wait_for_first_visible(page, selectors, timeout_ms=timeout_ms)
    loc = page.locator(sel).first
    for click_fn in (
        lambda: loc.click(timeout=5000),
        lambda: loc.click(force=True, timeout=5000),
        lambda: loc.evaluate("node => node.click()"),
    ):
        try:
            await click_fn()
            return sel
        except Exception:
            continue
    raise PlaywrightTimeoutError(f"Could not click visible element: {sel}")


HEADLINE_EDIT_SELECTORS = [
    # Pencil after the Naukri 360 crown icon (crown is the first sibling span).
    "xpath=//div[contains(@class,'widgetHead')][.//span[normalize-space()='Resume headline']]/span[contains(@class,'edit')]",
    "#lazyResumeHead .widgetHead:has-text('Resume headline') span.edit.icon",
    "#lazyResumeHead .widgetHead:has-text('Resume headline') span.edit",
    "xpath=//div[contains(@class,'widgetHead')][.//span[contains(.,'Resume headline')]]/span[last()]",
    ".widgetHead:has-text('Resume headline') span.edit.icon",
]

HEADLINE_TEXTAREA_SELECTORS = [
    "#resumeHeadlineTxt",
    "#lazyResumeHead textarea",
    "textarea[name*='headline' i]",
    "textarea[id*='headline' i]",
]

HEADLINE_SAVE_SELECTORS = [
    ".form-actions button[type='submit']",
    "#lazyResumeHead button[type='submit']:has-text('Save')",
    "button.btn-dark-ot[type='submit']:has-text('Save')",
    "button:has-text('Save')",
]


async def update_resume_headline(page):
    logging.info("Updating resume headline...")

    async def scroll_to_headline_section():
        """Bring the Resume headline widget into view and wait for lazy content."""
        await dismiss_overlays(page)

        # Quick-link jump is the most reliable way to scroll the profile sidebar section.
        quick_link = page.locator("a, button, [role='link']").filter(has_text="Resume headline").first
        if await quick_link.count() > 0 and await quick_link.is_visible():
            try:
                await quick_link.click(timeout=5000)
                await page.wait_for_timeout(1000)
            except Exception:
                pass

        await page.evaluate("""
            () => {
                const targets = [
                    document.querySelector('#lazyResumeHead'),
                    ...document.querySelectorAll('.widgetHead'),
                ].filter(Boolean);
                for (const el of targets) {
                    if (el.textContent && el.textContent.includes('Resume headline')) {
                        el.scrollIntoView({ block: 'center' });
                        return;
                    }
                }
                const label = [...document.querySelectorAll('span, div, h2, h3')]
                    .find(n => n.textContent && n.textContent.trim() === 'Resume headline');
                label?.scrollIntoView({ block: 'center' });
            }
        """)
        await page.wait_for_selector("text=Resume headline", timeout=15000)
        await page.wait_for_timeout(2000)

    async def scroll_and_open_editor() -> str:
        """Scroll to the Resume Headline section then click the edit (pencil) icon."""
        await scroll_to_headline_section()
        await dismiss_overlays(page)

        used_edit = await click_first_visible(page, HEADLINE_EDIT_SELECTORS, timeout_ms=20000)
        logging.info("Clicked headline edit control via %s", used_edit)
        await page.wait_for_timeout(1000)

        used_textarea = await wait_for_first_visible(
            page, HEADLINE_TEXTAREA_SELECTORS, timeout_ms=20000
        )
        logging.info("Editor opened (%s)", used_textarea)
        return used_textarea

    async def save_and_close(textarea_selector: str):
        """Click Save and wait for the editor/modal to close."""
        await click_first_visible(page, HEADLINE_SAVE_SELECTORS, timeout_ms=15000)
        try:
            await page.locator(textarea_selector).first.wait_for(state="hidden", timeout=20000)
        except PlaywrightTimeoutError:
            await dismiss_overlays(page)
        await page.wait_for_timeout(1500)

    # ── FIRST EDIT: open, append dot, save ───────────────────────────────────
    textarea_selector = await scroll_and_open_editor()

    textarea = page.locator(textarea_selector).first
    current_text = await textarea.input_value()
    if not current_text.strip():
        current_text = (await textarea.text_content() or "").strip()
    logging.info("Current headline: %r", current_text)

    await textarea.fill(current_text + ".")
    await save_and_close(textarea_selector)
    logging.info("First save done (dot added)")

    # ── SECOND EDIT: re-open, restore original, save ──────────────────────────
    await scroll_and_open_editor()

    textarea = page.locator(textarea_selector).first
    await textarea.fill(current_text)
    await save_and_close(textarea_selector)
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
        user_data_dir = resolve_user_data_dir()
        browser = None
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            logging.info("Using persistent browser profile at %s", user_data_dir)
            context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                viewport={"width": 1920, "height": 1080},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                args=launch_args,
            )
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            if is_ci:
                logging.info("Ephemeral browser (CI) — no saved session; logging in each run.")
            browser = await p.chromium.launch(headless=headless, args=launch_args)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = await context.new_page()

        try:
            await ensure_logged_in(page)

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
            await dismiss_overlays(page)
            await page.wait_for_timeout(2000)   # brief pause for JS to wire up

            # Update headline
            await update_resume_headline(page)

        except Exception:
            await dump_debug_artifacts(page, "upload_resume_once")
            raise

        finally:
            if browser:
                await browser.close()
            else:
                await context.close()


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
    validate_env()
    asyncio.run(upload_with_retry())
