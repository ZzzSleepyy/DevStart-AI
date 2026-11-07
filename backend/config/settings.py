import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from backend/.env or project root .env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")


class Settings:
    """Application configuration loaded from environment variables."""

    APP_NAME: str = "DevStart AI Backend"
    APP_VERSION: str = "1.0.0"

    # API Keys & Credentials
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
    # GitHub API Configuration
    GITHUB_API_BASE: str = "https://api.github.com"
    GITHUB_TIMEOUT: int = int(os.getenv("GITHUB_TIMEOUT", "15"))

    # Batching & Safety Limits (per docs: analyze large repos in manageable batches)
    MAX_FILES_PER_BATCH: int = int(os.getenv("MAX_FILES_PER_BATCH", "16"))
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", "50000"))
    MAX_TOTAL_CODE_CHARS: int = int(os.getenv("MAX_TOTAL_CODE_CHARS", "60000"))


settings = Settings()
