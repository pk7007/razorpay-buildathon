"""Residual resolver — both backends, plus the LLM contract (mocked)."""
from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from finance_controller import resolver as R
from finance_controller.audit import AuditLog
from finance_controller.models import Entry


def _e(id, source, amt, d="2026-07-01", ref=None, narr=None):
    return Entry(id=id, source=source, amount_paise=amt, value_date=date.fromisoformat(d),
                 reference=ref, narration=narr)


def test_heuristic_pairs_close_amount_and_date():
    res = [
        _e("pay_1", "payment", 500000, "2026-07-01", narr="upi acme"),
        _e("bank_1", "bank", 500000, "2026-07-02", narr="neft acme ref"),
        _e("pay_2", "payment", 999999, "2026-07-01"),
    ]
    groups, leftover, usage = R._heuristic_resolve(res, AuditLog())
    assert len(groups) == 1
    assert set(groups[0].entry_ids) == {"bank_1", "pay_1"}
    assert {e.id for e in leftover} == {"pay_2"}
    assert usage["llm_calls"] == 0


def test_parse_groups_tolerates_fencing_and_junk():
    assert R._parse_groups('```json\n{"groups":[{"entry_ids":["a","b"],"confidence":0.9}]}\n```')
    assert R._parse_groups("not json at all") == []
    assert R._parse_groups('{"groups": []}') == []


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 90


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMsg:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **_):
        return _FakeMsg(self._text)


@pytest.fixture
def llm_on(monkeypatch):
    patched = dataclasses.replace(
        R.SETTINGS, anthropic_api_key="sk-ant-test", resolver_accept_threshold=0.72
    )
    monkeypatch.setattr(R, "SETTINGS", patched)


def test_llm_backend_accepts_valid_proposal(llm_on, monkeypatch):
    payload = '{"groups":[{"entry_ids":["pay_9","bank_9"],"confidence":0.95,"rationale":"x"}]}'
    monkeypatch.setattr("anthropic.Anthropic", lambda **_: _FakeClient(payload))

    res = [_e("pay_9", "payment", 700000), _e("bank_9", "bank", 700000), _e("x", "ledger", 1)]
    groups, leftover, usage = R._llm_resolve(res, AuditLog())
    assert len(groups) == 1 and set(groups[0].entry_ids) == {"bank_9", "pay_9"}
    assert groups[0].stage == "agent"
    assert usage["llm_calls"] == 1 and usage["llm_cost_usd"] > 0
    assert {e.id for e in leftover} == {"x"}


def test_llm_backend_rejects_low_confidence_and_hallucinated_ids(llm_on, monkeypatch):
    payload = (
        '{"groups":[{"entry_ids":["pay_9","GHOST"],"confidence":0.99},'
        '{"entry_ids":["pay_9","bank_9"],"confidence":0.10}]}'
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **_: _FakeClient(payload))
    res = [_e("pay_9", "payment", 700000), _e("bank_9", "bank", 700000)]
    groups, leftover, _ = R._llm_resolve(res, AuditLog())
    assert groups == []                       # one hallucinated id, one below threshold
    assert {e.id for e in leftover} == {"pay_9", "bank_9"}


def test_resolve_falls_back_to_heuristic_when_llm_raises(llm_on, monkeypatch):
    def boom(**_):
        raise RuntimeError("network down")
    monkeypatch.setattr("anthropic.Anthropic", boom)
    res = [_e("pay_1", "payment", 500000, narr="acme"), _e("bank_1", "bank", 500000, narr="acme")]
    groups, _, usage = R.resolve(res, AuditLog())
    assert usage["llm_calls"] == 0            # heuristic took over
    assert len(groups) == 1
