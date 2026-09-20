from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from quanttrading.signals import Signal
from tests.helpers import make_signal


def test_limit_order_requires_limit_price() -> None:
    with pytest.raises(ValidationError, match="limit_price"):
        make_signal(order_type="limit", limit_price=None)


def test_strength_must_be_unit_interval() -> None:
    with pytest.raises(ValidationError):
        make_signal(strength=1.5)
    with pytest.raises(ValidationError):
        make_signal(strength=-0.01)


def test_open_requires_positive_qty() -> None:
    with pytest.raises(ValidationError, match="qty"):
        make_signal(intent="open", qty=0)


def test_flat_requires_close_or_reduce() -> None:
    with pytest.raises(ValidationError, match="flat"):
        make_signal(side="flat", intent="open", qty=1)


def test_json_roundtrip() -> None:
    signal = make_signal(
        ts=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        symbol="BTC/USDT:USDT",
        order_type="limit",
        limit_price=65000.5,
        meta={"note": "v0.1"},
    )
    raw = signal.model_dump_json()
    restored = Signal.model_validate_json(raw)
    assert restored.symbol == "BTC/USDT:USDT"
    assert restored.limit_price == 65000.5
    assert restored.ts == signal.ts
    assert restored.model_dump(mode="json")["ts"].endswith("Z")
