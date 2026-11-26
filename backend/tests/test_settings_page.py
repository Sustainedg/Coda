"""桌面端设置页的信息架构守卫。

设置按**类型**分标签页：一页只放一类设置，只读信息（系统信息 / 版本记录）收进「关于」；
「正在运行的智能体」与内核本身同属「内核管理」（那里每 8 秒刷新运行状态）。
分页只是分组 —— 各项设置的组件与文档语义都没变，这条守卫防的是后续改动
把分页拆掉、或又把只读信息摊回每个标签页里。

**移动端设置页不跟这套**（它刻意只留「现在要动的开关」，见 AGENTS.md），因此这里只读桌面端那一份。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")

#: 桌面端设置页：标签分组的信息架构就在这一个文件里。
SETTINGS = FRONTEND / "src" / "pages" / "SettingsPage.tsx"


@needs_frontend
def test_desktop_settings_is_grouped_into_tabs_by_type():
    """设置页把每一类设置放进自己的标签页（标签栏复用全站的 `.tabs` / `.tab`）。

    IM 不在设置页里：它是按智能体绑定的通道，收在每个智能体设定页的「IM」标签
    （见 `test_im_lives_in_the_agent_settings_page`）。
    """
    page = SETTINGS.read_text(encoding="utf-8")
    assert "const TABS" in page, "设置页缺少标签页定义"
    assert "className={`tab" in page, "标签栏应当复用全站的 `.tab` 样式"

    for key, label, component in (
        ("appearance", "外观", "THEME_OPTIONS"),
        ("chat", "对话", "<TitleSettingsSection />"),
        ("gateways", "网关", "<GatewayManager />"),
        ("credentials", "凭据", "<CredentialVault />"),
        ("network", "网络", "<ProxySettings />"),
        ("kernels", "内核管理", "<KernelManager />"),
        ("about", "关于", "版本记录"),
    ):
        assert f"id: '{key}', label: '{label}'" in page, f"设置页少了「{label}」这个标签"
        assert f"tab === '{key}'" in page, f"少了「{label}」标签页的分支"
        assert component in page, f"「{label}」标签页里没有对应的内容（{component}）"

    # IM 标签已经移进智能体设定页，设置页不得再出现。
    assert "id: 'im'" not in page, "IM 已移到智能体设定页，设置页不该再有「IM」标签"
    assert "ImSettings" not in page, "设置页不该再引用 ImSettings（已并入 AgentImSettings）"


@needs_frontend
def test_im_lives_in_the_agent_settings_page():
    """IM 绑定收在每个智能体设定页的「IM」标签里（不再挂在平台设置页）。"""
    edit = FRONTEND / "src" / "pages" / "AgentEditPage.tsx"
    assert edit.is_file(), "缺少智能体设定页 AgentEditPage.tsx"
    page = edit.read_text(encoding="utf-8")
    for marker in ("id: 'im', label: 'IM'", "AgentImSettings", "AgentUsagePanel"):
        assert marker in page, f"智能体设定页少了 {marker}"
    # 五个标签齐全：基础信息 / 技能 / MCP / IM / 统计。
    for tab_id, label in (
        ("basic", "基础信息"),
        ("skills", "技能"),
        ("mcp", "MCP"),
        ("im", "IM"),
        ("usage", "统计"),
    ):
        assert f"id: '{tab_id}', label: '{label}'" in page, f"智能体设定页少了「{label}」标签"

    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/agents/:agentId"' in app, "桌面端要挂上智能体设定页路由"
    assert 'path="/agents/new"' in app, "「新建智能体」也走设定页"

    # 编辑入口是跳转路由，不再有编辑弹窗。
    agents = (FRONTEND / "src" / "pages" / "AgentsPage.tsx").read_text(encoding="utf-8")
    assert "AgentModal" not in agents, "编辑弹窗 AgentModal 应已删除（改为整页路由）"
    assert "/agents/${agent.id}" in agents, "智能体卡片的「设定」要跳转整页"


@needs_frontend
def test_new_agent_route_passes_creating_explicitly():
    """`/agents/new` 是静态路由，必须**显式**告诉设定页「我在新建」。

    回归点：`/agents/new` 不匹配 `:agentId`，页面里 `useParams().agentId` 是
    `undefined`；若页面靠 `id === 'new'` 判断，新建分支永远进不去，而 `load()`
    又因为「没有 id」直接 return、不落 `loading` —— 界面会卡在「正在加载智能体…」。
    所以路由要传 `creating`，页面也要把 `creating` 当入参而不是自己推断。
    """
    desktop_app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "<AgentEditPage creating />" in desktop_app, "桌面端 /agents/new 要显式传 creating"

    mobile_app = (FRONTEND / "src" / "mobile" / "MobileApp.tsx").read_text(encoding="utf-8")
    assert "<MobileAgentEditPage creating />" in mobile_app, "移动端 /agents/new 要显式传 creating"

    for page in (
        FRONTEND / "src" / "pages" / "AgentEditPage.tsx",
        FRONTEND / "src" / "mobile" / "pages" / "MobileAgentEditPage.tsx",
    ):
        source = page.read_text(encoding="utf-8")
        assert "creating = false" in source, f"{page.name} 要从路由接收 creating"
        # 只盯代码（注释里也会出现这个字样），不许再从路由参数反推「我在新建」。
        assert "rawId === 'new'" not in source, f"{page.name} 不能靠 id === 'new' 判断新建（拿不到参数）"
        assert "const creating = " not in source, f"{page.name} 的 creating 必须来自路由入参"
        assert "setLoading(false)" in source, f"{page.name} 没有 id 时必须落下 loading"


@needs_frontend
def test_kernel_management_is_not_nested_behind_another_entry():
    """内核管理只有一份，别再嵌套一个「运行时」入口 + 适配器清单。

    这里同时钉住两件事：设置页里不再有指向 `/runtimes` 的入口 / 已注册的适配器清单，
    而老地址 `/runtimes` 仍能落到「设置 → 内核管理」（书签不失效）。
    """
    page = SETTINGS.read_text(encoding="utf-8")
    assert "navigate('/runtimes')" not in page, "设置页不该再嵌一个「运行时」入口（会与内核管理页重复）"
    assert "settings-entries" not in page, "「配置入口」那一排按钮已不再需要"
    assert "已注册的适配器" not in page, "适配器清单已删（内核卡片就在内核管理页里）"
    assert "如何接入一个新的智能体" not in page, "接入说明随适配器清单一并移出设置页"

    app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/runtimes"' in app, "老地址 /runtimes 要保留重定向"
    assert "/settings?tab=kernels" in app, "/runtimes 应当重定向到「设置 → 内核管理」"
    assert "RuntimesPage" not in app, "桌面端不再有独立的运行时页面"


@needs_frontend
def test_version_history_and_system_info_live_in_the_about_tab():
    """版本记录与系统信息是只读信息，收在「关于」里，不再和设置项混在同一页。"""
    page = SETTINGS.read_text(encoding="utf-8")
    parts = page.split("tab === 'about'", 1)
    assert len(parts) == 2, "设置页里找不到「关于」标签页"
    about = parts[1]
    for marker in ("系统信息", "版本记录", "version-timeline"):
        assert marker in about, f"「关于」标签页里少了{marker}"

    # 「正在运行的智能体」随内核一起收进「内核管理」：那里既管安装态，也盯着跑着的任务。
    kernel = (FRONTEND / "src" / "components" / "KernelManager.tsx").read_text(encoding="utf-8")
    assert "正在运行" in kernel, "「内核管理」里缺少「正在运行」板块"
    assert "setInterval(() => void loadRuns(), 8000)" in kernel, (
        "「正在运行」要每 8 秒刷新（状态与时长自己在走）"
    )
    assert "正在运行" not in about, "「正在运行」已挪去内核管理页，不该在「关于」里重复出现"
