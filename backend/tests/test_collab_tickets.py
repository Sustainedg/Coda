"""工单协议：三级容错解析、语义校验与交付载荷渲染。

这套用例是整条协作链路的保险丝 —— 解析器一旦开始「猜」，智能体之间就会传错话。
"""
from __future__ import annotations

from app.collab.schemas import TicketAction
from app.collab.tickets import (
    ChatUpdate,
    FENCE,
    TicketDraft,
    escape_inner_quotes,
    extract_body,
    extract_chat_body,
    merge_chat_update,
    normalize_title,
    parse_chat_advance,
    parse_chat_update,
    parse_work_order,
    render_protocol_guide,
    resolve_order_targets,
    sanitize_json_text,
    titles_look_duplicated,
    validate_order,
)

GOOD = """我理解了这个需求，先安排下去。

```work_order
{
  "action": "delegate",
  "assignee": "cto",
  "ticket": {
    "title": "登录页暗色主题",
    "goal": "不改后端就能切换主题",
    "requirements": ["新增主题开关", "持久化选择"],
    "acceptance": ["刷新后主题保持"],
    "constraints": ["只改 frontend/src/**"]
  }
}
```
"""


def test_parse_standard_fenced_block():
    result = parse_work_order(GOOD)
    assert result.ok and result.source == "fence"
    order = result.order
    assert order is not None
    assert order.action is TicketAction.DELEGATE
    assert order.assignee == "cto"
    assert order.ticket is not None
    assert order.ticket.title == "登录页暗色主题"
    assert order.ticket.requirements == ["新增主题开关", "持久化选择"]
    # 正文只保留给人看的部分，工单块不重复出现在卡片上
    assert extract_body(GOOD) == "我理解了这个需求，先安排下去。"


def test_parse_tolerates_trailing_comma_and_control_chars():
    text = '```work_order\n{"action":"submit",\x07"summary":"做完了","artifacts":["a.py",],}\n```'
    result = parse_work_order(text)
    assert result.ok
    assert result.order is not None
    assert result.order.summary == "做完了"
    # 字符串形态的 artifacts 会被规整成 {kind, path}
    assert result.order.artifacts == [{"kind": "file", "path": "a.py"}]


def test_parse_repairs_smart_quotes():
    text = '```work_order\n{“action”:“submit”,“summary”:“ok”}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None and result.order.summary == "ok"


def test_parse_recovers_from_truncated_output():
    # 输出被截断：围栏没闭合、JSON 少了右括号
    text = '```work_order\n{"action":"submit","summary":"做完了","artifacts":["a.py"]'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.summary == "做完了"

    # 截断发生在字符串中间时，按最后一个逗号回退
    text = '```work_order\n{"action":"submit","summary":"做完了一半'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.action is TicketAction.SUBMIT


def test_parse_falls_back_to_inline_json():
    text = '我想了想，就这样吧：{"action":"done","remark":"收工"} 以上。'
    result = parse_work_order(text)
    assert result.ok and result.source == "inline"
    assert result.order is not None and result.order.remark == "收工"


def test_parse_takes_the_last_block_when_a_round_has_several():
    """工具型内核会把草稿和结论写进同一轮：取最后一个，但必须留下可看见的提醒。"""
    text = f'```{FENCE}\n{{"action":"done"}}\n```\n补充一句：\n```{FENCE}\n{{"action":"submit","summary":"x"}}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None
    assert result.order.action is TicketAction.SUBMIT and result.order.summary == "x"
    assert "2 个" in (result.warning or "") and FENCE in (result.warning or "")
    # 没有偏差时不产生噪声
    assert parse_work_order(f'```{FENCE}\n{{"action":"done"}}\n```').warning is None


def test_parse_reports_missing_action_and_unknown_action():
    assert "action" in (parse_work_order(f'```{FENCE}\n{{"summary":"没有动作"}}\n```').error or "")
    error = parse_work_order(f'```{FENCE}\n{{"action":"coding"}}\n```').error or ""
    assert "coding" in error and "delegate" in error


def test_parse_reports_garbage_and_empty_output():
    assert not parse_work_order("我这就去办，稍等。").ok
    assert not parse_work_order("   ").ok
    # 缺字段时给出的是可读原因，而不是抛异常
    assert parse_work_order(f'```{FENCE}\n{{"action":\n```').error


def test_sanitize_keeps_json_semantics():
    assert sanitize_json_text('{"a": 1,}') == '{"a": 1}'
    assert sanitize_json_text('\ufeff{"a":\x00 1}') == '{"a": 1}'


def test_validate_order_enforces_contract_and_whitelist():
    order = parse_work_order(GOOD).order
    assert order is not None

    # 派发对象不在白名单里 → 明确指出允许谁
    error = validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["se"])
    assert error is not None and "se" in error

    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["cto"]) is None

    # 越权动作
    error = validate_order(order, allowed_actions=[TicketAction.SUBMIT], allowed_assignees=["cto"])
    assert error is not None and "delegate" in error

    # 审查必须给结论；打回必须给意见
    review = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"changes_requested"}}\n```').order
    assert review is not None
    error = validate_order(review, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[])
    assert error is not None and "review_comment" in error

    pass_order = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"pass"}}\n```').order
    assert pass_order is not None
    assert validate_order(pass_order, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[]) is None

    # 非入口节点不能向用户提问
    ask = parse_work_order(f'```{FENCE}\n{{"action":"ask_user","question":"要暗色还是亮色？"}}\n```').order
    assert ask is not None
    assert validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=False)

    # 入口节点提问必须给可点选项；只把选项写在正文里不算
    error = validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True)
    assert error is not None and "options" in error


def test_ask_user_parses_and_requires_clickable_options():
    text = f'''```{FENCE}
{{
  "action": "ask_user",
  "question": "首期要先做最小闭环，还是一次做全？",
  "choices": [
    "先做最小闭环",
    {{"label": "一次做全", "value": "首期范围一次做全", "description": "周期更长"}},
    {{"label": "我来补充", "value": "", "freeform": true}}
  ]
}}
```'''
    parsed = parse_work_order(text)
    assert parsed.ok, parsed.error
    assert parsed.order is not None
    options = parsed.order.options
    assert [option.label for option in options] == ["先做最小闭环", "一次做全", "我来补充"]
    assert options[0].value == "先做最小闭环"
    assert options[1].description == "周期更长"
    assert options[2].freeform is True and options[2].value == ""
    assert validate_order(
        parsed.order,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    ) is None

    # 少于两条可比较的选项 → 明确拒绝，逼模型把选择做成卡片而不是一段长问题
    single = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":["按默认来"]}}\n```'
    ).order
    assert single is not None
    error = validate_order(
        single, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True
    )
    assert error is not None and "至少" in error

    # 只有 freeform 也没有可点的答复
    freeform_only = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":'
        '[{"label":"我来补充","value":"","freeform":true},'
        '{"label":"还是补充","value":"","freeform":true}]}\n```'
    ).order
    assert freeform_only is not None
    error = validate_order(
        freeform_only,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    )
    assert error is not None and "可直接点选" in error


def test_step_prompt_hides_ask_user_from_non_entry_nodes():
    """非入口节点的「允许的动作」里绝不能出现 ask_user。

    否则模型会照提示词做，触发「只有入口节点能向用户提问」的运行时错误。
    """
    from app.collab.prompt import TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    node = NodeSpec(
        key="dev",
        agent_profile_id="r-dev",
        duty=Duty.ENGINEER,
        output_contract=[TicketAction.DONE, TicketAction.ASK_USER],
    )
    ticket = TicketView(id="t1", title="做点事", brief={"goal": "目标"}, state=TicketState.OPEN.value)

    def prompt(*, is_entry: bool) -> str:
        return build_step_prompt(
            team_name="小组",
            workspace_path="/tmp/x",
            node=node,
            ticket=ticket,
            history=[],
            assignees=[],
            reviewer=None,
            is_entry=is_entry,
        )

    non_entry = prompt(is_entry=False)
    assert "ask_user" not in non_entry
    # 生效动作里没有 submit 时，本轮要求也不能催它去 submit
    assert "用 done 收尾" in non_entry and "submit 提交结果" not in non_entry
    assert "ask_user" in prompt(is_entry=True)


def test_validate_order_escalate_needs_reason_and_barred_at_the_top():
    """escalate 必须说清卡在哪；入口节点不许上报（上面没人了，应该 ask_user）。"""
    order = parse_work_order(f'```{FENCE}\n{{"action":"escalate"}}\n```').order
    assert order is not None
    error = validate_order(
        order, allowed_actions=[TicketAction.ESCALATE], allowed_assignees=[], is_entry=False
    )
    assert error is not None and "reason" in error

    with_reason = parse_work_order(
        f'```{FENCE}\n{{"action":"escalate","reason":"本环境禁止执行命令，测不了"}}\n```'
    ).order
    assert with_reason is not None
    assert (
        validate_order(
            with_reason,
            allowed_actions=[TicketAction.ESCALATE],
            allowed_assignees=[],
            is_entry=False,
        )
        is None
    )
    entry_error = validate_order(
        with_reason,
        allowed_actions=[TicketAction.ESCALATE],
        allowed_assignees=[],
        is_entry=True,
    )
    assert entry_error is not None and "ask_user" in entry_error


def test_step_prompt_for_escalated_ticket_bars_delegate():
    """上抛件：提示词不许再教它往下派；到了入口智能体则要求用 ask_user 交给用户。

    真实事故：UI 卡在「环境禁止执行」后只能把失败写进 done，CTO 手里没有上报动作，
    只好把同一件事改写成「静态核对」又派给码农 —— 阻塞原地转了一圈。
    """
    from app.collab.prompt import TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    cto = NodeSpec(key="cto", agent_profile_id="r-cto", duty=Duty.COORDINATOR)
    ceo = NodeSpec(key="ceo", agent_profile_id="r-ceo", duty=Duty.COORDINATOR)
    ticket = TicketView(
        id="t1",
        title="跑运行时验收",
        brief={"goal": "启动服务并跑通验收"},
        state=TicketState.ESCALATED.value,
        escalation={
            "from": "se",
            "reason": "本环境禁止执行命令",
            "need": "需要用户决定是否授权",
            "hops": ["se", "cto"],
        },
    )

    def prompt(node: NodeSpec, *, is_entry: bool) -> str:
        return build_step_prompt(
            team_name="小组",
            workspace_path="/tmp/x",
            node=node,
            ticket=ticket,
            history=[],
            assignees=["se"],
            reviewer=None,
            is_entry=is_entry,
        )

    mid = prompt(cto, is_entry=False)
    actions_line = next(line for line in mid.splitlines() if "允许的动作" in line)
    assert "delegate" not in actions_line and "escalate" in actions_line
    assert "不要再往下派" in mid
    # 上报表：让上层看清是谁报的、卡在哪、需要什么
    assert "本环境禁止执行命令" in mid and "需要用户决定是否授权" in mid

    top = prompt(ceo, is_entry=True)
    assert "ask_user" in top
    # 已经到顶：入口智能体的动作里不再有 escalate，只剩「转给用户」
    top_actions = next(line for line in top.splitlines() if "允许的动作" in line)
    assert "escalate" not in top_actions and "ask_user" in top_actions


def test_protocol_guide_states_the_fence_and_the_whitelist():
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=[TicketAction.DELEGATE, TicketAction.ASK_USER],
        assignees=["cto"],
    )
    assert FENCE in guide
    assert "cto" in guide
    assert "delegate" in guide and "ask_user" in guide
    assert "options" in guide and "freeform" in guide
    # 示例用四重围栏包住，避免模型把示例当成真输出照抄
    assert "````" in guide


def test_protocol_guide_demands_per_ticket_assignee_when_multiple_assignees():
    """多个派发对象时，split 多张的每张必须自带 assignee（否则并行名存实亡）。

    真实故障：图里有 dev / dev-2 / dev-3 三个实施者、max_parallel=3，lead 也确实拆了
    3 张互不依赖的子工单 —— 但示例形状里子工单没有 assignee 字段，规则又只是一句
    「要并行干时可以写」，模型思考里计划了、落笔时全丢，3 张全被外层的 assignee 接走，
    同岗互斥护栏让波次永远只有一张。
    """
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=(TicketAction.DELEGATE,),
        assignees=["coco（dev）", "ark（dev-2）", "gok（dev-3）"],
    )
    # 硬性要求 + 点破后果（缺了会全部落进外层 assignee，同岗只能串行）
    assert "每张子工单都必须写它自己的 assignee" in guide
    assert "同一岗位只能串行干活" in guide
    # 字段白名单必须包含 assignee：漏了它，模型会按「白名单没有」把刚被要求的
    # 字段主动丢掉（第二次真实故障：思考里计划了、落笔时按白名单删掉了）
    assert "assignee / title" in guide
    # 示例直接演示每张带 assignee 的形状（形状锚定比规则文字有效）
    assert '"assignee": "coco（dev）"' in guide
    assert '"assignee": "ark（dev-2）"' in guide


def test_protocol_guide_keeps_fallback_wording_for_single_assignee():
    """只有一个派发对象时没有并行可言：维持旧措辞，不强迫模型重复写同一个 key。"""
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=(TicketAction.DELEGATE,),
        assignees=["dev"],
    )
    assert "不指定每张的 assignee 时全部交给外层的 assignee" in guide
    assert "每张子工单都必须写它自己的 assignee" not in guide
    # 单岗位示例维持一张子工单、不写 per-ticket assignee 的旧形状：
    # "assignee" 带引号的 JSON 键只在示例外层出现 1 次，子工单里没有
    assert '"assignee": "dev"' in guide
    assert guide.count('"assignee"') == 1


def test_display_names_are_normalized_to_node_keys():
    """模型把 assignee 写成「CTO」时，唯一显示名应归一到图上的 key。"""
    order = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO",'
        '"mode":"handoff","ticket":{"title":"继续"}}\n```'
    ).order
    assert order is not None
    resolve_order_targets(order, {"CTO": "dev", "开发": "dev-2"})
    assert order.assignee == "dev"
    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["dev"]) is None

    # 带括号的展示形式也认；同名节点则要求模型写 key，不猜
    wrapped = parse_work_order(
        f'```{FENCE}\n{{"action":"review","verdict":"changes_requested",'
        '"review_comment":"再改一下","rework_to":"SE（se）"}}\n```'
    ).order
    assert wrapped is not None
    resolve_order_targets(wrapped, {"SE": "se"})
    assert wrapped.rework_to == "se"

    ambiguous = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO","mode":"handoff"}}\n```'
    ).order
    assert ambiguous is not None
    resolve_order_targets(ambiguous, {})  # 调用方对重名节点不提供映射
    assert ambiguous.assignee == "CTO"


# --- 近似重名（防「复读式拆解」） ------------------------------------------


def test_normalize_title_strips_noise():
    assert normalize_title("  列出文件清单，并回报。 ") == "列出文件清单并回报"
    assert normalize_title("Write Login API") == "writeloginapi"


def test_titles_look_duplicated_catches_tail_tweaks():
    """模型最常见的绕法：在标题尾巴上追加几个字——必须仍然算同一件事。"""
    assert titles_look_duplicated("列出文件清单", "列出文件清单并回报")
    assert titles_look_duplicated("写登录接口", "写登录接口")
    assert titles_look_duplicated(
        "递归列出工作路径下的完整文件清单并回报结果",
        "递归列出工作路径下的完整文件清单并原样回报",
    )


def test_titles_look_duplicated_keeps_distinct_tasks_apart():
    assert not titles_look_duplicated("实现导出功能", "修复登录崩溃")
    assert not titles_look_duplicated("第 1 层", "第 2 层")
    assert not titles_look_duplicated("登录页改造为暗色主题", "登录页改造为浅色主题")
    assert not titles_look_duplicated("", "写登录接口")


def test_protocol_guide_tells_done_to_write_a_remark():
    """done 的 remark 是工单对外的交付摘要，协议说明里必须讲清楚。"""
    guide = render_protocol_guide(
        duty_label="协调者", actions=(TicketAction.DONE, TicketAction.DELEGATE), assignees=["se"]
    )
    assert "交付摘要" in guide


def test_protocol_guide_never_bakes_in_a_node_key():
    """协议说明里不能出现写死的示例 key（真实故障：图上的节点被改名后必错）。

    节点 key 是用户在画布上自己起的；曾经写成「例如 dev」，模型照着抄，
    于是「把 id 从 dev 改成别的」之后每一轮派发都被判「不能把工单交给 dev」。
    示例只能来自本次运行的真实白名单（``_example_for``），规则文字里一律不给具体 key。
    """
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=(TicketAction.DELEGATE,),
        assignees=["Leo-SDE（SDE）"],
    )
    assert "dev" not in guide
    # 该指哪儿：活的白名单（上一行渲染出来的那一串）
    assert "允许派发的对象" in guide
    assert "Leo-SDE（SDE）" in guide


def test_coordinator_example_demonstrates_delegation():
    """协调者的示例必须演示 delegate：它契约里也带 submit，但主场是派活。

    真实故障里 CTO 的动作是 delegate/done/submit/reject，示例挑中了 submit 那份，
    于是「assignee 填什么」只剩规则文字里那句写死的「例如 dev」可抄。
    """
    from app.collab.schemas import Duty

    guide = render_protocol_guide(
        duty_label="协调者",
        actions=(
            TicketAction.DELEGATE,
            TicketAction.DONE,
            TicketAction.SUBMIT,
            TicketAction.REJECT,
        ),
        assignees=["Leo-SDE（SDE）"],
        duty=Duty.COORDINATOR,
    )
    assert '"action": "delegate"' in guide
    assert '"assignee": "Leo-SDE（SDE）"' in guide

    # 没有 duty 时维持原样（工程师仍然看到 submit 示例），改动不外溢
    engineer_guide = render_protocol_guide(
        duty_label="工程师",
        actions=(TicketAction.SUBMIT, TicketAction.DELEGATE),
        assignees=["se"],
    )
    assert '"action": "submit"' in engineer_guide


def test_step_prompt_lists_the_whole_team_not_only_my_assignees():
    """每个岗位都要看得见整个团队：有谁、谁负责什么、入口是谁。

    用户反馈：入口智能体（CEO）答不出「团队里有哪些人」。此前 prompt 只告诉它自己的派发对象，
    组里配了审查者也无从得知 —— 同一张图里 CTO 因此说过「本协作没有安排审查者」。
    """
    from app.collab.prompt import TeamMember, TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    node = NodeSpec(key="se", agent_profile_id="r-se", duty=Duty.ENGINEER)
    ticket = TicketView(id="t1", title="做点事", brief={"goal": "目标"}, state=TicketState.OPEN.value)
    roster = [
        TeamMember(key="ceo", label="CEO", duty=Duty.COORDINATOR, is_entry=True, instructions="只负责派活"),
        TeamMember(key="se", label="SE", duty=Duty.ENGINEER),
        TeamMember(key="qa", label="审查员", duty=Duty.REVIEWER, instructions="逐条核对验收标准"),
    ]
    text = build_step_prompt(
        team_name="小组",
        workspace_path="/tmp/x",
        node=node,
        ticket=ticket,
        history=[],
        assignees=[],
        reviewer=None,
        is_entry=False,
        roster=roster,
    )
    assert "—— 协作组名册（系统提供的团队结构，只读）——" in text
    assert "CEO（ceo） · 协调者 · 入口，唯一和用户对话的智能体 · 只负责派活" in text
    assert "SE（se） · 实施者" in text
    assert "审查员（qa） · 审查者 · 逐条核对验收标准" in text
    # 名册只是背景，不能顶着「输出协议」的权威：派发 / 审查对象仍以协议为准
    assert "一律以「输出协议」里列出的对象为准" in text
    # 名册排在输出协议之前（协议带一长串规则与示例，埋在后面等于没说）
    assert text.index("—— 协作组名册") < text.index("输出协议：")


def test_step_prompt_without_a_roster_is_unchanged():
    """不给名册（老调用方、纯函数单测）时不凭空多出一段。"""
    from app.collab.prompt import TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    node = NodeSpec(key="se", agent_profile_id="r-se", duty=Duty.ENGINEER)
    ticket = TicketView(id="t1", title="做点事", brief={"goal": "目标"}, state=TicketState.OPEN.value)
    text = build_step_prompt(
        team_name="小组",
        workspace_path="/tmp/x",
        node=node,
        ticket=ticket,
        history=[],
        assignees=[],
        reviewer=None,
        is_entry=False,
    )
    assert "名册" not in text


def test_protocol_guide_never_speaks_for_the_whole_team_about_reviewers():
    """带 submit 但不是提交方的节点，不能被说成「本协作没有安排审查者」。

    真实事故：CTO 的契约是 delegate/done/submit/reject，它自己没有 review 出边，协议说明就渲染成
    「本协作没有安排审查者，提交即完成」；CTO 把这句关于**整张图**的断言原样写进下游工单与最终汇报。
    而组里其实配了审查者（SDE --review--> QA），于是审查明明跑了，用户收到的却是「没设置审查员」。
    """
    from app.collab.schemas import Duty

    guide = render_protocol_guide(
        duty_label="协调者",
        actions=(
            TicketAction.DELEGATE,
            TicketAction.DONE,
            TicketAction.SUBMIT,
            TicketAction.REJECT,
        ),
        assignees=["Leo-SDE（SDE）"],
        reviewer=None,
        duty=Duty.COORDINATOR,
    )
    assert "本协作没有安排审查者" not in guide
    # 只描述这张工单自己的结局：还是得让模型知道 submit 之后就没有审查环节了
    assert "不经过审查环节" in guide

    # 真正的提交方不受影响：交到审查者手里时照样报出是谁审
    submitter_guide = render_protocol_guide(
        duty_label="实施者",
        actions=(TicketAction.SUBMIT, TicketAction.DONE),
        assignees=[],
        reviewer="Vera-QA（QA）",
        duty=Duty.ENGINEER,
    )
    assert "你提交后由「Vera-QA（QA）」审查。" in submitter_guide


# --- 字符串里的裸引号（线上真实故障） ----------------------------------------------

#: 模型在 JSON 字符串里直接写 ASCII 双引号：第一个引号会提前结束字符串，整块判死。
INNER_QUOTES_BLOCK = """```work_order
{
  "action": "delegate",
  "assignee": "se",
  "mode": "split",
  "ticket": {
    "title": "UI 子工单",
    "goal": "步骤要能被非技术人员照着做通，含"换电脑恢复"的具体步骤",
    "requirements": ["样式只用内联 SVG", "不得引用 CDN"]
  }
}
```"""


def test_inner_unescaped_quotes_are_repaired():
    """字符串里的裸引号要被自动补转义，而不是把整轮运行判成协议错误。"""
    result = parse_work_order(INNER_QUOTES_BLOCK)
    assert result.ok, result.error
    order = result.order
    assert order is not None and order.ticket is not None
    assert '含"换电脑恢复"的具体步骤' in order.ticket.goal
    assert order.ticket.requirements == ["样式只用内联 SVG", "不得引用 CDN"]


def test_escape_inner_quotes_leaves_valid_json_alone():
    text = '{"a": 1, "b": ["x", "y"], "c": {"d": "e\\"f"}, "g": "收尾"}'
    assert escape_inner_quotes(text) == text
    assert sanitize_json_text(text) == text


def test_unparseable_block_error_keeps_position_and_hint():
    """真的救不回来时，报错要带**出错位置**与可操作的原因，不能只说「Expecting value」。"""
    result = parse_work_order(f'```{FENCE}\n{{"action": delegate}}\n```')
    assert not result.ok
    assert "第 1 行" in (result.error or "")
    assert "双引号" in (result.error or "")


def test_ticket_draft_keeps_description_field():
    """模型自造的 description 字段不能被静默丢掉（否则下游只收到一个标题）。"""
    only_description = TicketDraft.from_value({"title": "UI 子工单", "description": "第一行\n第二行"})
    assert only_description.goal == "第一行\n第二行"
    assert only_description.requirements == []

    with_goal = TicketDraft.from_value({"title": "t", "goal": "目标", "description": "细节一\n细节二"})
    assert with_goal.goal == "目标"
    assert with_goal.requirements == ["细节一", "细节二"]


# --- 入口智能体聊天里的需求细化块 ------------------------------------------------


def test_chat_update_is_optional_and_stripped_from_display_text():
    text = "进度正常，CTO 正在拆任务。"
    update, warning = parse_chat_update(text)
    assert update is None and warning is None
    assert extract_chat_body(text) == text


def test_chat_update_parses_additions_and_is_removed_from_body():
    text = (
        "好，我把这两条补进工单。\n"
        "```chat_update\n"
        '{"requirements_add": ["增加重置按钮"], "acceptance_add": ["重置后恢复默认主题"]}\n'
        "```\n"
    )
    update, warning = parse_chat_update(text)
    assert warning is None
    assert update is not None
    assert update.requirements_add == ["增加重置按钮"]
    assert update.acceptance_add == ["重置后恢复默认主题"]
    assert extract_chat_body(text) == "好，我把这两条补进工单。"


def test_chat_update_merge_is_additive_and_deduplicated():
    brief = {"goal": "暗色主题", "requirements": ["主题开关"], "acceptance": ["刷新后保持"]}
    merged, changed = merge_chat_update(
        brief,
        ChatUpdate(
            requirements_add=["主题开关", "增加重置按钮"],
            acceptance_add=["重置后恢复默认主题"],
        ),
    )
    assert merged["requirements"] == ["主题开关", "增加重置按钮"]
    assert merged["acceptance"] == ["刷新后保持", "重置后恢复默认主题"]
    assert changed == ["requirements", "acceptance"]
    # 原 brief 不被原地修改，纯函数语义
    assert brief["requirements"] == ["主题开关"]


def test_malformed_chat_update_warns_but_does_not_raise():
    update, warning = parse_chat_update("```chat_update\n{不是 JSON}\n```")
    assert update is None
    assert warning and "不是合法 JSON" in warning


# --- 入口智能体聊天里的「推进判断」块 --------------------------------------------


def test_chat_advance_is_optional_and_stripped_from_display_text():
    text = "进度正常，这事先不用动。"
    advance, warning = parse_chat_advance(text)
    assert advance is None and warning is None
    assert extract_chat_body(text) == text


def test_chat_advance_parses_reason_order_and_options():
    text = (
        "这需要真正改代码，我建议直接推进。\n"
        "```chat_advance\n"
        "{\n"
        '  "reason": "新功能要动手实现",\n'
        '  "order": "加一个导出按钮",\n'
        '  "options": [\n'
        '    {"label": "按建议推进", "value": "加一个导出按钮"},\n'
        '    {"label": "我来补充", "value": "", "freeform": true}\n'
        "  ]\n"
        "}\n"
        "```\n"
    )
    advance, warning = parse_chat_advance(text)
    assert warning is None
    assert advance is not None
    assert advance.reason == "新功能要动手实现"
    assert advance.order == "加一个导出按钮"
    assert [option.label for option in advance.options] == ["按建议推进", "我来补充"]
    assert advance.options[1].freeform is True
    assert advance.to_dict()["options"][0] == {
        "label": "按建议推进",
        "value": "加一个导出按钮",
        "freeform": False,
    }
    # 协议块不进聊天正文。
    assert extract_chat_body(text) == "这需要真正改代码，我建议直接推进。"


def test_chat_advance_without_options_gets_fallback_card():
    text = (
        "我来安排。\n"
        "```chat_advance\n"
        '{"reason": "需要改代码", "order": "把导出按钮加到登录页右上角"}\n'
        "```\n"
    )
    advance, _ = parse_chat_advance(text)
    assert advance is not None
    labels = [option.label for option in advance.options]
    assert labels == ["按建议推进", "我再想想"]
    assert advance.options[0].value == "把导出按钮加到登录页右上角"
    assert advance.options[1].freeform is True


def test_chat_advance_recovers_options_from_bulleted_order():
    text = (
        "按这两条来：\n"
        "```chat_advance\n"
        '{"reason": "分两步做", "order": "1) 先加导出按钮\\n2) 再加导入按钮"}\n'
        "```\n"
    )
    advance, _ = parse_chat_advance(text)
    assert advance is not None
    labels = [option.label for option in advance.options]
    assert labels == ["先加导出按钮", "再加导入按钮"]


def test_chat_advance_shares_body_with_chat_update_and_survives_bad_json():
    both = (
        "补一条要求，然后直接推进。\n"
        "```chat_update\n"
        '{"requirements_add": ["导出为 CSV"]}\n'
        "```\n"
        "```chat_advance\n"
        '{"reason": "要动手实现", "order": "支持导出 CSV"}\n'
        "```\n"
    )
    advance, warning = parse_chat_advance(both)
    assert advance is not None and warning is None
    # 两种块都会从正文里剥掉，用户只看到人话。
    assert extract_chat_body(both) == "补一条要求，然后直接推进。"
    # 解析失败只告警、不判死（与 chat_update 同一套容错策略）。
    broken, warning = parse_chat_advance("```chat_advance\n{不是 JSON}\n```")
    assert broken is None
    assert warning and "不是合法 JSON" in warning
    # 空块也只告警。
    empty, warning = parse_chat_advance("```chat_advance\n{}\n```")
    assert empty is None
    assert warning and "chat_advance" in warning
