"""The benchmark generator must be deterministic and internally consistent."""
from __future__ import annotations

import pytest

from finance_controller.synth import PROFILES, generate


@pytest.mark.parametrize("profile", list(PROFILES))
def test_deterministic(profile):
    assert generate(profile, 7) == generate(profile, 7)


@pytest.mark.parametrize("profile", list(PROFILES))
def test_labels_reference_real_entries(profile):
    d = generate(profile, 7)
    ids = {r["id"] for src in ("payments", "settlements", "bank", "ledger") for r in d[src]}
    for group in d["labels"].values():
        assert set(group) <= ids
    assert set(d["truth"]) <= ids


@pytest.mark.parametrize("profile", list(PROFILES))
def test_amounts_parse_as_money(profile):
    d = generate(profile, 7)
    for r in d["payments"]:
        assert float(r["amount"]) > 0


def test_seed_changes_output():
    assert generate("realistic", 1) != generate("realistic", 2)
