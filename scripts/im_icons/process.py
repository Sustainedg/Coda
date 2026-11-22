"""把下载的原始图标处理成统一规格：裁透明边、补正方形、256×256 WebP q90。

输出到 frontend/public/icons/im/，与内核图标同一套约定（正方形、透明底、
随仓库发布、运行时不外链）。
"""
import io
import sys
from pathlib import Path

from PIL import Image

RAW = Path(__file__).resolve().parent / "raw"
OUT = Path(__file__).resolve().parents[2] / "frontend" / "public" / "icons" / "im"
OUT.mkdir(parents=True, exist_ok=True)

SIZE = 256


def square_fit(img: Image.Image) -> Image.Image:
    """裁掉透明边，再等比放进正方形底（透明补边），居中。"""
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    offset = ((side - w) // 2, (side - h) // 2)
    canvas.paste(img, offset)
    return canvas.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    for path in sorted(RAW.glob("*.img")):
        name = path.stem
        with Image.open(path) as src:
            out = square_fit(src)
        dest = OUT / f"{name}.webp"
        out.save(dest, "WEBP", quality=90)
        print(f"{name}: {src.size} -> {SIZE}x{SIZE} {dest.stat().st_size}B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
