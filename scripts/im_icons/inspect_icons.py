"""检查 raw 目录里已下载图标的尺寸和格式。"""
import io
import sys
from pathlib import Path

from PIL import Image

RAW = Path(__file__).resolve().parent / "raw"

for path in sorted(RAW.glob("*.img")):
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as img:
            print(f"{path.name}: {img.format} {img.size} mode={img.mode} {len(data)}B")
    except Exception as exc:  # noqa: BLE001
        print(f"{path.name}: 无法识别 {exc} ({len(data)}B)")
