# github_backup.py
import os
import requests
from datetime import datetime

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
DB_PATH = "main.db"

API_BASE = "https://api.github.com"


def _headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def download_latest_db():
    """最新のReleaseから main.db をダウンロード"""
    print("🌀 GitHub Releases から main.db を取得中...")

    releases_url = f"{API_BASE}/repos/{GITHUB_REPO}/releases/latest"
    r = requests.get(releases_url, headers=_headers())
    if r.status_code != 200:
        print(f"⚠️ Release取得失敗: {r.status_code} {r.text}")
        return False

    release = r.json()
    assets = release.get("assets", [])
    db_asset = next((a for a in assets if a["name"] == "main.db"), None)

    if not db_asset:
        print("⚠️ main.db が Release に見つかりません。")
        return False

    download_url = db_asset["browser_download_url"]
    r = requests.get(download_url, headers=_headers())
    with open(DB_PATH, "wb") as f:
        f.write(r.content)

    print("✅ main.db をダウンロード完了。")
    return True


def upload_db_to_release():
    """main.db を Release にアップロード（バックアップ）"""
    print("📤 main.db を GitHub Release にアップロード中...")

    tag_name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    create_url = f"{API_BASE}/repos/{GITHUB_REPO}/releases"

    data = {
        "tag_name": tag_name,
        "name": f"Backup {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "body": "Automatic database backup",
        "draft": False,
        "prerelease": False,
    }

    r = requests.post(create_url, headers=_headers(), json=data)
    if r.status_code not in (200, 201):
        print(f"⚠️ Release作成失敗: {r.status_code} {r.text}")
        return False

    upload_url = r.json()["upload_url"].split("{")[0] + f"?name={DB_PATH}"
    with open(DB_PATH, "rb") as f:
        r = requests.post(upload_url, headers={**_headers(), "Content-Type": "application/octet-stream"}, data=f)

    if r.status_code in (200, 201):
        print("✅ main.db を Release にアップロード完了。")
        return True
    else:
        print(f"⚠️ アップロード失敗: {r.status_code} {r.text}")
        return False
