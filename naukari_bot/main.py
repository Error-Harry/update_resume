import asyncio
import os
import shutil
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
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ================== UTIL ==================

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

        logging.info("Email sent successfully")

    except Exception as e:
        logging.error(f"Email failed: {e}")


# ================== CORE ==================

async def login(page):
    await page.goto("https://www.naukri.com/nlogin/login", timeout=60000)
    await page.wait_for_selector("input[type='password']", timeout=20000)

    inputs = await page.query_selector_all("input")

    email_input = None
    password_input = None

    for i, inp in enumerate(inputs):
        if await inp.get_attribute("type") == "password":
            password_input = inp
            if i > 0:
                email_input = inputs[i - 1]
            break

    if not email_input or not password_input:
        raise Exception("Login inputs not found")

    await email_input.fill(EMAIL)
    await password_input.fill(PASSWORD)

    await page.click("button[type='submit']")
    await page.wait_for_selector("text=View profile", timeout=20000)


async def update_resume_headline(page):
    logging.info("Updating resume headline...")

    # Step 1: Locate Resume Headline section container
    section = page.locator("text=Resume headline").first

    # Step 2: Move up to parent container
    container = section.locator("xpath=ancestor::div[1]")

    # Step 3: Click inside container (this triggers edit modal)
    await container.click()

    # Step 4: Wait for modal textarea (increase timeout)
    await page.wait_for_selector("textarea", timeout=20000)

    textarea = page.locator("textarea").first
    current_text = await textarea.input_value()

    # Step 5: Add dot
    await textarea.fill(current_text + ".")
    await page.locator("button:has-text('Save')").click()

    await page.wait_for_timeout(3000)

    # Step 6: Remove dot
    await page.wait_for_selector("textarea", timeout=20000)
    textarea = page.locator("textarea").first

    await textarea.fill(current_text)
    await page.locator("button:has-text('Save')").click()

    logging.info("Headline updated successfully")

async def upload_resume_once(resume_path):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
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

            # 🔥 NEW STEP: Update headline
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


if __name__ == "__main__":
    asyncio.run(upload_with_retry())