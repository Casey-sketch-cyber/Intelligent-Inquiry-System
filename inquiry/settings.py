"""路径与配置：settings.toml 放可调参数，.env 放密钥。"""
import os
import tomllib

from dotenv import load_dotenv

from .runtime import DATA_DIR, ROOT

load_dotenv(ROOT / ".env")

_toml = ROOT / "settings.toml"
SETTINGS = tomllib.loads(_toml.read_text(encoding="utf-8")) if _toml.exists() else {}

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
