"""移动端（`/m/`）与 PWA 的守卫测试。

移动端是一条与桌面端**物理独立**的链接：入口 HTML、页面、样式表、PWA 资产各自独立，
后端只用一个 SPA 兜底按前缀分派。下面的断言把这条约定固化下来，避免被后续改动悄悄破坏。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.main import resolve_spa_target

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
MOBILE_SRC = FRONTEND / "src" / "mobile"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")
#: 前端构建产物被 git 忽略：没 build 过就跳过「按前缀分派」的接口级断言。
needs_dist = pytest.mark.skipif(
    not (FRONTEND / "dist" / "index.html").is_file(), reason="未构建前端产物（frontend/dist）"
)


def test_spa_deep_links_split_between_desktop_and_mobile(tmp_path: Path):
    """`/m/*` 一律回落到移动端入口；其余深链回落桌面端入口。"""
    (tmp_path / "index.html").write_text("<html>desktop</html>", encoding="utf-8")
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "index.html").write_text("<html>mobile</html>", encoding="utf-8")
    (tmp_path / "m" / "sw.js").write_text("// service worker", encoding="utf-8")
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "icon.svg").write_text("<svg/>", encoding="utf-8")

    desktop = tmp_path / "index.html"
    mobile = tmp_path / "m" / "index.html"

    assert resolve_spa_target(tmp_path, "") == desktop
    assert resolve_spa_target(tmp_path, "chat/abc") == desktop
    assert resolve_spa_target(tmp_path, "agent_profiles") == desktop

    assert resolve_spa_target(tmp_path, "m") == mobile
    assert resolve_spa_target(tmp_path, "m/") == mobile
    assert resolve_spa_target(tmp_path, "m/chat/abc") == mobile
    assert resolve_spa_target(tmp_path, "m/collab/runs/xyz") == mobile

    # 静态产物原样返回：Service Worker 与图标不能被当成 SPA 深链。
    assert resolve_spa_target(tmp_path, "m/sw.js") == tmp_path / "m" / "sw.js"
    assert resolve_spa_target(tmp_path, "icons/icon.svg") == tmp_path / "icons" / "icon.svg"


def test_spa_falls_back_to_desktop_when_mobile_entry_is_missing(tmp_path: Path):
    """没有构建出移动端入口时（例如旧版本的前端产物），不要 404。"""
    (tmp_path / "index.html").write_text("<html>desktop</html>", encoding="utf-8")
    assert resolve_spa_target(tmp_path, "m/chat") == tmp_path / "index.html"


@needs_frontend
def test_mobile_entry_is_physically_separate_from_desktop():
    """两条链接各有自己的 HTML 入口、页面、样式表，互不引用。"""
    desktop_html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    mobile_html = (FRONTEND / "m" / "index.html").read_text(encoding="utf-8")

    assert "/src/mobile/main.tsx" in mobile_html
    assert "/m/manifest.webmanifest" in mobile_html
    assert "/src/main.tsx" not in mobile_html
    assert "src/mobile" not in desktop_html

    assert (MOBILE_SRC / "main.tsx").is_file()
    assert (MOBILE_SRC / "MobileApp.tsx").is_file()
    assert (MOBILE_SRC / "mobile.css").is_file()
    # 移动端不加载桌面端的样式表与页面。
    assert "index.css" not in (MOBILE_SRC / "main.tsx").read_text(encoding="utf-8")
    assert list(MOBILE_SRC.glob("pages/*.tsx")), "移动端应当有自己的页面目录"


@needs_frontend
def test_mobile_pages_do_not_import_desktop_ui():
    """移动端只允许复用 `lib/` 与纯展示组件，不得引用桌面端的页面与样式表。"""
    desktop_ui = {(FRONTEND / "src" / "pages").resolve(), (FRONTEND / "src").resolve()}
    offenders: list[str] = []
    for path in sorted(MOBILE_SRC.rglob("*.tsx")):
        for spec in re.findall(r"from '([^']+)'", path.read_text(encoding="utf-8")):
            if not spec.startswith("."):
                continue
            target = (path.parent / spec).resolve()
            name = target.name + (target.suffix if target.suffix else "")
            if target.parent in desktop_ui and target.parent.name == "pages":
                offenders.append(f"{path.name} 引用了桌面端页面：{spec}")
            elif target.parent.name == "src" and name in {"App.tsx", "App.ts", "main.tsx", "index.css"}:
                offenders.append(f"{path.name} 引用了桌面端入口：{spec}")
    assert offenders == [], "\n".join(offenders)


#: 两端共用一套 class 的样式段（移动端不加载 index.css，所以样式各写一份）：逐字一致，守卫见下。
#: 「协作流程图」：节点进度 + 状态徽标 + 详情卡（组件 components/CollabFlowMap.tsx，两端共用）。
FLOW_STYLE_BANNER = (
    "/* ================= 协作流程图：节点进度 + 状态徽标 + 详情卡（两端共用这一份）================= */"
)
#: 「工单看板」：工单卡 / 动作徽章 / 流转时间线 / 规则手风琴（到「协作流程图」横幅为止）。
BOARD_STYLE_BANNER = (
    "/* ================= 工单看板：工单卡 / 动作徽章 / 流转时间线 / 规则手风琴 ================= */"
)
#: 「输入区」：一个框 + 框里的线条图标。组件只有一份（`components/Composer.tsx`），
#: 样式仍是两端各一份 —— 共享段逐字一致，桌面端 / 移动端各自的尺寸微调写在共享段之后。
COMPOSER_STYLE_BANNER = (
    "/* ================= 输入区：一个框 + 框里的线条图标（两端共用这一份）================= */"
)
#: 「折叠分组」：技能页按内核、智能体页按分组的标题行（组件见 `components/CollapsibleGroup.tsx`）。
COLLAPSE_STYLE_BANNER = (
    "/* ================= 折叠分组：标题一行（点一下展开 / 折叠），内容默认收起（两端共用这一份）================= */"
)
#: 「MCP 详情」：身份头 + 分区卡片（组件见 `components/McpDetailBody.tsx`）。
MCP_DETAIL_STYLE_BANNER = (
    "/* ================= MCP 详情：身份头 + 分区卡片（桌面端与移动端共用这一份）================= */"
)
#: 「标签页」：胶囊分段控件（设置 / 技能 / 用量 / 协作导航共用）。样式在共享块之前，故取到 MCP 详情为止。
TABS_STYLE_BANNER = (
    "/* ================= 标签页：胶囊分段控件（设置 / 技能 / 用量 / 协作导航共用，两端共用这一份）================= */"
)
MOBILE_FLOW_TUNING = "/* ---------- 移动端：流程图画布矮一点"
#: 桌面端在流程图共享段之后还有登录 / 账号样式；它不是流程图的一部分，比对时在此截断。
AUTH_STYLE_BANNER = (
    "/* ================= 登录 / 注册（两端共用表单组件，样式各写一份）================= */"
)
DESKTOP_COMPOSER_TUNING = "/* ---------- 桌面端：输入区寄在会话栏底部"
MOBILE_COMPOSER_TUNING = "/* ---------- 移动端：输入区贴着屏幕最下边"


def _shared_style_block(path: Path, banner: str, cut: str | None = None) -> str:
    """取出一份样式表里的共享样式段：`banner` 到 `cut` 为止（不给 `cut` 就取到文件结尾）。

    两端专用 class 的样式各写一份，所以这些段落必须逐字一致；各自那几条微调规则写在
    共享段之后，不参与比对。改动时忘了同步另一边，会在这里被拦下。
    """
    text = path.read_text(encoding="utf-8")
    start = text.find(banner)
    assert start >= 0, f"{path.name} 里找不到共享样式段：{banner[:24]}…（横幅注释被删了？）"
    if cut is not None:
        end = text.find(cut, start)
        assert end > start, f"{path.name} 里 {banner[:16]}… 之后找不到 {cut[:24]}…（微调段被删了？）"
        text = text[:end]
    return text[start:].rstrip("\n")


@needs_frontend
def test_flow_styles_are_identical_on_both_ends():
    """协作流程图的样式两端各写一份（移动端不加载 index.css），两份必须逐字一致。

    改了一边忘了另一边，手机上就会走样：节点错位、徽标变色、详情卡飘出画布。
    移动端自己那几条微调规则写在这一段之后，不参与比对。
    """
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", FLOW_STYLE_BANNER, AUTH_STYLE_BANNER
    )
    mobile = _shared_style_block(MOBILE_SRC / "mobile.css", FLOW_STYLE_BANNER, MOBILE_FLOW_TUNING)
    assert desktop == mobile, (
        "index.css 与 mobile.css 的协作流程图样式不一致：改了一边记得把另一边同步（两端各一份）"
    )


@needs_frontend
def test_mobile_css_defines_every_canvas_variable_it_uses():
    """移动端流程图用到的每个 ``--canvas-*`` 变量都要在 mobile.css 里定义。

    移动端**不加载 index.css**：只搬用变量、不搬定义时，构建与类型检查都能过、页面也能打开，
    但流程图的底色 / 网格点 / 连线 / 节点阴影会整片失效（未定义的变量按初始值兜底，
    等于透明 / 不画）。这里把「用了就必须有定义」固化下来。
    """
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--(canvas-[a-z-]+)\)", css))
    defined = set(re.findall(r"--(canvas-[a-z-]+):", css))
    missing = used - defined
    assert not missing, (
        f"mobile.css 使用了未定义的 --canvas-* 变量：{sorted(missing)}；"
        "移动端不加载 index.css，这些变量必须在 mobile.css 的 :root 与 [data-theme='light'] 里各定义一份"
    )


@needs_frontend
def test_account_styles_exist_on_both_ends():
    """账号 / 用户管理组件两端共用，桌面端与移动端样式表都必须有这组 class。

    移动端不加载 index.css；漏写时页面能打开，但用户行、操作区会全部失去排版。
    """
    desktop = (FRONTEND / "src" / "index.css").read_text(encoding="utf-8")
    mobile = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    for name in (
        "account-card",
        "account-user-row",
        "account-user-main",
        "account-user-name",
        "account-user-actions",
        "account-reset-row",
    ):
        assert f".{name}" in desktop, f"index.css 缺少 .{name}"
        assert f".{name}" in mobile, f"mobile.css 缺少 .{name}"


@needs_frontend
def test_ticket_board_card_styles_are_identical_on_both_ends():
    """工单卡（徽章 / 流转卡片 / 手风琴）的样式两端各一份，必须逐字一致。

    只读节点越权时的红色高亮就在这段里：改了一边忘了另一边，手机上就看不见越权提示。
    两端各自的布局（桌面三栏、移动端单栏）写在这一段之外，不参与比对。
    """
    desktop = _shared_style_block(FRONTEND / "src" / "index.css", BOARD_STYLE_BANNER, FLOW_STYLE_BANNER)
    mobile = _shared_style_block(MOBILE_SRC / "mobile.css", BOARD_STYLE_BANNER, FLOW_STYLE_BANNER)
    assert desktop == mobile, (
        "index.css 与 mobile.css 的「工单看板」卡片样式不一致：改了一边记得把另一边同步"
    )
    # 越权高亮必须真的在共享段里，不然这条守卫等于没守。
    assert ".board-entry.violation" in desktop
    assert ".board-tag.tone-error" in desktop


@needs_frontend
def test_collapsible_group_styles_are_identical_on_both_ends():
    """折叠分组的样式两端各一份，共享段必须逐字一致。

    技能页（按内核）与智能体页（按分组）用的是同一个组件、同一套 class；移动端不加载
    index.css，忘了同步那一边，手机上标题行就会走样（箭头不转、数量徽章错位）。
    """
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", COLLAPSE_STYLE_BANNER, COMPOSER_STYLE_BANNER
    )
    mobile = _shared_style_block(
        MOBILE_SRC / "mobile.css", COLLAPSE_STYLE_BANNER, COMPOSER_STYLE_BANNER
    )
    assert desktop == mobile, (
        "index.css 与 mobile.css 的「折叠分组」样式不一致：改了一边记得把另一边同步（共享段逐字一致）"
    )
    # 关键规则必须真的在共享段里，不然「逐字一致」可能只是两边一起空着。
    assert ".collapse-head.open .collapse-caret" in desktop
    assert ".collapse-count" in desktop


@needs_frontend
def test_tab_styles_are_identical_on_both_ends():
    """标签页（胶囊分段控件）的样式两端各一份，共享段必须逐字一致。

    `.tabs` / `.tab` 被设置 / 技能 / 用量 / 协作导航共用；移动端不加载 index.css，
    忘了同步那一边，手机上选中块的圆角、圆点与焦点环就会跟桌面端不一样。
    """
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", TABS_STYLE_BANNER, MCP_DETAIL_STYLE_BANNER
    )
    mobile = _shared_style_block(
        MOBILE_SRC / "mobile.css", TABS_STYLE_BANNER, MCP_DETAIL_STYLE_BANNER
    )
    assert desktop == mobile, (
        "index.css 与 mobile.css 的「标签页」样式不一致：改了一边记得把另一边同步（共享段逐字一致）"
    )
    # 关键规则必须真的在共享段里，不然「逐字一致」可能只是两边一起空着。
    assert ".tab.active::before" in desktop
    assert ":focus-visible" in desktop


@needs_frontend
def test_mcp_detail_styles_are_identical_on_both_ends():
    """MCP 详情（身份头 + 分区卡片）的样式两端各一份，共享段必须逐字一致。

    同一个 `McpDetailBody` 组件同时喂桌面端的右侧面板与移动端的详情弹层；移动端不加载
    index.css，忘了同步那一边，手机上身份头就会塌掉（图标与状态标签挤在一起）。
    """
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", MCP_DETAIL_STYLE_BANNER, COLLAPSE_STYLE_BANNER
    )
    mobile = _shared_style_block(
        MOBILE_SRC / "mobile.css", MCP_DETAIL_STYLE_BANNER, COLLAPSE_STYLE_BANNER
    )
    assert desktop == mobile, (
        "index.css 与 mobile.css 的「MCP 详情」样式不一致：改了一边记得把另一边同步（共享段逐字一致）"
    )
    # 关键规则必须真的在共享段里，不然「逐字一致」可能只是两边一起空着。
    assert ".mcp-hero-tile" in desktop
    # 值要换行而不是省略号截断 —— 启动命令很长，截断就等于看不到。
    assert "overflow-wrap: anywhere" in desktop


@needs_frontend
def test_composer_styles_are_identical_on_both_ends():
    """输入区的样式两端各一份，共享那段必须逐字一致。

    组件只有一份（`components/Composer.tsx`），但两端样式表各自加载不了对方 —— 忘了同步
    会变成「手机上框里图标挤在一起、桌面上又空一截」。桌面端 / 移动端各自的尺寸微调
    （`DESKTOP_COMPOSER_TUNING` / `MOBILE_COMPOSER_TUNING` 之后）不参与比对。
    """
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", COMPOSER_STYLE_BANNER, DESKTOP_COMPOSER_TUNING
    )
    mobile = _shared_style_block(
        MOBILE_SRC / "mobile.css", COMPOSER_STYLE_BANNER, MOBILE_COMPOSER_TUNING
    )
    assert desktop == mobile, (
        "index.css 与 mobile.css 的输入区样式不一致：改了一边记得把另一边同步（共享段逐字一致）"
    )
    # 关键规则必须真的在共享段里，不然「逐字一致」可能只是两边一起空着。
    for selector in (
        ".composer-box",
        ".composer textarea",
        ".composer-tool.send",
        ".composer-tool.stop",
        ".composer-attach-chip button",
    ):
        assert selector in desktop, f"输入区共享样式段里少了 {selector}"
    # 共享段是两端共同的基础值；各自那几条微调（贴底收边、高度上限）写在它之外。
    assert "max-height: calc(160px * var(--composer-vscale))" in desktop, "共享段里应当有输入框高度的基础值"
    assert "safe-bottom" not in desktop, "贴底安全区的处理应当写在移动端微调段里"


@needs_frontend
def test_composer_settings_popover_closes_on_any_outside_click():
    """齿轮气泡「点外面收起」的判定范围只能是齿轮 + 气泡，不能连整只输入框一起算。

    `settingsRef` 曾同时挂在 `.composer-box` 与 `.composer-settings` 上；同一个 ref
    挂两处时 React 以最后挂上的（外层输入框）为准，于是点文本框 / 加号 / 发送都被当成
    「内部」，气泡只能靠再点一次齿轮才关得掉。这里把「只挂一处」固化下来。
    """
    composer = (FRONTEND / "src" / "components" / "Composer.tsx").read_text(encoding="utf-8")
    assert composer.count("ref={settingsRef}") == 1, (
        "settingsRef 只能挂在齿轮 + 气泡的小容器上：挂两处会让整只输入框都算「内部」，"
        "点哪里都关不掉气泡"
    )
    assert 'className="composer-box" ref={settingsRef}' not in composer
    # 点外面收起靠 mousedown；禁用的按钮在 Chrome 里不派发 mousedown，必须放行指针事件。
    assert "pointer-events: none" in _shared_style_block(
        FRONTEND / "src" / "index.css", COMPOSER_STYLE_BANNER, DESKTOP_COMPOSER_TUNING
    ), "禁用的输入区按钮要让点击穿透到框上，否则点它时齿轮气泡收不起来"


@needs_frontend
def test_replying_keeps_input_usable_and_queues_messages():
    """AI 回复期间仍能打字：右侧按钮按「框里有没有内容」在停止 / 排队发送之间切换，消息入队后本轮结束自动发出。

    实现在两端共用组件 + 各自的页面里：`Composer` 只提供 `queueWhenBusy` 开关（不锁输入、按内容切按钮），
    「待发送队列」与「本轮结束自动发出」的 effect 在 `ChatPage`（桌面）与 `MobileChatPage`（移动端）。
    """
    composer = (FRONTEND / "src" / "components" / "Composer.tsx").read_text(encoding="utf-8")
    assert "queueWhenBusy" in composer, "输入区要能声明「回复期间继续输入」"
    # 运行中只有「有空内容 → 排队发送」「空着 → 停止」两种形态，两个按钮不并存。
    assert "disabled || (busy && !queueWhenBusy)" in composer, "开了排队就不该再锁输入框"
    assert "busy && (!queueWhenBusy || !canSend)" in composer, "空输入时才显示停止"
    assert "(queueWhenBusy && canSend)" in composer, "有内容时显示排队发送"

    for page, chip in (
        (FRONTEND / "src" / "pages" / "ChatPage.tsx", "chat-queued-row"),
        (MOBILE_SRC / "pages" / "MobileChatPage.tsx", "m-queued-row"),
    ):
        text = page.read_text(encoding="utf-8")
        assert "queueWhenBusy" in text, f"{page.name} 应当开启排队输入"
        assert "interface QueuedMessage" in text, f"{page.name} 缺少待发送消息类型"
        # 自动发出的 effect 盯着「队列 + 运行状态 + 当前对话」；只写 queue 会漏掉运行结束的时机。
        assert re.search(r"\}, \[queue, runActive, active\?\.id\]\)", text), (
            f"{page.name} 的自动发送 effect 依赖不对"
        )
        assert "setQueue([])" in text, f"{page.name} 换对话时要清空队列"
        assert chip in text, f"{page.name} 缺少待发送提示条（{chip}）"


@needs_frontend
def test_ticket_board_is_shared_between_ends():
    """工单看板：视图模型与卡片零件只有一份，两端只是外壳与交互不同。"""
    kit = (FRONTEND / "src" / "components" / "TicketBoardKit.tsx").read_text(encoding="utf-8")
    assert "lib/ticketBoard" in kit

    desktop_board = (FRONTEND / "src" / "components" / "TicketBoard.tsx").read_text(encoding="utf-8")
    mobile_board = (MOBILE_SRC / "pages" / "MobileTicketBoardPage.tsx").read_text(encoding="utf-8")
    for text in (desktop_board, mobile_board):
        assert "lib/ticketBoard" in text, "工单看板应当用共享视图模型"
        assert "TicketBoardKit" in text, "工单看板应当用共享卡片零件"
        # 徽章配色 / 工单状态词汇 / 越权判定不许各写一份。
        for symbol in ("const ACTION_STYLE", "const TICKET_STATE", "const TICKET_LABELS", "const DEFAULT_SCOPE_BY_DUTY"):
            assert symbol not in text, f"工单看板又抄了一份词汇表：{symbol}"

    # 移动端要真的能走到看板（这正是之前缺失的那条路）
    assert "MobileTicketBoardPage.tsx" in {path.name for path in (MOBILE_SRC / "pages").glob("*.tsx")}
    assert "/collab/runs/:runId/tickets" in (MOBILE_SRC / "MobileApp.tsx").read_text(encoding="utf-8")
    run_page = (MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx").read_text(encoding="utf-8")
    assert "/tickets`" in run_page, "移动端运行页要有打开工单看板的入口"


@needs_frontend
def test_collab_progress_lines_reuse_the_editor_router():
    """协作进度图的连线必须复用协作组设定画布的路由，别又各画各的。

    两个页面都从 `lib/collabRouting.ts` 的 `routeEdge` 出线：端口方向、避障、多条线错开
    才同源；进度图的节点坐标也原样照搬（不缩放），否则行距和编辑器对不上，线就画得不一样。
    """
    flow = (FRONTEND / "src" / "components" / "CollabFlowMap.tsx").read_text(encoding="utf-8")
    assert "lib/collabRouting" in flow and "routeEdge" in flow, "进度图应当复用画布的连线路由"
    assert "canvasEdgePath" not in flow, "进度图不该再自己实现一套折线"
    assert "CANVAS_SCALE" not in flow, "画布坐标要原样照搬，缩放会让节点间距与编辑器不一致"


@needs_frontend
def test_ticket_board_avatars_follow_agent_settings():
    """工单看板头像要跟智能体设定同步：自定义图 / 内置头像包都要带到看板上。

    看板以前只拿 emoji（`agent.avatar`）直接当文字画，用户在智能体页换了头像，
    看板还是老 emoji。现在统一交给共享 `AgentAvatar` 渲染，三字段都要从模型传过去。
    """
    kit = (FRONTEND / "src" / "components" / "TicketBoardKit.tsx").read_text(encoding="utf-8")
    model = (FRONTEND / "src" / "lib" / "ticketBoard.ts").read_text(encoding="utf-8")
    assert "AgentAvatar" in kit, "看板头像要用共享 AgentAvatar 渲染（与设定同一套回退）"
    for field in ("avatarPreset", "avatarFile", "agentId"):
        assert field in kit, f"看板头像缺少 {field}，头像包 / 自定义图就不会生效"
    for field in ("avatar_preset", "avatar_file"):
        assert field in model, f"看板模型要从智能体取 {field}，否则头像同步不了"


@needs_frontend
def test_mobile_manifest_scopes_to_the_mobile_link():
    manifest_path = FRONTEND / "public" / "m" / "manifest.webmanifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["scope"] == "/m/"
    assert manifest["start_url"] == "/m/"
    assert manifest["display"] == "standalone"

    icons = manifest["icons"]
    sizes = {icon["sizes"] for icon in icons}
    purposes = {icon.get("purpose") for icon in icons}
    assert {"192x192", "512x512"} <= sizes
    assert "maskable" in purposes
    for icon in icons:
        assert (FRONTEND / "public" / icon["src"].lstrip("/")).is_file(), f"图标缺失：{icon['src']}"


@needs_frontend
def test_mobile_service_worker_never_caches_the_api():
    """接口（含 SSE）必须直连后端：Service Worker 里要有明确的放行分支。"""
    script = (FRONTEND / "public" / "m" / "sw.js").read_text(encoding="utf-8")
    assert "self.addEventListener('fetch'" in script
    assert "'/api/'" in script
    assert "navigator.serviceWorker" not in script  # 注册放在入口，不写在 SW 里


@needs_frontend
def test_mobile_collab_editor_is_form_based_not_a_canvas():
    """移动端也能编排协作组，只是不用桌面端的画布连线（手机上拖不准）。"""
    pages = {path.name for path in (MOBILE_SRC / "pages").glob("*.tsx")}
    assert "MobileCollabTeamPage.tsx" in pages, "移动端应当有一个协作组编排页"
    assert "/collab/teams/:teamId" in (MOBILE_SRC / "MobileApp.tsx").read_text(encoding="utf-8")

    desktop_editor = ("CollabCanvas", "CollabSettingsPanel", "NodeInspector")
    offenders = [
        f"{path.relative_to(MOBILE_SRC)} → {spec}"
        for path in sorted(MOBILE_SRC.rglob("*.tsx"))
        for spec in re.findall(r"from '([^']+)'", path.read_text(encoding="utf-8"))
        if any(name in spec for name in desktop_editor)
    ]
    assert offenders == [], f"移动端不该引用桌面端的编排画布：{offenders}"


@needs_frontend
def test_collab_draft_vocabulary_has_a_single_source():
    """协作编排的字段语义与默认值只有一份：两端都从 `lib/collabDraft.ts` 取。"""
    draft = (FRONTEND / "src" / "lib" / "collabDraft.ts").read_text(encoding="utf-8")
    for symbol in ("DUTIES", "EDGE_KINDS", "SCOPES", "ACTIONS", "TICKET_STATES", "DEFAULT_BUDGET", "clampBudget"):
        assert symbol in draft, f"lib/collabDraft.ts 缺少 {symbol}"

    mobile_page = (MOBILE_SRC / "pages" / "MobileCollabTeamPage.tsx").read_text(encoding="utf-8")
    assert "lib/collabDraft" in mobile_page
    # 工单状态的中文标签也只有一份（进度卡从共享词汇表取）。
    progress = (FRONTEND / "src" / "lib" / "collabProgress.ts").read_text(encoding="utf-8")
    assert "const TICKET_LABELS" not in progress, "collabProgress 又抄了一份工单状态词汇表"

    for name in ("CollabCanvas.tsx", "CollabSettingsPanel.tsx", "NodeInspector.tsx"):
        text = (FRONTEND / "src" / "components" / name).read_text(encoding="utf-8")
        assert "lib/collabDraft" in text
        for symbol in ("const DUTIES", "const EDGE_KINDS", "const SCOPES", "const ACTIONS"):
            assert symbol not in text, f"{name} 又复制了一份编排词汇表（{symbol}）"


@needs_frontend
def test_workspace_image_paths_are_recognised_from_plain_text():
    """智能体把图片写成普通文本（文件名 + 工作目录）时，气泡也要能画出来。

    真实故障：回复里是「文件：xxx.jpg」与「位置：/app/data/workspaces/<id>/」，
    既不是 Markdown 图片也不是「📎 附件」块，旧渲染器只显示一串路径。
    解析只有一份（`lib/media.tsx`），桌面端 / 移动端 / 协作都复用它。
    """
    media = (FRONTEND / "src" / "lib" / "media.tsx").read_text(encoding="utf-8")
    assert "export function extractImagePaths" in media, "lib/media.tsx 缺少 extractImagePaths"
    # 真实回复是「xxx.jpg（896×1152，51KB）」：全角括号必须被识别为文件名边界，
    # 否则文件名会整段漏掉，后面的取图逻辑根本不会触发。
    assert "?![A-Za-z0-9_/\\\\]" in media, "文件名边界要容忍全角括号等中文标点"

    message = (FRONTEND / "src" / "components" / "MessageView.tsx").read_text(encoding="utf-8")
    assert "extractImagePaths" in message, "MessageView 要从正文里提取图片路径"
    assert "bodyImagePaths" in message, "MessageView 要把正文图片画成缩略图"

    for page, symbol in (
        (FRONTEND / "src" / "pages" / "ChatPage.tsx", "conversationFileUrl"),
        (MOBILE_SRC / "pages" / "MobileChatPage.tsx", "conversationFileUrl"),
        (FRONTEND / "src" / "components" / "CollabRunsPanel.tsx", "collabRunFileUrl"),
        (MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx", "collabRunFileUrl"),
    ):
        text = page.read_text(encoding="utf-8")
        assert symbol in text, f"{page.name} 要接只读文件出口"


@needs_frontend
def test_mobile_entry_locks_zoom_and_desktop_keeps_it():
    """移动端是「固定外壳」的应用形态，锁定缩放；桌面端保持可缩放（浏览器缩放是它的无障碍能力）。"""
    mobile_html = (FRONTEND / "m" / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<meta name="viewport" content="([^"]+)"', mobile_html)
    assert match, "移动端入口缺少 viewport"
    viewport = {part.strip() for part in match.group(1).split(",")}
    assert "maximum-scale=1.0" in viewport
    assert "user-scalable=no" in viewport

    # iOS Safari 会忽略 user-scalable=no，必须靠 touch-action 兜底（允许集里不含 pinch-zoom）。
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    assert "touch-action: pan-x pan-y" in css
    # 外壳本身不许锁住手势；只有「协作流程图画布」例外——它要自己接管拖动与缩放。
    locked = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)
        if "touch-action: none" in body
    ]
    assert all(".flow-map" in selector for selector in locked), (
        f"只有协作流程图画布能锁触摸手势，这里是：{locked}"
    )

    # 锁定缩放后输入框必须 ≥ 16px：iOS 聚焦 font-size < 16px 的输入框会强制放大页面。
    for selector in (".input, .select, .textarea", ".composer textarea", ".input.mono"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert block, f"mobile.css 缺少 {selector} 样式"
        size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", block.group(1))
        assert size and float(size.group(1)) >= 16, f"{selector} 字号须 ≥ 16px"

    # 桌面端不禁用缩放。
    desktop_html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "user-scalable" not in desktop_html
    assert "maximum-scale" not in desktop_html


@needs_frontend
def test_mobile_shell_is_pinned_to_the_visible_viewport():
    """外壳必须钉在「可视视口」上，底栏才会真正贴到屏幕底部。

    真机上地址栏与底部工具栏会伸缩：`100vh` / `100dvh` 与 `height: 100%` 的父链高度
    经常对不齐，底栏要么浮起来留缝、要么被工具栏压住。四边贴 0 的固定定位跟的是可视视口。
    """
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")

    app_rule = re.search(r"\.m-app \{([^}]*)\}", css)
    assert app_rule, "mobile.css 缺少 .m-app 样式"
    rule = app_rule.group(1)
    assert "position: fixed" in rule
    for edge in ("top: 0", "right: 0", "bottom: 0", "left: 0"):
        assert edge in rule, f".m-app 需要 {edge}（四边贴住可视视口）"
    assert "100vh" not in rule and "100dvh" not in rule, "外壳不该再用视口高度单位定高"

    html_rule = re.search(r"html, body \{([^}]*)\}", css)
    assert html_rule and "overflow: hidden" in html_rule.group(1), "文档本身不应滚动"


@needs_frontend
def test_mobile_shell_has_a_self_service_refresh():
    """独立应用（已添加主屏）没有刷新按钮：卡在旧外壳时必须能自助清缓存重载。

    改外壳（入口 HTML / 样式表 / 构建产物）必须把 SW 的缓存名版本 +1，老客户端下次打开才会换掉；
    设置页的「更新到最新版本」是兜底出口。
    """
    sw = (FRONTEND / "public" / "m" / "sw.js").read_text(encoding="utf-8")
    for name in ("coda-m-shell-v", "coda-m-assets-v"):
        assert name in sw, f"sw.js 的缓存名要带版本号（{name}<n>）"

    pwa = (MOBILE_SRC / "pwa.ts").read_text(encoding="utf-8")
    assert "export async function refreshShell" in pwa
    assert "unregister()" in pwa and "caches.keys()" in pwa
    assert "location.reload()" in pwa

    settings = (MOBILE_SRC / "pages" / "MobileSettingsPage.tsx").read_text(encoding="utf-8")
    assert "refreshShell" in settings, "设置页要能触发自助刷新"


@needs_dist
async def test_unknown_api_path_returns_json_instead_of_the_spa_shell(client):
    """未知接口必须显式回 404 JSON，绝不能落到 index.html。

    真实踩过的坑：接口名写错（或前端比后端新）时，`/api/...` 拿到的是 200 + HTML，
    前端 `response.json()` 解析失败，WebKit 抛出的却是一句与业务毫无关系的
    "The string did not match the expected pattern." —— 在手机上完全无从下手。
    """
    response = await client.get("/api/no-such-endpoint")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "接口不存在" in response.json()["detail"]

    # 只有非 `/api/` 的深链才回落到前端入口，交给前端路由接管。
    spa = await client.get("/collab/runs/example")
    assert spa.status_code == 200
    assert spa.headers["content-type"].startswith("text/html")


@needs_dist
async def test_spa_shell_is_revalidated_instead_of_cached(client):
    """入口 HTML 与 `sw.js` 必须每次回源校验：独立应用不会手动刷新，缓存住就永远换不到新版本。

    带哈希的 `/assets/*` 是另一回事（不可变、可长缓存），不在这个断言的范围内。
    """
    for path in ("/", "/m/", "/m/sw.js"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", f"{path} 的外壳不该被缓存"


@needs_frontend
def test_mobile_chat_keeps_a_bare_composer_and_speaks_replies():
    """输入区只留一个「框」：线条图标长在框里、没有背景提示文字、贴着屏幕底边。

    顶栏右上角是「导出对话」+ 朗读开关（**默认关闭**）；会话级操作挪到抽屉里每个对话行的
    「⋯」上 —— 那几件事要用到聊天页的消息与会话状态，所以信号经路由 state 传回。
    """
    page = (MOBILE_SRC / "pages" / "MobileChatPage.tsx").read_text(encoding="utf-8")
    assert "components/Composer" in page, "对话页的输入区应当用两端共享的 components/Composer"
    assert 'aria-label="更多操作"' not in page, "顶栏右上角的「⋯」应当已挪到抽屉的对话行"
    assert "useSpeechOutput" in page and "speechSupported" not in page, "朗读开关只用 lib/tts 那一份实现"
    assert "speechSynthesis" not in page, "页面里不许自己调语音合成"
    assert "openActions" in page, "抽屉点「⋯」后聊天页要负责弹会话操作面板"

    # 输入区的样子只有一份实现（对话页与协作对话共用）：线条图标长在框里、没有背景提示文字。
    composer = (FRONTEND / "src" / "components" / "Composer.tsx").read_text(encoding="utf-8")
    assert not re.search(r"placeholder\s*=", composer), "输入框不该再有背景提示文字"
    for symbol in ("PlusIcon", "MicIcon", "SendIcon", "StopIcon", "SettingsIcon"):
        assert symbol in composer, f"输入区应当用 {symbol} 这类线条图标"
    for emoji in ("📎", "🎤", "🔊", "🔈", "➤", "■"):
        assert emoji not in composer, f"图标不该再用 emoji / 文字符号：{emoji}"
    assert re.search(r"voiceSupported", composer), "麦克风按钮要一直显示（不支持时给出中文说明）"
    assert "voiceSupported &&" not in composer, "麦克风不该再被 voiceSupported 藏起来"
    for symbol in ("SpeakerIcon", "ShareIcon"):
        assert symbol in page, f"顶栏应当用 {symbol} 这类线条图标"
    for emoji in ("📎", "🎤", "🔊", "🔈", "➤", "■"):
        assert emoji not in page, f"图标不该再用 emoji / 文字符号：{emoji}"

    # 导出：弹窗里选图片 / Markdown、正文 / 完整数据（组件两端共用，见
    # components/ConversationExportForm.tsx）；图片走 lib/shareImage 直接下载，
    # 文本整理走 lib/conversationExport，页面不自己拼导出内容。
    assert "ConversationExportForm" in page, "顶栏的导出按钮要打开两端共享的导出表单"
    assert "downloadConversationImage" in page, "导出图片要接 lib/shareImage"
    assert "buildConversationMarkdown" in page, "导出 Markdown 要接 lib/conversationExport"
    share = (FRONTEND / "src" / "lib" / "shareImage.ts").read_text(encoding="utf-8")
    for symbol in ("renderConversationImage", "downloadConversationImage", "toBlob"):
        assert symbol in share, f"lib/shareImage.ts 缺少 {symbol}"

    drawer = (MOBILE_SRC / "components" / "MobileDrawer.tsx").read_text(encoding="utf-8")
    assert "openActions" in drawer, "抽屉的对话行要能把会话操作交给聊天页"
    assert "avatar" not in drawer.split("m-conv-row")[1].split("</div>")[0], "对话行不再画头像"

    # 朗读只有一份实现，且开关默认关闭（关闭时把键删掉，不靠写 'off'）。
    tts = (FRONTEND / "src" / "lib" / "tts.ts").read_text(encoding="utf-8")
    for symbol in (
        "SPEECH_STORAGE_KEY", "coda.tts", "readSpeechEnabled", "storeSpeechEnabled",
        "toSpeechText", "useSpeechOutput",
    ):
        assert symbol in tts, f"lib/tts.ts 缺少 {symbol}"
    assert "removeItem" in tts, "关掉朗读要把存储键删掉（默认关闭只有一个真值来源）"

    # 输入区贴着屏幕底边：底部安全区只补在 padding-bottom 上（基础 6px + 安全区），
    # 这样输入框下方的用量条不会被 Home 指示条盖住；不能用整段 var(--safe-bottom)
    # 顶替基础收边，否则 iOS 独立应用里会留出一大条空白。
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    # 先去掉注释：注释里也可能有花括号，留着会把选择器认错。
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = [body for selector, body in re.findall(r"([^{}]*)\{([^}]*)\}", plain) if selector.strip() == ".composer"]
    assert blocks, "mobile.css 缺少 .composer 样式"
    assert any(
        re.search(r"padding-bottom\s*:\s*calc\([^;]*var\(--safe-bottom\)", body)
        for body in blocks
    ), "移动端 .composer 要用 calc(基础收边 + var(--safe-bottom)) 补底部安全区，别让用量条被 Home 条盖住"

    # 抽屉的底部安全区只能算一次：`.m-drawer` 与它最下面的 `.m-drawer-account` 都补一遍的话，
    # 账号头像会被顶到离屏幕底边 ~78px（两遍 34px + 10px 间距），看起来「没到底」。
    drawer_blocks = [
        body for selector, body in re.findall(r"([^{}]*)\{([^}]*)\}", plain) if selector.strip() == ".m-drawer"
    ]
    assert drawer_blocks, "mobile.css 缺少 .m-drawer 样式"
    assert not any(
        re.search(r"padding-bottom\s*:", body) and "safe-bottom" in body for body in drawer_blocks
    ), "抽屉底部安全区重复计算：.m-drawer 不要再留 padding-bottom（底部交给 .m-drawer-account）"


@needs_frontend
def test_lists_show_dates_with_a_single_time_source():
    """两端的列表都显示时间：今天只显示时间，其余显示日期 + 时间（口径只有 `lib/datetime.ts` 一份）。"""
    dt = (FRONTEND / "src" / "lib" / "datetime.ts").read_text(encoding="utf-8")
    for symbol in ("formatListTime", "isSameDay"):
        assert symbol in dt, f"lib/datetime.ts 缺少 {symbol}"
    assert "getHours()" in dt and "getMonth()" in dt, "时间文案在这里算，组件里不要再各写一份"

    drawer = (MOBILE_SRC / "components" / "MobileDrawer.tsx").read_text(encoding="utf-8")
    assert "formatListTime" in drawer, "移动端的对话列表要显示时间"
    desktop = (FRONTEND / "src" / "components" / "ConversationList.tsx").read_text(encoding="utf-8")
    assert "formatListTime" in desktop, "桌面端的对话列表也要显示时间"


@needs_frontend
def test_collab_run_is_the_flow_map_with_the_chat_hidden_behind_a_tap():
    """协作运行页的主页面就是那张协作流程图；聊天界面点节点才出来。

    底部只有输入框，而且与对话页共用同一份（`components/Composer`）；流程图配色走
    `--canvas-*` 主题变量 —— 浅色外壳下不能还是一块深色画布。
    """
    page = (MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx").read_text(encoding="utf-8")
    assert "components/Composer" in page, "协作运行页的输入区要复用对话页那一份"
    assert not re.search(r"placeholder\s*=", page), "输入框不该再有背景提示文字"
    # 时间线只在对话弹层里出现：主页面是流程图，不是聊天界面。
    assert 'className="m-thread m-run-chat-thread"' in page, "时间线应当只在对话弹层里"
    assert "m-run-stage" in page, "主页面留一块专门放流程图画布的区域"
    # 点节点 = 打开对话；入口智能体的对话就是「你 ↔ 领导者」那条线。
    assert "openNode" in page and "setChatOpen(true)" in page, "点流程图里的节点要能打开对话界面"
    assert 'aria-label="与领导者对话"' not in page, "顶栏不要再塞一个对话按钮（点节点才是入口）"

    # 协作聊天也能带附件（后端与对话走同一套 app.attachments）。
    assert "uploadCollabAttachment" in page, "输入区的回形针要接到协作附件接口"

    # 流程图组件两端共用同一份（components/CollabFlowMap.tsx）。
    flow = (FRONTEND / "src" / "components" / "CollabFlowMap.tsx").read_text(encoding="utf-8")
    assert "flow-node-badge" in flow, "节点上要有状态徽标（等待 / 执行 / 提问等状态）"
    assert "flow-popover" in flow, "点节点要弹「现在在做什么」的详情卡"
    progress = (MOBILE_SRC / "components" / "MobileCollabProgress.tsx").read_text(encoding="utf-8")
    assert "CollabFlowMap" in progress, "移动端协作进度要复用同一份流程图组件"
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    assert "--canvas-bg" in css, "流程图画布底色要走主题变量（浅色下不能还是深色）"


@needs_frontend
def test_collab_composer_routes_every_message_through_the_leader():
    """协作输入区没有「聊天 / 推进」模式切换：所有消息统一发进入口智能体的聊天通道。

    是否推进任务由 CEO 判断：它认为需要推进时输出 `chat_advance`，界面渲染「推进任务」
    决策卡，用户点选后才把指令发进工单通道（`/messages`）。用户不再手动选线。
    """
    page = (MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx").read_text(encoding="utf-8")
    # 模式切换控件彻底移除：不再有「普通对话 / 推进任务」按钮，也不再有 effectiveWork 判定。
    assert "m-mode-toggle" not in page, "输入区不该再有切换模式的按钮（路由交给 CEO 判断）"
    assert "setSendMode" not in page and "effectiveWork" not in page, "发送不再分两条线"
    assert "allowEmptySend" not in page, "没有「空内容 = 继续推进」这回事了（继续走 /resume 按钮）"
    # 单一通道：日常消息全部走 /chat（可带附件）；推进指令只在决策卡点选后走 /messages。
    for path in ("/chat`", "/messages`", "/resume`"):
        assert path in page, f"协作页缺少 {path} 这条通道"
    # CEO 判断 → 决策卡：advance 事件的还原 + 点选后进入工单通道。
    assert "chatAdvanceFrom" in page, "要能从聊天事件/落库 meta 还原推进决策卡"
    assert "advanceAsk" in page and "answerAdvance" in page, "要渲染决策卡并支持点选推进"
    assert "m-advance-card" in page, "决策卡要有自己的样式类（与「需要你决定」卡区分）"
    assert "sheetComposerRef" in page and "inputRef={inputRef}" in page, (
        "自由答复要聚焦到当前可见的输入框（共享组件必须支持传入 textarea ref）"
    )
    # 模式切换的死样式一并清掉；决策卡的样式要在。
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    assert ".m-mode-toggle" not in css, "模式切换按钮的死样式要删干净"
    assert ".m-advance-card" in css, "mobile.css 缺少推进决策卡的样式"


@needs_frontend
def test_agents_own_the_configuration_and_runtimes_own_the_install():
    """网关升格为唯一密钥来源后：配置在「智能体」页，安装 / 更新在「运行时」页，两者不再混在一起。"""
    pages = {path.name for path in (MOBILE_SRC / "pages").glob("*.tsx")}
    assert "MobileAgentsPage.tsx" in pages, "移动端要有独立的智能体配置页（原「角色」页改名而来）"
    assert "MobileRolesPage.tsx" not in pages, "「角色」这个词应当已经彻底换成「智能体」"
    assert "MobileGatewaysPage.tsx" in pages, "自定义网关要有自己的管理页"

    components = {path.name for path in (MOBILE_SRC / "components").glob("*.tsx")}
    assert "MobileAgentSettings.tsx" not in components, "内核的「实例配置」面板已经取消（密钥归网关）"

    app = (MOBILE_SRC / "MobileApp.tsx").read_text(encoding="utf-8")
    assert "/agents" in app, "移动端要挂上 /agents"
    assert "/gateways" in app, "移动端要挂上 /gateways"

    runtimes = (MOBILE_SRC / "pages" / "MobileRuntimesPage.tsx").read_text(encoding="utf-8")
    assert "MobileAgentSettings" not in runtimes, "运行时页不再内嵌智能体配置"

    agents = (MOBILE_SRC / "pages" / "MobileAgentsPage.tsx").read_text(encoding="utf-8")
    assert "DynamicForm" in agents, "适配器选项仍由 DynamicForm 渲染（前端不硬编码字段）"
    assert "listGateways" in agents, "网关下拉要读 /api/gateways"

    settings = (MOBILE_SRC / "pages" / "MobileSettingsPage.tsx").read_text(encoding="utf-8")
    assert "内核管理" in settings and "技能" in settings
    assert "网关" in settings, "设置页要有自定义网关入口"
    assert "'/mcp'" in settings, "设置页要有 MCP 入口"
    # 运行概览 / 版本记录这些信息不再占小屏（只认小节标题，注释里提到不算）。
    for label in ("运行概览", "版本记录"):
        assert f'card-section-title">{label}' not in settings, f"设置页不该再有「{label}」小节"
    assert "refreshShell" in settings, "「更新到最新版本」是独立应用唯一的重载出口，必须留着"


#: 主导航项的字面形态（两端各写一遍，这里把它抽出来比对）。
NAV_ENTRY = re.compile(
    r"\{\s*to:\s*'(?P<to>[^']+)',\s*icon:\s*'(?P<icon>[^']+)',\s*label:\s*'(?P<label>[^']+)'\s*\}"
)


@needs_frontend
def test_main_nav_is_the_same_menu_on_both_ends():
    """主导航两端是**同一份菜单**：顺序、图标、命名都对齐（MCP 也各占一项），不能一边有一边没有。"""
    desktop = [
        match.groupdict()
        for match in NAV_ENTRY.finditer((FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8"))
    ]
    mobile = [
        match.groupdict()
        for match in NAV_ENTRY.finditer(
            (MOBILE_SRC / "components" / "MobileDrawer.tsx").read_text(encoding="utf-8")
        )
    ]
    assert desktop and mobile, "两端的主导航都要能被解析出来"
    assert desktop == mobile, "两端主导航必须是同一份菜单（顺序 / 图标 / 命名对齐）"
    assert "/mcp" in {item["to"] for item in desktop}, "MCP 页要在主导航里占一项（两端一致）"


@needs_frontend
def test_final_only_view_is_one_switch_on_both_ends():
    """「显示工具调用」开关（输入区齿轮菜单）：只有一份实现（lib/finalOnly），两端对话页都接上。

    行为约定：勾选 = 完整视图；**取消勾选 = 纯净视图**（`MessageView` 的 `finalOnly`）——
    运行中只显示思考气泡，跑完只亮时间线上最后一段正文，出错 / 中断的报错永远保留。
    开关值是本机偏好：页面持有、存 localStorage（默认显示），输入区组件只收受控 props。
    """
    lib = (FRONTEND / "src" / "lib" / "finalOnly.ts").read_text(encoding="utf-8")
    for symbol in (
        "TOOLS_VISIBILITY_STORAGE_KEY", "coda.tools-hidden", "readShowTools",
        "storeShowTools", "useShowTools",
    ):
        assert symbol in lib, f"lib/finalOnly.ts 缺少 {symbol}"
    # 默认显示：只有显式关过才藏；回到显示要把键删掉（「默认显示」只有一个真值来源）。
    assert "!== 'off'" in lib, "读开关要按「键存在且为 off 才藏」判断（默认显示）"
    assert "removeItem" in lib, "回到显示要把存储键删掉"

    # 输入区组件：齿轮按钮与勾选气泡是受控的（值在页面手里），不传这对 props 就不显示。
    composer = (FRONTEND / "src" / "components" / "Composer.tsx").read_text(encoding="utf-8")
    for symbol in ("showTools", "onShowToolsChange", "SettingsIcon", "显示工具调用"):
        assert symbol in composer, f"输入区设置按钮 / 勾选气泡缺少 {symbol}"
    assert "typeof showTools === 'boolean'" in composer, "设置按钮要按「传了这对 props 才显示」"
    # 勾选项是 menuitemcheckbox（无原生 checkbox），选中态走 aria-checked。
    assert 'role="menuitemcheckbox"' in composer, "勾选项要声明 menuitemcheckbox 角色"
    assert "aria-checked={showTools}" in composer, "勾选项的选中态要走 aria-checked"
    # 菜单收起：点外面 / Esc（与侧栏账号菜单同一套收法）。
    assert "Escape" in composer, "设置气泡要支持 Esc 收起"
    # 附件是加号、语音挪到发送左侧：布局约定固化在图标引用里。
    assert "PlusIcon" in composer, "附件按钮应当用加号图标"
    assert "PaperclipIcon" not in composer, "附件按钮不再用回形针"
    icons = (FRONTEND / "src" / "components" / "Icons.tsx").read_text(encoding="utf-8")
    assert "export function PlusIcon" in icons, "Icons.tsx 缺少加号图标"
    assert "PaperclipIcon" not in icons, "回形针已无引用，别留死代码"
    # 语音在发送左边：textarea 之后、停止/发送按钮之前出现麦克风按钮。
    # 布局：加号、齿轮贴输入框左侧；语音紧贴发送（textarea 之后、停止 / 发送之前）。
    voice_pos = composer.find("toggleVoice")
    mic_pos = composer.find("<MicIcon")
    stop_pos = composer.find("composer-tool stop")
    send_pos = composer.find("composer-tool send")
    textarea_pos = composer.find("onPaste={handlePaste}")
    assert 0 < textarea_pos < mic_pos < stop_pos < send_pos, "语音按钮要放在发送按钮左侧（textarea 之后）"
    assert 0 < voice_pos, "语音按钮的 toggleVoice 逻辑缺失"

    # 两端对话页都要接同一份开关，并把它取反传给消息渲染。
    for page in (
        FRONTEND / "src" / "pages" / "ChatPage.tsx",
        MOBILE_SRC / "pages" / "MobileChatPage.tsx",
    ):
        text = page.read_text(encoding="utf-8")
        assert "useShowTools" in text, f"{page.name} 应当接 lib/finalOnly 的开关"
        assert "showTools={showTools}" in text, f"{page.name} 要把开关传给输入区齿轮菜单"
        assert "finalOnly={!showTools}" in text, f"{page.name} 要把开关取反传给消息渲染"

    # MessageView 的纯净分支：藏过程、亮最后一段正文、保留报错。
    view = (FRONTEND / "src" / "components" / "MessageView.tsx").read_text(encoding="utf-8")
    assert "finalOnly" in view, "MessageView 要支持 finalOnly prop"
    assert "findFinalText" in view, "纯净视图要按「时间线上最后一段正文」取最终回复"
    assert "ThinkingBubble" in view, "纯净视图运行中要有思考气泡兜底"
    # 透传组件也要有出口（桌面端经 ConversationThread 装配）。
    thread = (FRONTEND / "src" / "components" / "ConversationThread.tsx").read_text(encoding="utf-8")
    assert "finalOnly" in thread, "ConversationThread 要能透传 finalOnly"

    # 「设置」勾选气泡的样式写在输入区共享段里：两端逐字一致（守卫同 test_composer_styles… 的口径）。
    desktop = _shared_style_block(
        FRONTEND / "src" / "index.css", COMPOSER_STYLE_BANNER, DESKTOP_COMPOSER_TUNING
    )
    mobile = _shared_style_block(
        MOBILE_SRC / "mobile.css", COMPOSER_STYLE_BANNER, MOBILE_COMPOSER_TUNING
    )
    for selector in (".composer-settings-menu", ".composer-settings-caret"):
        assert selector in desktop, f"输入区共享段里缺少 {selector}"
        assert selector in mobile, f"移动端输入区共享段里缺少 {selector}"
    assert desktop == mobile, "输入区样式两端不一致：改了一边记得同步另一边"


@needs_frontend
def test_slash_clear_is_the_only_way_to_clear_agent_memory():
    """清除记忆统一走输入框的 `/clear`：两端提交时拦下命令，调 `resetSession`，不发给智能体。

    桌面端左侧栏对话行只留「✎ 重命名 / 🗑 删除」两枚悬停图标 —— 不再有「⋯」与会话操作弹窗。
    """
    for page, where in (
        (FRONTEND / "src" / "pages" / "ChatPage.tsx", "桌面端"),
        (MOBILE_SRC / "pages" / "MobileChatPage.tsx", "移动端"),
    ):
        text = page.read_text(encoding="utf-8")
        assert "'/clear'" in text, f"{where}的输入区要识别 /clear 命令"
        assert "resetSession" in text, f"{where}的 /clear 要调用 resetSession 清记忆"

    desktop = (FRONTEND / "src" / "components" / "ConversationList.tsx").read_text(encoding="utf-8")
    assert "MoreIcon" not in desktop, "桌面端对话行不再有「⋯」按钮"
    assert "openActions" not in desktop, "桌面端不再把会话操作信号交给聊天页"
