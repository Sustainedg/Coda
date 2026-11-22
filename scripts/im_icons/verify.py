"""校验成品图标：非透明像素占比 + 主色调，确认不是空白/错图。"""
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parents[2] / "frontend" / "public" / "icons" / "im"

BRAND_HINT = {
    "telegram": "蓝",
    "feishu": "蓝",
    "dingtalk": "蓝",
    "wecom": "蓝",
    "weixin": "绿",
    "qq": "蓝",
    "discord": "紫/白",
    "slack": "多彩",
}


def quantize(color):
    r, g, b, a = color
    return (r // 32 * 32, g // 32 * 32, b // 32 * 32)


for path in sorted(OUT.glob("*.webp")):
    with Image.open(path) as img:
        img = img.convert("RGBA")
        pixels = list(img.getdata())
        opaque = [p for p in pixels if p[3] > 128]
        ratio = len(opaque) / len(pixels)
        counter = Counter(quantize(p) for p in opaque)
        top = ", ".join(
            f"#{r:02x}{g:02x}{b:02x}({n * 100 // len(opaque)}%)"
            for (r, g, b), n in counter.most_common(3)
        )
        hint = BRAND_HINT.get(path.stem, "")
        print(f"{path.name}: 不透明占比 {ratio:.0%}  主色 {top}  期望: {hint}")
