#!/usr/bin/env python3
"""把历史消息里的 ``usage`` 回填进 ``usage_records``（默认 **dry-run**）。

背景见 ``docs/usage-gateway.md`` 的「审计与回填」章节。

账本是新的权威源，但升级前的历史用量只存在于 ``messages.usage``。这个脚本把**账本启用
之前**的助手消息补成 ``source=legacy`` 的账本行，并把 ``usage_ledger_started_at`` 前移到
最早一条历史之前。之后用量页「账本段」覆盖全历史、「旧消息段」自然为空，数字不再依赖
消息表，删对话也不会丢历史用量。

安全约定：

* **默认 dry-run**，只统计不写库；确认后加 ``--apply``；
* **幂等**：``run_id = legacy:<message.id>``、``request_id = message.id``，靠账本唯一键去重，
  重复执行不会多记；
* **只补账本启用之前**的消息：启用之后的消息已由记录网关 / 原生运行层记账，回填会双记；
* 回填行的时间用**消息创建时刻**，不是脚本运行时刻，保证按天统计正确。

用法（数据库由 ``CODA_DATA_DIR`` / ``CODA_DATABASE_URL`` 指向真实库）：

```bash
CODA_DATA_DIR=/app/data python scripts/backfill_usage_records.py          # 预览
CODA_DATA_DIR=/app/data python scripts/backfill_usage_records.py --apply  # 写入
```
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlmodel import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Conversation, Message  # noqa: E402
from app.runtime import usage_ledger  # noqa: E402


def _aware(value: datetime | None) -> datetime | None:
    """SQLite 取出的时间可能是裸 UTC；统一补上时区再比较。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def _run(*, apply: bool, verbose: bool) -> int:
    async with SessionLocal() as session:
        started_at = await usage_ledger.get_ledger_started_at(session)
        rows = (
            await session.execute(
                select(Message, Conversation).join(
                    Conversation, Conversation.id == Message.conversation_id
                ).where(Message.role == "assistant")
            )
        ).all()

        candidates: list[tuple[Message, Conversation, dict]] = []
        for message, conversation in rows:
            if not message.usage:
                continue
            created_at = _aware(message.created_at)
            if started_at is not None and created_at is not None and created_at >= started_at:
                # 账本启用后的消息由运行时 / 网关记账，回填会双记。
                continue
            counts = usage_ledger.normalize_usage(message.usage)
            if all(value is None for value in counts.values()):
                continue
            candidates.append((message, conversation, counts))

        if not candidates:
            print("没有需要回填的历史消息（账本已覆盖或没有可用 usage）。")
            return 0

        print(f"候选历史消息：{len(candidates)} 条（仅 created_at < 账本启用时刻）")
        if not apply:
            for message, _conversation, counts in candidates[:10]:
                print(
                    f"  - {message.id} {message.created_at:%Y-%m-%d %H:%M} "
                    f"input={counts['input_tokens']} output={counts['output_tokens']} "
                    f"context={counts['context_tokens']}"
                )
            if len(candidates) > 10:
                print(f"  …… 其余 {len(candidates) - 10} 条")
            print("dry-run：未写入。确认无误后加 --apply。")
            return 0

        written = 0
        for message, conversation, counts in candidates:
            created_at = _aware(message.created_at)
            record = usage_ledger.build_record(
                run_id=f"legacy:{message.id}",
                request_id=message.id,
                source=usage_ledger.SOURCE_LEGACY,
                counts=counts,
                owner_id=conversation.owner_id,
                conversation_id=conversation.id,
                agent_profile_id=conversation.agent_profile_id,
                created_at=created_at,
                request_started_at=created_at,
            )
            if await usage_ledger.record(session, record):
                written += 1
                if verbose:
                    print(f"  + {message.id}")
        await session.commit()

        # 回填完成后把启用边界前移到最早一条历史之前：
        # 旧消息段（created_at < started_at）变为空，账本段覆盖全历史。
        earliest = min(_aware(message.created_at) for message, _, _ in candidates)
        target = earliest - timedelta(seconds=1)
        if started_at is None or target < started_at:
            await usage_ledger.mark_ledger_started(session, when=target)
        print(
            f"已写入 {written} 行（重复行被唯一约束忽略）；"
            f"账本启用时刻已前移到 {target.isoformat()}。"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把历史 messages.usage 回填进 usage_records（默认 dry-run）"
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    parser.add_argument("--verbose", action="store_true", help="逐条打印写入的消息 id")
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply, verbose=args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())