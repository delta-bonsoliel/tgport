import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Environment variable {key} is required")
    return val


BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")

ALLOWED_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in _require("ALLOWED_USER_IDS").split(",")
    if uid.strip()
}

CLAUDE_WORK_DIR: str = os.getenv("CLAUDE_WORK_DIR", os.path.expanduser("~"))
CLAUDE_MAX_TURNS: int = int(os.getenv("CLAUDE_MAX_TURNS", "3"))
CLAUDE_MAX_BUDGET_USD: float = float(os.getenv("CLAUDE_MAX_BUDGET_USD", "1.0"))
CLAUDE_SKIP_PERMISSIONS: bool = os.getenv("CLAUDE_SKIP_PERMISSIONS", "").lower() in ("1", "true", "yes")
DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", os.path.expanduser("~/workspace/assets/downloads"))
EDIT_INTERVAL: float = float(os.getenv("EDIT_INTERVAL", "1.5"))
RESPONSE_TIMEOUT: int = int(os.getenv("RESPONSE_TIMEOUT", "300"))
LOG_DIR: str = os.getenv("LOG_DIR", os.path.expanduser("~/workspace/projects/tgport/logs"))
COST_DISPLAY: str = os.getenv("COST_DISPLAY", "dollar")  # none / dollar / yen
LOG_RETENTION_DAYS: int = int(os.getenv("LOG_RETENTION_DAYS", "14"))
