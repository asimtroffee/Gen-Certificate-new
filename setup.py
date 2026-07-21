"""
One-time setup: downloads Great Vibes font and creates a sample config.
Run: python setup.py
"""

import os
import json
import urllib.request
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "backend", "fonts")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/greatvibes/GreatVibes-Regular.ttf"
FONT_FILE = "GreatVibes-Regular.ttf"


INTER_FONT_URL = "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bslnt%2Cwght%5D.ttf"
INTER_FONT_FILE = "Inter-Regular.ttf"

def download_font():
    os.makedirs(FONTS_DIR, exist_ok=True)
    
    fonts_to_download = [
        (FONT_URL, FONT_FILE),
        (INTER_FONT_URL, INTER_FONT_FILE)
    ]

    for url, filename in fonts_to_download:
        target = os.path.join(FONTS_DIR, filename)

        if os.path.exists(target) and os.path.getsize(target) > 1000:
            print(f"[OK] Font {filename} already exists")
            continue

        print(f"[..] Downloading {filename}...")
        try:
            urllib.request.urlretrieve(url, target)
            size = os.path.getsize(target)
            if size > 1000:
                print(f"[OK] Downloaded {filename} ({size} bytes)")
            else:
                print(f"[!!] File too small ({size} bytes), download may have failed")
        except Exception as e:
            print(f"[!!] Could not download {filename}: {e}")
            print(f"[!!] Manually place {filename} in backend/fonts/")


def create_sample_config():
    os.makedirs(FRONTEND_DIR, exist_ok=True)
    config_path = os.path.join(FRONTEND_DIR, "certificate-config.json")

    if os.path.exists(config_path):
        print("[OK] Config already exists")
        return

    config = {
        "templateWidth": 1920,
        "templateHeight": 1080,
        "text": {
            "x": 50.0,
            "y": 45.0,
            "fontFamily": "Great Vibes",
            "fontSize": 72,
            "fontColor": "#1a1a1a",
            "textAlign": "center",
        },
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print("[OK] Sample config created")


if __name__ == "__main__":
    download_font()
    create_sample_config()
    print()
    print("Next steps:")
    print("  1. Place your certificate template as backend/templates/template.png")
    print("  2. pip install -r backend/requirements.txt")
    print("  3. python -m uvicorn backend.main:app --reload --port 8000")
