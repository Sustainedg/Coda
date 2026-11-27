"""P3：原始留痕、双轨标记与跨来源幂等去重。"""
from __future__ import annotations

import json

import pytest_asyncio
from sqlmodel import select

from app.api.usage import _record_to_dict
from app.db import SessionLocal, engine, init_db
from app.agents import metering as m
from app.models import SQLModel, UsageRecord
from app.runtime import usage_ledger as ledger


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    """每个用例一份干净库：新列 / 新唯一索引都由 init_db 建好。"""
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


def _row(
    *,
    source: str,
    request_id: str,
    upstream_request_id: str | None,
    input_tokens: int,
    output_tokens: int,
    run_id: str = "run-1",
    protocol: str | None = m.PROTOCOL_OPENAI_RESPONSES,
    raw_usage: dict | None = None,
) -> UsageRecord:
    return ledger.build_record(
        run_id=run_id,
        request_id=request_id,
        source=source,
        counts=_counts(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_tokens=input_tokens,
        ),
        agent_type_id="codex",
        model="gpt-test",
        usage_protocol=protocol,
        upstream_request_id=upstream_request_id,
        raw_usage=raw_usage
        or {"input_tokens": input_tokens, "output_tokens": output_tokens},
    )


async def _insert(record: UsageRecord) -> bool:
    async with SessionLocal() as session:
        added = await ledger.record(session, record)
        await session.commit()
        return added


async def _all_rows() -> list[UsageRecord]:
    async with SessionLocal() as session:
        return list(
            (await session.execute(select(UsageRecord).order_by(UsageRecord.id)))
            .scalars()
            .all()
        )


def test_build_record_persists_raw_and_dual_track_fields():
    record = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="chatcmpl-1",
        upstream_request_id="chatcmpl-1",
        input_tokens=60,
        output_tokens=5,
    )
    assert record.telemetry_source == m.TELEMETRY_SOURCE_GATEWAY_PROXY
    assert record.usage_protocol == m.PROTOCOL_OPENAI_RESPONSES
    assert record.normalizer_version == m.NORMALIZER_VERSION == "v1"
    assert record.response_shape == m.RESPONSE_SHAPE_RESPONSES
    assert record.upstream_request_id == "chatcmpl-1"
    assert record.raw_usage == {"input_tokens": 60, "output_tokens": 5}

    native = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id=None,
        input_tokens=4,
        output_tokens=6,
    )
    assert native.telemetry_source == m.TELEMETRY_SOURCE_AGENT_REPORT


def test_build_record_structured_truncation_keeps_valid_json():
    raw = {"input_tokens": 1, "output_tokens": 2, "noise": "x" * 20_000}
    record = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="req-big",
        upstream_request_id="req-big",
        input_tokens=1,
        output_tokens=2,
        raw_usage=raw,
    )
    stored = record.raw_usage
    assert stored["_truncated"] is True
    assert stored["_original_bytes"] > m.RAW_USAGE_LIMIT_BYTES
    assert "noise" in stored["_keys"]
    assert stored["usage"]["input_tokens"] == 1
    json.dumps(stored, ensure_ascii=False)


async def test_gateway_promotes_agent_report_for_same_upstream_id():
    native = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id="resp_same",
        input_tokens=100,
        output_tokens=10,
    )
    assert await _insert(native) is True

    gateway = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="resp_same",
        upstream_request_id="resp_same",
        input_tokens=200,
        output_tokens=20,
    )
    assert await _insert(gateway) is False  # 不是新增，而是覆盖同一行

    rows = await _all_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.source == ledger.SOURCE_GATEWAY
    assert row.telemetry_source == m.TELEMETRY_SOURCE_GATEWAY_PROXY
    assert row.input_tokens == 200
    assert row.output_tokens == 20
    assert row.upstream_request_id == "resp_same"
    assert row.raw_usage == {"input_tokens": 200, "output_tokens": 20}
    # 身份 / 时间列保持第一次观察到的值。
    assert row.request_id == "run-1:native"


async def test_empty_gateway_does_not_overwrite_agent_report():
    """网关未解析出 usage 时，空值不能覆盖自报的有效计数。"""
    native = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id="resp_empty",
        input_tokens=100,
        output_tokens=10,
    )
    assert await _insert(native) is True

    empty_gateway = ledger.build_record(
        run_id="run-1",
        request_id="resp_empty",
        source=ledger.SOURCE_GATEWAY,
        counts=_counts(),  # 全 None
        upstream_request_id="resp_empty",
    )
    assert await _insert(empty_gateway) is False

    rows = await _all_rows()
    assert len(rows) == 1
    assert rows[0].source == ledger.SOURCE_NATIVE
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 10


async def test_gateway_first_is_not_downgraded_by_agent_report():
    gateway = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="chatcmpl-1",
        upstream_request_id="chatcmpl-1",
        input_tokens=60,
        output_tokens=5,
    )
    assert await _insert(gateway) is True

    native = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id="chatcmpl-1",
        input_tokens=999,
        output_tokens=999,
    )
    assert await _insert(native) is False

    rows = await _all_rows()
    assert len(rows) == 1
    assert rows[0].source == ledger.SOURCE_GATEWAY
    assert rows[0].input_tokens == 60


async def test_rows_without_upstream_id_are_not_deduped_across_sources():
    native = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id=None,
        input_tokens=100,
        output_tokens=10,
    )
    gateway = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="run-1:1",
        upstream_request_id=None,
        input_tokens=200,
        output_tokens=20,
    )
    assert await _insert(native) is True
    assert await _insert(gateway) is True
    assert len(await _all_rows()) == 2


async def test_old_dedup_key_still_protects_same_source_request():
    first = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id=None,
        input_tokens=100,
        output_tokens=10,
    )
    second = _row(
        source=ledger.SOURCE_NATIVE,
        request_id="run-1:native",
        upstream_request_id=None,
        input_tokens=999,
        output_tokens=999,
    )
    assert await _insert(first) is True
    assert await _insert(second) is False
    rows = await _all_rows()
    assert len(rows) == 1
    assert rows[0].input_tokens == 100


async def test_native_snapshot_stores_raw_protocol_and_shape():
    ok = await ledger.record_native_snapshot_isolated(
        run_id="run-native",
        source=ledger.SOURCE_NATIVE,
        counts=_counts(input_tokens=4, output_tokens=6, context_tokens=5),
        raw_usage={
            "input_tokens": 5,
            "cached_input_tokens": 1,
            "output_tokens": 6,
        },
        usage_protocol=m.PROTOCOL_OPENAI_RESPONSES,
        finalize=True,
        started_at_hint=None,
        agent_type_id="codex",
        model="gpt-test",
    )
    assert ok is True
    rows = await _all_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.usage_protocol == m.PROTOCOL_OPENAI_RESPONSES
    assert row.response_shape == m.RESPONSE_SHAPE_RESPONSES
    assert row.telemetry_source == m.TELEMETRY_SOURCE_AGENT_REPORT
    assert row.normalizer_version == "v1"
    assert row.raw_usage == {
        "input_tokens": 5,
        "cached_input_tokens": 1,
        "output_tokens": 6,
    }


def test_record_to_dict_includes_new_audit_fields():
    record = _row(
        source=ledger.SOURCE_GATEWAY,
        request_id="req-audit",
        upstream_request_id="req-audit",
        input_tokens=1,
        output_tokens=2,
    )
    data = _record_to_dict(record)
    for key in (
        "telemetry_source",
        "usage_protocol",
        "normalizer_version",
        "response_shape",
        "upstream_request_id",
        "raw_usage",
    ):
        assert key in data
    assert data["telemetry_source"] == m.TELEMETRY_SOURCE_GATEWAY_PROXY
    assert data["upstream_request_id"] == "req-audit"