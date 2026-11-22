"""外观（深色 / 浅色 / 跟随系统）的守卫测试。

主题是两端共用的一份实现（`frontend/src/lib/theme.ts`）：模式存在 localStorage 的同一个键上，
解析结果写在 `<html data-theme>`，CSS 靠 `:root`（深色兜底）+ `[data-theme='light']` 覆盖块换色。
两个入口 HTML 各有一段等价的内联脚本，负责在首屏渲染前把主题定下来 —— 这几处必须成套，
缺一处就是「浅色用户每次打开先闪一下深色外壳」。

这些断言不测视觉效果（那得靠人看），只把「成套」这件事固化下来：换主题的地方只有这几处。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
SRC = FRONTEND / "src"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")

#: 主题相关的公共出口：设置页、入口都从这里拿。
THEME_EXPORTS = (
    "THEME_STORAGE_KEY",
    "THEME_COLORS",
    "THEME_OPTIONS",
    "resolveTheme",
    "applyTheme",
    "initTheme",
    "useThemeMode",
)


@needs_frontend
def test_one_theme_module_serves_both_ends():
    """两端共用同一份主题实现，别再各写一套（否则深浅色会飘）。"""
    theme = (SRC / "lib" / "theme.ts").read_text(encoding="utf-8")
    for symbol in THEME_EXPORTS:
        assert symbol in theme, f"lib/theme.ts 缺少 {symbol}"

    # 三个选项就是用户能选的三种外观，文案中文。
    for label in ("深色", "浅色", "跟随系统"):
        assert f"'{label}'" in theme, f"外观选项缺少「{label}」"


@needs_frontend
def test_both_entries_apply_the_theme_before_first_paint():
    """两个入口都要：内联脚本（head 里、无依赖）+ 渲染前调用 initTheme()。"""
    for entry, main in (("index.html", "main.tsx"), ("m/index.html", "mobile/main.tsx")):
        html = (FRONTEND / entry).read_text(encoding="utf-8")
        assert "coda.theme" in html, f"{entry} 缺少首屏防闪的内联脚本"
        assert "dataset.theme" in html, f"{entry} 的内联脚本要写 <html data-theme>"
        assert html.index("coda.theme") < html.index("</head>"), f"{entry} 的内联脚本要在 head 里"

        entry_main = (SRC / main).read_text(encoding="utf-8")
        assert "initTheme()" in entry_main, f"{main} 要在渲染前调用 initTheme()"

    # 内联脚本与模块里的存储键、主题色、判定规则必须是同一套。
    theme = (SRC / "lib" / "theme.ts").read_text(encoding="utf-8")
    for token in ("coda.theme", "#0b0a12", "#f4f3fa", "prefers-color-scheme: light"):
        assert token in theme, f"lib/theme.ts 与内联脚本对不上：缺少 {token}"


@needs_frontend
def test_default_theme_follows_the_system():
    """没设置过时默认「跟随系统」，不是固定深色。

    这条要同时钉住模块与两段内联脚本：只改一处就会出现「首屏深色、React 起来后变浅色」
    的闪烁，或者反过来 —— 用户看到的默认外观和设置页选中的选项对不上。
    """
    theme = (SRC / "lib" / "theme.ts").read_text(encoding="utf-8")
    assert "return 'auto'" in theme, "readThemeMode() 没设置过时要返回 'auto'（跟随系统）"

    for entry in ("index.html", "m/index.html"):
        html = (FRONTEND / entry).read_text(encoding="utf-8")
        # 判定规则：只有显式选了深色才固定深色，其余（没设置过 / auto / light）都看系统。
        assert "mode !== 'dark'" in html, f"{entry} 的内联脚本没把「没设置过」当成跟随系统"
        assert "(mode === 'auto' &&" not in html, f"{entry} 的内联脚本仍把默认当成固定深色"


@needs_frontend
def test_both_stylesheets_carry_a_light_override():
    """浅色不是另一套样式表：`:root` 仍是深色兜底，浅色靠 `[data-theme='light']` 覆盖。"""
    for name in ("index.css", "mobile/mobile.css"):
        css = (SRC / name).read_text(encoding="utf-8")
        assert "color-scheme: dark" in css, f"{name} 要声明深色是 `:root` 兜底（原生控件跟着变）"
        assert "[data-theme='light']" in css, f"{name} 缺少 [data-theme='light'] 覆盖块"
        assert "color-scheme: light" in css, f"{name} 的浅色块要声明 color-scheme: light"
        # html 也要铺主题底色：系统预留的那一条露出来时才不会跟页面两个色。
        assert "html { background: var(--bg); }" in css, f"{name} 要给 html 铺主题底色"


@needs_frontend
def test_both_settings_pages_expose_the_appearance_picker():
    """设置页要有外观入口（两端各一份，交互各表、选项同一份）。"""
    for name in ("pages/SettingsPage.tsx", "mobile/pages/MobileSettingsPage.tsx"):
        page = (SRC / name).read_text(encoding="utf-8")
        assert "THEME_OPTIONS" in page and "useThemeMode" in page, f"{name} 缺少外观设置"
        assert "外观" in page, f"{name} 的可见文案要用「外观」"
