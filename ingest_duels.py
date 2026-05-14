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
# Using yesterday's date for automation
RUN_DATE = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "data", "raw")

# S3 / Cloudflare R2 Config
S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_KEY = os.getenv("S3_KEY")
S3_SECRET = os.getenv("S3_SECRET")
BUCKET_NAME = "lorcana-raw-data"

def upload_to_s3(file_path, object_name):
    """Uploads file to Cloudflare R2 with specific folder prefix"""
    s3 = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_KEY,
        aws_secret_access_key=S3_SECRET,
        config=Config(signature_version='s3v4')
    )
    s3.upload_file(file_path, BUCKET_NAME, object_name)
    print(f"Successfully uploaded to: {object_name}")

def run_ingestion():
    # 1. Define and Create Local Subfolders
    subfolders = ["game-history", "game-logs", "game-replays"]
    for folder in subfolders:
        os.makedirs(os.path.join(DOWNLOAD_DIR, folder), exist_ok=True)

    with sync_playwright() as p:
        # Launch browser with a real-world User Agent
        context = p.chromium.launch_persistent_context(
            os.path.join(BASE_DIR, "playwright_session"),
            headless=IS_GITHUB,
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        
        # 2. Inject Authentication Cookie
        token = os.getenv("DISCORD_SESSION_TOKEN")
        if token:
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
        
        # 3. Robust Navigation
        print("Navigating to Duels...")
        page.goto("https://duels.ink/account", wait_until="domcontentloaded", timeout=60000)
        
        try:
            page.wait_for_selector('button:has-text("Export Game History")', timeout=20000)
            print("Login verified. Starting export...")
        except:
            print("Export button not found. Site might be down or token expired.")
            page.screenshot(path="debug_screen.png")
            raise

        # 4. Interaction
        page.get_by_role("button", name="Export Game History").click()
        page.wait_for_selector("text=Export Logs") 

        # Set Dates
        date_inputs = page.locator('input[type="date"]')
        date_inputs.first.fill(RUN_DATE)
        date_inputs.last.fill(RUN_DATE)

        # Filters
        page.get_by_label("Matchmaking", exact=True).check()
        page.get_by_label("Lobby", exact=True).check()
        time.sleep(1)
        page.get_by_label("All Queues", exact=True).uncheck()

        # Core Set Selection
        pattern = re.compile(r"Core Set .* BO[13]")
        for label in page.locator("label").all():
            text = label.inner_text()
            if pattern.search(text):
                page.get_by_label(text, exact=True).first.check()

        # 5. Download & Organized Upload
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
                    # Special handling to extract ZIP to CSV
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
                    # Logs and Replays stay as ZIPs
                    final_name = f"{folder_name}-{RUN_DATE}.zip"
                    final_path = os.path.join(DOWNLOAD_DIR, folder_name, final_name)
                    download.save_as(final_path)
                    upload_to_s3(final_path, f"raw/{folder_name}/{final_name}")
        else:
            print(f"No games found for {RUN_DATE}")

        context.close()

if __name__ == "__main__":
    run_ingestion()