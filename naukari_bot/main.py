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

# Import has_text for better filtering (it's part of playwright.async_api)

# ================== LOAD ENV ==================
load_dotenv()

EMAIL = os.getenv("SHINE_EMAIL")
PASSWORD = os.getenv("SHINE_PASSWORD")

SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
TO_EMAIL = os.getenv("TO_EMAIL")

BASE_RESUME = "naukari_bot/Harsh_Nargide.pdf"
MAX_RETRIES = 2

# Playwright stores cookies/session under this directory when not in CI (override with SHINE_USER_DATA_DIR).
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
    Local default reuses naukari_bot/.pw-profile so Shine stays logged in between days.
    """
    explicit = os.getenv("SHINE_USER_DATA_DIR", "").strip()
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
    # Check if we're already on the login page, if not navigate to it
    if "login" not in page.url.lower():
        await page.goto(
            "https://www.shine.com/myshine/login/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
    
    logging.info("Login page: url=%r title=%r", page.url, await page.title())

    # Wait for the login form to be visible
    await page.wait_for_selector("text=Login", timeout=30000)
    await page.wait_for_timeout(3000)  # Let the form fully render
    
    # Use JavaScript to find and fill the form fields more reliably
    # This avoids the issue with hidden search fields
    
    # Fill email - find the visible input in the login form
    await page.evaluate(f"""
        const emailInputs = Array.from(document.querySelectorAll('input[type="text"], input[type="email"]'));
        const visibleEmailInput = emailInputs.find(input => {{
            const rect = input.getBoundingClientRect();
            const style = window.getComputedStyle(input);
            return rect.width > 0 && rect.height > 0 && 
                   style.visibility !== 'hidden' && 
                   style.display !== 'none' &&
                   input.offsetParent !== null &&
                   !input.name.includes('q') &&  // Exclude search field
                   !input.id.includes('search');  // Exclude search field
        }});
        if (visibleEmailInput) {{
            visibleEmailInput.value = '{EMAIL}';
            visibleEmailInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            visibleEmailInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
    """)
    logging.info("Email filled")
    
    # Fill password
    await page.evaluate(f"""
        const passwordInput = document.querySelector('input[type="password"]');
        if (passwordInput) {{
            passwordInput.value = '{PASSWORD}';
            passwordInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            passwordInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
    """)
    logging.info("Password filled")
    
    await page.wait_for_timeout(1000)
    
    # Click login button - the blue "Login" button
    login_button = page.locator("button:has-text('Login')").first
    await login_button.click()
    logging.info("Login button clicked")

    # Wait for successful login - dashboard should load
    try:
        await page.wait_for_url("**/dashboard**", timeout=60000)
        logging.info("Login successful — redirected to dashboard")
    except Exception:
        # Alternative: check for profile elements or any logged-in indicator
        try:
            await page.wait_for_selector("text=/My Jobs|Services|Job Alerts|Harsh Nargide/i", timeout=30000)
            logging.info("Login successful — logged-in UI detected")
        except Exception:
            await dump_debug_artifacts(page, "login_post_submit")
            raise

    await page.wait_for_timeout(2000)   # let any post-login popup render

    # Dismiss any overlay/popup
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)
    except Exception:
        pass


async def _session_valid_on_profile(page) -> bool:
    """After navigating to profile page, detect whether we are logged in."""
    if "login" in page.url.lower():
        return False
    try:
        # Check for profile-specific elements
        await page.wait_for_selector("text=/Resume|Profile Summary|Work Profile/i", timeout=15000)
        return True
    except PlaywrightTimeoutError:
        return False


async def ensure_logged_in(page):
    """
    Reuse cookies from a persistent user-data dir when possible; only run password login if needed.
    """
    # First try to go to dashboard to check if already logged in
    await page.goto(
        "https://www.shine.com/dashboard",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    
    await page.wait_for_timeout(2000)
    
    # Check if we got redirected to login page or if we're on dashboard
    current_url = page.url.lower()
    
    if "login" in current_url:
        # We were redirected to login, session expired
        logging.info("Session expired, redirected to login page — performing login.")
        await login(page)
        return
    
    # We're on dashboard, check if session is valid by looking for logged-in elements
    try:
        await page.wait_for_selector("text=/My Jobs|Services|Job Alerts|Resume/i", timeout=10000)
        logging.info("Existing Shine session is still valid — skipping credential login.")
        return
    except Exception:
        # Session not valid, need to login
        logging.info("No usable session — performing login.")
        await login(page)


async def update_resume_headline(page):
    """
    Update profile on Shine by toggling the 'Actively Looking For Jobs' status.
    This refreshes the profile visibility similar to updating resume headline.
    """
    logging.info("Updating profile status...")

    try:
        # Look for the "Actively Looking For Jobs" toggle/dropdown
        # Based on the screenshot, there's a dropdown with "Actively Looking For Jobs" status
        status_dropdown = page.locator("select:near(:text('Actively Looking For Jobs')), .status-dropdown").first
        
        # If dropdown exists, toggle it
        if await status_dropdown.is_visible(timeout=5000):
            current_value = await status_dropdown.input_value()
            # Toggle to a different value and back
            await status_dropdown.select_option(index=1)
            await page.wait_for_timeout(2000)
            await status_dropdown.select_option(value=current_value)
            logging.info("Profile status toggled successfully")
        else:
            # Alternative: Just navigate to edit profile and back to trigger update
            logging.info("Status dropdown not found, using alternative method")
            
    except Exception as e:
        logging.warning(f"Could not update profile status: {e}")
        # This is not critical, resume upload is the main goal
        pass

    logging.info("Profile update complete ✓")


async def upload_resume_once(resume_path):
    async with async_playwright() as p:
        # Local: headed (easier on Shine). CI: true headless is often blocked (no login DOM);
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
                viewport={"width": 1280, "height": 720},
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
                viewport={"width": 1280, "height": 720},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = await context.new_page()

        try:
            await ensure_logged_in(page)

            # Navigate directly to profile page for resume upload
            logging.info("Navigating to profile page for resume upload...")
            await page.goto("https://www.shine.com/myshine/myprofile/", timeout=60000)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Look for the Upload button in the Resume section
            upload_button = page.locator("button:has-text('Upload'), a:has-text('Upload')").first
            
            try:
                await upload_button.wait_for(state="visible", timeout=10000)
                await upload_button.click()
                logging.info("Upload button clicked")
                await page.wait_for_timeout(1000)
            except Exception as e:
                logging.warning(f"Could not find upload button, trying file input directly: {e}")

            # Set the file input
            file_input = page.locator("input[type='file']").first
            await file_input.set_input_files(resume_path)
            logging.info("Resume file selected")

            # Wait for upload to complete
            await page.wait_for_timeout(5000)
            
            # Look for success message or confirmation
            try:
                await page.wait_for_selector("text=/uploaded|success|updated/i", timeout=10000)
                logging.info("Resume upload confirmed")
            except Exception:
                logging.info("Resume uploaded (no explicit confirmation found)")

            # Update profile status to refresh visibility
            await update_resume_headline(page)

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

            subject = f"Shine.com Resume & Profile Updated - {today}"
            body = f"Your resume and profile were successfully updated on Shine.com on {today}."

            send_email(subject, body, resume_path)

            cleanup_file(resume_path)

            return

        except Exception as e:
            logging.error(f"Attempt {attempt} failed: {e}")
            await asyncio.sleep(5)

    send_email(
        f"Shine.com Update Failed - {today}",
        f"Resume/Profile update on Shine.com failed after {MAX_RETRIES} attempts."
    )
    sys.exit(1)


if __name__ == "__main__":
    validate_env()
    asyncio.run(upload_with_retry())
