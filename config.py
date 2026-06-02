"""Centralised configuration loaded from environment variables."""
import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Google Sheets
SHEET_ID = os.environ["SHEET_ID"]
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "listings")
# Either a path to the service-account JSON file (local dev) ...
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
# ... or the JSON content itself in an env var (cloud hosting).
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Model used both for extraction and conversation.
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# Optional scraping fallback for when the sites block plain requests.
# Currently supports ZenRows (https://www.zenrows.com). Leave empty to disable.
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "")

# Hosts we treat as real-estate listings.
LISTING_HOSTS = ("realestate.com.au", "domain.com.au")
