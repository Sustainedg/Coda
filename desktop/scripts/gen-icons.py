#!/usr/bin/env python3
"""生成桌面壳图标：应用图标（icns / ico / png）与托盘图标。

图形与网页图标是**同一套渲染器**：直接复用主项目 ``scripts/gen_icons.py`` 里的
``render_icon`` / ``render_silhouette``，改品牌图形时两边一起变，不会出现「桌面端
图标和网页不一样」。

产物：
  build/icon.icns                macOS 应用图标（16→1024，内嵌 PNG）
  build/icon.ico                 Windows 应用图标（16→256，PNG 压缩条目）
  build/icon.png                 Linux / Electron 窗口图标（512）
  resources/icons/trayTemplate.png (+ @2x)   macOS 托盘：模板图（纯黑 + alpha，系统自动适配明暗）
  resources/icons/tray-win.png (+ @2x)       Windows 托盘：全彩品牌图
  resources/icons/tray.png       Linux 托盘：品牌色剪影
  resources/icons/icon.png       窗口图标（Windows 任务栏 / Linux，随 files 打进 asar）

用法：python3 desktop/scripts/gen-icons.py
"""
from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # 仓库根
DESKTOP = ROOT / "desktop"
BUILD = DESKTOP / "build"
TRAY_DIR = DESKTOP / "resources" / "icons"

#: 复用网页图标生成器（母版位图 + Pillow，与其它图标脚本同一套工具链）
_spec = importlib.util.spec_from_file_location("coda_icons", ROOT / "scripts" / "gen_icons.py")
icons = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(icons)

#: Windows / Linux 托盘的品牌色（取自品牌图的主蓝，加深一档保证托盘可读）
TRAY_COLOR = (0x4E, 0x5A, 0xA2)


def png_bytes(size: int, body: bytes) -> bytes:
    """RGBA 字节 → PNG 字节。"""
    return icons.encode_png(size, size, body)


def write_ico(path: Path, entries: list[tuple[int, bytes]]) -> None:
    """PNG 压缩条目的 ICO（Vista+ 支持，现代 Windows 图标的标准做法）。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    payload = b""
    for size, png in entries:
        dim = 0 if size >= 256 else size  # 0 表示 256
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    path.write_bytes(header + directory + payload)
    print(f"→ {path.relative_to(ROOT)} ({[s for s, _ in entries]})")


def write_icns(path: Path, entries: list[tuple[bytes, bytes]]) -> None:
    """PNG 条目的 ICNS（macOS 10.7+ 支持，electron-builder 也接受）。"""
    chunks = b""
    for kind, png in entries:
        chunks += kind + struct.pack(">I", len(png) + 8) + png
    path.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)
    print(f"→ {path.relative_to(ROOT)} ({len(entries)} sizes)")


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    TRAY_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 应用图标 ----
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    write_ico(
        BUILD / "icon.ico",
        [(s, png_bytes(s, icons.render_icon(s, rounded=True, fill=0.80))) for s in ico_sizes],
    )

    # ICNS 的 PNG 类型：icp4=16, icp5=32, icp6=64, ic07=128, ic08=256, ic09=512, ic10=1024
    icns_map = [
        (b"icp4", 16),
        (b"icp5", 32),
        (b"icp6", 64),
        (b"ic07", 128),
        (b"ic08", 256),
        (b"ic09", 512),
        (b"ic10", 1024),
    ]
    write_icns(
        BUILD / "icon.icns",
        [(kind, png_bytes(size, icons.render_icon(size, rounded=True, fill=0.80))) for kind, size in icns_map],
    )
    (BUILD / "icon.png").write_bytes(png_bytes(512, icons.render_icon(512, rounded=True, fill=0.80)))
    print(f"→ {(BUILD / 'icon.png').relative_to(ROOT)} (512x512)")

    # ---- 托盘图标（resources/icons，随 files 打包） ----
    # macOS 模板图必须是**纯黑 + alpha**（系统按菜单栏明暗重新着色），
    # 名字带 ``Template`` 才会被当成模板；Linux 用品牌色剪影。
    for name, size, template in (
        ("trayTemplate.png", 16, True),
        ("trayTemplate@2x.png", 32, True),
        ("tray.png", 32, False),
        ("tray@2x.png", 64, False),
    ):
        color = (0, 0, 0) if template else TRAY_COLOR
        (TRAY_DIR / name).write_bytes(
            png_bytes(size, icons.render_silhouette(size, color))
        )
        print(f"→ {(TRAY_DIR / name).relative_to(ROOT)} ({size}x{size})")

    # Windows 托盘用**全彩品牌图**：Windows 任务栏不做模板着色，单色剪影在高分屏
    # 缩放后只剩一团纯色，辨识度差；直接缩放母版，和网页 / 应用图标同一张图。
    # 带 ``@2x`` 的变体供高 DPI（125%/150%/200%）使用，Electron 会按缩放比例自动挑选。
    for name, size in (("tray-win.png", 32), ("tray-win@2x.png", 64)):
        (TRAY_DIR / name).write_bytes(png_bytes(size, icons.render_icon(size)))
        print(f"→ {(TRAY_DIR / name).relative_to(ROOT)} ({size}x{size})")
    # 窗口图标（Windows 任务栏 / Linux）：随 files 打进 asar，运行时用 iconsDir()/icon.png 取
    (TRAY_DIR / "icon.png").write_bytes(png_bytes(256, icons.render_icon(256, rounded=True, fill=0.80)))
    print(f"→ {(TRAY_DIR / 'icon.png').relative_to(ROOT)} (256x256)")


if __name__ == "__main__":
    main()
