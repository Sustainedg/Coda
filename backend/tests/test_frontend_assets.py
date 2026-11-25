"""前端静态资源的守卫测试：应用图标 / 内核 Logo / 头像包。

内核 Logo（`frontend/public/icons/kernels/*`）的约定：图形是手工/厂商产物（不生成），
但**必须由适配器声明**（`AgentDefinition.logo`，`logo_style` 决定遮罩还是原色），
且两张运行时页都只留「Logo · 内核名 · 公司 · 版本」。
"""
from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
MOBILE_SRC = FRONTEND / "src" / "mobile"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")


# --- 运行时卡片：内核 Logo 与精简后的文案 -------------------------------------

KERNEL_LOGO_DIR = REPO / "frontend" / "public" / "icons" / "kernels"


SVG_SUFFIX = ".svg"
RASTER_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
MAX_RASTER_BYTES = 256 * 1024
MAX_RASTER_SIDE = 512


@needs_frontend
def test_every_kernel_declares_a_logo_that_is_committed():
    """每个内核都要有官方 Logo，且声明的图形文件真的在仓库里（界面按路径直接取）。

    两种形态：
    * ``logo_style = "mask"``（默认）—— 24×24 单色 SVG，界面当遮罩染色；
    * ``logo_style = "image"`` —— 真彩色位图（PNG/WebP），按原色显示。
    """
    from app.agents import all_agents

    for definition in all_agents():
        logo = definition.logo
        assert logo, f"{definition.id} 未声明 logo"
        assert logo.startswith("/icons/kernels/"), (
            f"{definition.id} 的 logo 应指向 /icons/kernels/*，实际是 {logo}"
        )
        path = KERNEL_LOGO_DIR / logo.rsplit("/", 1)[-1]
        assert path.is_file(), f"{definition.id} 声明的 Logo 文件不存在：{path}"
        assert definition.logo_style in ("mask", "image"), (
            f"{definition.id} 的 logo_style 只能是 mask 或 image"
        )
        if path.suffix == SVG_SUFFIX:
            text = path.read_text(encoding="utf-8")
            assert 'viewBox="0 0 24 24"' in text, f"{path.name} 缺少统一画布（24×24）"
            assert "<path" in text, f"{path.name} 里没有图形"
            continue

        assert path.suffix in RASTER_SUFFIXES, f"{path.name} 不是受支持的图片格式"
        assert definition.logo_style == "image", (
            f"{definition.id} 用的是位图 Logo，必须声明 logo_style = \"image\""
        )
        blob = path.read_bytes()
        width, height = _raster_size(path.name, blob)
        assert width == height and width <= MAX_RASTER_SIDE, (
            f"{path.name} 应为正方形且不超过 {MAX_RASTER_SIDE}px，实际 {width}×{height}"
        )
        assert len(blob) <= MAX_RASTER_BYTES, (
            f"{path.name} 超过 {MAX_RASTER_BYTES // 1024} KB，请压缩后再提交"
        )


def _raster_size(name: str, blob: bytes) -> tuple[int, int]:
    """读位图的宽高：只支持仓库里用到的 PNG / WebP（不引第三方依赖）。"""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", blob[16:24])
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        kind = blob[12:16]
        if kind == b"VP8X":  # 扩展格式：24 位小端宽高（减一）在 24..30
            width = int.from_bytes(blob[24:27], "little") + 1
            height = int.from_bytes(blob[27:30], "little") + 1
            return width, height
        if kind == b"VP8 ":  # 有损：帧头 14 位小端宽高
            width, height = struct.unpack("<HH", blob[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if kind == b"VP8L":  # 无损：14 位宽高交错存放
            bits = int.from_bytes(blob[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        raise AssertionError(f"{name} 的 WebP 子格式不认识：{kind!r}")
    raise AssertionError(f"{name} 不是 PNG / WebP（只校验这两种位图）")


@needs_frontend
def test_runtime_cards_use_the_shared_logo_and_trimmed_copy():
    """内核卡片：名字前面挂官方 Logo，文案只留名称 / 公司 / 当前版本。

    桌面端这份 UI 已经收进「设置 → 内核管理」（`components/KernelManager.tsx`，
    列表卡片 + 详情弹层）；移动端仍是自己一页（卡片 + 底部弹层）。
    """
    pages = [
        FRONTEND / "src" / "components" / "KernelManager.tsx",
        MOBILE_SRC / "pages" / "MobileRuntimesPage.tsx",
    ]
    for path in pages:
        text = path.read_text(encoding="utf-8")
        assert "KernelLogo" in text, f"{path.name} 要在内核名字前显示官方 Logo"
        # 已经是最新版就不给更新入口；有更新时按钮写明目标版本。
        assert "showUpdate" in text, f"{path.name} 缺「按需显示更新按钮」的判断"
        assert "更新到 ${updateTarget}" in text, f"{path.name} 的更新按钮要写明「更新到 X」"
        for dropped in ("runtime.description", "runtime.instances", "provisioning.path"):
            assert dropped not in text, f"{path.name} 的卡片文案已精简，不该再渲染 {dropped}"

    logo = (FRONTEND / "src" / "components" / "KernelLogo.tsx").read_text(encoding="utf-8")
    assert "maskImage" in logo, "单色 Logo 用遮罩染色，深浅两套外观才能共用一份图形"
    assert "<img" in logo, "真彩色位图 Logo 要用 <img> 原色显示"
    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        assert ".kernel-logo" in text, f"{css.name} 缺少 .kernel-logo 规则"
        assert ".kernel-logo-image" in text, f"{css.name} 缺少位图 Logo 的样式"


# --- 内核 Logo 的内嵌清单（图标秒显） ------------------------------------------

KERNEL_LOGO_MANIFEST = FRONTEND / "src" / "lib" / "kernelLogos.ts"
KERNEL_LOGO_GENERATOR = REPO / "scripts" / "gen_kernel_logos.py"


def _load_kernel_logo_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_kernel_logos", KERNEL_LOGO_GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_frontend
def test_kernel_logo_inline_manifest_matches_the_committed_graphics():
    """内嵌清单必须与 `public/icons/kernels/` 里的图形一致（图形改了要重跑生成脚本）。

    「内核管理」页的图标走内嵌资源（不进网络），这份清单是内嵌的唯一来源。
    清单与源文件不一致 = 界面显示的是旧图形。
    """
    assert KERNEL_LOGO_MANIFEST.is_file(), (
        "缺少 frontend/src/lib/kernelLogos.ts（跑 python3 scripts/gen_kernel_logos.py）"
    )
    module = _load_kernel_logo_generator()
    assert KERNEL_LOGO_MANIFEST.read_text(encoding="utf-8") == module.render_module(), (
        "内核 Logo 内嵌清单与 public/icons/kernels/ 下的图形不一致，"
        "请重新执行 python3 scripts/gen_kernel_logos.py"
    )


@needs_frontend
def test_every_kernel_logo_is_inlined_and_used_by_the_shared_component():
    """每个内核声明的 Logo 都要在清单里，且两端页面都走同一个组件取它。

    - 清单漏了一个 = 那个内核的图标退回外部路径，又会「慢半拍」；
    - 两端各画各的 = 一端内嵌一端外链，优化只做了一半。
    """
    from app.agents import all_agents

    manifest = KERNEL_LOGO_MANIFEST.read_text(encoding="utf-8")
    for definition in all_agents():
        assert definition.logo in manifest, (
            f"{definition.id} 的 Logo 没进内嵌清单，请重跑 scripts/gen_kernel_logos.py"
        )

    # 取图地址统一走 lib/kernelIcons.ts（内嵌清单 + 内存缓存 + 预解码）。
    icons_lib = (FRONTEND / "src" / "lib" / "kernelIcons.ts").read_text(encoding="utf-8")
    assert "kernelLogoSource" in icons_lib, "取图标地址要走 lib/kernelIcons.ts 的统一入口"
    logo_component = (FRONTEND / "src" / "components" / "KernelLogo.tsx").read_text(encoding="utf-8")
    assert "kernelLogoSource" in logo_component, "KernelLogo 要用统一入口取图标地址"

    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        assert ".kernel-logo-missing" in text, f"{css.name} 缺少图标加载失败时的兜底样式"


# --- 应用图标与品牌标 ---------------------------------------------------------

ICON_DIR = FRONTEND / "public" / "icons"
ICON_GENERATOR = REPO / "scripts" / "gen_icons.py"


def _load_icon_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_icons", ICON_GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_frontend
def test_committed_app_icons_are_up_to_date():
    """应用图标是生成产物：改了 `scripts/gen_icons.py` 就要重新生成，否则线上还是旧图。"""
    module = _load_icon_generator()
    svg_file = ICON_DIR / "icon.svg"
    assert svg_file.is_file(), "缺少 frontend/public/icons/icon.svg"
    assert svg_file.read_text(encoding="utf-8") == module.build_svg(), (
        "icon.svg 与 scripts/gen_icons.py 的产物不一致，请重新执行 python3 scripts/gen_icons.py"
    )
    # PNG 只比 192 这一张（生成最快），其余几张同一趟脚本一起产出。
    png_file = ICON_DIR / "icon-192.png"
    assert png_file.is_file(), "缺少 frontend/public/icons/icon-192.png"
    assert png_file.read_bytes() == _png_bytes(module, 192), (
        "icon-192.png 与生成脚本的产物不一致，请重新执行 python3 scripts/gen_icons.py"
    )
    for name, size in (
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        blob = (ICON_DIR / name).read_bytes()
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
        assert struct.unpack(">II", blob[16:24]) == (size, size), f"{name} 尺寸不对"


def _png_bytes(module: ModuleType, size: int) -> bytes:
    """把生成器的渲染结果写成 PNG 字节（走临时目录，避免覆盖仓库里的文件）。"""
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        target = _Path(tmp) / "x.png"
        module._write_png(target, size, module.render_icon(size))
        return target.read_bytes()


@needs_frontend
def test_brand_mark_is_one_shared_logo_on_both_ends():
    """界面里的品牌标只有一份实现，且直接用用户提供的品牌图（与应用图标同一张）。"""
    mark = (FRONTEND / "src" / "components" / "BrandMark.tsx").read_text(encoding="utf-8")
    assert "<img" in mark, "品牌标用 <img> 显示用户提供的品牌位图"
    assert "icon-512.png" in mark, "品牌标要引用与应用图标同源的品牌图"

    users = [
        FRONTEND / "src" / "App.tsx",
        FRONTEND / "src" / "components" / "LoginScreen.tsx",
        MOBILE_SRC / "components" / "MobileDrawer.tsx",
    ]
    for path in users:
        text = path.read_text(encoding="utf-8")
        assert "BrandMark" in text, f"{path.name} 应当用共享的品牌标组件"
        assert "✦" not in text, f"{path.name} 里还留着旧的 ✦ 字符"

    # 顶栏左上角不再是品牌 logo 而是汉堡折叠按钮（用户指定的导航入口形态），
    # 品牌标留在侧栏 / 抽屉 / 登录页；这里锁住「汉堡用共享的线条图标」。
    menu = (MOBILE_SRC / "components" / "MobileMenuButton.tsx").read_text(encoding="utf-8")
    assert "MenuIcon" in menu, "顶栏菜单按钮应当用 Icons 里的汉堡线条图标"


@needs_frontend
def test_avatar_styles_exist_on_both_ends():
    """头像组件两端共用（移动端不加载 index.css），样式两边都要有。"""
    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        for selector in (".user-avatar", ".avatar-editor", ".avatar-editor-pic"):
            assert selector in text, f"{css.name} 缺少 {selector}"
        assert "--avatar-size" in text, f"{css.name} 的头像尺寸要走 --avatar-size 变量"


AVATAR_DIR = FRONTEND / "public" / "avatars"
AVATAR_MANIFEST = FRONTEND / "src" / "lib" / "avatarPack.ts"


@needs_frontend
def test_avatar_pack_manifest_matches_files():
    """内置头像包：清单与图片必须一一对应，且每个编号都挂了分类标签。

    「随机生成」直接按清单抽编号拼 `/avatars/<编号>.webp`，清单多一条少一条都会让抽到
    的头像 404。清单由 `scripts/gen_avatar_pack.py` 生成，两边只允许来自同一次运行。
    画廊还要按标签分组（人物 / 卡通），漏了标签那张就会被静默藏起来。
    """
    assert AVATAR_MANIFEST.is_file(), "缺少 frontend/src/lib/avatarPack.ts（跑 scripts/gen_avatar_pack.py）"
    text = AVATAR_MANIFEST.read_text(encoding="utf-8")
    # 只取编号数组那一段：后面的分类映射里也有 'pack-NNN' 字样。
    array_part = text.split("] as const", 1)[0]
    manifest_ids = re.findall(r"'(pack-\d{3})'", array_part)
    file_ids = sorted(path.stem for path in AVATAR_DIR.glob("pack-*.webp"))
    assert manifest_ids == file_ids, "头像包清单与 public/avatars/ 下的图片不一致，请重跑生成脚本"
    # 太少就失去「随机」的意义：至少两排才有抽卡感。
    assert len(manifest_ids) >= 20

    # 分类映射：每个编号恰好一条，且标签都在分组表里登记过。
    labels_part = text.split("AVATAR_CATEGORY_LABELS = {", 1)[1].split("} as const", 1)[0]
    labels = dict(re.findall(r"^\s{2}(\w+): '([^']+)',$", labels_part, re.MULTILINE))
    mapping_part = text.split("AVATAR_PRESET_CATEGORIES: Record<string, string> = {", 1)[1].split("}", 1)[0]
    categories = dict(re.findall(r"^\s{2}'(pack-\d{3})': '(\w+)',$", mapping_part, re.MULTILINE))
    assert set(categories) == set(manifest_ids), "分类映射漏了编号（画廊会少显示头像）"
    assert set(categories.values()) <= set(labels), "分类映射里出现了没登记的分组标签"
    assert "person" in labels and "cartoon" in labels, "人物与卡通两个分组必须都在"


@needs_frontend
def test_avatar_pack_categories_cover_people_and_cartoons():
    """头像包必须同时含人物与卡通两类，且画廊两端都按分组渲染（别混成一锅粥）。"""
    from app.avatars import is_valid_preset

    text = AVATAR_MANIFEST.read_text(encoding="utf-8")
    mapping_part = text.split("AVATAR_PRESET_CATEGORIES: Record<string, string> = {", 1)[1].split("}", 1)[0]
    categories = dict(re.findall(r"^\s{2}'(pack-\d{3})': '(\w+)',$", mapping_part, re.MULTILINE))
    for key in ("person", "cartoon"):
        picked = [pid for pid, category in categories.items() if category == key]
        assert len(picked) >= 10, f"分类 {key} 的图太少（{len(picked)} 张）"
        assert all(is_valid_preset(pid) for pid in picked), f"分类 {key} 里有形状不合法的编号"

    # 两端的画廊都从分组清单取图，而不是把全部编号平铺。
    for page in (
        FRONTEND / "src" / "components" / "AgentAvatarPicker.tsx",
        MOBILE_SRC / "pages" / "MobileAgentEditPage.tsx",
    ):
        body = page.read_text(encoding="utf-8")
        assert "AVATAR_PRESET_GROUPS" in body, f"{page.name} 的头像画廊要按标签分组渲染"
        assert "ALL_AVATAR_PRESETS" not in body, f"{page.name} 还在平铺全部头像"

    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        style = css.read_text(encoding="utf-8")
        for selector in (".agent-avatar-group", ".agent-avatar-group-label"):
            assert selector in style, f"{css.name} 缺少分组样式 {selector}"


@needs_frontend
def test_agent_avatars_only_come_from_the_avatar_pack():
    """智能体头像**只保留头像包**（外加自定义上传），emoji 头像已全部下线。

    三处一起收口，任何一处漏了都会让 emoji 脸重新露出来：
    ① 选择器（桌面 / 移动）不再提供 emoji 快选池；
    ② `AgentAvatar` 不再回退 emoji —— 没有自定义图 / 头像包时按智能体 id 稳定兜底一张；
    ③ 下拉框等只能显示文字的地方不再拼 emoji 前缀。
    人物 / 卡通头像包必须原样保留（下架的是 emoji，不是图片）。
    """
    picker = FRONTEND / "src" / "components" / "AgentAvatarPicker.tsx"
    picker_body = picker.read_text(encoding="utf-8")
    assert "FALLBACK_AVATARS" not in picker_body, "选择器里还有 emoji 快选池"
    assert "CUTE_AVATARS" not in picker_body, "选择器里还有 emoji 快选池"
    assert "onEmojiChange" not in picker_body, "选择器还在渲染 emoji 头像"
    assert "liveAnimation" not in picker_body, "选择器还在渲染 emoji 头像"
    assert "AVATAR_PRESET_GROUPS" in picker_body, "头像包画廊被误删了"
    assert "avatarPresetUrl" in picker_body

    mobile = (MOBILE_SRC / "pages" / "MobileAgentEditPage.tsx").read_text(encoding="utf-8")
    assert "const AVATARS = [" not in mobile, "移动端还留着一排 emoji 头像"
    assert "AVATAR_PRESET_GROUPS" in mobile, "移动端头像包画廊被误删了"

    avatar_component = (
        FRONTEND / "src" / "components" / "AgentAvatar.tsx"
    ).read_text(encoding="utf-8")
    assert "fallbackAvatarPreset" in avatar_component
    assert "avatarPresetUrl" in avatar_component
    assert "'🤖'" not in avatar_component, "AgentAvatar 还在回退 emoji"

    # 文字型下拉 / 只读框不再展示 emoji 前缀。
    text_targets = (
        FRONTEND / "src" / "components" / "NodeInspector.tsx",
        FRONTEND / "src" / "components" / "AgentImSettings.tsx",
        MOBILE_SRC / "components" / "CollabNodeForm.tsx",
    )
    for target in text_targets:
        text = target.read_text(encoding="utf-8")
        assert "{item.avatar}" not in text, f"{target.name} 还在拼 emoji 头像"
        assert "{agent.avatar}" not in text, f"{target.name} 还在拼 emoji 头像"

    # 全前端不再有「没头像就画 🤖」的兜底。
    for path in (FRONTEND / "src").rglob("*.tsx"):
        assert "'🤖'" not in path.read_text(encoding="utf-8"), f"{path.name} 还在回退 🤖 emoji"

    # 头像包（人物 / 卡通）必须完好：下架的是 emoji，不是整批图片头像。
    assert len(list(AVATAR_DIR.glob("pack-*.webp"))) >= 20, "人物 / 卡通头像包被误删了"


# --- 协作：工单更新小卡片（两端都在用，样式不能只写一端） ---------------------

TICKET_UPDATE_CLASSES = (
    "collab-ticket-update-card",
    "collab-ticket-update-icon",
    "collab-ticket-update-text",
    "collab-ticket-update-more",
)
TICKET_UPDATE_USERS = (
    FRONTEND / "src" / "components" / "CollabRunsPanel.tsx",
    MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx",
)


@needs_frontend
def test_ticket_update_card_is_styled_on_both_ends():
    """工单更新卡片桌面端与移动端都在渲染：每个 class 两端样式表都要有定义。

    漏定义不会报错，只会让按钮退化成浏览器默认样式（无边框、无留白、无主题色）。
    移动端不加载 `index.css`，所以两端必须各写一份。
    """
    for page in TICKET_UPDATE_USERS:
        body = page.read_text(encoding="utf-8")
        for name in TICKET_UPDATE_CLASSES:
            assert name in body, f"{page.name} 引用了 {name}，但组件里找不到它（测试需同步）"
    for css in (FRONTEND / "src" / "index.css", MOBILE_SRC / "mobile.css"):
        text = css.read_text(encoding="utf-8")
        for name in TICKET_UPDATE_CLASSES:
            assert f".{name}" in text, f"{css.name} 缺少 .{name}（工单更新卡片会裸奔）"
