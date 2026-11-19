"""检查 .env 里的 DeepSeek 密钥能否使用，并列出可用模型。不会打印密钥。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inquiry.runtime import configure_runtime
configure_runtime()
from dotenv import load_dotenv
from openai import OpenAI
load_dotenv(ROOT / ".env")

key = os.getenv("DEEPSEEK_API_KEY", "").strip()
base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
if not key:
    sys.exit("没有读到 DEEPSEEK_API_KEY，请检查 .env")
print(f"密钥已读取（长度 {len(key)}，前缀 {key[:3]}...），接口地址 {base_url}")

client = OpenAI(api_key=key, base_url=base_url)
models = client.models.list()
print("可用模型：", ", ".join(m.id for m in models.data))
