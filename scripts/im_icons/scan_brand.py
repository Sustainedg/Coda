"""重新扫描钉钉 / Slack 页面，存 HTML，按品牌主色挑选更合适的图标。"""
import io
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

RAW = Path(__file__).resolve().parent / "raw"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch(url: str) -> bytes:
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-A", UA, url],
        capture_output=True,
        text=True,
    )
    return result.stdout.encode("utf-8", "replace") if result.returncode == 0 else b""


def quantize(color):
    r, g, b, a = color
    return (r // 32 * 32, g // 32 * 32, b // 32 * 32)


def probe(url: str) -> dict | None:
    """下载候选并统计主色；返回信息或 None。"""
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "30", "-A", UA, "-o", str(RAW / "brand.probe"), url],
        capture_output=True,
        text=True,
    )
    data = (RAW / "brand.probe").read_bytes() if (RAW / "brand.probe").exists() else b""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGBA")
            w, h = img.size
            if not (48 <= w == h <= 512):
                return None
            opaque = [p for p in img.getdata() if p[3] > 128]
            if not opaque:
                return None
            counter = Counter(quantize(p) for p in opaque)
            top = counter.most_common(3)
            return {"url": url, "size": (w, h), "n": len(data), "top": top}
    except Exception:
        return None


def scan(name: str, page: str, want: str):
    html = fetch(page)
    if not html:
        print(f"[{name}] 页面抓取失败")
        return
    (RAW / f"{name}.page2.html").write_bytes(html)
    urls = set()
    for pattern in (r'(?:src|href)=["\']([^"\']+)', r'url\(["\']?([^"\')]+)'):
        urls.update(re.findall(pattern, html.decode("utf-8", "replace"), re.I))
    print(f"[{name}] 页面 {len(html)}B，候选 {len(urls)} 个，逐个探测…")
    for url in sorted(urls):
        if any(bad in url.lower() for bad in (".js", ".css", "sprite", "loader", "blank", "qr", "avatar", "logo-text")):
            continue
        full = url if url.startswith("http") else f"https://{page.split('/')[2]}{url}"
        info = probe(full)
        if not info:
            continue
        colors = ", ".join(f"#{r:02x}{g:02x}{b:02x}({c * 100 // len(opaque_n)}%)" if False else f"#{r:02x}{g:02x}{b:02x}({c})" for (r, g, b), c in info["top"])
        print(f"  {info['size']} {info['n']}B {colors} {full[:100]}")


# 钉钉：找蓝色系（品牌蓝）
scan("dingtalk", "https://www.dingtalk.com/", "blue")
# Slack：页面上所有方形候选
scan("slack", "https://pipedream.com/apps/slack-v2", "multi")
