#!/usr/bin/env python3
"""「任务进度卡片」替身内核：按提示词决定输出建卡 / 更新 / 闲聊 / 坏 JSON。

- 提示词里没有任务卡协议 → 输出普通闲聊正文（服务端不会建卡）；
- 协议里写「当前卡片：无」→ 输出一个合法的 create 块；
- 已有卡片 → 输出一个只报状态变化的 update 块；
- 启动参数 ``bad`` → 输出被截断的坏块，用于验证失败降级。
"""
import json
import sys

prompt = sys.stdin.read()
mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
has_notice = "【任务进度卡片协议】" in prompt

if mode == "bad":
    body = "我试试整理进度。\n\n```task_card\n{\"action\":\"create\",\"items\":[\n```"
elif not has_notice:
    body = "你好呀，今天想聊点什么？"
elif "当前卡片：无" in prompt:
    body = (
        "我先把这件事拆成两步。\n\n"
        "```task_card\n"
        '{"action":"create","title":"实现进度卡","items":['
        '{"id":"t1","text":"后端存储与接口","done":false},'
        '{"id":"t2","text":"前端卡片组件","done":false}]}\n'
        "```"
    )
else:
    body = (
        "第一步已经完成。\n\n"
        "```task_card\n"
        '{"action":"update","items":[{"id":"t1","done":true}]}\n'
        "```"
    )

print(json.dumps({"type": "session", "session_id": "task-card-session"}), flush=True)
print(json.dumps({"type": "message", "delta": body}), flush=True)
print(json.dumps({"type": "result", "usage": {"input_tokens": 9, "output_tokens": 12}}), flush=True)
