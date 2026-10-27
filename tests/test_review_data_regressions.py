"""Data-source and daily orchestration review regressions.

@author cylovelife
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import fetcher  # noqa: E402
import research_cli  # noqa: E402
import store  # noqa: E402


def test_raw_only_qfq_response_reports_adjustment_failure(monkeypatch):
    from datasources import etf_qfq_source

    class Response:
        def json(self):
            return {
                "code": 0,
                "data": {
                    "sh512880": {
                        "day": [
                            [
                                "2026-10-26",
                                "1.01",
                                "1.02",
                                "1.03",
                                "1.00",
                                "100",
                                "1000",
                            ]
                        ]
                    }
                },
            }

    monkeypatch.setattr(fetcher, "_get", lambda *a, **k: Response())
    monkeypatch.setattr(etf_qfq_source, "fetch_etf_qfq", lambda *a, **k: pd.DataFrame())
    frame = fetcher.fetch_kline("512880", "etf", use_db=False)
    assert frame.empty
    assert frame.attrs["fetch_failure"]["kind"] == "adjustment_unavailable"
    assert "qfqday" in frame.attrs["fetch_failure"]["reason"]


def test_sync_preserves_adjustment_failure_reason(monkeypatch):
    frame = pd.DataFrame()
    frame.attrs["fetch_failure"] = {
        "kind": "adjustment_unavailable",
        "reason": "qfqday missing; day rejected",
    }
    monkeypatch.setattr(store, "enabled", lambda: True)
    monkeypatch.setattr(store, "connect", lambda: object())
    monkeypatch.setattr(store, "kline_is_fresh", lambda *a, **k: False)
    monkeypatch.setattr(fetcher, "fetch_kline", lambda *a, **k: frame)
    result = research_cli.synchronize({"etf": ["512880"]}, 300, 10)
    assert result["series"][0]["reason"] == "adjustment_unavailable"
    assert "qfqday" in result["series"][0]["detail"]


@pytest.mark.parametrize("defect", ["invalid_source_rows", "wrong_adjustment"])
def test_sync_never_caches_rejected_source_rows(tmp_path, monkeypatch, defect):
    monkeypatch.setenv("QUANT_DB", str(tmp_path / "market.db"))
    store.close()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-26"]),
            "open": [10.0],
            "close": [10.0],
            "high": [11.0],
            "low": [9.0],
            "volume": [100.0],
            "amount": [1000.0],
        }
    )
    frame.attrs.update(
        source="fixture", fetched_at="2026-10-26T18:00:00+08:00", adjust="qfq"
    )
    if defect == "wrong_adjustment":
        frame.attrs["adjust"] = "raw"
    else:
        frame.attrs["ingest_issues"] = [defect]
    monkeypatch.setattr(store, "kline_is_fresh", lambda *a, **k: False)
    monkeypatch.setattr(fetcher, "fetch_kline", lambda *a, **k: frame)
    try:
        result = research_cli.synchronize({"cn": ["000001"]}, 1, 10)
        assert result["failed"] == 1
        assert result["series"][0]["reason"] == "source_data_invalid"
        assert store.load_kline("cn", "000001", "qfq") is None
    finally:
        store.close()


def test_main_event_collection_does_not_replace_signal_evaluation(
    tmp_path, monkeypatch
):
    import run

    cfg = {
        "markets": [],
        "backtest_top_n": 1,
        "exit_model": {"enabled": False},
        "model_select": {"enabled": False},
    }
    params = {"backtest": {"hold_days": 5}}
    evaluation = {"count": 3, "avg_ret": 0.02, "details": []}
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(run, "ROOT", str(tmp_path))
    monkeypatch.setattr(run, "log", lambda *a, **k: None)
    monkeypatch.setattr(run, "load_configs", lambda: (params, cfg))
    monkeypatch.setattr(run, "apply_runtime_config", lambda *a: None)
    monkeypatch.setattr(run, "kline_fn", lambda *a: None)
    monkeypatch.setattr(run, "evaluate_previous", lambda *a, **k: evaluation)
    monkeypatch.setattr(run, "update_weights", lambda *a, **k: {"updated": False})
    monkeypatch.setattr(
        run,
        "freeze_data_contract",
        lambda *a, **k: {"series": {}, "snapshot_id": "fixture"},
    )
    monkeypatch.setattr(run, "save_today_picks", lambda *a, **k: None)
    monkeypatch.setattr(run, "rolling_factor_ic", lambda *a, **k: None)
    monkeypatch.setattr(store, "enabled", lambda: False)
    monkeypatch.setattr(store, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(run.csc_source, "available", lambda: False)
    monkeypatch.setattr(run.dsbase, "sources_enabled", lambda: True)
    monkeypatch.setattr(run.aks, "collect_fund_flow", lambda **k: {"ok": True})
    monkeypatch.setattr(
        run.ake, "collect_all", lambda *a, **k: {"ok": True, "lhb": {"rows": 20}}
    )
    monkeypatch.setattr(run.msel, "save_result", lambda *a: "fixture")
    monkeypatch.setattr(run, "drain_missing", lambda: [])
    monkeypatch.setattr(fetcher, "drain_fallbacks", lambda: [])
    seen = {}

    def render(*args, **kwargs):
        seen["evaluation"] = args[2]
        return str(tmp_path / "report.md"), str(tmp_path / "report.html")

    monkeypatch.setattr(run.rpt, "generate_report", render)
    monkeypatch.setattr(run, "_save_manifest", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["run.py", "--mode", "short"])
    run.main()
    assert seen["evaluation"] == evaluation


def test_invalid_source_date_is_rejected_without_losing_other_symbols(
    tmp_path, monkeypatch
):
    class Response:
        def json(self):
            return {
                "data": {
                    "sh600519": {
                        "qfqday": [["not-a-date", "10", "10", "11", "9", "100", "1000"]]
                    }
                }
            }

    monkeypatch.setattr(fetcher, "_get", lambda *a, **k: Response())
    frame = fetcher.fetch_kline("600519", "cn", use_db=False)
    assert "invalid_dates" in frame.attrs["ingest_issues"]


def test_margin_normalizer_preserves_each_sources_actual_date():
    from datasources.akshare_events import normalize_margin

    frame = pd.DataFrame(
        {
            "信用交易日期": ["20261023", "20261026"],
            "标的证券代码": ["600519", "600519"],
            "融资余额": [100.0, 200.0],
        }
    )
    result, anomalies = normalize_margin(frame, "sse", "2026-10-27")
    assert result.trade_date.tolist() == ["2026-10-23", "2026-10-26"]
    assert anomalies["date_mismatch"]


@pytest.mark.parametrize("value", ["2026-02-30", "20261301", "2026-00-12"])
def test_event_dates_must_be_real_calendar_dates(value):
    from datasources.base import norm_date

    assert norm_date(value) is None
