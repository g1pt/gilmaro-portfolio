from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from paper_trader import EvoEdgeGenome, ict_edge_v3


def _frame(rows: list[dict[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def _normalized_genome() -> EvoEdgeGenome:
    return EvoEdgeGenome(
        genome_id="TEST_META",
        pd_bull_max=0.80,
        sweep_lookback=2,
        sweep_tolerance=3.0,
        displacement_min=0.40,
        mss_lookback=2,
        mss_tolerance=0.0,
        fvg_buffer_mult=0.0,
    )


def _pd_context_frame(*, last_open: float, last_high: float, last_low: float, last_close: float) -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    rows.append({"open": 104.0, "high": 110.0, "low": 100.0, "close": 105.0})
    rows.append({"open": 130.0, "high": 145.0, "low": 129.0, "close": 131.0})
    for i in range(2, 57):
        base = 126.0 + (i * 0.10)
        rows.append({"open": base, "high": base + 1.0, "low": base - 1.0, "close": base + 0.5})

    rows[-4] = {"open": 131.0, "high": 132.0, "low": 131.0, "close": 131.7}
    rows[-3] = {"open": 128.5, "high": 129.0, "low": 128.0, "close": 128.9}
    rows[-2] = {"open": 132.4, "high": 133.2, "low": 132.0, "close": 132.8}
    rows[-1] = {"open": last_open, "high": last_high, "low": last_low, "close": last_close}
    return _frame(rows)


def test_no_disp_keeps_confluence_and_override_metadata() -> None:
    rows: list[dict[str, float]] = []
    for i in range(60):
        base = 100.0 + i
        rows.append(
            {
                "open": base,
                "high": base + 1.0,
                "low": base - 1.0,
                "close": base + 0.8,
            }
        )

    # Keep bullish bias + sweep, but force very low body ratio for no_disp.
    rows[-1] = {
        "open": 145.00,
        "high": 150.00,
        "low": 140.00,
        "close": 145.01,
    }

    decision = ict_edge_v3(_frame(rows), symbol="EURUSD", genome=EvoEdgeGenome(genome_id="TEST_GENOME"))

    assert decision.reason == "no_disp"
    assert decision.should_trade is False
    assert decision.confluence_score == 0.0
    assert decision.sweep_override_used is False
    assert decision.mss_override_used is False
    assert decision.imbalance_fallback_used is False


def test_failure_paths_expose_consistent_observability_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", "0.75")

    early_no_mss = ict_edge_v3(
        _pd_context_frame(last_open=130.4, last_high=135.0, last_low=130.0, last_close=132.5),
        symbol="META_EARLY",
        genome=_normalized_genome(),
    )
    late_bad_pd = ict_edge_v3(
        _pd_context_frame(last_open=138.0, last_high=145.0, last_low=130.0, last_close=142.0),
        symbol="META_LATE",
        genome=_normalized_genome(),
    )

    assert early_no_mss.reason == "no_mss"
    assert late_bad_pd.reason == "bad_pd"
    for decision in (early_no_mss, late_bad_pd):
        assert decision.pd_state in {"ideal", "borderline", "invalid", "unknown"}
        assert decision.confluence_score is not None
        assert isinstance(decision.sweep_override_used, bool)
        assert isinstance(decision.mss_override_used, bool)
        assert isinstance(decision.imbalance_fallback_used, bool)


def test_weak_displacement_return_keeps_explicit_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", "0.75")

    decision = ict_edge_v3(
        _pd_context_frame(last_open=131.0, last_high=133.8, last_low=130.0, last_close=133.2),
        symbol="META_WEAK_DISP",
        genome=_normalized_genome(),
    )

    assert decision.reason == "bad_pd"
    assert decision.pd_state in {"ideal", "borderline", "invalid"}
    assert decision.confluence_score > 0.0
    assert isinstance(decision.sweep_override_used, bool)
    assert isinstance(decision.mss_override_used, bool)
    assert isinstance(decision.imbalance_fallback_used, bool)
