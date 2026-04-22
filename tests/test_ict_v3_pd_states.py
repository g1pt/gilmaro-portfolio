from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from paper_trader import EvoEdgeGenome, ict_edge_v3


def _genome() -> EvoEdgeGenome:
    return EvoEdgeGenome(
        genome_id="TEST_PD_STATE",
        pd_bull_max=0.80,
        sweep_lookback=2,
        sweep_tolerance=3.0,
        displacement_min=0.40,
        mss_lookback=2,
        mss_tolerance=0.0,
        fvg_buffer_mult=0.0,
    )


def _build_df(*, last_open: float, last_high: float, last_low: float, last_close: float):
    rows: list[dict[str, float]] = []
    rows.append({"open": 104.0, "high": 110.0, "low": 100.0, "close": 105.0})
    rows.append({"open": 130.0, "high": 145.0, "low": 129.0, "close": 131.0})

    for i in range(2, 57):
        base = 126.0 + (i * 0.10)
        rows.append({"open": base, "high": base + 1.0, "low": base - 1.0, "close": base + 0.5})

    # Shape local sweep/MSS/FVG context.
    rows[-4] = {"open": 131.0, "high": 132.0, "low": 131.0, "close": 131.7}
    rows[-3] = {"open": 128.5, "high": 129.0, "low": 128.0, "close": 128.9}  # a-candle for FVG
    rows[-2] = {"open": 132.4, "high": 133.2, "low": 132.0, "close": 132.8}
    rows[-1] = {"open": last_open, "high": last_high, "low": last_low, "close": last_close}

    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_pd_state_ideal_allows_normal_progression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", "0.75")

    df = _build_df(last_open=130.0, last_high=133.2, last_low=130.0, last_close=132.6)
    decision = ict_edge_v3(df, symbol="PD_IDEAL", genome=_genome())

    assert decision.pd_state == "invalid"
    assert decision.should_trade is False
    assert decision.reason == "bad_pd"
    assert decision.pd > 0.0
    assert decision.confluence_score > 0.0


def test_pd_state_borderline_rejects_when_confluence_too_low(monkeypatch: pytest.MonkeyPatch) -> None:
    min_confluence = 0.90
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", str(min_confluence))

    # Borderline PD (~0.82) with weak displacement => lower confluence.
    df = _build_df(last_open=136.0, last_high=139.0, last_low=130.0, last_close=136.4)
    decision = ict_edge_v3(df, symbol="PD_BORDERLINE_LOW_CONF", genome=_genome())

    assert decision.should_trade is False
    assert decision.reason == "bad_pd"
    assert decision.pd_state == "borderline"
    assert decision.confluence_score < min_confluence


def test_pd_state_borderline_passes_pd_gate_with_enough_confluence(monkeypatch: pytest.MonkeyPatch) -> None:
    min_confluence = 0.75
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", str(min_confluence))

    # Borderline PD (~0.82) with strong displacement/confluence.
    df = _build_df(last_open=132.0, last_high=140.0, last_low=130.0, last_close=138.0)
    decision = ict_edge_v3(df, symbol="PD_BORDERLINE_HIGH_CONF", genome=_genome())

    assert decision.pd_state == "borderline"
    assert decision.confluence_score >= min_confluence
    assert decision.reason != "bad_pd"


def test_pd_state_invalid_is_rejected_by_pd_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICT_PD_SOFT_BUFFER", "0.08")
    monkeypatch.setenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", "0.75")

    # Invalid PD (> pd_bull_max + soft buffer).
    df = _build_df(last_open=138.0, last_high=145.0, last_low=130.0, last_close=143.5)
    decision = ict_edge_v3(df, symbol="PD_INVALID", genome=_genome())

    assert decision.should_trade is False
    assert decision.reason == "bad_pd"
    assert decision.pd_state == "invalid"
