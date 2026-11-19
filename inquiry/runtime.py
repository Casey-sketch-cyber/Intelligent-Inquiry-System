"""在加载第三方库之前，把本程序的运行数据固定到项目内的 D 盘目录。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
TEMP_DIR = CACHE_DIR / "tmp"
DOWNLOAD_DIR = DATA_DIR / "downloads"


def require_d_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.drive.upper() != "D:":
        raise RuntimeError(f"项目数据必须保存在 D 盘：{resolved}")
    return resolved


def configure_runtime() -> None:
    require_d_path(ROOT)
    paths = {
        "TEMP": TEMP_DIR, "TMP": TEMP_DIR, "TMPDIR": TEMP_DIR,
        "SQLITE_TMPDIR": TEMP_DIR,
        "PIP_CACHE_DIR": CACHE_DIR / "pip", "XDG_CACHE_HOME": CACHE_DIR,
        "PLAYWRIGHT_BROWSERS_PATH": CACHE_DIR / "playwright",
        "PYTHONPYCACHEPREFIX": CACHE_DIR / "pycache",
    }
    for path in {DATA_DIR, DOWNLOAD_DIR, *paths.values()}:
        require_d_path(path).mkdir(parents=True, exist_ok=True)
    for key, path in paths.items():
        os.environ[key] = str(path)
    tempfile.tempdir = str(TEMP_DIR)
    sys.pycache_prefix = str(paths["PYTHONPYCACHEPREFIX"])


configure_runtime()
