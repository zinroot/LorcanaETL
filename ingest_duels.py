import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import boto3
from botocore.client import Config
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURATION ---
IS_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"
# Default to yesterday for automation; can be overridden for manual testing
RUN_DATE = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "data", "raw")

# S3 / Cloudflare R2 Config
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_KEY = os.getenv("S3_KEY")
S3_SECRET = os.getenv("S3_SECRET")
BUCKET_NAME = "lorcana-raw-data"

def upload_to_s3(file_path, object_name):
    """Uploads file to Cloudflare R2 with folder organization"""
    s3 = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        config=Config(signature_version='s3v4')
    )
    s3.upload_file(file_path, BUCKET_NAME, object_name)
    print(f"Successfully uploaded to: {object_name}")

def prepare_session():
    # Only unzip if we are in GitHub and the session folder doesn't exist yet
    if IS_GITHUB and os.path.exists("session.zip"):
        print("Extracting session.zip for Cloud environment...")
        # Path where Playwright expects the profile
        target_dir = os.path.join(BASE_DIR, "playwright_session")
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
            
        with zipfile.ZipFile("session.zip", 'r') as z:
            z.extractall(target_dir)
        print("Session extraction complete.")

def run_ingestion():
    prepare_session()
    # 1. Create Local Subfolders
    subfolders = ["game-history", "game-logs", "game-replays"]
    for folder in subfolders:
        os.makedirs(os.path.join(DOWNLOAD_DIR, folder), exist_ok=True)

    with sync_playwright() as p:
        # Launch browser with a real-world User Agent to avoid bot detection
        context = p.chromium.launch_persistent_context(
            os.path.join(BASE_DIR, "playwright_session"),
            headless=False,
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        
        # 2. BRUTE FORCE COOKIE INJECTION
        token = os.getenv("DISCORD_SESSION_TOKEN")
        if token:
            print("Injecting session tokens...")
            # We inject both variations to ensure NextAuth recognizes the session
            cookies = [
                {
                    'name': '__Secure-next-auth.session-token',
                    'value': token,
                    'domain': 'duels.ink',
                    'path': '/',
                    'secure': True,
                    'httpOnly': True,
                    'sameSite': 'Lax'
                },
                {
                    'name': 'next-auth.session-token',
                    'value': token,
                    'domain': 'duels.ink',
                    'path': '/',
                    'secure': True,
                    'httpOnly': True,
                    'sameSite': 'Lax'
                }
            ]
            context.add_cookies(cookies)

        page = context.new_page()
        
        # 3. STRATEGIC NAVIGATION
        # Go to base domain first so the browser 'activates' the cookie context
        print("Navigating to base domain...")
        page.goto("https://duels.ink", wait_until="commit")
        time.sleep(2) # Brief pause for cookie propagation
        
        print(f"Navigating to account page for date: {RUN_DATE}")
        page.goto("https://duels.ink/account", wait_until="domcontentloaded", timeout=60000)
        
        try:
            # Look for the export button specifically
            page.wait_for_selector('button:has-text("Export Game History")', timeout=20000)
            print("Auth successful! Export button found.")
        except:
            print("ERROR: Export button not found. Browser is likely at Sign-In screen.")
            page.screenshot(path="debug_screen.png")
            # If we're in GitHub Actions, this will trigger the 'failure' step and upload the artifact
            raise Exception("Authentication failed - Export button not visible.")

        # 4. INTERACTION & EXPORT
        page.get_by_role("button", name="Export Game History").click()
        page.wait_for_selector("text=Export Logs") 

        # Set Dates for the export
        date_inputs = page.locator('input[type="date"]')
        date_inputs.first.fill(RUN_DATE)
        date_inputs.last.fill(RUN_DATE)

        # Filters
        page.get_by_label("Matchmaking", exact=True).check()
        page.get_by_label("Lobby", exact=True).check()
        time.sleep(1)
        page.get_by_label("All Queues", exact=True).uncheck()

        # Selection logic for Core Sets
        pattern = re.compile(r"Core Set .* BO[13]")
        for label in page.locator("label").all():
            text = label.inner_text()
            if pattern.search(text):
                page.get_by_label(text, exact=True).first.check()

        # 5. DOWNLOAD & ORGANIZED UPLOAD
        if page.get_by_role("button", name="Download CSV").is_enabled():
            download_map = {
                "Export Logs": "game-logs",
                "Export Replays": "game-replays",
                "Download CSV": "game-history"
            }

            for btn_name, folder_name in download_map.items():
                print(f"Downloading {btn_name}...")
                with page.expect_download() as d_info:
                    page.get_by_role("button", name=btn_name).click()
                download = d_info.value
                
                if folder_name == "game-history":
                    # Extract ZIP directly into CSV
                    temp_zip = os.path.join(DOWNLOAD_DIR, folder_name, "tmp.zip")
                    download.save_as(temp_zip)
                    with zipfile.ZipFile(temp_zip, 'r') as z:
                        internal_csv = [f for f in z.namelist() if f.endswith('.csv')][0]
                        target_dir = os.path.join(DOWNLOAD_DIR, folder_name)
                        z.extract(internal_csv, target_dir)
                        
                        final_name = f"game-history-{RUN_DATE}.csv"
                        final_path = os.path.join(target_dir, final_name)
                        if os.path.exists(final_path): os.remove(final_path)
                        os.rename(os.path.join(target_dir, internal_csv), final_path)
                        
                        upload_to_s3(final_path, f"raw/game-history/{final_name}")
                    os.remove(temp_zip)
                else:
                    # Logs and Replays stay as ZIP archives
                    final_name = f"{folder_name}-{RUN_DATE}.zip"
                    final_path = os.path.join(DOWNLOAD_DIR, folder_name, final_name)
                    download.save_as(final_path)
                    upload_to_s3(final_path, f"raw/{folder_name}/{final_name}")
        else:
            print(f"No games found to download for {RUN_DATE}")

        context.close()

if __name__ == "__main__":
    run_ingestion()