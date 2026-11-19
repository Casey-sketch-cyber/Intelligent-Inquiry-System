"""路径与配置：settings.toml 放可调参数，.env 放密钥。"""
import os
import tomllib

from dotenv import load_dotenv

from .runtime import DATA_DIR, ROOT

load_dotenv(ROOT / ".env")

SETTINGS_PATH = ROOT / "settings.toml"


def load_settings(path=None):
    try:
        return tomllib.loads((path or SETTINGS_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


SETTINGS = load_settings()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
