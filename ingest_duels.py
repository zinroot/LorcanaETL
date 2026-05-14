import os
import re
import time
import zipfile
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import boto3
from botocore.client import Config
from dotenv import load_dotenv

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "playwright_session")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "data", "raw")
load_dotenv()

# Date configuration
YESTERDAY = "2026-05-12" 
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "playwright_session")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "data", "raw")

def prepare_session():
    """Unzips the session.zip if we are running in GitHub Actions"""
    if os.path.exists("session.zip") and not os.path.exists(USER_DATA_DIR):
        print("Extracting Discord session...")
        with zipfile.ZipFile("session.zip", 'r') as zip_ref:
            zip_ref.extractall(BASE_DIR)

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_KEY = os.getenv("S3_KEY")
S3_SECRET = os.getenv("S3_SECRET")
BUCKET_NAME = "lorcana-raw-data"
print(S3_KEY)
print(S3_SECRET)
print(S3_ENDPOINT)

def upload_to_s3(file_path, object_name):
    s3 = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        config=Config(signature_version='s3v4')
    )
    s3.upload_file(file_path, BUCKET_NAME, object_name)
    print(f"Uploaded {object_name} to R2 storage.")

def run_ingestion():
    prepare_session()
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    with sync_playwright() as p:
        is_github = os.getenv("GITHUB_ACTIONS") == "true"
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=is_github,
            accept_downloads=True,
            # This makes the GitHub runner look like a standard Windows Chrome user
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        
        # --- NEW: INJECT AUTH COOKIE ---
        token = os.getenv("DISCORD_SESSION_TOKEN")
        if token and is_github:
            print("Injecting session token...")
            context.add_cookies([{
                'name': '__Secure-next-auth.session-token',
                'value': token,
                'domain': 'duels.ink',
                'path': '/',
                'secure': True,
                'httpOnly': True,
                'sameSite': 'Lax'
            }])

        page = context.new_page()
        # Increase timeout to 60 seconds and wait for the basic page load
        page.goto("https://duels.ink/account", wait_until="domcontentloaded", timeout=60000)

        # Debugging: Take a screenshot if it fails so you can see what the bot sees
        try:
            page.wait_for_selector('text=Export Game History', timeout=15000)
        except:
            print("Export button not found. Taking debug screenshot...")
            page.screenshot(path="debug_screen.png")
            # This screenshot will be available in GitHub Action "Artifacts" if it fails
            raise

        # Manually wait for the specific button we need
        print("Waiting for the Export button to appear...")
        page.wait_for_selector('role=button[name="Export Game History"]', timeout=30000)
        # 1. Open Export Modal
        page.get_by_role("button", name="Export Game History").click()
        page.wait_for_selector("text=Export Logs") 

        # 2. Set Dates
        date_inputs = page.locator('input[type="date"]')
        date_inputs.first.fill(YESTERDAY)
        date_inputs.last.fill(YESTERDAY)

        # 3. Selections
        page.get_by_label("Matchmaking", exact=True).check()
        page.get_by_label("Lobby", exact=True).check()
        time.sleep(1) 

        # Reset Queues
        all_queues = page.get_by_label("All Queues", exact=True)
        if all_queues.is_visible():
            all_queues.uncheck()

        # Select Core Sets
        pattern = re.compile(r"Core Set .* BO[13]")
        labels = page.locator("label").all()
        for label in labels:
            text = label.inner_text()
            if pattern.search(text):
                page.get_by_label(text, exact=True).first.check()
            elif text not in ["Matchmaking", "Lobby", "Direct"]:
                target_cb = page.get_by_label(text, exact=True).first
                if target_cb.is_visible() and target_cb.is_checked():
                    target_cb.uncheck()

        # 4. Downloads Logic
        if page.get_by_role("button", name="Download CSV").is_enabled():
            
            # Map button names to the core filenames
            download_map = {
                "Export Logs": "game-logs",
                "Export Replays": "game-replays",
                "Download CSV": "game-history"
            }

            for btn_name, base_name in download_map.items():
                print(f"Downloading {btn_name}...")
                with page.expect_download() as download_info:
                    page.get_by_role("button", name=btn_name).click()
                
                download = download_info.value
                
                if base_name == "game-history":
                    # Download the zip, extract the CSV, name it game-history-DATE.csv
                    temp_zip = os.path.join(DOWNLOAD_DIR, f"game-history-{YESTERDAY}.csv")
                    download.save_as(temp_zip)
                    upload_to_s3(save_path, f"game-history-{YESTERDAY}")
                    print(f"Saved: {base_name}-{YESTERDAY}.csv")
                
                else:
                    # Save logs and replays as game-type-DATE.zip
                    final_filename = f"{base_name}-{YESTERDAY}.zip"
                    save_path = os.path.join(DOWNLOAD_DIR, final_filename)
                    if os.path.exists(save_path): os.remove(save_path)
                    download.save_as(save_path)
                    upload_to_s3(save_path, f"raw/{final_filename}")
                    print(f"Saved: {final_filename}")

        context.close()

if __name__ == "__main__":
    run_ingestion()