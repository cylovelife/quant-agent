"""ETF qfq fallback requires explicit, validated adjustment evidence.

@author cylovelife
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

import quant_agent.infrastructure.market_data.fetcher as fetcher  # noqa: E402
from quant_agent.infrastructure.market_data.datasources import base  # noqa: E402
from quant_agent.infrastructure.market_data.datasources import (
    etf_qfq_source as source,  # noqa: E402
)


def _factors(code="510300"):
    return {
        "total": 3,
        "data": [
            {"d": "2026-01-19", "f": "1", "s": "1", "u": "0"},
            {"d": "2025-06-18", "f": "1", "s": "1", "u": "0.123"},
            {"d": "1900-01-01", "f": "1", "s": "1", "u": "0.211"},
        ],
    }


def _raw():
    return [
        {
            "day": "2025-06-17",
            "open": "3.991",
            "close": "3.989",
            "high": "3.997",
            "low": "3.974",
            "volume": "482891300",
        },
        {
            "day": "2025-06-18",
            "open": "3.903",
            "close": "3.904",
            "high": "3.910",
            "low": "3.882",
            "volume": "480803410",
        },
        {
            "day": "2026-01-19",
            "open": "4.737",
            "close": "4.738",
            "high": "4.769",
            "low": "4.718",
            "volume": "2913439864",
        },
    ]


def _texts(factors=None, raw=None, code="510300"):
    return (
        "var _quant_etf_kline=(" + json.dumps(_raw() if raw is None else raw) + ");",
        "var sh"
        + code
        + "qfq="
        + json.dumps(_factors() if factors is None else factors)
        + "\n/* receipt */",
    )


def _supplement():
    rows = []
    for row in _raw():
        rows.append(
            [
                row["day"],
                row["open"],
                row["close"],
                row["high"],
                row["low"],
                float(row["volume"]) / 100,
                {},
                "1",
                "100",
            ]
        )
    return {"symbol": "sh510300", "source": "fixture_tencent_day", "day": rows}


def test_cash_dividend_factor_is_applied_before_and_on_the_ex_date():
    frame, receipt = source.normalize_qfq("510300", *_texts(), supplement=_supplement())
    assert list(frame.close) == pytest.approx([3.778, 3.781, 4.738])
    assert list(frame.open) == pytest.approx([3.780, 3.780, 4.737])
    assert list(frame.volume) == pytest.approx([4828913, 4808034.1, 29134398.64])
    assert list(frame.amount) == [1000000, 1000000, 1000000]
    assert receipt["factor_rows"] == _factors()["data"]
    assert frame.attrs["adjust"] == "qfq"


def test_explicit_identity_factor_is_accepted_but_missing_factors_are_not():
    factor = {"total": 1, "data": [{"d": "1900-01-01", "f": "1", "s": "1", "u": "0"}]}
    frame, _ = source.normalize_qfq(
        "512880",
        *_texts(factor, code="512880"),
        supplement={**_supplement(), "symbol": "sh512880"},
    )
    assert list(frame.close) == [float(row["close"]) for row in _raw()]
    with pytest.raises(ValueError):
        source.normalize_qfq("512880", *_texts({"total": 0, "data": []}, code="512880"))


@pytest.mark.parametrize(
    "mutation",
    [
        "count",
        "wrong_identity",
        "split",
        "future",
        "coverage",
        "unordered",
        "duplicate",
        "negative",
        "unanchored",
    ],
)
def test_invalid_or_unsupported_adjustment_evidence_is_rejected(mutation):
    factor = _factors()
    if mutation == "count":
        factor["total"] = 99
    elif mutation == "split":
        factor["data"][1]["s"] = "2"
    elif mutation == "future":
        factor["data"][0]["d"] = "2030-01-01"
    elif mutation == "coverage":
        factor["data"][-1]["d"] = "2025-06-18"
    elif mutation == "unordered":
        factor["data"] = list(reversed(factor["data"]))
    elif mutation == "duplicate":
        factor["data"][2]["d"] = factor["data"][1]["d"]
    elif mutation == "negative":
        factor["data"][2]["u"] = "-1"
    elif mutation == "unanchored":
        factor["data"][0]["u"] = "0.01"
    raw, factors = _texts(factor)
    if mutation == "wrong_identity":
        factors = factors.replace("sh510300qfq", "sh512880qfq")
    with pytest.raises(ValueError):
        source.normalize_qfq("510300", raw, factors)


def test_bad_raw_row_rejects_the_complete_window():
    raw = _raw()
    raw[0]["close"] = "bad"
    with pytest.raises(ValueError):
        source.normalize_qfq("510300", *_texts(raw=raw))


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "identity",
        "missing_day",
        "missing_amount",
        "bad_amount",
        "price_mismatch",
    ],
)
def test_turnover_must_be_actual_and_match_the_same_raw_bars(damage):
    supplement = _supplement()
    if damage == "missing":
        supplement = None
    elif damage == "identity":
        supplement["symbol"] = "sh512880"
    elif damage == "missing_day":
        supplement["day"].pop()
    elif damage == "missing_amount":
        supplement["day"][0] = supplement["day"][0][:6]
    elif damage == "bad_amount":
        supplement["day"][0][8] = ""
    else:
        supplement["day"][0][2] = "99"
    with pytest.raises(ValueError):
        source.normalize_qfq("510300", *_texts(), supplement=supplement)


def test_explicit_zero_turnover_is_distinct_from_a_missing_amount():
    supplement = _supplement()
    supplement["day"][0][8] = "0"
    frame, receipt = source.normalize_qfq("510300", *_texts(), supplement=supplement)
    assert frame.amount.iloc[0] == 0
    assert receipt["turnover_supplement"]["day"][0][8] == "0"
    assert receipt["amount_status"] == "actual_tencent_day_field_8_in_10000_CNY"


def test_receipt_content_hash_is_bound_to_source(tmp_path, monkeypatch):
    raw, factors = _texts()
    monkeypatch.setattr(base, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(source, "_GUARD", base.SourceGuard("fixture", min_gap=0, retries=1))
    monkeypatch.setattr(base.store, "record_health", lambda *_a, **_kw: None)

    def get(url, params=None, **kwargs):
        assert kwargs["retries"] == 1
        assert kwargs["timeout"] <= 8
        if "sina" not in url:
            return SimpleNamespace(
                json=lambda: {"data": {"sh510300": {"day": _supplement()["day"]}}}
            )
        return SimpleNamespace(text=factors if url.endswith("qfq.js") else raw)

    result = source.fetch_etf_qfq("510300", 2, get=get, receipt_dir=tmp_path / "receipts")
    assert len(result) == 3
    source_id = result.attrs["source"]
    digest = source_id.rsplit(":", 1)[1]
    payload = (tmp_path / "receipts" / f"{digest}.json").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == digest
    assert json.loads(payload)["factor_rows"] == _factors()["data"]


def test_fetcher_uses_validated_sina_etf_only_after_tencent_qfq_is_missing(tmp_path, monkeypatch):
    raw, factors = _texts()
    monkeypatch.setattr(base, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(fetcher, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(source, "_GUARD", base.SourceGuard("fixture", min_gap=0, retries=1))
    monkeypatch.setattr(base.store, "record_health", lambda *_a, **_kw: None)
    called = []

    def get(url, params=None, **_):
        called.append(url)
        if "sina" in url:
            return SimpleNamespace(text=factors if url.endswith("qfq.js") else raw)
        return SimpleNamespace(json=lambda: {"data": {"sh510300": {"day": _supplement()["day"]}}})

    monkeypatch.setattr(fetcher, "_get", get)
    frame = fetcher.fetch_kline("510300", "etf", limit=2, use_db=False)
    assert frame.attrs["source"].startswith("sina_etf_cash_qfq_v1:")
    assert frame.iloc[0].close == pytest.approx(3.778)
    assert sum("sina" in url for url in called) == 2


def test_available_tencent_qfq_does_not_call_the_backup(monkeypatch):
    called = []

    def get(url, params=None, **_):
        called.append(url)
        return SimpleNamespace(
            json=lambda: {"data": {"sh512880": {"qfqday": [["2026-10-26", 1, 1, 1, 1, 100]]}}}
        )

    monkeypatch.setattr(fetcher, "_get", get)
    frame = fetcher.fetch_kline("512880", "etf", limit=1, use_db=False)
    assert frame.attrs["source"] == "tencent_ifzq_qfq"
    assert len(called) == 1


def test_bad_adjustment_never_publishes_a_receipt_and_does_not_block_other_etfs(
    tmp_path, monkeypatch
):
    raw, factors = _texts()
    broken = factors.replace('"s": "1"', '"s": "2"')
    monkeypatch.setattr(base, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(source, "_GUARD", base.SourceGuard("fixture", min_gap=0, retries=1))
    monkeypatch.setattr(base.store, "record_health", lambda *_a, **_kw: None)
    current = [broken]

    def get(url, params=None, **_):
        if "sina" not in url:
            return SimpleNamespace(
                json=lambda: {"data": {"sh510300": {"day": _supplement()["day"]}}}
            )
        return SimpleNamespace(text=current[0] if url.endswith("qfq.js") else raw)

    result = source.fetch_etf_qfq("510300", 2, get=get, receipt_dir=tmp_path / "receipts")
    assert result.empty
    assert not (tmp_path / "receipts").exists()
    current[0] = factors
    assert not source.fetch_etf_qfq("510300", 2, get=get, receipt_dir=tmp_path / "receipts").empty


def test_corrupt_existing_receipt_is_rejected_and_preserved(tmp_path):
    _, receipt = source.normalize_qfq("510300", *_texts(), supplement=_supplement())
    digest = source._publish_receipt(receipt, tmp_path)
    target = tmp_path / f"{digest}.json"
    target.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupted"):
        source._publish_receipt(receipt, tmp_path)
    assert target.read_text(encoding="utf-8") == "corrupt"
    assert not list(tmp_path.glob(".receipt-*"))
