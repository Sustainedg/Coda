#!/usr/bin/env python3
"""生成应用图标：母版是用户提供的品牌图 ``assets/brand/logo.png``。

母版是一张正方形品牌图（圆角外已抠透明），本脚本只负责把它派生成各尺寸图标，
**不自行绘制图形** —— 品牌长什么样以母版为准，避免「脚本画出来的和用户给的不一样」。

产物：
  frontend/public/icons/icon-192.png               任意尺寸（圆角由平台裁）
  frontend/public/icons/icon-512.png               任意尺寸
  frontend/public/icons/icon-maskable-512.png      满幅（母版本身即满幅方块）
  frontend/public/icons/apple-touch-icon.png       满幅，iOS 自己倒角
  frontend/public/icons/icon.svg                   内嵌母版位图的矢量外壳

``icon.svg`` 用 ``<image>`` 内嵌母版 PNG：浏览器任意缩放外壳都锐利，
而图形与 PNG 图标是**同一张图**，不会出现「网页图标和桌面图标长得不一样」。

用法：python3 scripts/gen_icons.py
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "assets" / "brand"
MASTER_PATH = BRAND_DIR / "logo.png"
OUT_DIR = REPO_ROOT / "frontend" / "public" / "icons"

#: SVG 里内嵌的位图尺寸：256 已是 2 倍过采样，足够浏览器缩放。
SVG_EMBED_SIZE = 256


def load_master() -> Image.Image:
    """读取品牌母版（正方形、圆角外透明）。"""
    if not MASTER_PATH.is_file():
        raise FileNotFoundError(
            f"缺少品牌母版 {MASTER_PATH}：把用户提供的 logo 图放到这里再运行本脚本"
        )
    master = Image.open(MASTER_PATH).convert("RGBA")
    if master.size[0] != master.size[1]:
        raise ValueError(f"品牌母版必须是正方形，实际是 {master.size}")
    return master


def encode_png(width: int, height: int, body: bytes) -> bytes:
    """RGBA 字节 → PNG 字节。"""
    image = Image.frombytes("RGBA", (width, height), body)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_icon(size: int, *, rounded: bool = True, fill: float = 1.0) -> bytes:
    """把母版缩放成 ``size`` 的 RGBA 字节。

    ``rounded`` / ``fill`` 为兼容旧调用保留：母版本身就是设计好的满幅方块，
    缩放即得，不再做圆角或缩放图形的二次加工。
    """
    master = load_master().resize((size, size), Image.LANCZOS)
    return master.tobytes()


def render_silhouette(size: int, color: tuple[int, int, int]) -> bytes:
    """用母版的轮廓（alpha 通道）生成单色剪影，供托盘模板使用。"""
    master = load_master().resize((size, size), Image.LANCZOS)
    alpha = master.getchannel("A")
    silhouette = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    silhouette.putalpha(alpha)
    # 用纯色填充轮廓内部
    solid = Image.new("RGBA", (size, size), (*color, 255))
    solid.putalpha(alpha)
    return solid.tobytes()


def _write_png(path: Path, size: int, body: bytes) -> None:
    """把 RGBA 字节写成 PNG 文件。"""
    path.write_bytes(encode_png(size, size, body))


def build_svg(size: int = 512) -> str:
    """生成内嵌母版位图的 SVG（外壳矢量、图形位图，与 PNG 图标同源）。

    不再在下面垫一块深色矩形：母版本身已经是「圆角外透明」的完整品牌图，
    透明区域应当保持透明；垫底色会让浏览器标签页 / PWA 外框变成深色方块。
    """
    embed = load_master().resize((SVG_EMBED_SIZE, SVG_EMBED_SIZE), Image.LANCZOS)
    buffer = io.BytesIO()
    embed.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}">\n'
        f'  <image href="{data_url}" x="0" y="0" width="{size}" height="{size}"/>\n'
        f'</svg>\n'
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in (
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        _write_png(OUT_DIR / name, size, render_icon(size))
        print(f"→ {OUT_DIR / name} ({size}x{size})")
    (OUT_DIR / "icon.svg").write_text(build_svg(), encoding="utf-8")
    print(f"→ {OUT_DIR / 'icon.svg'}")


if __name__ == "__main__":
    main()
