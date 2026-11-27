"""一手账本（P0）：token 归一化、窗口口径、去重写入、权威源互斥与分段策略。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.db import SessionLocal, engine, init_db
from app.models import (
    AgentProfile,
    Conversation,
    Message,
    SQLModel,
    UsagePricing,
    UsageRecord,
)
from app.runtime import usage_ledger as ledger
from sqlmodel import select


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    """每个用例一份干净库：账本 / 平台设置表由 ``init_db`` 自动建（新表无需补列）。"""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    yield


def _counts(**overrides):
    base = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "reasoning_tokens": None,
        "context_tokens": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 归一化与窗口口径
# ---------------------------------------------------------------------------


def test_normalize_anthropic_exclusive_cache():
    """Anthropic 输入侧与缓存分离：窗口占用 = 输入 + 缓存读 + 缓存写。"""
    counts = ledger.normalize_usage(
        {
            "input_tokens": 75,
            "cache_read_input_tokens": 14_848,
            "cache_creation_input_tokens": 1_200,
            "output_tokens": 12,
        }
    )
    assert counts["input_tokens"] == 75
    assert counts["cache_read_tokens"] == 14_848
    assert counts["cache_creation_tokens"] == 1_200
    assert counts["output_tokens"] == 12
    assert counts["context_tokens"] == 75 + 14_848 + 1_200


def test_normalize_openai_chat_inclusive_cache():
    """OpenAI Chat 的 prompt_tokens 已含缓存：普通输入要减去缓存读，窗口占用 = prompt_tokens。"""
    counts = ledger.normalize_usage(
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 300},
        }
    )
    assert counts["input_tokens"] == 700
    assert counts["cache_read_tokens"] == 300
    assert counts["output_tokens"] == 50
    assert counts["context_tokens"] == 1_000


def test_normalize_openai_responses_inclusive_cache_and_reasoning():
    """OpenAI Responses：input_tokens 含缓存，缓存读与推理各自从 details 取。"""
    counts = ledger.normalize_usage(
        {
            "input_tokens": 800,
            "output_tokens": 60,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens_details": {"reasoning_tokens": 20},
        }
    )
    assert counts["input_tokens"] == 600
    assert counts["cache_read_tokens"] == 200
    assert counts["reasoning_tokens"] == 20
    assert counts["context_tokens"] == 800


def test_normalize_empty_is_none_not_zero():
    """没有可用计数时返回全 None：调用方记 unknown，绝不假装是 0。"""
    assert ledger.normalize_usage({}) == _counts()
    assert ledger.normalize_usage(None) == _counts()
    assert ledger.normalize_usage("nonsense") == _counts()
    # 只有布尔等无效字段，同样视为无计数。
    assert ledger.normalize_usage({"input_tokens": True}) == _counts()


def test_window_tokens_delegates_to_normalize():
    assert ledger.window_tokens({"input_tokens": 10, "cache_read_input_tokens": 5}) == 15
    assert ledger.window_tokens({"prompt_tokens": 42}) == 42
    assert ledger.window_tokens({}) is None


# ---------------------------------------------------------------------------
# 写入与去重
# ---------------------------------------------------------------------------


async def test_record_dedup_same_key_only_one_row():
    """(run_id, source, request_id) 重复写入只入账一次。"""
    async with SessionLocal() as session:
        first = ledger.build_record(run_id="r1", request_id="req-1", source=ledger.SOURCE_GATEWAY,
                                    counts=_counts(input_tokens=10, output_tokens=2, context_tokens=10))
        assert await ledger.record(session, first) is True
        # 同键重试（上游复用同一响应 id）：应被忽略。
        again = ledger.build_record(run_id="r1", request_id="req-1", source=ledger.SOURCE_GATEWAY,
                                    counts=_counts(input_tokens=999, output_tokens=999, context_tokens=999))
        assert await ledger.record(session, again) is False
        await session.commit()

        rows = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "r1")
            )
        ).scalars().all()
        assert len(rows) == 1
        # 去重不能把首次的有效值冲掉。
        assert rows[0].input_tokens == 10


async def test_record_different_request_id_both_rows():
    """上游为每次尝试生成新 id：视为两次真实请求，记两行。"""
    async with SessionLocal() as session:
        for request_id in ("req-a", "req-b"):
            row = ledger.build_record(run_id="r2", request_id=request_id, source=ledger.SOURCE_GATEWAY,
                                      counts=_counts(input_tokens=5, context_tokens=5))
            assert await ledger.record(session, row) is True
        await session.commit()
        count = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "r2")
            )
        ).scalars().all()
        assert len(count) == 2


async def test_record_usage_normalizes_and_writes():
    async with SessionLocal() as session:
        assert await ledger.record_usage(
            session,
            run_id="r3",
            request_id="req-3",
            source=ledger.SOURCE_NATIVE,
            raw_usage={"prompt_tokens": 100, "completion_tokens": 10,
                       "prompt_tokens_details": {"cached_tokens": 40}},
            owner_id="u1",
            conversation_id="c1",
        )
        await session.commit()
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "r3")
            )
        ).scalar_one()
        assert row.input_tokens == 60
        assert row.cache_read_tokens == 40
        assert row.context_tokens == 100


async def test_update_cost_only_touches_cost():
    async with SessionLocal() as session:
        await ledger.record_usage(
            session, run_id="r4", request_id="req-4", source=ledger.SOURCE_GATEWAY,
            raw_usage={"input_tokens": 10, "output_tokens": 1},
        )
        await session.commit()
        assert await ledger.update_cost(
            session, run_id="r4", source=ledger.SOURCE_GATEWAY, request_id="req-4",
            cost_usd=0.0012, cost_source=ledger.COST_REPORTED,
        )
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "r4")
            )
        ).scalar_one()
        assert row.cost_usd == pytest.approx(0.0012)
        assert row.cost_source == ledger.COST_REPORTED
        # token 计数不被成本补写改动。
        assert row.input_tokens == 10


# ---------------------------------------------------------------------------
# 权威源互斥
# ---------------------------------------------------------------------------


async def test_resolve_authority_prefers_gateway():
    """已有 gateway 行时，权威源判为 gateway（原生事件应丢弃入账）。"""
    async with SessionLocal() as session:
        assert await ledger.resolve_authority(session, "nope") == "none"
        await ledger.record_usage(session, run_id="auth", request_id="g1",
                                  source=ledger.SOURCE_GATEWAY, raw_usage={"input_tokens": 1})
        assert await ledger.resolve_authority(session, "auth") == ledger.SOURCE_GATEWAY


async def test_resolve_authority_native_fallback():
    """没有 gateway 行、只有 native 行时，权威源为 native。"""
    async with SessionLocal() as session:
        await ledger.record_usage(session, run_id="auth2", request_id="n1",
                                  source=ledger.SOURCE_NATIVE, raw_usage={"input_tokens": 1})
        assert await ledger.resolve_authority(session, "auth2") == ledger.SOURCE_NATIVE
        # 同 run 再补一行 gateway → 翻转成 gateway。
        await ledger.record_usage(session, run_id="auth2", request_id="g1",
                                  source=ledger.SOURCE_GATEWAY, raw_usage={"input_tokens": 1})
        assert await ledger.resolve_authority(session, "auth2") == ledger.SOURCE_GATEWAY


# ---------------------------------------------------------------------------
# 分段策略
# ---------------------------------------------------------------------------


async def test_ledger_started_at_is_idempotent():
    async with SessionLocal() as session:
        first = await ledger.mark_ledger_started(session)
        second = await ledger.mark_ledger_started(session)
        assert first == second
        assert await ledger.get_ledger_started_at(session) == first


async def test_apply_cost_uses_longest_prefix_and_never_recomputes():
    """手工单价：最长前缀优先；不匹配保持 unknown；已有 cost 不重算。"""
    async with SessionLocal() as session:
        session.add(
            UsagePricing(
                owner_id=None, model_pattern="claude-",
                input_per_million=3.0, output_per_million=15.0,
            )
        )
        session.add(
            UsagePricing(
                owner_id=None, model_pattern="claude-sonnet-4",
                input_per_million=2.0, output_per_million=10.0,
            )
        )
        await session.commit()

        record = ledger.build_record(
            run_id="cost-run", request_id="c1", source=ledger.SOURCE_GATEWAY,
            counts=ledger.normalize_usage(
                {"input_tokens": 1_000_000, "output_tokens": 100_000}
            ),
            model="claude-sonnet-4-20250514",
        )
        await ledger.apply_cost(session, record)
        # 最长前缀 claude-sonnet-4：1M × 2/1e6 + 0.1M × 10/1e6 = 3.0
        assert record.cost_usd == pytest.approx(3.0)
        assert record.cost_source == ledger.COST_ESTIMATED

        unmatched = ledger.build_record(
            run_id="cost-run", request_id="c2", source=ledger.SOURCE_GATEWAY,
            counts=ledger.normalize_usage({"input_tokens": 10}), model="gpt-x",
        )
        await ledger.apply_cost(session, unmatched)
        assert unmatched.cost_usd is None and unmatched.cost_source == ledger.COST_UNKNOWN

        # 只有窗口快照、没有计费计数：不应被算成 0 美元（保持 unknown）。
        context_only = ledger.build_record(
            run_id="cost-run", request_id="c4", source=ledger.SOURCE_GATEWAY,
            counts={**ledger.normalize_usage({}), "context_tokens": 300},
            model="claude-sonnet-4",
        )
        await ledger.apply_cost(session, context_only)
        assert context_only.cost_usd is None

        already = ledger.build_record(
            run_id="cost-run", request_id="c3", source=ledger.SOURCE_GATEWAY,
            counts=ledger.normalize_usage({"input_tokens": 10}),
            model="claude-sonnet-4", cost_usd=99.0,
        )
        await ledger.apply_cost(session, already)
        assert already.cost_usd == 99.0  # 不重算


async def test_ledger_totals_aggregates():
    async with SessionLocal() as session:
        await ledger.record_usage(session, run_id="t1", request_id="r1",
                                  source=ledger.SOURCE_GATEWAY,
                                  raw_usage={"input_tokens": 100, "output_tokens": 20,
                                             "cache_read_input_tokens": 300})
        await ledger.record_usage(session, run_id="t1", request_id="r2",
                                  source=ledger.SOURCE_GATEWAY,
                                  raw_usage={"prompt_tokens": 50, "completion_tokens": 5,
                                             "prompt_tokens_details": {"cached_tokens": 10}})
        await session.commit()
        totals = await ledger.ledger_totals(session, owner_id=None)
        # r1：input 100 / cache_read 300 / output 20；r2：input 40 / cache_read 10 / output 5
        assert totals["input"] == 140
        assert totals["cache_read"] == 310
        assert totals["output"] == 25
        assert totals["total"] == 140 + 310 + 25
        assert totals["messages"] == 2


def _load_backfill_module():
    """加载仓库根 ``scripts/backfill_usage_records.py``（脚本自己把 backend 加进 sys.path）。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "backfill_usage_records.py"
    spec = importlib.util.spec_from_file_location("backfill_usage_records", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_backfill_script_dry_run_then_apply_moves_boundary():
    """回填脚本：默认 dry-run 不写库；apply 后账本覆盖历史、边界前移到最早消息之前。"""
    module = _load_backfill_module()
    async with SessionLocal() as session:
        session.add(AgentProfile(id="bf-profile", name="历史智能体", type_id="claude-code"))
        session.add(Conversation(id="bf-conv", title="历史", agent_profile_id="bf-profile"))
        await session.commit()
        session.add(
            Message(
                conversation_id="bf-conv", role="assistant", content="旧回复",
                usage={"input_tokens": 100, "output_tokens": 10},
                created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    # dry-run：不写账本。
    assert await module._run(apply=False, verbose=False) == 0
    async with SessionLocal() as session:
        assert (await session.execute(select(UsageRecord))).scalars().all() == []

    # apply：写一行 legacy，并把启用时刻前移到最早消息之前。
    assert await module._run(apply=True, verbose=False) == 0
    async with SessionLocal() as session:
        rows = (await session.execute(select(UsageRecord))).scalars().all()
        started = await ledger.get_ledger_started_at(session)
    assert len(rows) == 1
    assert rows[0].source == ledger.SOURCE_LEGACY
    assert rows[0].input_tokens == 100
    assert rows[0].context_tokens == 100
    assert started is not None and started < datetime(2026, 11, 1, tzinfo=timezone.utc)


async def test_ledger_totals_time_filter():
    async with SessionLocal() as session:
        await ledger.record_usage(session, run_id="tf", request_id="old",
                                  source=ledger.SOURCE_GATEWAY, raw_usage={"input_tokens": 1})
        await session.commit()
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "tf")
            )
        ).scalar_one()
        row.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await session.commit()

        all_totals = await ledger.ledger_totals(session)
        assert all_totals["messages"] == 1
        recent = await ledger.ledger_totals(
            session, since=datetime(2021, 1, 1, tzinfo=timezone.utc)
        )
        assert recent["messages"] == 0


async def test_apply_cost_falls_back_to_models_dev_catalog(monkeypatch):
    """没有手工价时用目录价；来源记 estimated，并写入 cost_pricing 依据。"""
    from app import model_catalog

    price = model_catalog.CatalogPrice(
        provider="anthropic", model="claude-x",
        input_per_million=3.0, output_per_million=15.0,
        cache_read_per_million=0.3, cache_creation_per_million=3.75,
        sha256="deadbeef", downloaded_at="2026-11-27T00:00:00+00:00",
    )
    monkeypatch.setattr(
        model_catalog, "lookup",
        lambda provider, model: price if model == "claude-x" else None,
    )
    async with SessionLocal() as session:
        record = ledger.build_record(
            run_id="catalog-run", request_id="c1", source=ledger.SOURCE_GATEWAY,
            counts=ledger.normalize_usage(
                {"input_tokens": 1_000_000, "output_tokens": 100_000}
            ),
            model="claude-x", provider="anthropic",
        )
        await ledger.apply_cost(session, record)
    assert record.cost_usd == pytest.approx(4.5)  # 1M×3 + 0.1M×15
    assert record.cost_source == ledger.COST_ESTIMATED
    assert record.cost_pricing["source"] == "models.dev"
    assert record.cost_pricing["catalog_sha256"] == "deadbeef"


async def test_apply_cost_unknown_model_stays_unknown(monkeypatch):
    """目录里没有 provider/model 对 → 保持 unknown，不假装 0。"""
    from app import model_catalog

    monkeypatch.setattr(model_catalog, "lookup", lambda provider, model: None)
    async with SessionLocal() as session:
        record = ledger.build_record(
            run_id="unknown-price", request_id="c1", source=ledger.SOURCE_GATEWAY,
            counts=ledger.normalize_usage({"input_tokens": 10}), model="relay/unknown-model",
        )
        await ledger.apply_cost(session, record)
    assert record.cost_usd is None
    assert record.cost_source == ledger.COST_UNKNOWN


async def test_record_isolated_applies_catalog_cost_on_write(monkeypatch):
    """写入路径：没有手工价时自动套 models.dev 目录价（estimated + cost_pricing）。"""
    from app import model_catalog

    price = model_catalog.CatalogPrice(
        provider="anthropic", model="claude-x",
        input_per_million=3.0, output_per_million=15.0,
        cache_read_per_million=0.0, cache_creation_per_million=0.0,
        sha256="cafe", downloaded_at="2026-11-27T00:00:00+00:00",
    )
    monkeypatch.setattr(
        model_catalog, "lookup",
        lambda provider, model: price if model == "claude-x" else None,
    )
    record = ledger.build_record(
        run_id="iso-cost", request_id="r1", source=ledger.SOURCE_GATEWAY,
        counts=ledger.normalize_usage({"input_tokens": 1_000_000}),
        model="claude-x", provider="anthropic",
    )
    assert await ledger.record_isolated(record)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(UsageRecord).where(UsageRecord.run_id == "iso-cost")
            )
        ).scalar_one()
    assert row.cost_usd == pytest.approx(3.0)
    assert row.cost_source == ledger.COST_ESTIMATED
    assert row.cost_pricing["source"] == "models.dev"
