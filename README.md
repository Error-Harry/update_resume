# Naukri Profile Auto-Updater

A Python automation script built with Playwright to automatically update your Naukri.com profile daily. The script performs a "no-op" update by adding and removing a dot (`.`) to your resume headline and re-uploading your resume. This tricks the Naukri algorithm into registering recent activity, bringing your profile to the top of recruiter searches.

The project is built using Python, Poetry (for dependency management), and Playwright, with a GitHub Action to automate the script every morning.

---

## Features
- **Headless automation**: Uses Playwright to interactively log in and bypass popups.
- **Smart updates**: Appends and removes a period (`.`) in the resume headline to cleanly simulate a profile modification without actually changing text.
- **Resume uploading**: Automatically uploads a given PDF resume to the portal.
- **Email notifications**: Sends a success/failure email report via SMTP.
- **CI/CD ready**: Pre-configured GitHub Actions workflow runs the script daily at 9:30 AM IST.

---

## Prerequisites
- Python 3.11+
- [Poetry](https://python-poetry.org/) package manager
- Chrome or Chromium browser

---

## 🛠 Local Setup

### 1. Clone & Install Dependencies
Navigate into your project folder and install the dependencies using Poetry:
```bash
poetry install
```

### 2. Install Playwright Browsers
Install the necessary binaries and dependencies for Playwright:
```bash
poetry run playwright install chromium --with-deps
```

### 3. Environment Variables
Create a `.env` file in the `naukari_bot/` directory (or use the main project root depending on how you run your script) with the following credentials:
```env
# Naukri Credentials
EMAIL=your_naukri_email@example.com
PASSWORD=your_naukri_password

# Email Notification Settings
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_EMAIL=your_sender_email@gmail.com
SMTP_PASSWORD=your_sender_app_password
TO_EMAIL=your_receiver_email@example.com
```
*Note: If using Gmail for SMTP, you must generate and use an **App Password**, not your standard account password.*

### 4. Running the Script locally
To run the project locally, execute:
```bash
poetry run python naukari_bot/main.py
```
*Locally, the script will run headed (the browser will open visually) so you can debug and watch it work. Running headless is automatically enabled for CI environments natively.*

---

## 🚀 GitHub Actions Setup (Daily Automation)

This repository includes a predefined GitHub Actions workflow (`.github/workflows/daily_update.yml`) designed to run this script automatically every day at exactly **9:30 AM IST**.

To enable this action:
1. Push your code to your GitHub repository.
2. Go to **Settings** > **Secrets and variables** > **Actions**.
3. Create a **New repository secret** for *each* of the following variables:
   - `NAUKRI_EMAIL`
   - `NAUKRI_PASSWORD`
   - `SMTP_EMAIL`
   - `SMTP_PASSWORD`
   - `SMTP_SERVER`
   - `SMTP_PORT`
   - `TO_EMAIL`
4. Make sure your actual `Harsh_Nargide.pdf` (or your relevant resume) is present in the `naukari_bot/` directory in the repository exactly as defined in your code.
5. The Github Action workflow supports `workflow_dispatch`. Go to the **Actions** tab in Github, click on **Daily Naukri Profile Update**, and click **Run workflow** to test it immediately.

---

## Troubleshooting
- **GitHub Action Fails**: Ensure your resume pdf is correctly pushed to GitHub (if not using .gitignore) or fetched from a storage unit, as the script expects it to exist to upload.
- **Timeouts**: Naukri occasionally changes object properties or adds new dynamic overlays (like popups or alerts). The script relies on robust waiting logic and Javascript bypasses, but DOM structure changes on Naukri may periodically require selector adjustments in `main.py` (`SAVE_BTN`, `TEXTAREA_ID`, etc.).
