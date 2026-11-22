"""抓取 Slack / 钉钉页面图标（复用 download.py 的提取逻辑）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download import PAGES, RAW, fetch, pick_image_from_page  # noqa: E402

for name, page_url in PAGES.items():
    dest = RAW / f"{name}.img"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[skip] {name} 已有 {dest.stat().st_size} bytes")
        continue
    url = pick_image_from_page(name, page_url)
    data = fetch(url, dest)
    print(f"[page] {name}: {len(data)} bytes <- {url}")
