"""扫描钉钉相关页面，找可用的品牌图标。"""
import io
import re
import subprocess
from pathlib import Path

from PIL import Image

RAW = Path(__file__).resolve().parent / "raw"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

PAGES = [
    "https://www.dingtalk.com/",
    "https://dingtalk.com/",
    "https://open-dev.dingtalk.com/",
]


def fetch(url: str) -> bytes:
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-A", UA, url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  页面抓取失败 {url}: rc={result.returncode}")
        return b""
    return result.stdout.encode("utf-8", "replace")


def probe(url: str) -> bool:
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-A", UA, "-o", str(RAW / "dingtalk.probe"), url],
        capture_output=True,
        text=True,
    )
    data = (RAW / "dingtalk.probe").read_bytes() if (RAW / "dingtalk.probe").exists() else b""
    if not data:
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            ok = w == h and 48 <= w <= 512
            print(f"    {img.format} {w}x{h} {len(data)}B {'✔' if ok else '✘'} {url[:90]}")
            return ok
    except Exception:
        return False


for page in PAGES:
    html = fetch(page)
    if not html:
        continue
    print(f"页面 {page} ({len(html)}B)")
    urls = set()
    for pattern in (r'src=["\']([^"\']+)', r'href=["\']([^"\']+)', r'url\(["\']?([^"\')]+)'):
        urls.update(re.findall(pattern, html.decode("utf-8", "replace"), re.I))
    for url in sorted(urls):
        if not re.search(r"\.(png|jpg|jpeg|webp|ico|gif)($|\?)", url, re.I):
            continue
        if any(bad in url for bad in ("sprite", "loader", "blank", ".js", ".css", "qr", "avatar")):
            continue
        if probe(url if url.startswith("http") else f"https://www.dingtalk.com{url}"):
            subprocess.run(
                ["cp", str(RAW / "dingtalk.probe"), str(RAW / "dingtalk.img")], check=True
            )
            print("  已保存 -> dingtalk.img")
            raise SystemExit(0)

print("没找到合适的钉钉图标")
