"""尝试多个来源抓取钉钉图标。"""
import io
import subprocess
from pathlib import Path

from PIL import Image

RAW = Path(__file__).resolve().parent / "raw"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

CANDIDATES = [
    "https://logo.clearbit.com/dingtalk.com",
    "https://logo.clearbit.com/www.dingtalk.com",
    "https://dingtalk.com/favicon.ico",
    "https://www.dingtalk.com/favicon.ico",
]


def try_url(url: str) -> bool:
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-A", UA, "-o", str(RAW / "dingtalk.img"), url],
        capture_output=True,
        text=True,
    )
    data = (RAW / "dingtalk.img").read_bytes() if (RAW / "dingtalk.img").exists() else b""
    if not data:
        print(f"  {url} -> 空 (rc={result.returncode})")
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            print(f"  {url} -> {img.format} {w}x{h} {len(data)}B")
            return w == h and 48 <= w <= 512
    except Exception as exc:  # noqa: BLE001
        print(f"  {url} -> 非图片: {exc} ({len(data)}B)")
        return False


for url in CANDIDATES:
    if try_url(url):
        print(f"成功: {url}")
        break
