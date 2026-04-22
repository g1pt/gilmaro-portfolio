#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import math
import random
import re
import time
import csv
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from contextlib import suppress
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Literal
from execution.evo_edge_primary import (
    execution_allowed,
    manage,
    open_position,
    CONFIG as EVO_EDGE_CONFIG,
    STATE,
)
from execution.setup_audit import (
    AccountEquityResolution,
    SetupAuditRecord,
    SetupAuditRuntime,
    build_sizing_trace,
    normalize_block_reason,
    utc_now_iso,
)
from execution.position_truth import PositionTruth, resolve_position_truth
from execution.reconciliation import (
    LoopExecutionStatus,
    should_skip_blocking_stage,
    update_loop_status,
)
from execution.evo_v24 import (
    compute_symbol_rank_v24,
    evaluate_composite_assessment,
    evaluate_execution_assessment,
    evaluate_profit_gate,
    load_evo_v24_config,
    map_stage_to_outcome,
)
from evo_threshold_engine import EvoThresholdEngine
from trading.scan_compat import ScanHealthTracker, validate_strategy_api
from trading.runtime_scan_bridge import resolve_runtime_find_setups_fn, scan_symbol_setups_runtime
from trading.runtime_scan_research import RuntimeScanResearchTracker
from trading.fx_edge import (
    DiscoveryModeConfig,
    EdgeAnalysisConfig,
    FXTradeAuditLogger,
    FXTradeAuditRecord,
    evaluate_discovery_gates,
    write_edge_analysis,
)
from core.config.mode import IS_LIVE, IS_PAPER, IS_TEST, mode_info
from core.execution.clean_execution_patch import decide_trade
from evo_engine_v2 import (
    apply_evo_adjustments,

    get_symbol_state as evo_get_symbol_state,
    load_evo_state,
    register_block as evo_register_block,
    register_close as evo_register_close,
    register_entry as evo_register_entry,
    save_evo_state,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# =========================================
# 🔥 GLOBAL EVO MODE (FORCED)
# =========================================
EVO_ONLY_MODE = True

# Create logs directory
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

log_file = log_dir / "paper_trades.log"
RUNTIME_LOG_FILE = str(log_file)

# Clear existing handlers (VERY IMPORTANT)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logging.info("=" * 90)
logging.info(
    "RUN START | ts=%s | pid=%s | runtime_log=%s",
    datetime.now().isoformat(timespec="seconds"),
    os.getpid(),
    RUNTIME_LOG_FILE,
)
logging.info("LOGGING INITIALIZED | file=%s", str(log_file))
logging.info("MODE: %s", mode_info())
print("ENV CHECK:", os.getenv("LIVE_EXECUTION_ENABLED"), os.getenv("BROKER_BACKEND"))


def count_runtime_open_positions(symbols, symbol_states, broker_open_positions_count: int = 0) -> int:
    open_found = 0
    for sym in symbols:
        st = symbol_states.get(sym)
        active_trade = getattr(st, "active_trade", None) if st is not None else None
        truth = resolve_position_truth(
            sym,
            active_trade=active_trade,
            broker_open_positions_count=0,
        )
        if truth.is_open:
            open_found += 1
    if open_found == 0:
        try:
            if int(broker_open_positions_count or 0) > 0:
                return 1
        except Exception:
            pass
    return open_found

# =========================================
# 🧠 ICT V3 ENGINE (EMBEDDED)
# =========================================
SideStr = Literal["LONG", "SHORT"]
FVG_MEMORY_BY_SYMBOL: dict[str, list[dict[str, float | str | int]]] = {}
MAX_FVG_AGE = 20


@dataclass
class EdgeDecisionV3:
    should_trade: bool
    side: SideStr | None
    confidence: float
    reason: str
    entry: float = 0.0
    stop: float = 0.0
    tp: float = 0.0
    fvg_low: float = 0.0
    fvg_high: float = 0.0
    genome: str = "base"
    pd: float = 0.0
    pd_state: str = "unknown"
    confluence_score: float = 0.0
    sweep_override_used: bool = False
    mss_override_used: bool = False
    imbalance_fallback_used: bool = False


@dataclass
class EvoEdgeGenome:
    genome_id: str
    pd_bull_max: float = 0.80
    pd_bear_min: float = 0.20
    sweep_lookback: int = 12
    sweep_tolerance: float = 0.00005
    displacement_min: float = 0.45
    mss_lookback: int = 3
    mss_tolerance: float = 0.00002
    rr_multiple: float = 2.0
    max_fvg_age: int = 20
    fvg_buffer_mult: float = 0.50
    allow_mid_touch: bool = True
    entry_mode: str = "mid"
    enabled: bool = True
    weight: float = 1.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_r: float = 0.0


@dataclass
class EvoEdgeStats:
    genome_id: str
    symbol: str
    entries: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    blocked: int = 0
    blocked_not_pd: int = 0
    blocked_no_sweep: int = 0
    blocked_no_disp: int = 0
    blocked_no_mss: int = 0
    blocked_no_fvg: int = 0
    blocked_wait_retrace: int = 0
    pnl_r_sum: float = 0.0
    last_updated_ts: float = 0.0

    @property
    def expectancy(self) -> float:
        if self.closed <= 0:
            return 0.0
        return float(self.pnl_r_sum / max(self.closed, 1))

    @property
    def win_rate(self) -> float:
        if self.closed <= 0:
            return 0.0
        return float(self.wins / max(self.closed, 1))

    @property
    def sample_confidence(self) -> float:
        return min(1.0, float(self.closed / 40.0))

    @property
    def block_rate(self) -> float:
        denom = max(1, self.entries + self.blocked)
        return float(self.blocked / denom)

    @property
    def edge_score(self) -> float:
        return (
            0.45 * self.expectancy
            + 0.25 * self.win_rate
            + 0.20 * self.sample_confidence
            - 0.10 * self.block_rate
        )


EVO_EDGE_STATE_PATH = Path("logs") / "evo_edge_state.json"
EVO_EDGE_ACTIVE_BY_SYMBOL: dict[str, str] = {}
EVO_EDGE_GENOMES_BY_SYMBOL: dict[str, list[EvoEdgeGenome]] = {}
EVO_EDGE_STATS_BY_SYMBOL: dict[str, dict[str, EvoEdgeStats]] = {}
ACTIVE_GENOME_CONTEXT: dict[str, str] = {}
ACTIVE_GENOME_MODE_CONTEXT: dict[str, str] = {}
SETUP_AUDIT = SetupAuditRuntime()
ACCOUNT_EQUITY_CACHE_USD: float | None = None
FX_TRADE_AUDIT_LOGGER = FXTradeAuditLogger()


def _now_ts() -> float:
    try:
        return float(time.time())
    except Exception:
        return 0.0


def _symbol_key(symbol: str) -> str:
    return str(symbol or "DEFAULT").upper().strip()


def _genome_to_dict(g: EvoEdgeGenome) -> dict[str, object]:
    return {
        "genome_id": g.genome_id,
        "pd_bull_max": g.pd_bull_max,
        "pd_bear_min": g.pd_bear_min,
        "sweep_lookback": g.sweep_lookback,
        "sweep_tolerance": g.sweep_tolerance,
        "displacement_min": g.displacement_min,
        "mss_lookback": g.mss_lookback,
        "mss_tolerance": g.mss_tolerance,
        "rr_multiple": g.rr_multiple,
        "max_fvg_age": g.max_fvg_age,
        "fvg_buffer_mult": g.fvg_buffer_mult,
        "allow_mid_touch": g.allow_mid_touch,
        "entry_mode": g.entry_mode,
        "enabled": g.enabled,
        "weight": g.weight,
        "trades": g.trades,
        "wins": g.wins,
        "losses": g.losses,
        "pnl_r": g.pnl_r,
    }


def _stats_to_dict(s: EvoEdgeStats) -> dict[str, object]:
    return {
        "genome_id": s.genome_id,
        "symbol": s.symbol,
        "entries": s.entries,
        "closed": s.closed,
        "wins": s.wins,
        "losses": s.losses,
        "blocked": s.blocked,
        "blocked_not_pd": s.blocked_not_pd,
        "blocked_no_sweep": s.blocked_no_sweep,
        "blocked_no_disp": s.blocked_no_disp,
        "blocked_no_mss": s.blocked_no_mss,
        "blocked_no_fvg": s.blocked_no_fvg,
        "blocked_wait_retrace": s.blocked_wait_retrace,
        "pnl_r_sum": s.pnl_r_sum,
        "last_updated_ts": s.last_updated_ts,
    }


def _default_genomes_for_symbol(symbol: str) -> list[EvoEdgeGenome]:
    key = _symbol_key(symbol)
    return [
        EvoEdgeGenome(
            genome_id=f"{key}_SEED",
            pd_bull_max=0.95,
            pd_bear_min=0.05,
            sweep_lookback=6,
            sweep_tolerance=0.00020,
            displacement_min=0.30,
            mss_lookback=2,
            mss_tolerance=0.00010,
            rr_multiple=1.5,
            max_fvg_age=50,
            fvg_buffer_mult=0.80,
            allow_mid_touch=True,
            entry_mode="touch",
            weight=3.0,
        ),
        EvoEdgeGenome(
            genome_id=f"{key}_BASE_A",
            pd_bull_max=0.80,
            pd_bear_min=0.20,
            sweep_lookback=12,
            sweep_tolerance=0.00005,
            displacement_min=0.45,
            mss_lookback=3,
            mss_tolerance=0.00002,
            rr_multiple=2.0,
            max_fvg_age=20,
            fvg_buffer_mult=0.50,
            allow_mid_touch=True,
            entry_mode="mid",
            weight=1.0,
        ),
        EvoEdgeGenome(
            genome_id=f"{key}_BASE_B",
            pd_bull_max=0.88,
            pd_bear_min=0.12,
            sweep_lookback=10,
            sweep_tolerance=0.00008,
            displacement_min=0.40,
            mss_lookback=3,
            mss_tolerance=0.00005,
            rr_multiple=1.8,
            max_fvg_age=30,
            fvg_buffer_mult=0.35,
            allow_mid_touch=False,
            entry_mode="mid",
            weight=1.0,
        ),
    ]


def _ensure_symbol_evo(symbol: str) -> None:
    key = _symbol_key(symbol)
    if key not in EVO_EDGE_GENOMES_BY_SYMBOL:
        EVO_EDGE_GENOMES_BY_SYMBOL[key] = _default_genomes_for_symbol(key)
    if key not in EVO_EDGE_STATS_BY_SYMBOL:
        EVO_EDGE_STATS_BY_SYMBOL[key] = {}
    for g in EVO_EDGE_GENOMES_BY_SYMBOL[key]:
        if g.genome_id not in EVO_EDGE_STATS_BY_SYMBOL[key]:
            EVO_EDGE_STATS_BY_SYMBOL[key][g.genome_id] = EvoEdgeStats(genome_id=g.genome_id, symbol=key)
    if key not in EVO_EDGE_ACTIVE_BY_SYMBOL:
        EVO_EDGE_ACTIVE_BY_SYMBOL[key] = EVO_EDGE_GENOMES_BY_SYMBOL[key][0].genome_id


def _save_evo_edge_state() -> None:
    try:
        payload: dict[str, object] = {"symbols": {}}
        for symbol, genomes in EVO_EDGE_GENOMES_BY_SYMBOL.items():
            payload["symbols"][symbol] = {
                "active_genome_id": EVO_EDGE_ACTIVE_BY_SYMBOL.get(symbol),
                "genomes": [_genome_to_dict(g) for g in genomes],
                "stats": {
                    gid: _stats_to_dict(stats)
                    for gid, stats in EVO_EDGE_STATS_BY_SYMBOL.get(symbol, {}).items()
                },
            }
        EVO_EDGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVO_EDGE_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logging.warning("EVO EDGE SAVE FAILED | reason=%s", str(exc))


def _load_evo_edge_state() -> None:
    try:
        if not EVO_EDGE_STATE_PATH.exists():
            return
        raw = json.loads(EVO_EDGE_STATE_PATH.read_text(encoding="utf-8"))
        symbols = raw.get("symbols", {})
        for symbol, data in symbols.items():
            genomes = [EvoEdgeGenome(**g) for g in data.get("genomes", [])]
            if genomes:
                EVO_EDGE_GENOMES_BY_SYMBOL[symbol] = genomes
            active = str(data.get("active_genome_id") or (genomes[0].genome_id if genomes else ""))
            if active:
                EVO_EDGE_ACTIVE_BY_SYMBOL[symbol] = active
            EVO_EDGE_STATS_BY_SYMBOL[symbol] = {}
            for gid, stats_payload in data.get("stats", {}).items():
                EVO_EDGE_STATS_BY_SYMBOL[symbol][gid] = EvoEdgeStats(**stats_payload)
    except Exception as exc:
        logging.warning("EVO EDGE LOAD FAILED | reason=%s", str(exc))


def _select_active_genome(symbol: str) -> EvoEdgeGenome:
    _ensure_symbol_evo(symbol)
    key = _symbol_key(symbol)
    genomes = EVO_EDGE_GENOMES_BY_SYMBOL[key]
    enabled_genomes = [g for g in genomes if bool(getattr(g, "enabled", True))]
    if enabled_genomes:
        genomes = enabled_genomes

    seed_candidates = [g for g in genomes if "SEED" in str(g.genome_id).upper()]
    if seed_candidates and random.random() < 0.30:
        selected = seed_candidates[0]
        EVO_EDGE_ACTIVE_BY_SYMBOL[key] = selected.genome_id
        ACTIVE_GENOME_CONTEXT[key] = selected.genome_id
        ACTIVE_GENOME_MODE_CONTEXT[key] = "forced_seed"
        logging.info("EVO EDGE SELECT | symbol=%s genome=%s mode=forced_seed", key, selected.genome_id)
        return selected

    total = sum(max(0.0001, float(g.weight)) for g in genomes)
    pick = random.random() * total
    acc = 0.0
    selected = genomes[0]
    for genome in genomes:
        acc += max(0.0001, float(genome.weight))
        if acc >= pick:
            selected = genome
            break

    EVO_EDGE_ACTIVE_BY_SYMBOL[key] = selected.genome_id
    ACTIVE_GENOME_CONTEXT[key] = selected.genome_id
    ACTIVE_GENOME_MODE_CONTEXT[key] = "weighted"
    logging.info("EVO EDGE SELECT | symbol=%s genome=%s mode=weighted", key, selected.genome_id)
    return selected


def _register_blocked_genome(symbol: str, genome_id: str, reason: str) -> None:
    _ensure_symbol_evo(symbol)
    s = EVO_EDGE_STATS_BY_SYMBOL[_symbol_key(symbol)][genome_id]
    s.blocked += 1
    s.last_updated_ts = _now_ts()
    if reason == "not_pd":
        s.blocked_not_pd += 1
    elif reason == "no_sweep":
        s.blocked_no_sweep += 1
    elif reason in ("no_disp", "no_displacement"):
        s.blocked_no_disp += 1
    elif reason == "no_mss":
        s.blocked_no_mss += 1
    elif reason == "no_fvg":
        s.blocked_no_fvg += 1
    elif reason == "wait_for_retrace":
        s.blocked_wait_retrace += 1


def _register_entry_genome(symbol: str, genome_id: str) -> None:
    _ensure_symbol_evo(symbol)
    s = EVO_EDGE_STATS_BY_SYMBOL[_symbol_key(symbol)][genome_id]
    s.entries += 1
    s.last_updated_ts = _now_ts()


def evo_edge_register_close(symbol: str, genome_id: str, pnl_r: float) -> None:
    _ensure_symbol_evo(symbol)
    key = _symbol_key(symbol)
    s = EVO_EDGE_STATS_BY_SYMBOL[key][genome_id]
    s.closed += 1
    s.pnl_r_sum += float(pnl_r)
    if pnl_r > 0:
        s.wins += 1
    else:
        s.losses += 1
    s.last_updated_ts = _now_ts()
    genome = next((g for g in EVO_EDGE_GENOMES_BY_SYMBOL.get(key, []) if g.genome_id == genome_id), None)
    if genome is not None:
        genome.trades = int(genome.trades) + 1
        if pnl_r > 0:
            genome.wins = int(genome.wins) + 1
            genome.weight = min(5.0, float(genome.weight) * 1.20)
        else:
            genome.losses = int(genome.losses) + 1
            genome.weight = max(0.05, float(genome.weight) * 0.80)
        wr = float(genome.wins / max(1, genome.trades))
        if int(genome.trades) >= 30 and wr < 0.30:
            genome.enabled = False
            logging.warning(
                "EVO EDGE KILL | symbol=%s genome=%s trades=%d wr=%.3f",
                key, genome.genome_id, int(genome.trades), wr,
            )
    if not any(bool(getattr(g, "enabled", True)) for g in EVO_EDGE_GENOMES_BY_SYMBOL.get(key, [])):
        best = max(EVO_EDGE_GENOMES_BY_SYMBOL[key], key=lambda g: float(g.weight))
        best.enabled = True
    _save_evo_edge_state()


def _log_evo_edge_summary(symbol: str) -> None:
    _ensure_symbol_evo(symbol)
    key = _symbol_key(symbol)
    payload = []
    for genome in EVO_EDGE_GENOMES_BY_SYMBOL[key]:
        payload.append(
            {
                "id": genome.genome_id,
                "w": round(float(genome.weight), 4),
                "trades": int(genome.trades),
                "wins": int(genome.wins),
                "losses": int(genome.losses),
                "enabled": bool(getattr(genome, "enabled", True)),
                "pnl_r": round(float(genome.pnl_r), 4),
            }
        )
    logging.info("EVO EDGE SUMMARY | symbol=%s data=%s", key, json.dumps(payload, separators=(",", ":")))


def _f(x: object, d: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except Exception:
        return d


def _rolling_min(series: pd.Series, n: int, default: float = 0.0) -> float:
    try:
        if len(series) < max(2, n):
            return float(default)
        return float(series.rolling(n).min().iloc[-2])
    except Exception:
        return float(default)


def _rolling_max(series: pd.Series, n: int, default: float = 0.0) -> float:
    try:
        if len(series) < max(2, n):
            return float(default)
        return float(series.rolling(n).max().iloc[-2])
    except Exception:
        return float(default)


def _bar_body_ratio(row: pd.Series) -> float:
    try:
        body = abs(float(row["close"]) - float(row["open"]))
        rng = max(1e-9, float(row["high"]) - float(row["low"]))
        return float(body / rng)
    except Exception:
        return 0.0


def _ict_v3_classify_pd_state(
    side: str,
    pd_value: float,
    pd_bull_max: float,
    pd_bear_min: float,
    pd_soft_buffer: float,
) -> tuple[str, float, float]:
    """
    LONG:
      - ideal      : pd <= pd_bull_max
      - borderline : pd_bull_max < pd <= pd_bull_max + soft_buffer
      - invalid    : pd > pd_bull_max + soft_buffer

    SHORT:
      - ideal      : pd >= pd_bear_min
      - borderline : pd_bear_min - soft_buffer <= pd < pd_bear_min
      - invalid    : pd < pd_bear_min - soft_buffer
    """
    side_norm = str(side or "").upper()
    pd_value = float(pd_value)
    pd_bull_max = float(pd_bull_max)
    pd_bear_min = float(pd_bear_min)
    pd_soft_buffer = max(0.0, float(pd_soft_buffer))

    if side_norm == "SHORT":
        ideal_th = pd_bear_min
        soft_th = pd_bear_min - pd_soft_buffer
        if pd_value >= ideal_th:
            return "ideal", ideal_th, soft_th
        if pd_value >= soft_th:
            return "borderline", ideal_th, soft_th
        return "invalid", ideal_th, soft_th

    ideal_th = pd_bull_max
    soft_th = pd_bull_max + pd_soft_buffer
    if pd_value <= ideal_th:
        return "ideal", ideal_th, soft_th
    if pd_value <= soft_th:
        return "borderline", ideal_th, soft_th
    return "invalid", ideal_th, soft_th


def _ict_v3_apply_pd_gate(
    decision: EdgeDecisionV3,
    *,
    side: str,
    pd_value: float,
    confluence_score: float,
    genome_id: str,
    pd_bull_max: float,
    pd_bear_min: float,
    pd_soft_buffer: float,
    pd_borderline_min_confluence: float,
    logger_fn=None,
) -> EdgeDecisionV3:
    pd_state, ideal_th, soft_th = _ict_v3_classify_pd_state(
        side=side,
        pd_value=float(pd_value),
        pd_bull_max=float(pd_bull_max),
        pd_bear_min=float(pd_bear_min),
        pd_soft_buffer=float(pd_soft_buffer),
    )
    override_allowed = bool(
        pd_state == "borderline"
        and float(confluence_score) >= float(pd_borderline_min_confluence)
    )
    enriched = replace(
        decision,
        pd=float(pd_value),
        pd_state=pd_state,
        confluence_score=float(confluence_score),
        genome=genome_id,
        sweep_override_used=bool(getattr(decision, "sweep_override_used", False)),
        mss_override_used=bool(getattr(decision, "mss_override_used", False)),
        imbalance_fallback_used=bool(getattr(decision, "imbalance_fallback_used", False)),
    )

    if logger_fn:
        logger_fn(
            "ICT V3 PD EVAL | side=%s pd=%.2f ideal_th=%.2f soft_th=%.2f pd_state=%s confluence=%.2f override_allowed=%s",
            side,
            float(pd_value),
            float(ideal_th),
            float(soft_th),
            pd_state,
            float(confluence_score),
            override_allowed,
        )

    if pd_state == "invalid" or (pd_state == "borderline" and not override_allowed):
        return replace(enriched, should_trade=False, reason="bad_pd", confidence=0.2)
    return enriched


def _symbol_fvg_memory_v3(symbol: str) -> list[dict[str, float | str | int]]:
    key = str(symbol).upper().strip() or "DEFAULT"
    if key not in FVG_MEMORY_BY_SYMBOL:
        FVG_MEMORY_BY_SYMBOL[key] = []
    return FVG_MEMORY_BY_SYMBOL[key]


def evaluate_htf_bias_v3(df: pd.DataFrame) -> dict[str, float | str]:
    if len(df) < 50:
        return {"bias": "neutral", "high": 0.0, "low": 0.0}
    high = float(df["high"].rolling(50).max().iloc[-1])
    low = float(df["low"].rolling(50).min().iloc[-1])
    trend = float(df["close"].iloc[-1]) > float(df["close"].rolling(50).mean().iloc[-1])
    return {"bias": "bullish" if trend else "bearish", "high": high, "low": low}


def ict_edge_v3(df: pd.DataFrame, symbol: str = "DEFAULT", genome: EvoEdgeGenome | None = None) -> EdgeDecisionV3:
    logging.info("ICT V3 CALLED | symbol=%s bars=%d", symbol, len(df))

    if len(df) < 30:
        logging.info("DEBUG STAGE | no_data")
        return EdgeDecisionV3(
            False,
            None,
            0.0,
            "no_data",
            pd=0.0,
            pd_state="unknown",
            confluence_score=0.0,
            sweep_override_used=False,
            mss_override_used=False,
            imbalance_fallback_used=False,
        )

    htf = evaluate_htf_bias_v3(df)
    bias = str(htf.get("bias", "neutral"))
    if bias not in ("bullish", "bearish"):
        logging.info("DEBUG STAGE | no_htf_bias")
        return EdgeDecisionV3(
            False,
            None,
            0.0,
            "no_htf_bias",
            pd=0.0,
            pd_state="unknown",
            confluence_score=0.0,
            sweep_override_used=False,
            mss_override_used=False,
            imbalance_fallback_used=False,
        )

    side: SideStr = "LONG" if bias == "bullish" else "SHORT"
    if genome is None:
        genome = _select_active_genome(symbol)

    logging.info(
        "EVO EDGE GENOME | symbol=%s genome=%s pd_bull_max=%.2f pd_bear_min=%.2f sweep_lb=%d disp=%.2f mss_lb=%d rr:%.2f",
        symbol,
        genome.genome_id,
        genome.pd_bull_max,
        genome.pd_bear_min,
        genome.sweep_lookback,
        genome.displacement_min,
        genome.mss_lookback,
        genome.rr_multiple,
    )

    price = _f(df["close"].iloc[-1])
    high = _f(htf["high"])
    low = _f(htf["low"])

    rng = max(1e-9, high - low)
    pd_pos = (price - low) / rng
    pd_soft_buffer = float(os.getenv("ICT_PD_SOFT_BUFFER", "0.08") or 0.08)
    pd_soft_buffer = max(0.0, min(0.20, pd_soft_buffer))
    pd_borderline_min_confluence = float(os.getenv("ICT_PD_BORDERLINE_MIN_CONFLUENCE", "0.75") or 0.75)
    pd_borderline_min_confluence = max(0.0, min(1.0, pd_borderline_min_confluence))

    pd_state, _, _ = _ict_v3_classify_pd_state(
        side=side,
        pd_value=float(pd_pos),
        pd_bull_max=float(genome.pd_bull_max),
        pd_bear_min=float(genome.pd_bear_min),
        pd_soft_buffer=float(pd_soft_buffer),
    )
    decision = EdgeDecisionV3(
        should_trade=False,
        side=side,
        confidence=0.0,
        reason="unknown",
        genome=genome.genome_id,
        pd=pd_pos,
        pd_state=pd_state,
        confluence_score=0.0,
        sweep_override_used=False,
        mss_override_used=False,
        imbalance_fallback_used=False,
    )
    # Initialize early so nested helpers can safely reference it before scoring.
    confluence_score = 0.0

    NON_PD_TERMINAL_REASONS = {
        "no_data",
        "no_htf_bias",
        "no_sweep",
        "no_disp",
        "no_mss",
        "weak_displacement",
        "bad_pd",
        "low_range",
        "weak_touch_entry",
    }

    def _finalize(dec: EdgeDecisionV3) -> EdgeDecisionV3:
        """
        Force PD metadata to be attached before any early return.
        """
        try:
            pd_gate_ready = side in ("LONG", "SHORT") and pd_pos is not None
        except Exception:
            pd_gate_ready = False

        if not pd_gate_ready:
            return dec

        current_state = getattr(dec, "pd_state", "unknown")
        current_reason = getattr(dec, "reason", None)

        # Attach PD metadata when missing, but never let finalize overwrite
        # an already-established non-PD terminal reason.
        if current_state == "unknown":
            dec = _ict_v3_apply_pd_gate(
                dec,
                side=side,
                pd_value=float(pd_pos),
                confluence_score=float(getattr(dec, "confluence_score", 0.0)),
                genome_id=genome.genome_id,
                pd_bull_max=float(genome.pd_bull_max),
                pd_bear_min=float(genome.pd_bear_min),
                pd_soft_buffer=float(pd_soft_buffer),
                pd_borderline_min_confluence=float(pd_borderline_min_confluence),
                logger_fn=logging.info,
            )
            if (
                current_reason in NON_PD_TERMINAL_REASONS
                and getattr(dec, "reason", None) == "bad_pd"
            ):
                dec = replace(
                    dec,
                    should_trade=False,
                    reason=current_reason,
                    confidence=getattr(dec, "confidence", 0.0),
                )
            elif current_reason not in (None, "unknown") and getattr(dec, "reason", None) != "bad_pd":
                dec = replace(dec, reason=current_reason)
        return dec

    def _pd_blocks_now(dec: EdgeDecisionV3) -> tuple[bool, EdgeDecisionV3]:
        """
        Central helper so bad_pd wins over generic early-stage reasons.
        """
        dec = _finalize(dec)
        return getattr(dec, "reason", None) == "bad_pd", dec

    def _pd_hard_invalid_now(dec: EdgeDecisionV3) -> tuple[bool, EdgeDecisionV3]:
        """
        Early structural exits like raw no_disp should only be replaced by
        bad_pd when PD is truly invalid, not merely borderline.
        """
        pd_state_now, _, _ = _ict_v3_classify_pd_state(
            side=side,
            pd_value=float(pd_pos),
            pd_bull_max=float(genome.pd_bull_max),
            pd_bear_min=float(genome.pd_bear_min),
            pd_soft_buffer=float(pd_soft_buffer),
        )
        dec = replace(
            dec,
            pd=float(pd_pos),
            pd_state=pd_state_now,
            genome=genome.genome_id,
        )
        if pd_state_now == "invalid":
            return True, replace(dec, should_trade=False, reason="bad_pd", confidence=0.2)
        return False, dec

    def _pd_failure_now(dec: EdgeDecisionV3, *, confluence_score: float) -> tuple[bool, EdgeDecisionV3]:
        """
        Progressed setups may allow PD to become authoritative, including
        borderline rejection via confluence threshold.
        """
        pd_checked = _ict_v3_apply_pd_gate(
            replace(dec, pd=float(pd_pos), confluence_score=float(confluence_score), genome=genome.genome_id),
            side=side,
            pd_value=float(pd_pos),
            confluence_score=float(confluence_score),
            genome_id=genome.genome_id,
            pd_bull_max=float(genome.pd_bull_max),
            pd_bear_min=float(genome.pd_bear_min),
            pd_soft_buffer=float(pd_soft_buffer),
            pd_borderline_min_confluence=float(pd_borderline_min_confluence),
            logger_fn=logging.info,
        )
        return getattr(pd_checked, "reason", None) == "bad_pd", pd_checked

    if len(df) < 3:
        logging.info("DEBUG STAGE | no_data")
        return EdgeDecisionV3(
            False,
            side,
            0.1,
            "no_data",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=0.0,
            sweep_override_used=False,
            mss_override_used=False,
            imbalance_fallback_used=False,
        )

    last = df.iloc[-1]
    # Relaxed sweep:
    # 1) use shorter lookback
    # 2) allow tiny penetration tolerance
    sweep_lookback = int(genome.sweep_lookback)
    sweep_tol = float(genome.sweep_tolerance)
    if bias == "bullish":
        ref = _rolling_min(df["low"], sweep_lookback, default=float(last["low"]))
        sweep = float(last["low"]) <= (ref + sweep_tol)
    else:
        ref = _rolling_max(df["high"], sweep_lookback, default=float(last["high"]))
        sweep = float(last["high"]) >= (ref - sweep_tol)
    sweep_override_used = False

    body_ratio_pre = _bar_body_ratio(last)
    if (not sweep) and ("SEED" in str(genome.genome_id).upper()) and body_ratio_pre >= 0.60:
        sweep = True
        sweep_override_used = True
        logging.info("DEBUG STAGE | sweep_override genome=%s ratio=%.2f", genome.genome_id, body_ratio_pre)

    if not sweep:
        logging.info("DEBUG STAGE | no_sweep")
        return EdgeDecisionV3(
            False,
            side,
            0.3,
            "no_sweep",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=0.0,
            sweep_override_used=sweep_override_used,
            mss_override_used=False,
            imbalance_fallback_used=False,
        )

    body_ratio = _bar_body_ratio(last)
    if body_ratio < float(genome.displacement_min):
        blocked, decision = _pd_blocks_now(decision)
        if blocked:
            return decision

        decision = _ict_v3_apply_pd_gate(
            decision,
            side=side,
            pd_value=float(pd_pos),
            confluence_score=0.0,
            genome_id=genome.genome_id,
            pd_bull_max=float(genome.pd_bull_max),
            pd_bear_min=float(genome.pd_bear_min),
            pd_soft_buffer=float(pd_soft_buffer),
            pd_borderline_min_confluence=float(pd_borderline_min_confluence),
            logger_fn=logging.info,
        )
        if not bool(decision.should_trade) and getattr(decision, "reason", "") == "bad_pd":
            logging.info(
                "EVO EDGE KILL | bad_pd state=%s pd=%.2f confluence=%.2f min_confluence=%.2f",
                decision.pd_state,
                pd_pos,
                float(getattr(decision, "confluence_score", confluence_score)),
                pd_borderline_min_confluence,
            )
            return _finalize(
                replace(
                    decision,
                    sweep_override_used=sweep_override_used,
                    mss_override_used=False,
                    imbalance_fallback_used=False,
                )
            )

        logging.info("DEBUG STAGE | no_displacement ratio=%.2f", body_ratio)
        return _finalize(
            replace(
                decision,
                should_trade=False,
                confidence=0.4,
                reason="no_disp",
                pd=float(pd_pos),
                pd_state=getattr(decision, "pd_state", "unknown"),
                confluence_score=float(getattr(decision, "confluence_score", confluence_score)),
                sweep_override_used=sweep_override_used,
                mss_override_used=False,
                imbalance_fallback_used=False,
            )
        )
    strong_displacement = body_ratio > (float(genome.displacement_min) * 1.2)

    # Relaxed MSS:
    # shorter structure window and tolerance on close
    mss_ok = True
    mss_override_used = False
    if bias == "bullish":
        prior_high = _rolling_max(df["high"], int(genome.mss_lookback), default=float(last["close"]))
        if float(last["close"]) <= (prior_high - float(genome.mss_tolerance)):
            mss_ok = False
    else:
        prior_low = _rolling_min(df["low"], int(genome.mss_lookback), default=float(last["close"]))
        if float(last["close"]) >= (prior_low + float(genome.mss_tolerance)):
            mss_ok = False

    if (not mss_ok) and strong_displacement:
        logging.info("DEBUG STAGE | mss_override strong_displacement=%.2f", body_ratio)
        mss_ok = True
        mss_override_used = True

    if not mss_ok:
        logging.info("DEBUG STAGE | no_mss")
        return _finalize(
            replace(
                decision,
                should_trade=False,
                confidence=0.5,
                reason="no_mss",
                sweep_override_used=sweep_override_used,
                mss_override_used=mss_override_used,
                imbalance_fallback_used=False,
            )
        )
    blocked, decision = _pd_blocks_now(decision)
    if blocked:
        return decision

    memory = _symbol_fvg_memory_v3(symbol)

    a = df.iloc[-3]
    c = df.iloc[-1]
    if bias == "bullish":
        fvg_low = float(a["high"])
        fvg_high = float(c["low"])
        has_fvg = fvg_high > fvg_low
    else:
        fvg_low = float(c["high"])
        fvg_high = float(a["low"])
        has_fvg = fvg_high > fvg_low

    if not has_fvg:
        logging.info("DEBUG STAGE | no_fvg -> using_imbalance")
        if bias == "bullish":
            fvg_low = float(last["open"])
            fvg_high = float(last["high"])
        else:
            fvg_low = float(last["low"])
            fvg_high = float(last["open"])
    imbalance_fallback_used = not has_fvg

    raw_width = max(1e-9, float(fvg_high - fvg_low))
    buffer = raw_width * float(getattr(genome, "fvg_buffer_mult", 0.50))
    fvg_low = float(fvg_low - buffer)
    fvg_high = float(fvg_high + buffer)
    logging.info(
        "ZONE ACTIVE | genome=%s low=%.5f high=%.5f width=%.5f",
        genome.genome_id,
        fvg_low,
        fvg_high,
        raw_width,
    )

    memory.append(
        {
            "side": side,
            "low": float(fvg_low),
            "high": float(fvg_high),
            "age": 0,
        }
    )

    for zone in memory:
        zone["age"] = int(zone.get("age", 0)) + 1

    memory[:] = [zone for zone in memory if int(zone.get("age", 0)) <= int(genome.max_fvg_age)]

    selected_zone: dict[str, float | str | int] | None = None
    selected_entry_mode = "mid"
    for zone in reversed(memory):
        if str(zone.get("side")) != side:
            continue
        zone_low = float(zone.get("low", 0.0))
        zone_high = float(zone.get("high", 0.0))
        zone_mid = (zone_low + zone_high) / 2.0
        zone_width = max(1e-9, zone_high - zone_low)
        touch = (
            float(last["low"]) <= zone_high and float(last["high"]) >= zone_low
        )
        mid_touch = bool(getattr(genome, "allow_mid_touch", True)) and (
            abs(float(last["close"]) - zone_mid) <= zone_width
        )
        continuation = strong_displacement and (
            (side == "LONG" and float(last["close"]) > zone_high)
            or (side == "SHORT" and float(last["close"]) < zone_low)
        )
        touched = touch or mid_touch or continuation
        if touched:
            selected_zone = zone
            if continuation:
                selected_entry_mode = "continuation"
            elif touch:
                selected_entry_mode = "touch"
            else:
                selected_entry_mode = "mid"
            break

    if selected_zone is None:
        logging.info("DEBUG STAGE | wait_for_retrace")
        latest_low = float(memory[-1]["low"]) if memory else float(fvg_low)
        latest_high = float(memory[-1]["high"]) if memory else float(fvg_high)
        return EdgeDecisionV3(
            False,
            side,
            0.6,
            "wait_for_retrace",
            fvg_low=latest_low,
            fvg_high=latest_high,
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=0.0,
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )

    selected_low = float(selected_zone["low"])
    selected_high = float(selected_zone["high"])

    # =========================================
    # 🔥 V1.4 ENTRY FILTERS (EDGE BUILDER)
    # =========================================

    # 1. weak displacement can be either:
    # - early structural no_disp
    # - later explicit weak_displacement
    # - or masked by PD only when the setup has progressed far enough
    weak_displacement_pending = body_ratio < float(getattr(genome, "min_body_ratio", 0.45))

    # 2. KILL ULTRA SMALL ZONES (noise)
    if (selected_high - selected_low) < float(getattr(genome, "min_zone_size", 0.00005)):
        logging.info("EVO EDGE KILL | tiny_zone")
        return EdgeDecisionV3(
            False,
            side,
            0.2,
            "tiny_zone",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=0.0,
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )

    # 3. Normalized PD evaluation with soft-borderline confluence override.
    sweep_score = 1.0 if (sweep and not sweep_override_used) else (0.7 if sweep_override_used else 0.0)
    displacement_floor = max(1e-9, float(genome.displacement_min))
    displacement_score = max(0.0, min(1.0, (float(body_ratio) - displacement_floor) / displacement_floor))
    mss_score = 1.0 if (mss_ok and not mss_override_used) else (0.7 if mss_override_used else 0.0)
    fvg_score = 1.0 if has_fvg else (0.75 if imbalance_fallback_used else 0.0)
    retrace_score = 1.0 if selected_entry_mode in {"touch", "mid"} else 0.7
    confluence_score = float(
        0.30 * sweep_score
        + 0.25 * displacement_score
        + 0.20 * mss_score
        + 0.15 * fvg_score
        + 0.10 * retrace_score
    )
    decision = replace(decision, confluence_score=confluence_score)

    # Preserve legacy early-stage no_disp behavior only when setup has not
    # progressed enough for PD to become authoritative.
    if (
        weak_displacement_pending
        and not mss_ok
        and not has_fvg
        and not imbalance_fallback_used
        and not sweep
    ):
        dec_no_disp = EdgeDecisionV3(
            False,
            side,
            0.4,
            "no_disp",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=float(confluence_score),
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )
        blocked, dec_no_disp = _pd_hard_invalid_now(dec_no_disp)
        if blocked:
            return _finalize(dec_no_disp)
        logging.info("DEBUG STAGE | no_displacement ratio=%.2f", body_ratio)
        return _finalize(dec_no_disp)

    # PD becomes authoritative only once the setup has progressed beyond the
    # raw-sweep stage. A sweep alone is not enough.
    pd_ready = bool(mss_ok or has_fvg or imbalance_fallback_used)
    if pd_ready:
        decision = _ict_v3_apply_pd_gate(
            decision,
            side=side,
            pd_value=float(pd_pos),
            confluence_score=float(confluence_score),
            genome_id=genome.genome_id,
            pd_bull_max=float(genome.pd_bull_max),
            pd_bear_min=float(genome.pd_bear_min),
            pd_soft_buffer=float(pd_soft_buffer),
            pd_borderline_min_confluence=float(pd_borderline_min_confluence),
            logger_fn=logging.info,
        )
        if (
            not weak_displacement_pending
            and not decision.should_trade
            and decision.reason == "bad_pd"
        ):
            logging.info(
                "EVO EDGE KILL | bad_pd state=%s pd=%.2f confluence=%.2f min_confluence=%.2f",
                decision.pd_state,
                pd_pos,
                confluence_score,
                pd_borderline_min_confluence,
            )
            return _finalize(
                replace(
                    decision,
                    sweep_override_used=sweep_override_used,
                    mss_override_used=mss_override_used,
                    imbalance_fallback_used=imbalance_fallback_used,
                )
            )

    # If displacement is still weak after setup progression, keep the explicit
    # weak_displacement reason unless PD already rejected the setup.
    if weak_displacement_pending:
        if getattr(decision, "pd_state", None) == "ideal":
            logging.info(
                "ICT V3 WEAK DISP BYPASS | symbol=%s pd_state=%s ratio=%.2f",
                symbol,
                getattr(decision, "pd_state", None),
                body_ratio,
            )
            strong_displacement = True
            displacement_score = max(displacement_score, 0.55)
            confluence_score = max(confluence_score, 0.55)
            decision = replace(decision, confluence_score=float(confluence_score))
        else:
            return replace(
                decision,
                should_trade=False,
                confidence=0.2,
                reason="weak_displacement",
                pd=pd_pos,
                pd_state=pd_state,
                confluence_score=float(getattr(decision, "confluence_score", confluence_score)),
                sweep_override_used=sweep_override_used,
                mss_override_used=mss_override_used,
                imbalance_fallback_used=imbalance_fallback_used,
            )

    # 4. KILL CHOP (range filter)
    recent_range = float(df["high"].iloc[-5:].max() - df["low"].iloc[-5:].min())
    if recent_range < float(getattr(genome, "min_range", 0.0002)):
        logging.info("EVO EDGE KILL | low_range")
        return EdgeDecisionV3(
            False,
            side,
            0.2,
            "low_range",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=getattr(decision, "pd_state", pd_state),
            confluence_score=float(getattr(decision, "confluence_score", confluence_score)),
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )

    # 5. LIMIT TOUCH DOMINANCE (IMPORTANT)
    if selected_entry_mode == "touch" and not strong_displacement:
        logging.info("EVO EDGE KILL | weak_touch_entry")
        return _finalize(
            replace(
                decision,
                should_trade=False,
                confidence=0.2,
                reason="weak_touch_entry",
                sweep_override_used=sweep_override_used,
                mss_override_used=mss_override_used,
                imbalance_fallback_used=imbalance_fallback_used,
            )
        )

    if selected_entry_mode == "continuation":
        entry = float(last["close"])
        logging.info("ENTRY MODE | continuation")
    elif selected_entry_mode == "touch":
        entry = float(last["close"])
        logging.info("ENTRY MODE | touch")
    else:
        entry = (selected_low + selected_high) / 2.0
        logging.info("ENTRY MODE | mid")
    stop = float(last["low"]) if side == "LONG" else float(last["high"])
    risk = abs(entry - stop)
    if risk <= 1e-9:
        logging.info("DEBUG STAGE | invalid_risk")
        return EdgeDecisionV3(
            False,
            side,
            0.1,
            "invalid_risk",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=confluence_score,
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )
    if risk < 0.00001:
        logging.info("DEBUG STAGE | ultra_small_risk -> skip")
        return EdgeDecisionV3(
            False,
            side,
            0.1,
            "invalid_risk",
            genome=genome.genome_id,
            pd=pd_pos,
            pd_state=pd_state,
            confluence_score=confluence_score,
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )
    tp = entry + risk * float(genome.rr_multiple) if side == "LONG" else entry - risk * float(genome.rr_multiple)
    if ("SEED" in str(genome.genome_id).upper()) and not math.isfinite(tp):
        tp = entry + risk * 1.5 if side == "LONG" else entry - risk * 1.5
    logging.info(
        "DEBUG STAGE | ENTRY CONFIRMED genome=%s side=%s entry=%.5f stop=%.5f tp=%.5f",
        genome.genome_id, side, entry, stop, tp,
    )
    decision = _finalize(
        replace(
            decision,
            should_trade=True,
            side=side,
            confidence=0.9,
            reason="ict_v3_retrace_confirmed",
            entry=entry,
            stop=stop,
            tp=tp,
            fvg_low=selected_low,
            fvg_high=selected_high,
            sweep_override_used=sweep_override_used,
            mss_override_used=mss_override_used,
            imbalance_fallback_used=imbalance_fallback_used,
        )
    )
    return decision

# =========================================
# 🔥 MINI CONFIG FIX (ENV PRIORITY)
# =========================================
def _env_bool(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).lower() in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_list(name: str, default: str) -> list:
    raw = os.getenv(name, default)
    return [x.strip() for x in str(raw).split(",") if x.strip()]


# =========================================
# EXECUTION MODE (SINGLE SOURCE OF TRUTH)
# =========================================
EXECUTION_MODE = str(os.getenv("EXECUTION_MODE", "paper")).strip().lower()
IS_PAPER_MODE = EXECUTION_MODE in ("paper", "discovery", "test")
IS_LIVE_MODE = EXECUTION_MODE == "live"

# 🔥 SINGLE EXECUTION SWITCH (MASTER)
ENABLE_TRADING = str(os.getenv("ENABLE_TRADING", "false")).lower() in ("1", "true", "yes")

# FINAL AUTHORITY
LIVE_EXECUTION_ENABLED = bool(IS_LIVE_MODE and ENABLE_TRADING)

logging.info(
    "EXECUTION CONTROL | live=%s trading_enabled=%s",
    str(LIVE_EXECUTION_ENABLED), str(ENABLE_TRADING)
)

# =========================================
# CONFIG (ENV FIRST, FALLBACK SAFE)
# =========================================
MODE = os.getenv("MODE", "TEST").upper()

ALLOWED_SETUPS = _env_list(
    "ALLOWED_SETUPS",
    os.getenv(
        "ALLOWED_SETUPS_DEFAULT",
        "sweep_only,structure_basic,sweep_displacement,displacement_only",
    ),
)

# =========================================
# 🔒 OPTIE 1 HARD LOCK (CONTROLLED TEST)
# =========================================
ALLOWED_SETUPS = [
    "sweep_only",
    "structure_basic"
]

logging.warning(
    "OPTIE 1 ACTIVE | controlled testing | allowed_setups=%s",
    ALLOWED_SETUPS
)

DISCOVERY_IGNORE_SESSION = _env_bool("DISCOVERY_IGNORE_SESSION", "true")

MIN_SIGNAL = _env_float("MIN_SIGNAL", 0.45)
MIN_QUALITY = _env_float("MIN_QUALITY", 0.45)

logging.info(
    "CONFIG LOADED | MODE=%s ALLOWED_SETUPS=%s MIN_SIGNAL=%.2f MIN_QUALITY=%.2f",
    MODE,
    ALLOWED_SETUPS,
    MIN_SIGNAL,
    MIN_QUALITY,
)
logging.info("EXECUTION MODE | mode=%s is_paper=%s is_live=%s", EXECUTION_MODE, IS_PAPER_MODE, IS_LIVE_MODE)

FORCE_MODE = str(os.getenv("FORCE_MODE", "false")).lower() in ("1", "true", "yes")
FORCE_EXECUTION = str(os.getenv("FORCE_EXECUTION", "false")).lower() in ("1", "true", "yes")
MAX_LIVE_RISK = float(os.getenv("MAX_LIVE_RISK", "0.01"))
DISCOVERY_MODE = str(os.getenv("DISCOVERY_MODE", "false")).lower() in ("1", "true", "yes")
DISCOVERY_MODE_CONFIG = DiscoveryModeConfig(
    enabled=DISCOVERY_MODE,
    min_ict_confidence=float(os.getenv("DISCOVERY_MIN_ICT_CONFIDENCE", "0.65") or 0.65),
    min_entry_quality=float(os.getenv("DISCOVERY_MIN_ENTRY_QUALITY", "0.65") or 0.65),
    min_signal_delta_above_threshold=float(os.getenv("DISCOVERY_MIN_SIGNAL_DELTA_ABOVE_THRESHOLD", "0.03") or 0.03),
    allow_force_paths=str(os.getenv("DISCOVERY_ALLOW_FORCE_PATHS", "false")).lower() in ("1", "true", "yes"),
    allow_override_paths=str(os.getenv("DISCOVERY_ALLOW_OVERRIDE_PATHS", "false")).lower() in ("1", "true", "yes"),
)
EDGE_ANALYSIS_CONFIG = EdgeAnalysisConfig(
    min_trades_for_candidate=int(os.getenv("FX_EDGE_MIN_TRADES_CANDIDATE", "5") or 5),
    min_trades_for_promotable=int(os.getenv("FX_EDGE_MIN_TRADES_PROMOTABLE", "20") or 20),
    min_trades_for_disable=int(os.getenv("FX_EDGE_MIN_TRADES_DISABLE", "8") or 8),
    min_expectancy_for_candidate=float(os.getenv("FX_EDGE_MIN_EXPECTANCY_CANDIDATE", "0.05") or 0.05),
    min_expectancy_for_promotable=float(os.getenv("FX_EDGE_MIN_EXPECTANCY_PROMOTABLE", "0.15") or 0.15),
    max_expectancy_for_disable=float(os.getenv("FX_EDGE_MAX_EXPECTANCY_DISABLE", "-0.05") or -0.05),
    min_winrate_for_promotable=float(os.getenv("FX_EDGE_MIN_WINRATE_PROMOTABLE", "0.52") or 0.52),
)


def effective_force_mode() -> bool:
    """
    Single source of truth for force-mode authority.
    Final lock disables all force behavior regardless of env/requested state.
    """
    return False


def effective_force_execution() -> bool:
    """
    Single source of truth for force-execution authority.
    Final lock disables all force behavior regardless of env/requested state.
    """
    return False


def effective_force_any() -> bool:
    return bool(effective_force_mode() or effective_force_execution())


# =========================================
# 🔒 FINAL LOCK CONFIG (NO FORCE / NO OVERRIDE)
# =========================================
FORCE_MODE = False
FORCE_EXECUTION = False
TEST_FORCE_ENTRY = False
logging.warning("FINAL LOCK ACTIVE | force disabled | single execution authority")
logging.info(
    "FORCE AUTHORITY SNAPSHOT | requested_mode=%s requested_execution=%s effective_mode=%s effective_execution=%s effective_any=%s",
    DISCOVERY_MODE,
    FORCE_EXECUTION,
    effective_force_mode(),
    effective_force_execution(),
    effective_force_any(),
)

# =========================================
# HARD DISABLE LIVE EXECUTION (SAFE MODE)
# =========================================
# DO NOT OVERRIDE LIVE_EXECUTION_ENABLED HERE

if IS_PAPER_MODE:
    LIVE_EXECUTION_ENABLED = False


def _mt5_available() -> bool:
    try:
        return (not IS_PAPER_MODE) and (mt5 is not None)
    except Exception:
        return False


def _mt5_safe(method_name: str, *args, **kwargs):
    if IS_PAPER_MODE:
        logging.info("MT5 SKIP | paper/discovery mode | method=%s", method_name)
        return None
    if mt5 is None:
        logging.warning("MT5 UNAVAILABLE | method=%s", method_name)
        return None
    try:
        method = getattr(mt5, method_name, None)
        if method is None:
            logging.warning("MT5 METHOD MISSING | method=%s", method_name)
            return None
        return method(*args, **kwargs)
    except Exception as e:
        logging.error("MT5 CALL FAILED | method=%s err=%s", method_name, e)
        return None


if FORCE_MODE or FORCE_EXECUTION:
    logging.warning(
        "FORCE FLAGS DETECTED AT STARTUP | FORCE_MODE=%s FORCE_EXECUTION=%s",
        str(FORCE_MODE).lower(),
        str(FORCE_EXECUTION).lower(),
    )

# =========================================
# SWEEP TEST STATS (DISCOVERY MODE)
# =========================================
sweep_stats = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "pnl_total": 0.0,
    "timeouts": 0,
}

# =========================================================
# 🔥 VALIDATION ENGINE (VIRTUAL TRADES)
# =========================================================
_VIRTUAL_TRADES: list[dict] = []
_VIRTUAL_TRADE_ID = 0
ENABLE_VIRTUAL_TRADING = str(os.getenv("ENABLE_VIRTUAL_TRADING", "true")).lower() in ("1", "true", "yes")
RESULTS_DIR = Path("results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = RESULTS_DIR / "virtual_trade_results.csv"

def _virtual_trade_detailed_file() -> Path:
    p = Path("results/virtual_trades_detailed.csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

# =========================================================
# 🔥 TEST MODE (FULL SIMULATION)
# =========================================================
ENABLE_TEST_MODE = str(os.getenv("ENABLE_TEST_MODE", "false")).lower() in ("1", "true", "yes")
TEST_VOLATILITY = float(os.getenv("TEST_VOLATILITY", "0.003"))
TEST_FORCE_ENTRY = str(os.getenv("TEST_FORCE_ENTRY", "false")).lower() in ("1", "true", "yes")
TEST_MODE_BYPASS_FILTER_GATE = str(os.getenv("TEST_MODE_BYPASS_FILTER_GATE", "false")).lower() in ("1", "true", "yes")
DISABLE_REENTRY_BLOCK = str(os.getenv("DISABLE_REENTRY_BLOCK", "false")).lower() in ("1", "true", "yes")

# 🔒 TEST FORCE DISABLED
TEST_FORCE_ENTRY = False


# =========================================================
# 🔥 GLOBAL SPEED CONTROL
# =========================================================
FAST_MODE = ENABLE_TEST_MODE

def _sleep() -> None:
    if FAST_MODE:
        time.sleep(0.05)
    else:
        time.sleep(1)

# =========================================================
# 🔥 STACK + DUPLICATE CONTROL
# =========================================================
MAX_STACK_PER_SYMBOL = int(os.getenv("MAX_STACK_PER_SYMBOL", "1"))
LAST_TRADE_TS = {}
TEST_MODE_MIN_ENTRY_GAP_SEC = float(os.getenv("TEST_MODE_MIN_ENTRY_GAP_SEC", "2.0"))


def count_open_trades(symbol):
    return sum(1 for t in _VIRTUAL_TRADES if t["symbol"] == symbol)


def _simulate_price(price: float) -> float:
    move = random.uniform(-TEST_VOLATILITY, TEST_VOLATILITY)
    new_price = price + move
    logging.info(
        "TEST MODE PRICE | move=%.6f price=%.5f",
        move,
        new_price,
    )
    return new_price


def _virtual_trades_file() -> Path:
    p = Path("results/virtual_trades.csv")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _virtual_trade_results_file() -> Path:
    return RESULTS_FILE


def ensure_results_file() -> None:
    if RESULTS_FILE.exists():
        return
    with RESULTS_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "symbol",
                "result",
                "entry_price",
                "tp",
                "sl",
                "timestamp",
                "setup_type",
            ]
        )
    logging.warning("RESULTS FILE CREATED | path=%s", RESULTS_FILE)


def _append_csv_row(path: Path, header: list[str], row: dict) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log_virtual_result(symbol, result, entry_price, tp, sl, setup_type="unknown"):
    try:
        ensure_results_file()
        with RESULTS_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([symbol, result, entry_price, tp, sl, time.time(), setup_type])
        logging.info(
            "VIRTUAL RESULT LOGGED | symbol=%s result=%s entry=%.5f",
            symbol,
            result,
            float(entry_price),
        )
    except Exception as e:
        logging.error("VIRTUAL RESULT LOG FAILED | %s", str(e))


ensure_results_file()

def create_virtual_trade(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    signal_score: float,
    entry_quality: float,
    latest_bar_id,
    reason: str,
    setup_type: str = "unknown",
    now_ts: float | None = None,
    max_hold_seconds: float | None = None,
) -> dict | None:

    # =========================================================
    # 🔥 DUPLICATE TRADE BLOCK (same timestamp)
    # =========================================================
    now_ts = float(now_ts if now_ts is not None else time.time())
    now_ts_int = int(now_ts)
    key = str(symbol).upper()

    last_ts = LAST_TRADE_TS.get(key)
    if last_ts == now_ts_int:
        logging.warning("DUPLICATE TRADE BLOCKED | symbol=%s reason=same_second", symbol)
        return None

    if ENABLE_TEST_MODE and last_ts is not None:
        delta = float(now_ts) - float(last_ts)
        if delta < TEST_MODE_MIN_ENTRY_GAP_SEC:
            logging.warning(
                "DUPLICATE TRADE BLOCKED | symbol=%s reason=min_gap delta=%.2f required=%.2f",
                symbol,
                delta,
                TEST_MODE_MIN_ENTRY_GAP_SEC,
            )
            return None

    LAST_TRADE_TS[key] = now_ts

    # =========================================================
    # 🔥 STACK LIMIT
    # =========================================================
    if count_open_trades(str(symbol).upper()) >= MAX_STACK_PER_SYMBOL:
        logging.warning("STACK LIMIT HIT | symbol=%s", symbol)
        return None

    # ✅ ONLY increment when trade is VALID
    global _VIRTUAL_TRADE_ID
    _VIRTUAL_TRADE_ID += 1

    hold_limit = float(max_hold_seconds or float(os.getenv("VIRTUAL_MAX_HOLD_SECONDS", "1800")))
    if ENABLE_TEST_MODE:
        hold_limit = min(hold_limit, float(os.getenv("TEST_MAX_HOLD_SECONDS", "15")))

    trade = {
        "id": int(_VIRTUAL_TRADE_ID),
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "entry_price": float(entry_price),
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
        "signal_score": float(signal_score),
        "entry_quality": float(entry_quality),
        "setup_type": str(setup_type or "unknown"),
        "created_ts": float(now_ts),
        "created_iso": datetime.utcnow().isoformat(),
        "latest_bar_id": str(latest_bar_id or ""),
        "reason": str(reason),
        "status": "open",
        "max_hold_seconds": hold_limit,
    }

    _VIRTUAL_TRADES.append(trade)

    _append_csv_row(
        _virtual_trades_file(),
        [
            "id",
            "symbol",
            "side",
            "entry_price",
            "sl_price",
            "tp_price",
            "signal_score",
            "entry_quality",
            "setup_type",
            "created_ts",
            "created_iso",
            "latest_bar_id",
            "reason",
            "status",
            "max_hold_seconds",
        ],
        trade,
    )

    logging.warning(
        "VIRTUAL TRADE OPENED | id=%s symbol=%s side=%s entry=%.5f sl=%.5f tp=%.5f reason=%s",
        trade["id"],
        trade["symbol"],
        trade["side"],
        trade["entry_price"],
        trade["sl_price"],
        trade["tp_price"],
        trade["reason"],
    )
    logging.warning(
        "NEW TRADE | %s %s entry=%.5f tp=%.5f sl=%.5f",
        trade["symbol"],
        trade["side"],
        trade["entry_price"],
        trade["tp_price"],
        trade["sl_price"],
    )
    return trade


def close_virtual_trade(trade: dict, exit_price: float, exit_reason: str, now_ts: float | None = None) -> None:
    now_ts = float(now_ts if now_ts is not None else time.time())
    if str(trade.get("status")) != "open":
        return

    entry = float(trade["entry_price"])
    side = str(trade["side"]).upper()
    exit_px = float(exit_price)
    pnl_pct = ((exit_px - entry) / max(entry, 1e-9)) if side == "LONG" else ((entry - exit_px) / max(entry, 1e-9))

    trade["status"] = "closed"
    trade["exit_price"] = exit_px
    trade["exit_reason"] = str(exit_reason)
    trade["closed_ts"] = now_ts
    trade["closed_iso"] = datetime.utcnow().isoformat()
    trade["pnl_pct"] = float(pnl_pct)

    result = "timeout"
    if exit_reason in ("tp_hit", "take_profit"):
        result = "tp"
    elif exit_reason in ("sl_hit", "stop_loss"):
        result = "sl"

    # =========================================================
    # 🔥 CRITICAL: ALWAYS LOG RESULT
    # =========================================================
    try:
        log_virtual_result(
            symbol=trade["symbol"],
            result=result,
            entry_price=trade["entry_price"],
            tp=trade["tp_price"],
            sl=trade["sl_price"],
            setup_type=trade.get("setup_type", "unknown"),
        )
    except Exception as e:
        logging.error("❌ RESULT LOGGING FAILED: %s", str(e))

    _append_csv_row(
        _virtual_trade_detailed_file(),
        [
            "id", "symbol", "side", "entry_price", "sl_price", "tp_price",
            "exit_price", "exit_reason", "pnl_pct", "signal_score",
            "entry_quality", "setup_type", "created_ts", "closed_ts", "created_iso", "closed_iso"
        ],
        trade,
    )

    logging.warning(
        "VIRTUAL TRADE CLOSED | id=%s symbol=%s side=%s exit=%.5f reason=%s pnl_pct=%.5f",
        trade["id"], trade["symbol"], trade["side"], exit_px, exit_reason, float(pnl_pct)
    )
    logging.warning(
        "TRADE CLOSED | symbol=%s result=%s entry=%.5f exit=%.5f",
        trade["symbol"],
        result,
        float(trade["entry_price"]),
        exit_px,
    )


def update_virtual_trades(symbol: str, current_price: float, now_ts: float | None = None) -> None:
    now_ts = float(now_ts if now_ts is not None else time.time())
    no_progress_seconds = float(os.getenv("EARLY_EXIT_NO_PROGRESS_SECONDS", "90") or 90.0)
    no_progress_min_pct = float(os.getenv("EARLY_EXIT_MIN_PROGRESS_PCT", "0.00015") or 0.00015)
    for trade in list(_VIRTUAL_TRADES):
        if str(trade.get("status")) != "open":
            continue
        if str(trade.get("symbol", "")).upper() != str(symbol).upper():
            continue

        side = str(trade["side"]).upper()
        sl = float(trade["sl_price"])
        tp = float(trade["tp_price"])
        age = now_ts - float(trade["created_ts"])
        hold_limit = float(trade.get("max_hold_seconds", 1800.0))

        if side == "LONG":
            if current_price <= sl:
                close_virtual_trade(trade, sl, "sl_hit", now_ts=now_ts)
                continue
            if current_price >= tp:
                close_virtual_trade(trade, tp, "tp_hit", now_ts=now_ts)
                continue
        else:
            if current_price >= sl:
                close_virtual_trade(trade, sl, "sl_hit", now_ts=now_ts)
                continue
            if current_price <= tp:
                close_virtual_trade(trade, tp, "tp_hit", now_ts=now_ts)
                continue

        if age >= hold_limit:
            close_virtual_trade(trade, current_price, "timeout", now_ts=now_ts)
            continue

        entry = float(trade.get("entry_price", current_price))
        progress_abs = abs(float(current_price) - entry) / max(abs(entry), 1e-9)
        if age >= no_progress_seconds and progress_abs < no_progress_min_pct:
            logging.info(
                "VIRTUAL EARLY EXIT | id=%s symbol=%s age=%.2f progress=%.6f required=%.6f",
                str(trade.get("id", "na")),
                str(trade.get("symbol", symbol)),
                float(age),
                float(progress_abs),
                float(no_progress_min_pct),
            )
            close_virtual_trade(trade, current_price, "no_progress_exit", now_ts=now_ts)
            continue

# =========================================
# 🔥 GLOBAL SYMBOL CACHE
# =========================================
_SYMBOL_CACHE = {}

# =========================================
# 🔥 GLOBAL FLAGS
# =========================================
FORCE_MODE = str(os.getenv("FORCE_EXECUTION", "true")).lower() in ("1", "true", "yes")
LIVE_EXECUTION_ENABLED_ENV = str(os.getenv("LIVE_EXECUTION_ENABLED", "false")).lower() in ("1", "true", "yes")
BROKER_BACKEND = str(os.getenv("BROKER_BACKEND", "mt5") or "mt5").strip().lower()

# =========================================================
# 🔥 EXECUTION FAILURE COOLDOWN (GLOBAL)
# =========================================================
_EXECUTION_FAILURE_STATE = {}

def execution_blocked(symbol: str, cooldown: float = 60.0) -> bool:
    import time as _t
    state = _EXECUTION_FAILURE_STATE.get(symbol)
    if not state:
        return False
    if _t.time() - state["ts"] < cooldown:
        return True
    return False

def register_execution_failure(symbol: str, reason: str):
    import time as _t
    _EXECUTION_FAILURE_STATE[symbol] = {
        "ts": _t.time(),
        "reason": reason,
    }
    logging.warning("EXECUTION COOLDOWN | symbol=%s reason=%s", symbol, reason)

# ================================
# RISK ENGINE CONFIG (ENV)
# ================================
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
MIN_POSITION_SIZE = float(os.getenv("MIN_POSITION_SIZE", "0.01"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "5.0"))


# =========================================================
# 🔥 EXECUTION PIPELINE (CLEAN)
# =========================================================
def evaluate_entry_pipeline(
    symbol: str,
    signal_score: float,
    open_positions: int,
    cooldown_remaining: float,
    min_time_remaining: float,
    spread_ok: bool,
    evo_allowed: bool,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    # 1. signal
    if signal_score <= 0:
        return False, ["no_signal"]

    # 2. filters
    if not spread_ok:
        reasons.append("spread")

    if not evo_allowed:
        reasons.append("evo")

    # 3. position constraint
    if open_positions > 0:
        reasons.append("position_exists")

    # 4. cooldown ONLY if position exists
    if open_positions > 0:
        if cooldown_remaining > 0:
            reasons.append("cooldown")
        if min_time_remaining > 0:
            reasons.append("min_time")

    allowed = len(reasons) == 0
    return allowed, reasons


# =========================================================
# 🔥 RISK SCALING ENGINE V2
# =========================================================
class RiskScalingEngine:
    def __init__(self):
        self.base_risk = float(os.getenv("RISK_PER_TRADE", "0.005"))
        self.max_risk = float(os.getenv("MAX_RISK_PER_TRADE", "0.01"))
        self.min_risk = float(os.getenv("MIN_RISK_PER_TRADE", "0.001"))
        self.win_boost = float(os.getenv("WIN_STREAK_BOOST", "1.2"))
        self.loss_penalty = float(os.getenv("LOSS_STREAK_PENALTY", "0.7"))
        self.max_dd = float(os.getenv("MAX_DRAWDOWN_PCT", "0.05"))
        self.current_risk = self.base_risk
        self.win_streak = 0
        self.loss_streak = 0
        self.peak_equity = None

    def update_after_trade(self, pnl_pct: float, equity: float):
        if self.peak_equity is None:
            self.peak_equity = equity

        self.peak_equity = max(self.peak_equity, equity)
        drawdown = (self.peak_equity - equity) / max(self.peak_equity, 1e-9)

        if pnl_pct > 0:
            self.win_streak += 1
            self.loss_streak = 0
            self.current_risk *= self.win_boost
        else:
            self.loss_streak += 1
            self.win_streak = 0
            self.current_risk *= self.loss_penalty

        # Drawdown protection
        if drawdown > self.max_dd:
            self.current_risk *= 0.5

        # Clamp
        self.current_risk = max(self.min_risk, min(self.current_risk, self.max_risk))

        logging.info(
            "RISK UPDATE | pnl=%.4f equity=%.2f risk=%.4f win_streak=%d loss_streak=%d dd=%.4f",
            pnl_pct,
            equity,
            self.current_risk,
            self.win_streak,
            self.loss_streak,
            drawdown,
        )

    def get_risk(self) -> float:
        return float(self.current_risk)

if not BROKER_BACKEND:
    BROKER_BACKEND = "mt5"
    logging.warning("BROKER_BACKEND missing -> defaulting to mt5")

if BROKER_BACKEND not in {"mt5"}:
    logging.warning("Unsupported BROKER_BACKEND=%s -> forcing mt5", BROKER_BACKEND)
    BROKER_BACKEND = "mt5"


def _safe_env_float(name: str, default: float) -> float:
    try:
        value = os.getenv(name, None)
        if value is None or str(value).strip() == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_env_int(name: str, default: int) -> int:
    try:
        value = os.getenv(name, None)
        if value is None or str(value).strip() == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.floor(float(value) / float(step)) * float(step)


def _resolve_broker_lot_constraints(symbol_specs) -> tuple[float, float, float]:
    env_min_lot = _safe_env_float("BROKER_MIN_LOT", 0.01)
    env_max_lot = _safe_env_float("BROKER_MAX_LOT", 0.05)
    env_lot_step = _safe_env_float("LOT_STEP", 0.01)

    spec_min_lot = float(getattr(symbol_specs, "min_lot", getattr(symbol_specs, "minimum_size", 0.0)) or 0.0)
    spec_max_lot = float(getattr(symbol_specs, "max_lot", 0.0) or 0.0)
    spec_lot_step = float(getattr(symbol_specs, "lot_step", getattr(symbol_specs, "size_step", 0.0)) or 0.0)

    min_lot = spec_min_lot if spec_min_lot > 0 else env_min_lot
    max_lot = spec_max_lot if spec_max_lot > 0 else env_max_lot
    lot_step = spec_lot_step if spec_lot_step > 0 else env_lot_step

    if env_min_lot > 0:
        min_lot = max(min_lot, env_min_lot)
    if env_max_lot > 0:
        max_lot = min(max_lot, env_max_lot) if max_lot > 0 else env_max_lot
    if env_lot_step > 0:
        lot_step = env_lot_step

    if max_lot <= 0:
        max_lot = min_lot
    if max_lot < min_lot:
        max_lot = min_lot

    return float(min_lot), float(max_lot), float(lot_step)


def _compute_effective_risk_usd(equity: float) -> float:
    risk_fraction = _safe_env_float("RISK_PER_TRADE", 0.01)
    hard_cap_usd = _safe_env_float("RISK_PER_TRADE_USD", 0.0)

    equity = max(float(equity), 0.0)
    risk_fraction = max(float(risk_fraction), 0.0)

    computed = equity * risk_fraction
    if hard_cap_usd > 0:
        computed = min(computed, hard_cap_usd)

    return max(computed, 0.0)


def _estimate_fx_lot_from_risk(
    equity: float,
    entry_price: float,
    symbol_specs,
    stop_loss_pips: float,
) -> float:
    _ = float(entry_price)
    effective_risk_usd = _compute_effective_risk_usd(equity)
    pip_value_per_lot = float(getattr(symbol_specs, "pip_value_per_lot", 0.0) or 0.0)
    min_lot, max_lot, lot_step = _resolve_broker_lot_constraints(symbol_specs)

    if effective_risk_usd <= 0 or pip_value_per_lot <= 0 or stop_loss_pips <= 0:
        logging.warning(
            "POSITION SIZE BLOCK | invalid inputs risk_usd=%.6f pip_value_per_lot=%.6f stop_loss_pips=%.4f",
            float(effective_risk_usd),
            float(pip_value_per_lot),
            float(stop_loss_pips),
        )
        return 0.0

    raw_lot = float(effective_risk_usd) / (float(stop_loss_pips) * float(pip_value_per_lot))
    clipped_lot = _clamp(raw_lot, min_lot, max_lot)
    rounded_lot = _round_down_to_step(clipped_lot, lot_step)

    if rounded_lot < min_lot:
        rounded_lot = min_lot

    logging.info(
        "FX LOT SIZE | equity=%.2f risk_usd=%.4f stop_loss_pips=%.2f pip_value_per_lot=%.4f raw_lot=%.6f rounded_lot=%.6f min_lot=%.4f max_lot=%.4f step=%.4f",
        float(equity),
        float(effective_risk_usd),
        float(stop_loss_pips),
        float(pip_value_per_lot),
        float(raw_lot),
        float(rounded_lot),
        float(min_lot),
        float(max_lot),
        float(lot_step),
    )
    return float(rounded_lot)


def _live_broker_position_exists(adapter, symbol: str) -> bool:
    try:
        pos = fetch_open_position(adapter, symbol)
        return bool(getattr(pos, "is_open", False))
    except Exception:
        return False


# =========================================================
# ICT TP / SL ENGINE
# =========================================================
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _get_structure_anchor(latest_filter, side: str):
    last_swing_low = float(getattr(latest_filter, "last_swing_low", 0.0) or 0.0)
    last_swing_high = float(getattr(latest_filter, "last_swing_high", 0.0) or 0.0)
    next_swing_high = float(getattr(latest_filter, "next_swing_high", 0.0) or 0.0)
    next_swing_low = float(getattr(latest_filter, "next_swing_low", 0.0) or 0.0)
    equal_highs = list(getattr(latest_filter, "equal_highs", []) or [])
    equal_lows = list(getattr(latest_filter, "equal_lows", []) or [])
    last_higher_low = float(getattr(latest_filter, "last_higher_low", 0.0) or 0.0)
    last_lower_high = float(getattr(latest_filter, "last_lower_high", 0.0) or 0.0)

    return SimpleNamespace(
        side=side,
        last_swing_low=last_swing_low,
        last_swing_high=last_swing_high,
        next_swing_high=next_swing_high,
        next_swing_low=next_swing_low,
        equal_highs=equal_highs,
        equal_lows=equal_lows,
        last_higher_low=last_higher_low,
        last_lower_high=last_lower_high,
    )


def _compute_ict_exit_plan(entry_price: float, side: str, latest_filter):
    structure = _get_structure_anchor(latest_filter, side)
    fallback_sl_pct = _env_float("FALLBACK_SL_PCT", 0.0009)
    fallback_tp_rr = _env_float("FALLBACK_TP_RR", 2.0)
    min_rr = _env_float("MIN_RR_REQUIRED", 1.5)
    sl_buffer_pct = _env_float("STRUCTURE_SL_BUFFER_PCT", 0.00005)

    if side == "LONG":
        raw_sl = structure.last_swing_low
        if raw_sl <= 0 or raw_sl >= entry_price:
            raw_sl = entry_price * (1.0 - fallback_sl_pct)
        sl = raw_sl - (entry_price * sl_buffer_pct)
    else:
        raw_sl = structure.last_swing_high
        if raw_sl <= 0 or raw_sl <= entry_price:
            raw_sl = entry_price * (1.0 + fallback_sl_pct)
        sl = raw_sl + (entry_price * sl_buffer_pct)

    risk = abs(entry_price - sl)
    if risk <= 0 or not math.isfinite(risk):
        return None

    tp = 0.0
    if side == "LONG":
        if structure.equal_highs:
            try:
                tp = max(float(x) for x in structure.equal_highs if float(x) > entry_price)
            except Exception:
                tp = 0.0
        if tp <= entry_price and structure.next_swing_high > entry_price:
            tp = structure.next_swing_high
        if tp <= entry_price:
            tp = entry_price + (risk * fallback_tp_rr)
    else:
        if structure.equal_lows:
            try:
                tp = min(float(x) for x in structure.equal_lows if float(x) < entry_price)
            except Exception:
                tp = 0.0
        if (tp <= 0 or tp >= entry_price) and structure.next_swing_low > 0 and structure.next_swing_low < entry_price:
            tp = structure.next_swing_low
        if tp <= 0 or tp >= entry_price:
            tp = entry_price - (risk * fallback_tp_rr)

    reward = abs(tp - entry_price)
    rr = reward / risk if risk > 0 else 0.0
    if not math.isfinite(rr) or rr < min_rr:
        logging.warning(
            "ICT EXIT REJECTED | side=%s entry=%.5f tp=%.5f sl=%.5f rr:%.2f min_rr:%.2f",
            side, entry_price, tp, sl, rr, min_rr
        )
        return None

    min_hold_seconds = int(_env_float("MIN_HOLD_SECONDS", 60))
    partial_rr = _env_float("PARTIAL_TP_AT_RR", 1.0)
    trailing_activation_rr = _env_float("TRAILING_ACTIVATION_RR", 1.2)
    noise_spread_mult = _env_float("NOISE_SPREAD_MULT", 3.0)
    plan = {
        "tp_price": float(tp),
        "sl_price": float(sl),
        "risk_distance": float(risk),
        "rr": float(rr),
        "min_hold_seconds": int(min_hold_seconds),
        "partial_tp_price": float(entry_price + (risk * partial_rr) if side == "LONG" else entry_price - (risk * partial_rr)),
        "partial_fraction": 0.50,
        "trailing_activation_price": float(entry_price + (risk * trailing_activation_rr) if side == "LONG" else entry_price - (risk * trailing_activation_rr)),
        "noise_spread_mult": float(noise_spread_mult),
        "use_structure_trailing": True,
    }
    logging.info(
        "ICT EXIT PLAN | side=%s entry=%.5f tp=%.5f sl=%.5f rr:%.2f partial=%.5f trail=%.5f",
        side,
        entry_price,
        plan["tp_price"],
        plan["sl_price"],
        plan["rr"],
        plan["partial_tp_price"],
        plan["trailing_activation_price"],
    )
    return plan


def _maybe_manage_ict_trade(active_trade, current_price: float, latest_filter, spread: float, now_ts: float):
    if active_trade is None:
        return None
    side = str(getattr(active_trade, "side", "")).upper()
    entry_price = float(getattr(active_trade, "entry_price", 0.0) or 0.0)
    sl_price = float(getattr(active_trade, "sl_price", 0.0) or 0.0)
    tp_price = float(getattr(active_trade, "tp_price", 0.0) or 0.0)
    partial_tp_price = float(getattr(active_trade, "partial_tp_price", 0.0) or 0.0)
    trailing_activation_price = float(getattr(active_trade, "trailing_activation_price", 0.0) or 0.0)
    partial_fraction = float(getattr(active_trade, "partial_fraction", 0.50) or 0.50)
    min_hold_seconds = int(getattr(active_trade, "min_hold_seconds", 60) or 60)
    partial_taken = bool(getattr(active_trade, "partial_taken", False))
    runner = bool(getattr(active_trade, "runner", False))
    raw_entry_time = getattr(active_trade, "entry_time", now_ts)
    if isinstance(raw_entry_time, datetime):
        entry_ts = float(raw_entry_time.timestamp())
    else:
        entry_ts = float(raw_entry_time or now_ts)
    use_structure_trailing = bool(getattr(active_trade, "use_structure_trailing", True))
    noise_spread_mult = float(getattr(active_trade, "noise_spread_mult", 3.0) or 3.0)

    if now_ts - entry_ts < min_hold_seconds:
        return None
    if abs(current_price - entry_price) < (max(spread, 0.0) * noise_spread_mult):
        return None

    if not partial_taken:
        if side == "LONG" and current_price >= partial_tp_price:
            active_trade.partial_taken = True
            active_trade.runner = True
            active_trade.sl_price = max(sl_price, entry_price)
            return {"action": "partial_close", "fraction": partial_fraction, "reason": "partial_tp_hit"}
        if side == "SHORT" and current_price <= partial_tp_price:
            active_trade.partial_taken = True
            active_trade.runner = True
            active_trade.sl_price = min(sl_price, entry_price) if sl_price > 0 else entry_price
            return {"action": "partial_close", "fraction": partial_fraction, "reason": "partial_tp_hit"}

    if runner and use_structure_trailing:
        structure = _get_structure_anchor(latest_filter, side)
        if side == "LONG" and current_price >= trailing_activation_price and structure.last_higher_low > 0:
            new_sl = max(float(getattr(active_trade, "sl_price", 0.0) or 0.0), structure.last_higher_low)
            if new_sl > float(getattr(active_trade, "sl_price", 0.0) or 0.0):
                active_trade.sl_price = new_sl
        elif side == "SHORT" and current_price <= trailing_activation_price and structure.last_lower_high > 0:
            old_sl = float(getattr(active_trade, "sl_price", 0.0) or 0.0)
            new_sl = structure.last_lower_high if old_sl <= 0 else min(old_sl, structure.last_lower_high)
            if old_sl <= 0 or new_sl < old_sl:
                active_trade.sl_price = new_sl

    final_sl = float(getattr(active_trade, "sl_price", sl_price) or sl_price)
    final_tp = float(getattr(active_trade, "tp_price", tp_price) or tp_price)
    if side == "LONG":
        if current_price <= final_sl:
            return {"action": "close", "reason": "sl_hit"}
        if current_price >= final_tp:
            return {"action": "close", "reason": "tp_hit"}
    elif side == "SHORT":
        if current_price >= final_sl:
            return {"action": "close", "reason": "sl_hit"}
        if current_price <= final_tp:
            return {"action": "close", "reason": "tp_hit"}
    return None

# =========================================
# 🔥 PROFIT LOCK / TRAILING EXIT ENGINE
# =========================================
class ProfitLockEngine:
    def __init__(self):
        self.break_even_trigger = float(os.getenv("PE_BREAK_EVEN_TRIGGER", "0.0006"))
        self.break_even_buffer = float(os.getenv("PE_BREAK_EVEN_BUFFER", "0.0001"))
        self.profit_lock_trigger = float(os.getenv("PE_PROFIT_LOCK_TRIGGER", "0.0010"))
        self.profit_lock_floor = float(os.getenv("PE_PROFIT_LOCK_FLOOR", "0.0004"))
        self.trailing_trigger = float(os.getenv("PE_TRAILING_TRIGGER", "0.0014"))
        self.trailing_distance = float(os.getenv("PE_TRAILING_DISTANCE", "0.0005"))
        self.hard_take_profit = float(os.getenv("PE_HARD_TP", "0.0025"))
        self.max_loss = float(os.getenv("PE_MAX_LOSS", "-0.0008"))

    def _calc_pnl_pct(self, trade, current_price: float) -> float:
        pnl = (float(current_price) - float(trade.entry_price)) / max(float(trade.entry_price), 1e-12)
        if str(getattr(trade, "side", "")).upper() == "SHORT":
            pnl = -pnl
        return float(pnl)

    def _ensure_state(self, trade) -> None:
        if not hasattr(trade, "peak_profit_pct"):
            trade.peak_profit_pct = 0.0
        if not hasattr(trade, "break_even_armed"):
            trade.break_even_armed = False
        if not hasattr(trade, "profit_lock_armed"):
            trade.profit_lock_armed = False
        if not hasattr(trade, "trailing_armed"):
            trade.trailing_armed = False
        if not hasattr(trade, "profit_floor_pct"):
            trade.profit_floor_pct = 0.0
        if not hasattr(trade, "trailing_stop_pct"):
            trade.trailing_stop_pct = None

    def manage_trade(self, trade, current_price: float, bars: pd.DataFrame | None = None) -> tuple[str, str]:
        self._ensure_state(trade)
        pnl_pct = self._calc_pnl_pct(trade, current_price)
        trade.peak_profit_pct = max(float(trade.peak_profit_pct), float(pnl_pct))

        logging.info(
            "PROFIT LOCK DEBUG | symbol=%s side=%s pnl_pct=%.5f peak=%.5f be=%s lock=%s trailing=%s floor=%.5f trailing_stop=%s",
            str(getattr(trade, "symbol", "unknown")).upper(),
            str(getattr(trade, "side", "unknown")).upper(),
            float(pnl_pct),
            float(trade.peak_profit_pct),
            str(bool(trade.break_even_armed)).lower(),
            str(bool(trade.profit_lock_armed)).lower(),
            str(bool(trade.trailing_armed)).lower(),
            float(trade.profit_floor_pct),
            "none" if trade.trailing_stop_pct is None else f"{float(trade.trailing_stop_pct):.5f}",
        )

        # 1) hard stop loss
        if pnl_pct <= self.max_loss:
            return "close", "hard_loss_limit"

        # 2) hard TP
        if pnl_pct >= self.hard_take_profit:
            return "close", "hard_take_profit"

        # 3) break-even arm
        if (not trade.break_even_armed) and pnl_pct >= self.break_even_trigger:
            trade.break_even_armed = True
            trade.profit_floor_pct = max(float(trade.profit_floor_pct), float(self.break_even_buffer))
            logging.info(
                "BREAK EVEN ARMED | symbol=%s pnl_pct=%.5f floor=%.5f",
                str(getattr(trade, "symbol", "unknown")).upper(),
                float(pnl_pct),
                float(trade.profit_floor_pct),
            )

        # 4) profit lock arm
        if (not trade.profit_lock_armed) and pnl_pct >= self.profit_lock_trigger:
            trade.profit_lock_armed = True
            trade.profit_floor_pct = max(float(trade.profit_floor_pct), float(self.profit_lock_floor))
            logging.info(
                "PROFIT LOCK ARMED | symbol=%s pnl_pct=%.5f locked_floor=%.5f",
                str(getattr(trade, "symbol", "unknown")).upper(),
                float(pnl_pct),
                float(trade.profit_floor_pct),
            )

        # 5) trailing arm
        if (not trade.trailing_armed) and pnl_pct >= self.trailing_trigger:
            trade.trailing_armed = True
            trade.trailing_stop_pct = float(pnl_pct) - float(self.trailing_distance)
            logging.info(
                "TRAILING ARMED | symbol=%s pnl_pct=%.5f trailing_stop=%.5f",
                str(getattr(trade, "symbol", "unknown")).upper(),
                float(pnl_pct),
                float(trade.trailing_stop_pct),
            )

        # 6) trailing update
        if trade.trailing_armed:
            new_stop = float(trade.peak_profit_pct) - float(self.trailing_distance)
            if trade.trailing_stop_pct is None or new_stop > float(trade.trailing_stop_pct):
                trade.trailing_stop_pct = float(new_stop)
                logging.info(
                    "TRAILING UPDATE | symbol=%s peak=%.5f trailing_stop=%.5f",
                    str(getattr(trade, "symbol", "unknown")).upper(),
                    float(trade.peak_profit_pct),
                    float(trade.trailing_stop_pct),
                )

        # =========================================
        # 🔥 EVO EDGE V1.5.1 EXIT ENGINE (WORKING)
        # =========================================
        side = str(getattr(trade, "side", "")).upper()
        if bars is not None and side in {"LONG", "SHORT"}:
            try:
                last = bars.iloc[-1]
                last_close = float(last["close"])
                raw_entry_price = float(getattr(trade, "entry_price", 0.0) or 0.0)
                raw_stop_price = float(getattr(trade, "sl_price", getattr(trade, "stop_loss", 0.0)) or 0.0)

                # ===== NORMALIZE VARS FOR EVO ENGINE =====
                in_position = True
                position_open = in_position
                direction = 1 if side == "LONG" else -1
                current_trade = {"entry": raw_entry_price}
                entry_price = float(current_trade["entry"]) if "current_trade" in locals() else raw_entry_price
                stop_price = float(raw_stop_price)
                side = "LONG" if direction == 1 else "SHORT"

                def close_position():
                    return "close", "structure_fail"

                # =========================================
                # 🔥 EVO EDGE V1.6 (FIXED REAL VERSION)
                # =========================================
                if position_open:
                    last_high = float(last["high"])
                    last_low = float(last["low"])

                    risk = abs(entry_price - stop_price)
                    if risk == 0:
                        risk = 0.0001

                    profit = (last_close - entry_price) if side == "LONG" else (entry_price - last_close)

                    # ---------------------------------
                    # 1. BREAK EVEN (0.5R sneller)
                    # ---------------------------------
                    if profit >= 0.5 * risk:
                        if side == "LONG" and stop_price < entry_price:
                            stop_price = entry_price
                            trade.sl_price = stop_price
                            trade.stop_loss = stop_price
                            logging.info("EVO EXIT | move_to_BE")

                        if side == "SHORT" and stop_price > entry_price:
                            stop_price = entry_price
                            trade.sl_price = stop_price
                            trade.stop_loss = stop_price
                            logging.info("EVO EXIT | move_to_BE")

                    # ---------------------------------
                    # 2. TRAILING (agressiever)
                    # ---------------------------------
                    if side == "LONG":
                        new_stop = last_low
                        if new_stop > stop_price:
                            stop_price = new_stop
                            trade.sl_price = stop_price
                            trade.stop_loss = stop_price
                            logging.info("EVO EXIT | trail_up")

                    if side == "SHORT":
                        new_stop = last_high
                        if new_stop < stop_price:
                            stop_price = new_stop
                            trade.sl_price = stop_price
                            trade.stop_loss = stop_price
                            logging.info("EVO EXIT | trail_down")

                    # ---------------------------------
                    # 3. HARD EXIT (fail safe)
                    # ---------------------------------
                    if profit < -0.8 * risk:
                        logging.info("EVO EXIT | hard_fail")
                        return close_position()
            except Exception as e:
                logging.warning("EVO EXIT ERROR | %s", str(e))

        # 7) close on profit floor loss
        if float(trade.profit_floor_pct) > 0.0 and pnl_pct <= float(trade.profit_floor_pct):
            return "close", "profit_floor_hit"

        # 8) close on trailing stop hit
        if trade.trailing_armed and trade.trailing_stop_pct is not None and pnl_pct <= float(trade.trailing_stop_pct):
            return "close", "trailing_stop_hit"

        return "hold", "profit_engine_hold"

# =========================================
# 🔥 SYMBOL CONTROL (CRITICAL FIX)
# =========================================

# 🔥 CACHE (VERY IMPORTANT)
_ALLOWED_SYMBOLS_CACHE = None

def get_allowed_symbols():
    global _ALLOWED_SYMBOLS_CACHE

    if _ALLOWED_SYMBOLS_CACHE is not None:
        return _ALLOWED_SYMBOLS_CACHE

    whitelist = os.getenv("SYMBOL_WHITELIST", "").strip()

    if whitelist:
        symbols = [s.strip().upper() for s in whitelist.split(",") if s.strip()]
        logging.info("SYMBOL WHITELIST ACTIVE | symbols=%s", symbols)
    else:
        symbols = ["EURUSD", "GBPUSD", "USDJPY"]
        logging.warning("SYMBOL WHITELIST EMPTY → using default FX set")

    # 🔥 HARD BLOCK XAU (EXTRA SAFETY)
    symbols = [s for s in symbols if s != "XAUUSD"]

    _ALLOWED_SYMBOLS_CACHE = symbols
    return symbols


def is_symbol_allowed(symbol: str) -> bool:
    return str(symbol).upper() in get_allowed_symbols()


# =========================================
# GLOBAL FLAGS
# =========================================
DISABLE_MT5 = str(os.getenv("DISABLE_MT5", "1")).lower() in ("1", "true", "yes")
USE_LEGACY_EXECUTION = False

# 🔥 FORCE MT5 DATA ONLY (unless explicitly disabled for safe paper mode)
DATA_BACKEND = "paper" if DISABLE_MT5 else "mt5"
if DISABLE_MT5:
    logging.warning("MT5 DISABLED | running in pure paper mode")
else:
    logging.warning("FORCED DATA BACKEND = MT5 (BYBIT DISABLED)")


def generate_fake_data(symbol: str, n: int = 200) -> pd.DataFrame:
    base_price = 1.1000 if "USD" in str(symbol).upper() else 100.0
    candles: list[dict[str, float | datetime]] = []
    now = datetime.now(timezone.utc)
    price = float(base_price)

    for i in range(n):
        open_price = float(price)
        move = float(random.uniform(-0.001, 0.001))
        close_price = float(open_price + move)
        high = float(max(open_price, close_price) + random.uniform(0.0, 0.0005))
        low = float(min(open_price, close_price) - random.uniform(0.0, 0.0005))
        ts = now - timedelta(minutes=(n - i))
        candles.append(
            {
                "timestamp": ts,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": 1.0,
                "price": close_price,
                "size": 1.0,
            }
        )
        price = close_price

    frame = pd.DataFrame(candles).sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    return frame


def get_market_data_safe(symbol: str, timeframe_seconds: int = 60, required_bars: int = 150) -> pd.DataFrame:
    bars: pd.DataFrame | None = None
    requested_count = max(200, int(required_bars) + 50)
    if DATA_BACKEND == "mt5":
        try:
            bars = fetch_mt5_bars(
                symbol=symbol,
                timeframe_seconds=timeframe_seconds,
                count=requested_count,
            )
            if bars is None or len(bars) < int(required_bars):
                raise ValueError("mt5_insufficient_data")
            return bars
        except Exception:
            logging.warning(
                "MT5 FAILED → switching to FAKE DATA | symbol=%s",
                symbol,
            )

    fallback_count = max(requested_count, int(required_bars))
    bars = generate_fake_data(symbol, n=fallback_count)
    logging.info(
        "FAKE DATA ACTIVE | symbol=%s bars=%s",
        symbol,
        len(bars),
    )
    return bars


def log_legacy_execution_disabled_once() -> None:
    if getattr(log_legacy_execution_disabled_once, "_done", False):
        return
    logging.info("LEGACY EXECUTION DISABLED | using single decision engine only")
    setattr(log_legacy_execution_disabled_once, "_done", True)


def should_skip_legacy_log(message: str) -> bool:
    if USE_LEGACY_EXECUTION:
        return False
    legacy_markers = (
        "EXECUTION FUNNEL",
        "EXEC DECISION V2",
        "LEVEL7 OVERRIDE",
        "SOFT BLOCK",
        "FORCE EXECUTION ENABLED",
        "REENTRY BLOCK DISABLED - forcing execution",
        "ENTRY FILTER GATE",
        "DEBUG FLOW",
        "PRE-EXEC CHECK",
        "THRESHOLD TUNED",
    )
    return any(marker in message for marker in legacy_markers)


_original_logging_info = logging.info
_original_logging_warning = logging.warning


def _patched_logging_info(msg, *args, **kwargs):
    text = str(msg)
    if should_skip_legacy_log(text):
        return
    return _original_logging_info(msg, *args, **kwargs)


def _patched_logging_warning(msg, *args, **kwargs):
    text = str(msg)
    if should_skip_legacy_log(text):
        return
    return _original_logging_warning(msg, *args, **kwargs)


logging.info = _patched_logging_info
logging.warning = _patched_logging_warning


# =========================================
# 🔥 PROFIT ENGINE V2 (BALANCED FX MODE)
# =========================================
class ProfitEngineV2:
    def __init__(self):
        # 🔥 FX REALISTIC THRESHOLDS
        self.min_signal = float(os.getenv("PE_MIN_SIGNAL", "0.18"))
        self.min_quality = float(os.getenv("PE_MIN_QUALITY", "0.20"))

        # 🔥 RISK / PROFIT CONTROL
        self.loss_kill_threshold = float(os.getenv("PE_LOSS_KILL", "-0.0004"))
        self.quick_exit_profit = float(os.getenv("PE_QUICK_TP", "0.0004"))
        self.runner_threshold = float(os.getenv("PE_RUNNER_TRIGGER", "0.0020"))

        # 🔥 NEW: HOLD + TIME STOP
        self.min_hold_time = float(os.getenv("PE_MIN_HOLD", "5"))
        self.time_stop_seconds = float(os.getenv("PE_TIME_STOP", "25"))

    def allow_entry(self, signal, quality):
        if signal < self.min_signal:
            return False, "signal_too_weak"
        if quality < self.min_quality:
            return False, "quality_too_low"
        return True, "ok"

    def manage_trade(self, trade, current_price):
        # 🔥 SAFE INIT
        if not hasattr(trade, "runner"):
            trade.runner = False

        if not hasattr(trade, "entry_time"):
            trade.entry_time = time.time()

        pnl = (current_price - trade.entry_price) / trade.entry_price
        if trade.side == "SHORT":
            pnl = -pnl

        elapsed = time.time() - trade.entry_time

        # 🔥 MIN HOLD (voorkomt instant exit noise)
        if elapsed < self.min_hold_time:
            return "hold", "min_hold"

        # 🔥 TIME STOP (chop killer)
        if elapsed > self.time_stop_seconds and pnl < 0.0001:
            return "close", "time_stop"

        if pnl < self.loss_kill_threshold:
            return "close", "loss_kill"

        if pnl > self.quick_exit_profit and not trade.runner:
            trade.runner = True
            return "partial_close", "quick_profit_lock"

        if pnl > self.runner_threshold:
            trade.runner = True
            return "hold", "runner_active"

        return "hold", "normal"


def resolve_mt5_path() -> str | None:
    mt5_path = os.getenv("MT5_PATH")

    if not mt5_path or str(mt5_path).lower() in {"auto", "none", ""}:
        logging.warning("MT5 PATH RESOLVER | auto mode → using active terminal")
        return None

    if not os.path.exists(mt5_path):
        logging.warning(
            "MT5 PATH RESOLVER | invalid path=%s → fallback to active terminal",
            mt5_path,
        )
        return None

    logging.info("MT5 PATH RESOLVER | using explicit path=%s", mt5_path)
    return mt5_path


def init_mt5_connection() -> bool:
    if mt5 is None:
        logging.critical("MT5 PACKAGE NOT INSTALLED")
        return False

    mt5_path = resolve_mt5_path()
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")

    logging.info("MT5 INIT START | path=%s", mt5_path)

    # ===============================
    # 🔥 MULTI INIT STRATEGY
    # ===============================

    initialized = False

    # 1️⃣ try with path
    if mt5_path:
        logging.info("MT5 INIT TRY | method=path")
        initialized = mt5.initialize(path=mt5_path)

    # 2️⃣ fallback → no path (VERY IMPORTANT)
    if not initialized:
        logging.warning("MT5 INIT FALLBACK | method=auto")
        initialized = mt5.initialize()

    # 3️⃣ retry once
    if not initialized:
        logging.warning("MT5 INIT RETRY")
        time.sleep(2)
        initialized = mt5.initialize()

    if not initialized:
        logging.critical("MT5 INIT FAILED HARD → TERMINAL NOT CONNECTED")
        return False

    # 🔥 FORCE USE EXISTING TERMINAL SESSION
    logging.warning("MT5 LOGIN DISABLED → using active terminal session")

    account = mt5.account_info()
    if account is None:
        logging.critical("NO ACTIVE MT5 SESSION → TERMINAL NOT LOGGED IN")
        return False

    # 🔥 EXTRA DEBUG
    logging.info(
        "MT5 TERMINAL INFO | build=%s connected=%s",
        getattr(mt5.terminal_info(), "build", None),
        getattr(mt5.terminal_info(), "connected", None),
    )

    logging.info(
        "MT5 CONNECTED | login=%s balance=%s server=%s",
        getattr(account, "login", None),
        getattr(account, "balance", None),
        getattr(account, "server", None),
    )

    symbols = mt5.symbols_get()
    if symbols is None or len(symbols) == 0:
        logging.critical("MT5 NO SYMBOLS FOUND → MARKET WATCH EMPTY")
        return False

    logging.info("MT5 SYMBOLS AVAILABLE | count=%d", len(symbols))
    return True


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

import argparse
import copy
import csv
import io
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from statistics import median, pvariance
from types import SimpleNamespace
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from broker_adapter import (
    BrokerAdapter,
    BrokerOrderResult,
    MT5BrokerAdapter,
    NormalizedPosition,
    SymbolSpecs,
    calculate_fx_lot_size,
    calculate_order_size_from_notional,
    compute_safe_fx_lot,
    normalize_mt5_symbol_info,
    resolve_fx_pip_value_per_lot,
    round_size_to_step,
    sync_normalized_position,
)

import pandas as pd
import numpy as np

try:
    from pybit.unified_trading import HTTP
except Exception:  # pragma: no cover - optional dependency
    HTTP = None

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    mt5 = None

from config.risk_config_loader import load_risk_config
from evolution_engine import EvolutionEngine
from execution.bar_builder import build_bars_from_trades
from execution.risk_manager import (
    RiskConfig,
    RiskState,
    apply_profit_engine,
    can_open_new_position,
    maybe_pause_after_consecutive_losses,
    register_entry,
    register_exit,
    reset_risk_day,
)
from ict_entry import ict_entry_v2
from research.auto_discovery.strategy_runtime import compute_signal_strength_series, enrich_signal_strength_context as enrich_runtime_signal_strength_context, evaluate_entry_filter_snapshot, evaluate_runtime_signal, prepare_runtime_context
from spread_filter import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MIN_VOLUME_RATIO as SHARED_DEFAULT_MIN_VOLUME_RATIO,
    DEFAULT_SPREAD_MULTIPLIER,
    MarketRegime,
    MAX_SAFE_SPREAD_RATIO,
    MIN_SAFE_SPREAD_RATIO,
    cooldown_for_regime,
    spread_multiplier_for_regime,
    volume_threshold_for_regime,
)


TESTNET_URL = 'https://api-testnet.bybit.com/v5/market/recent-trade'
DEFAULT_MIN_VOLUME_RATIO = SHARED_DEFAULT_MIN_VOLUME_RATIO
DEFAULT_MIN_TREND_STRENGTH = 0.00005
DEFAULT_SPREAD_RELAXATION_FACTOR = 1.1
SIGNAL_PRIORITY_BYPASS_THRESHOLD = 1.2
HIGH_QUALITY_OVERRIDE_THRESHOLD = 0.85
FORCE_EXECUTE_SIGNAL_THRESHOLD = 1.1
FORCE_EXECUTE_VOLUME_THRESHOLD = 1.5
FILL_RISK_REDUCTION_FACTOR = 0.5
SOFT_FILL_RISK_SPREAD_MULTIPLE = 1.5
SIGNAL_PRIORITY_EXECUTE_THRESHOLD = 0.9
SIGNAL_PRIORITY_MIN_EXECUTION_SCORE = 0.6
SIGNAL_PRIORITY_FILTER_OVERRIDE_THRESHOLD = 0.9
AGGRESSION_MODE = True
STRONG_SIGNAL_POSITION_SCALE = 1.35
LOSS_SIZE_REDUCTION_FACTOR = 0.75
DRAWDOWN_AGGRESSION_FLOOR = 0.45
MAX_DAILY_TRADES_HARD_CAP = 250
MIN_ADAPTIVE_MAX_TRADES_PER_DAY = 6
MAX_ADAPTIVE_MAX_TRADES_PER_DAY = 600
EARLY_EXIT_GRACE_SECONDS = 8.0
DAILY_LOSS_STOP_BUFFER = 0.9
HIGH_CONFIDENCE_SPREAD_OVERRIDE_FACTOR = 1.2
ROLLING_SPREAD_WINDOW = 50
VOLATILITY_LOOKBACK = 20
MIN_SIGNAL_BARS = 8
DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSDT"]
DEFAULT_SYMBOL_VOLATILITY_THRESHOLDS = {
    "EURUSD": 0.00012,
    "GBPUSD": 0.00015,
    "USDJPY": 0.00010,
    "XAUUSD": 0.00035,
    "BTCUSDT": 0.00080,
}
SYMBOL_VOLATILITY_LOOKBACK = 200
SYMBOL_VOLATILITY_THRESHOLD_MIN_SCALE = 0.40
SYMBOL_VOLATILITY_THRESHOLD_MAX_SCALE = 1.1
EXECUTION_RATE_RELAX_THRESHOLD = 0.3
EXECUTION_RATE_TIGHTEN_THRESHOLD = 0.8
DEFAULT_SPREAD_MULTIPLIER_MAX = 6.0
DEFAULT_BASE_COOLDOWN_SECONDS = 60
DEFAULT_MIN_COOLDOWN_SECONDS = 10
DEFAULT_MAX_COOLDOWN_SECONDS = 180
ADAPTIVE_MIN_TIME_FLOOR_SECONDS = 5
ADAPTIVE_MIN_TIME_CEILING_SECONDS = 60
SMART_COOLDOWN_FLOOR_SECONDS = 10
SMART_COOLDOWN_CEILING_SECONDS = 180
LEVEL7_MIN_TIME_FLOOR_SECONDS = 5
LEVEL7_MIN_TIME_CEILING_SECONDS = 45
LEVEL7_COOLDOWN_FLOOR_SECONDS = 15
LEVEL7_COOLDOWN_CEILING_SECONDS = 120
LEVEL7_FAST_SIGNAL_MIN = 0.65
LEVEL7_ULTRA_SIGNAL_MIN = 1.00
LEVEL7_FAST_QUALITY_MIN = 0.75
LEVEL7_ULTRA_QUALITY_MIN = 1.05
LEVEL7_FAST_VOL_MIN = 0.00003
LEVEL7_ULTRA_VOL_MIN = 0.00008
LEVEL7_MAX_SPREAD_USAGE = 0.80
LEVEL7_ULTRA_MAX_SPREAD_USAGE = 0.70
FALLBACK_MIN_SIGNAL_SCORE = 0.05
FALLBACK_MIN_QUALITY_SCORE = 0.55
MIN_SIGNAL_FOR_FALLBACK = 0.01
MIN_QUALITY_FOR_FALLBACK = 0.01
FALLBACK_ABSOLUTE_SYMBOL_FLOOR_FX = 0.00003
FALLBACK_ABSOLUTE_SYMBOL_FLOOR_XAUUSD = 0.00008
COOLDOWN_OVERRIDE_SPREAD_FACTOR = 0.75
RECOVERY_MIN_TIME_TRIGGER_LOOPS = 30
MIN_VOLUME_RATIO_MIN = 0.3
MIN_VOLUME_RATIO_MAX = 1.5
NO_TRADE_RELAX_LOOPS = 50
NO_TRADE_DISABLE_LOOPS = 150
OVERTRADING_THRESHOLD_5MIN = 12
FILTER_STATE_LOG_INTERVAL_SECONDS = 30.0
DECISION_COUNTER_LOG_INTERVAL_SECONDS = 30.0
FLAT_RECOVERY_CONFIRM_LOOPS = 3
FLAT_RECOVERY_STALE_SECONDS = 20
FLAT_RECOVERY_SAME_BAR_RESET_SECONDS = 15
NO_ENTRY_WATCHDOG_LOOPS = 30
MIN_VOLUME_DECAY_FLOOR = 0.35
WIN_RATE_WINDOW = 20
MARKET_REGIME_VOL_LOOKBACK = 20
EXECUTION_THRESHOLD_FLOOR = 0.48
EXECUTION_THRESHOLD_CEILING = 0.85
MIN_PROFIT_THRESHOLD = 0.00005
SMART_RELAX_TRIGGER_LOOPS = 40
SMART_RELAX_MAX = 0.15
SMART_RELAX_STEP = 0.02
MAX_REPEATED_SETUPS_DEFAULT = 10
REPEATED_SETUP_LOOKBACK_SECONDS = 90
SCORE_STAGNATION_MIN_WINDOW = 5
SCORE_STAGNATION_VARIANCE_THRESHOLD = 0.00003
FORCE_EXECUTE_LOOP_THRESHOLD = 20
LOOP_FORCE_POSITION_SCALE = 0.35
AGGRESSION_MODE = True
EXIT_TIER_WEAK = 'weak_quality'
EXIT_TIER_MEDIUM = 'medium_quality'
EXIT_TIER_HIGH = 'high_quality'
EXIT_TIER_ELITE = 'elite_quality'


bybit_session = HTTP(
    testnet=True,
    api_key="YOUR_API_KEY",
    api_secret="YOUR_API_SECRET",
) if HTTP is not None else None

performance_memory: list[dict[str, str | float]] = []
confidence_memory: list[dict[str, str | float]] = []
performance_tracker: dict[str, list[float]] = {
    "HIGH_VOL": [],
    "NORMAL": [],
    "LOW_VOL": [],
}
SYMBOL_ROTATION_MEMORY_MAX = 50
SYMBOL_ROTATION_LOOKBACK = 20
SYMBOL_ROTATION_PENALTY_STEP = 0.04
SYMBOL_ROTATION_PENALTY_MAX = 0.20
XAU_PRIORITY_MULTIPLIER = 0.75
NON_XAU_DIVERSITY_BOOST = 0.03
ELIGIBLE_SYMBOL_SCORE_FLOOR = 0.04
FAST_LANE_RANK_BONUS = 0.03
SELECTION_TIEBREAK_DELTA = 0.05
FRESH_SYMBOL_LOOKBACK = 10
FRESH_SYMBOL_BOOST_MAX = 0.03
RANK_SIGNAL_WEIGHT = 2.4
RANK_PRIORITY_WEIGHT = 0.55
RANK_VOLATILITY_WEIGHT = 0.20
RANK_MIN_SIGNAL_FOR_EXECUTION = 0.06
RANK_MIN_QUALITY_FOR_EXECUTION = 0.55
SIGNAL_DECAY_LOOKBACK = 4
SIGNAL_DECAY_MAX_DROP = 0.22
SIGNAL_DECAY_RATIO_FLOOR = 0.70
selected_symbol_memory: deque[str] = deque(maxlen=SYMBOL_ROTATION_MEMORY_MAX)
log_stats = {
    "evo_updates": 0,
    "evo_last_symbol": None,
    "signals": 0,
    "trades": 0,
    "blocked": 0,
    "last_symbols": deque(maxlen=20),
    "signal_values": deque(maxlen=50),
    "last_print": time.time(),
}
try:
    EVO2_LOG_EVERY_N_LOOPS = int(float(os.getenv("EVO2_LOG_EVERY_N_LOOPS", "50")))
except Exception:
    EVO2_LOG_EVERY_N_LOOPS = 50
_evo2_last_apply_signature: dict[str, tuple[float, float, float, float, float, float]] = {}
_evo2_last_apply_loop: dict[str, int] = {}


def _evo2_enabled() -> bool:
    return _env_bool("EVO_ENGINE_V2_ENABLED", "true")


def _evo2_state_path() -> str:
    return str(os.getenv("EVO_ENGINE_V2_STATE_PATH", "results/evo_engine_v2_state.json")).strip()


def _evo2_register_block(evo_state: dict[str, Any], symbol: str, reason: str) -> None:
    if not _evo2_enabled():
        return
    evo_register_block(evo_state, symbol, reason=reason)
    save_evo_state(_evo2_state_path(), evo_state)


def _maybe_log_evo2_apply(
    *,
    symbol: str,
    loop_count: int,
    old_signal: float,
    new_signal: float,
    old_quality: float,
    new_quality: float,
    old_exec: float,
    new_exec: float,
    old_risk: float,
    new_risk: float,
    old_cooldown: float,
    new_cooldown: float,
    old_priority: float,
    new_priority: float,
) -> None:
    signature = (
        round(new_signal, 6),
        round(new_quality, 6),
        round(new_exec, 6),
        round(new_risk, 6),
        round(new_cooldown, 6),
        round(new_priority, 6),
    )
    prev_sig = _evo2_last_apply_signature.get(symbol)
    prev_loop = _evo2_last_apply_loop.get(symbol, -999_999)
    periodic_due = (loop_count - prev_loop) >= max(1, EVO2_LOG_EVERY_N_LOOPS)
    changed = prev_sig != signature
    if changed or periodic_due:
        logging.info(
            "EVO2 APPLY | symbol=%s signal=%.4f->%.4f quality=%.4f->%.4f exec=%.4f->%.4f risk=%.4f->%.4f cooldown=%.1f->%.1f priority=%.4f->%.4f",
            symbol,
            old_signal,
            new_signal,
            old_quality,
            new_quality,
            old_exec,
            new_exec,
            old_risk,
            new_risk,
            old_cooldown,
            new_cooldown,
            old_priority,
            new_priority,
        )
        _evo2_last_apply_signature[symbol] = signature
        _evo2_last_apply_loop[symbol] = loop_count


def has_min_real_bars(bars, min_required: int = 150) -> bool:
    try:
        return bars is not None and len(bars) >= int(min_required)
    except Exception:
        return False


def get_bar_count_safe(bars) -> int:
    try:
        return len(bars) if bars is not None else 0
    except Exception:
        return 0


def mark_symbol_ineligible_for_data(
    symbol: str,
    reason: str,
    bars_count: int,
    min_required: int = 150,
) -> None:
    logging.warning(
        "SYMBOL DATA INELIGIBLE | symbol=%s reason=%s bars=%d required=%d",
        symbol, reason, int(bars_count), int(min_required),
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


MIN_SETUP_SCORE = _env_float("ENTRY_MIN_SETUP_SCORE", 1.0)


def calculate_position_size(
    balance: float,
    entry_price: float,
    stop_loss_price: float,
    signal_score: float,
) -> float:
    try:
        risk_pct = _safe_float(os.getenv("RISK_PER_TRADE", str(RISK_PER_TRADE)), RISK_PER_TRADE)

        sl_distance = abs(float(entry_price) - float(stop_loss_price))
        if sl_distance <= 0:
            return 0.01

        risk_amount = float(balance) * float(risk_pct)
        raw_position_size = risk_amount / sl_distance

        use_fixed_fx_micro = _env_bool("USE_FIXED_FX_MICRO_SIZING", "true")
        if use_fixed_fx_micro:
            position_size = _fx_micro_lot_for_balance(float(balance))
        else:
            position_size = raw_position_size

        min_lot = _safe_float(os.getenv("MIN_POSITION_SIZE", "0.01"), 0.01)
        max_position_lot = _safe_float(os.getenv("MAX_POSITION_SIZE", "0.20"), 0.20)

        position_size = _clamp(position_size, min_lot, max_position_lot)
        return round(float(position_size), 2)
    except Exception as e:
        logging.error("POSITION SIZE ERROR | %s", str(e))
        return 0.01


def _env_bool(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


EVO_V24_CONFIG = load_evo_v24_config()


def _fx_micro_lot_for_balance(balance: float) -> float:
    try:
        base = 0.01
        scale = float(balance) / 100.0
        lot = base * scale
        lot = max(0.01, min(lot, 0.20))
        return round(float(lot), 2)
    except Exception:
        return 0.01


def _round_down_to_step(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.floor(float(value) / float(step)) * float(step)


EXIT_ENGINE_V3 = _env_bool("EXIT_ENGINE_V3", "false")


def _safe_elapsed_seconds(entry_time, loop_now) -> float:
    try:
        if entry_time is None or loop_now is None:
            return 0.0
        elapsed = (loop_now - entry_time).total_seconds()
        if elapsed < 0:
            logging.warning(
                "ENTRY TIME FUTURE FIX | entry_time=%s loop_now=%s raw_elapsed=%.2f",
                str(entry_time),
                str(loop_now),
                float(elapsed),
            )
            return 0.0
        return float(elapsed)
    except Exception:
        return 0.0


def _is_fx_symbol(symbol: str) -> bool:
    s = str(symbol or "").upper()
    return len(s) == 6 and s.isalpha()


def _symbol_candidates(symbol: str) -> list[str]:
    s = str(symbol or "").strip()
    if not s:
        return []
    raw = s
    upper = s.upper()
    suffixes = ["", ".r", ".m", "m", ".pro", ".ecn", ".i", "_i", "-i"]
    out: list[str] = []
    for base in (raw, upper):
        for suf in suffixes:
            cand = f"{base}{suf}"
            if cand not in out:
                out.append(cand)
    return out


def _normalize_broker_symbol_name(name: str) -> str:
    n = str(name or "").upper().strip()
    return re.sub(r"[^A-Z]", "", n)


def _resolve_mt5_symbol(symbol: str) -> str | None:
    try:
        if symbol in _SYMBOL_CACHE:
            return _SYMBOL_CACHE[symbol]

        # =========================================
        # MT5 SYMBOL RESOLVE (SAFE)
        # =========================================
        if mt5 is not None:
            try:
                symbols = mt5.symbols_get()
            except Exception as e:
                logging.warning("MT5 SAFE SKIP | symbol resolve failed | %s", str(e))
                return None
        else:
            logging.info("MT5 SKIP | paper/discovery mode")
            return None
        if not symbols:
            logging.error("NO SYMBOLS FROM MT5")
            return None

        names = [s.name for s in symbols]

        if symbol in names:
            _SYMBOL_CACHE[symbol] = symbol
            return symbol

        for n in names:
            if n.startswith(symbol):
                _SYMBOL_CACHE[symbol] = n
                return n

        for n in names:
            if symbol.upper() in n.upper():
                _SYMBOL_CACHE[symbol] = n
                return n

        logging.warning("SYMBOL NOT FOUND | %s", symbol)
        return None
    except Exception as e:
        logging.error("SYMBOL RESOLVE ERROR | %s", str(e))
        return None


def _ensure_mt5_symbol(symbol: str) -> str | None:
    try:
        if mt5 is None:
            logging.info("MT5 SKIP | paper/discovery mode")
            return None
        resolved = _resolve_mt5_symbol(symbol)

        if resolved is None:
            if mt5.symbol_select(symbol, True):
                return symbol
            return None

        if not mt5.symbol_select(resolved, True):
            if mt5.symbol_select(resolved, True):
                return resolved
            return None

        logging.info("SYMBOL READY | %s -> %s", symbol, resolved)
        return resolved
    except Exception as e:
        logging.error("SYMBOL ERROR | %s", str(e))
        return None


def _get_supported_filling(symbol: str):
    if mt5 is None:
        return None
    try:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_FOK

        supported = int(getattr(info, "filling_mode", 0) or 0)
        for mode in (
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_RETURN,
        ):
            try:
                if supported & int(mode):
                    return mode
            except Exception:
                continue
        return mt5.ORDER_FILLING_FOK
    except Exception:
        return mt5.ORDER_FILLING_FOK


def _legacy_mt5_ready(symbol: str) -> bool:
    try:
        if not mt5.initialize():
            logging.error("MT5 INIT FAILED")
            return False
        account = mt5.account_info()
        if account is None:
            logging.error("MT5 ACCOUNT NOT CONNECTED")
            return False
        logging.info(
            "MT5 CONNECTED | balance=%.2f equity=%.2f",
            float(account.balance),
            float(account.equity),
        )

        try:
            all_symbols = mt5.symbols_get()
            if all_symbols:
                logging.info(
                    "BROKER SYMBOL SAMPLE | %s",
                    [str(getattr(x, "name", "")) for x in all_symbols[:20]],
                )
        except Exception:
            pass

        resolved = _ensure_mt5_symbol(symbol)
        return resolved is not None
    except Exception as e:
        logging.error("LEGACY MT5 READY ERROR | %s", str(e))
        return False


def _estimate_margin_per_lot_usd(symbol: str, entry_price: float, contract_size: float, leverage: float) -> float:
    """Estimate required margin per lot with FX-aware handling."""
    symbol = str(symbol or "").upper()
    entry_price = max(float(entry_price), 1e-12)
    contract_size = max(float(contract_size), 1.0)
    leverage = max(float(leverage), 1.0)

    if _is_fx_symbol(symbol):
        base = symbol[:3]
        quote = symbol[3:]
        if quote == "USD":
            return contract_size * entry_price / leverage
        if base == "USD":
            return contract_size / leverage
        return contract_size * entry_price / leverage

    return contract_size * entry_price / leverage


def _max_lot_by_margin(
    *,
    symbol: str,
    equity_usd: float,
    entry_price: float,
    leverage: float,
    contract_size: float,
    min_lot: float,
    lot_step: float,
    max_lot: float,
    margin_buffer: float = 0.85,
) -> float:
    equity_usd = max(float(equity_usd), 0.0)
    usable_equity = equity_usd * float(margin_buffer)
    margin_per_lot = _estimate_margin_per_lot_usd(symbol, entry_price, contract_size, leverage)

    if margin_per_lot <= 0:
        return 0.0

    raw_max_lot = usable_equity / margin_per_lot
    if raw_max_lot <= 0:
        return 0.0

    if lot_step > 0:
        raw_max_lot = math.floor(raw_max_lot / lot_step) * lot_step

    if max_lot > 0:
        raw_max_lot = min(raw_max_lot, max_lot)

    raw_max_lot = round(max(raw_max_lot, 0.0), 8)
    if raw_max_lot < min_lot:
        return 0.0
    return raw_max_lot


def _ensure_profit_engine_state(state) -> None:
    if not hasattr(state, "profit_v2"):
        state.profit_v2 = {
            "partial_taken": False,
            "be_armed": False,
            "trail_armed": False,
            "peak_pnl": 0.0,
            "peak_price": None,
            "entry_loop_index": None,
            "last_manage_action": None,
        }


def _reset_profit_engine_state(state, loop_index: int | None = None) -> None:
    state.profit_v2 = {
        "partial_taken": False,
        "be_armed": False,
        "trail_armed": False,
        "peak_pnl": 0.0,
        "peak_price": None,
        "entry_loop_index": loop_index,
        "last_manage_action": None,
    }


def _get_live_price_from_bar(bar) -> float:
    try:
        if isinstance(bar, dict):
            return float(bar.get("close", 0.0))
        return float(bar["close"])
    except Exception:
        return 0.0


def _compute_unrealized_pnl_pct(side: str, entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if str(side).upper() == "LONG":
        return (current_price - entry_price) / entry_price
    return (entry_price - current_price) / entry_price


def manage_exit_v3(trade, current_price: float, now_ts: float):
    if trade is None:
        return None
    plan = getattr(trade, "exit_plan", None)
    if not plan:
        return None

    side = str(getattr(trade, "side", "")).upper()
    entry_price = float(getattr(trade, "entry_price", 0.0))
    qty = float(getattr(trade, "qty", 0.0))
    entry_ts = float(getattr(trade, "entry_ts", now_ts))

    if entry_price <= 0 or qty <= 0:
        return None

    tp_pct = float(plan.get("tp_pct", 0.0))
    sl_pct = float(plan.get("sl_pct", 0.0))
    partial_tp_pct = float(plan.get("partial_tp_pct", 0.0))
    partial_tp_fraction = float(plan.get("partial_tp_fraction", 0.5))
    break_even_after_partial = bool(plan.get("break_even_after_partial", True))
    trailing_activation_pct = float(plan.get("trailing_activation_pct", 0.0))
    trailing_offset_pct = float(plan.get("trailing_offset_pct", 0.0))
    max_hold_seconds = float(plan.get("max_hold_seconds", 0.0))

    partial_taken = bool(plan.get("partial_taken", False))
    be_armed = bool(plan.get("be_armed", False))
    trail_armed = bool(plan.get("trail_armed", False))
    plan["partial_taken"] = partial_taken
    plan["be_armed"] = be_armed
    plan["trail_armed"] = trail_armed

    highest_price = float(plan.get("highest_price", entry_price))
    lowest_price = float(plan.get("lowest_price", entry_price))

    if side == "LONG":
        highest_price = max(highest_price, current_price)
        plan["highest_price"] = highest_price

        tp_price = entry_price * (1.0 + tp_pct)
        sl_price = entry_price * (1.0 - sl_pct)
        partial_tp_price = entry_price * (1.0 + partial_tp_pct)
        trailing_trigger = entry_price * (1.0 + trailing_activation_pct)

        if max_hold_seconds > 0 and (now_ts - entry_ts) >= max_hold_seconds:
            return {"action": "close_full", "reason": "time_exit"}

        if (
            not plan.get("partial_taken", False)
            and partial_tp_pct > 0
            and current_price >= partial_tp_price
        ):
            plan["partial_taken"] = True
            if break_even_after_partial and current_price > entry_price:
                plan["be_armed"] = True
            return {
                "action": "close_partial",
                "fraction": partial_tp_fraction,
                "reason": "partial_tp",
            }

        if current_price >= tp_price:
            return {"action": "close_full", "reason": "hard_tp"}

        effective_sl = sl_price
        if be_armed:
            effective_sl = max(effective_sl, entry_price)

        if current_price >= trailing_trigger:
            plan["trail_armed"] = True

        if plan.get("trail_armed", False):
            trailing_sl = highest_price * (1.0 - trailing_offset_pct)
            effective_sl = max(effective_sl, trailing_sl)

        if current_price <= effective_sl:
            return {"action": "close_full", "reason": "stop_or_trail"}

    elif side == "SHORT":
        lowest_price = min(lowest_price, current_price)
        plan["lowest_price"] = lowest_price

        tp_price = entry_price * (1.0 - tp_pct)
        sl_price = entry_price * (1.0 + sl_pct)
        partial_tp_price = entry_price * (1.0 - partial_tp_pct)
        trailing_trigger = entry_price * (1.0 - trailing_activation_pct)

        if max_hold_seconds > 0 and (now_ts - entry_ts) >= max_hold_seconds:
            return {"action": "close_full", "reason": "time_exit"}

        if (
            not plan.get("partial_taken", False)
            and partial_tp_pct > 0
            and current_price <= partial_tp_price
        ):
            plan["partial_taken"] = True
            if break_even_after_partial and current_price < entry_price:
                plan["be_armed"] = True
            return {
                "action": "close_partial",
                "fraction": partial_tp_fraction,
                "reason": "partial_tp",
            }

        if current_price <= tp_price:
            return {"action": "close_full", "reason": "hard_tp"}

        effective_sl = sl_price
        if be_armed:
            effective_sl = min(effective_sl, entry_price)

        if current_price <= trailing_trigger:
            plan["trail_armed"] = True

        if plan.get("trail_armed", False):
            trailing_sl = lowest_price * (1.0 + trailing_offset_pct)
            effective_sl = min(effective_sl, trailing_sl)

        if current_price >= effective_sl:
            return {"action": "close_full", "reason": "stop_or_trail"}

    return None


def build_exit_plan_v3(signal_score: float, volatility: float) -> dict:
    tp_pct = float(os.getenv("TP_PCT", "0.0012"))
    sl_pct = float(os.getenv("SL_PCT", "0.0008"))
    partial_tp_pct = float(os.getenv("PARTIAL_TP_PCT", "0.0006"))
    partial_tp_fraction = float(os.getenv("PARTIAL_TP_FRACTION", "0.50"))
    trailing_activation_pct = float(os.getenv("TRAILING_ACTIVATION_PCT", "0.0009"))
    trailing_offset_pct = float(os.getenv("TRAILING_OFFSET_PCT", "0.00035"))
    max_hold_seconds = float(os.getenv("MAX_HOLD_SECONDS", "180"))
    break_even_after_partial = str(
        os.getenv("BREAK_EVEN_AFTER_PARTIAL", "true")
    ).lower() in ("1", "true", "yes", "on")

    vol_factor = 1.0
    if volatility > 0.00008:
        vol_factor = 1.15
    elif volatility < 0.00004:
        vol_factor = 0.90

    if signal_score > 1.0:
        tp_pct *= 1.20
        trailing_activation_pct *= 1.10
    elif signal_score < 0.75:
        tp_pct *= 0.90
        max_hold_seconds *= 0.85

    return {
        "tp_pct": tp_pct * vol_factor,
        "sl_pct": sl_pct * vol_factor,
        "partial_tp_pct": partial_tp_pct * vol_factor,
        "partial_tp_fraction": partial_tp_fraction,
        "break_even_after_partial": break_even_after_partial,
        "trailing_activation_pct": trailing_activation_pct * vol_factor,
        "trailing_offset_pct": trailing_offset_pct * vol_factor,
        "max_hold_seconds": max_hold_seconds,
        "partial_taken": False,
        "be_armed": False,
        "trail_armed": False,
    }


def manage_profit_engine_v2(
    *,
    state,
    symbol: str,
    active_trade,
    latest_bar,
    loop_index: int,
    signal_score: float = 0.0,
    entry_quality: float = 0.0,
):
    result = {
        "action": "hold",
        "reason": "none",
        "close_fraction": 0.0,
        "move_stop_to_be": False,
        "trail_stop_price": None,
        "pnl_pct": 0.0,
    }

    if not _env_bool("PROFIT_ENGINE_V2_ENABLED", "true"):
        return result

    if not active_trade:
        return result

    _ensure_profit_engine_state(state)

    entry_price = _safe_float(getattr(active_trade, "entry_price", 0.0), 0.0)
    side = str(getattr(active_trade, "side", "")).upper()
    current_price = _get_live_price_from_bar(latest_bar)
    pnl_pct = _compute_unrealized_pnl_pct(side, entry_price, current_price)
    result["pnl_pct"] = pnl_pct

    pe = state.profit_v2
    pe["peak_pnl"] = max(float(pe.get("peak_pnl", 0.0)), pnl_pct)
    if pe.get("peak_price") is None:
        pe["peak_price"] = current_price
    else:
        if side == "LONG":
            pe["peak_price"] = max(_safe_float(pe["peak_price"], current_price), current_price)
        elif side == "SHORT":
            pe["peak_price"] = min(_safe_float(pe["peak_price"], current_price), current_price)

    # 🔥 SAFE INPUTS
    try:
        signal_score = float(signal_score or 0.0)
    except Exception:
        signal_score = 0.0

    try:
        entry_quality = float(entry_quality or 0.0)
    except Exception:
        entry_quality = 0.0

    # =========================================
    # 🔥 PROFIT ENGINE V3 FINAL
    # =========================================

    strong_setup = signal_score >= 0.85 and entry_quality >= 0.85
    medium_setup = signal_score >= 0.65
    if strong_setup:
        tp_target = 0.0030
        trailing_arm_pct = 0.0015
        trailing_distance_pct = 0.0008
        partial_tp_pct = 0.0010
        runner_mode = True
    elif medium_setup:
        tp_target = 0.0020
        trailing_arm_pct = 0.0010
        trailing_distance_pct = 0.0006
        partial_tp_pct = 0.0008
        runner_mode = False
    else:
        tp_target = 0.0008
        trailing_arm_pct = 0.0004
        trailing_distance_pct = 0.0003
        partial_tp_pct = 0.0006   # 🔥 FIX 1
        runner_mode = False

    partial_close_fraction = _safe_float(os.getenv("PARTIAL_CLOSE_FRACTION", "0.50"), 0.50)
    break_even_arm_pct = tp_target * 0.4
    max_bars_in_trade = int(_safe_float(os.getenv("MAX_BARS_IN_TRADE", "45"), 45))
    min_profit_for_time_exit = _safe_float(os.getenv("MIN_PROFIT_FOR_TIME_EXIT", "-0.0005"), -0.0005)
    hard_stop_loss_pct = _safe_float(os.getenv("HARD_STOP_LOSS_PCT", "0.0025"), 0.0025)
    peak_giveback_pct = _safe_float(os.getenv("PEAK_GIVEBACK_PCT", "0.0012"), 0.0012)
    logging.info(
        "PROFIT ENGINE V2 | symbol=%s signal=%.4f quality=%.4f strong=%s medium=%s tp_target=%.5f partial_tp=%.5f be_arm=%.5f trail_arm=%.5f trail_dist=%.5f runner=%s",
        symbol,
        float(signal_score),
        float(entry_quality),
        str(bool(strong_setup)).lower(),
        str(bool(medium_setup)).lower(),
        float(tp_target),
        float(partial_tp_pct),
        float(break_even_arm_pct),
        float(trailing_arm_pct),
        float(trailing_distance_pct),
        str(bool(runner_mode)).lower(),
    )

    if pnl_pct <= -abs(hard_stop_loss_pct):
        result["action"] = "close_full"
        result["reason"] = "hard_stop_loss"
        return result

    if (not pe.get("partial_taken", False)) and pnl_pct >= partial_tp_pct:
        pe["partial_taken"] = True
        pe["last_manage_action"] = "partial_close"
        result["action"] = "partial_close"
        result["reason"] = "partial_tp"
        result["close_fraction"] = partial_close_fraction
        return result

    if (not pe.get("be_armed", False)) and pnl_pct >= break_even_arm_pct:
        pe["be_armed"] = True
        pe["last_manage_action"] = "move_stop_to_be"
        result["move_stop_to_be"] = True
        result["reason"] = "break_even_arm"

    if runner_mode and pe.get("partial_taken", False) and pnl_pct >= tp_target:
        # 🔥 FIX 3: MIN HOLD
        entry_loop_index = pe.get("entry_loop_index")
        if entry_loop_index is not None:
            bars_held = max(0, int(loop_index) - int(entry_loop_index))
            if bars_held < 3:
                return result

        result["action"] = "close_full"
        result["reason"] = "runner_tp_hit"
        return result

    if (not runner_mode) and pnl_pct >= tp_target:
        result["action"] = "close_full"
        result["reason"] = "tp_target_hit"
        return result

    # 🔥 FIX 2: TRAILING ONLY AFTER BE
    if (not pe.get("trail_armed", False)) and pe.get("be_armed", False) and pnl_pct >= trailing_arm_pct:
        pe["trail_armed"] = True

    if pe.get("trail_armed", False):
        peak_price = _safe_float(pe.get("peak_price"), current_price)
        if side == "LONG":
            result["trail_stop_price"] = peak_price * (1.0 - trailing_distance_pct)
            if current_price <= result["trail_stop_price"]:
                result["action"] = "close_full"
                result["reason"] = "trailing_stop_hit"
                return result
        elif side == "SHORT":
            result["trail_stop_price"] = peak_price * (1.0 + trailing_distance_pct)
            if current_price >= result["trail_stop_price"]:
                result["action"] = "close_full"
                result["reason"] = "trailing_stop_hit"
                return result

    peak_pnl = _safe_float(pe.get("peak_pnl", 0.0), 0.0)
    if peak_pnl > 0 and (peak_pnl - pnl_pct) >= peak_giveback_pct:
        result["action"] = "close_full"
        result["reason"] = "peak_giveback_exit"
        return result

    entry_loop_index = pe.get("entry_loop_index")
    if entry_loop_index is not None:
        bars_held = max(0, int(loop_index) - int(entry_loop_index))
        if bars_held >= max_bars_in_trade and pnl_pct <= min_profit_for_time_exit:
            result["action"] = "close_full"
            result["reason"] = "time_exit"
            return result

    return result


def ensure_min_bars(bars, min_required: int = 100):
    try:
        length = len(bars)
    except Exception:
        return bars

    if length >= min_required:
        return bars

    logging.warning(
        "BAR FIX APPLIED | insufficient bars detected | current=%d required=%d",
        length,
        min_required,
    )

    if hasattr(bars, "tail"):
        return bars.tail(min_required)

    return bars[-min_required:]


def get_max_fill_risk(symbol: str) -> float:
    symbol = str(symbol).upper()
    if symbol == "XAUUSD":
        return float(os.getenv("MAX_FILL_RISK_SCORE_XAU", "60000"))
    return float(os.getenv("MAX_FILL_RISK_SCORE", "25000"))


def execute_bybit_order(side: str, qty: float, symbol: str = "BTCUSDT"):
    if bybit_session is None:
        logging.error("BYBIT ORDER FAILED | error=pybit_unavailable")
        return None
    try:
        response = bybit_session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy" if side == "LONG" else "Sell",
            orderType="Market",
            qty=str(qty),
        )
        logging.info("BYBIT ORDER SUCCESS | side=%s qty=%.6f", side, qty)
        return response
    except Exception as e:
        logging.error("BYBIT ORDER FAILED | error=%s", str(e))
        return None


class WindowsSafeFormatter(logging.Formatter):
    _REPLACEMENTS = str.maketrans({
        '→': '-',
        '—': '-',
        '–': '-',
        '•': '-',
        '…': '...',
        '✓': 'OK',
        '✗': 'X',
    })

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        sanitized = message.translate(self._REPLACEMENTS)
        return sanitized.encode('cp1252', errors='replace').decode('cp1252')


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _safe_param(params: dict[str, Any], key: str, default: float) -> float:
    value = params.get(key, default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if not parsed > 0:
        return float(default)
    return float(parsed)


def _round_dict(values: dict[str, float], digits: int = 4) -> dict[str, float]:
    return {key: round(float(value), digits) for key, value in values.items()}


def update_smart_threshold_relaxation(state: "SymbolRuntimeState", symbol: str) -> None:
    if state.loop_count_without_trade >= SMART_RELAX_TRIGGER_LOOPS:
        state.adaptive_threshold_relax = min(
            SMART_RELAX_MAX,
            state.adaptive_threshold_relax + SMART_RELAX_STEP,
        )
        logging.info(
            "SMART RELAX INCREASE | symbol=%s relax=%.4f loops=%d",
            symbol,
            state.adaptive_threshold_relax,
            state.loop_count_without_trade,
        )
    else:
        state.adaptive_threshold_relax = max(
            0.0,
            state.adaptive_threshold_relax - SMART_RELAX_STEP,
        )


def _trade_symbol(trade: Any) -> str:
    context = getattr(trade, "context", None) or {}
    symbol = (
        context.get("symbol")
        or getattr(trade, "symbol", None)
        or getattr(trade, "instrument", None)
    )
    if not symbol:
        logging.warning("SYMBOL FALLBACK USED | trade=%s", str(trade))
        symbol = "UNKNOWN_SAFE"
    return str(symbol)


def is_non_critical_execution_error(reason: str) -> bool:
    if not reason:
        return False
    return any(x in reason for x in [
        "missing_confirmation",
        "Unsupported filling mode",
        "retcode=10030",
    ])


def resolve_contract_size(symbol: str, specs: SymbolSpecs) -> float:
    broker_contract_size = float(getattr(specs, 'contract_size', 0.0) or 0.0)
    if broker_contract_size > 0:
        return broker_contract_size
    symbol_upper = str(symbol or '').upper()
    if symbol_upper.startswith('XAU'):
        return 100.0
    return 100000.0


@dataclass(frozen=True)
class LivePositionMetrics:
    qty: float
    contract_size: float
    entry_price: float
    notional_value_usd: float
    required_margin_usd: float


def compute_live_position_metrics(
    symbol: str,
    qty: float,
    entry_price: float,
    specs: SymbolSpecs,
    leverage: float,
) -> LivePositionMetrics:
    contract_size = resolve_contract_size(symbol, specs)
    notional_value = float(qty) * float(contract_size) * float(entry_price)
    required_margin = notional_value / max(float(leverage), 1e-9)
    return LivePositionMetrics(
        qty=float(qty),
        contract_size=float(contract_size),
        entry_price=float(entry_price),
        notional_value_usd=float(notional_value),
        required_margin_usd=float(required_margin),
    )


def _validate_protective_prices(
    *,
    side: str,
    entry_price: float,
    stop_loss_price: float | None,
    take_profit_price: float | None,
) -> tuple[bool, str]:
    normalized_side = str(side or '').upper()
    if entry_price <= 0:
        return False, 'entry_price_non_positive'
    if normalized_side not in {'LONG', 'SHORT'}:
        return False, f'invalid_side:{normalized_side or "missing"}'
    if stop_loss_price is not None:
        if stop_loss_price <= 0:
            return False, 'stop_loss_non_positive'
        if normalized_side == 'LONG' and not (stop_loss_price < entry_price):
            return False, 'long_sl_must_be_below_entry'
        if normalized_side == 'SHORT' and not (stop_loss_price > entry_price):
            return False, 'short_sl_must_be_above_entry'
    if take_profit_price is not None:
        if take_profit_price <= 0:
            return False, 'take_profit_non_positive'
        if normalized_side == 'LONG' and not (take_profit_price > entry_price):
            return False, 'long_tp_must_be_above_entry'
        if normalized_side == 'SHORT' and not (take_profit_price < entry_price):
            return False, 'short_tp_must_be_below_entry'
    return True, 'ok'


def classify_execution_failure(reason: str) -> str:
    text = str(reason or '').strip().lower()
    if not text:
        return 'unknown'
    if 'no money' in text or 'insufficient' in text and 'margin' in text or 'not enough money' in text:
        return 'insufficient_margin'
    if 'invalid volume' in text or 'volume' in text and 'invalid' in text or 'lot' in text and 'invalid' in text:
        return 'invalid_volume'
    if 'tp' in text and 'invalid' in text or 'sl' in text and 'invalid' in text or 'stop' in text and 'invalid' in text:
        return 'invalid_protection'
    if 'unsupported filling mode' in text or 'invalid fill' in text or 'retcode=10030' in text:
        return 'unsupported_filling_mode'
    if 'missing_confirmation' in text or 'confirmation' in text and 'missing' in text:
        return 'confirmation_missing'
    if 'rejected' in text or 'order_send' in text or 'retcode' in text:
        return 'broker_rejected'
    return 'unknown'


def mt5_preflight_check(
    *,
    session,
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    stop_loss_price: float | None,
    take_profit_price: float | None,
    specs,
    account_equity_usd: float | None,
) -> tuple[bool, str, dict[str, float | str]]:
    _ = session
    qty_step = float(getattr(specs, 'qty_step', 0.0) or 0.0)
    min_qty = float(getattr(specs, 'min_qty', 0.0) or 0.0)
    leverage = float(os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0)
    equity = float(account_equity_usd or 0.0)

    diagnostics: dict[str, float | str] = {
        'qty': float(qty),
        'min_qty': min_qty,
        'qty_step': qty_step,
        'entry_price': float(entry_price),
        'contract_size': 0.0,
        'estimated_notional': 0.0,
        'estimated_margin': 0.0,
        'equity': equity,
        'leverage': leverage,
    }

    if qty <= 0:
        return False, 'qty_non_positive', diagnostics
    if min_qty > 0 and qty < min_qty:
        return False, 'qty_below_min_qty', diagnostics
    if qty_step > 0:
        steps = qty / qty_step
        if not np.isfinite(steps) or abs(steps - round(steps)) > 1e-6:
            return False, 'qty_not_aligned_to_step', diagnostics
    if entry_price <= 0:
        return False, 'entry_price_non_positive', diagnostics
    if stop_loss_price is not None and abs(float(entry_price) - float(stop_loss_price)) <= 0:
        return False, 'stop_distance_non_positive', diagnostics
    if take_profit_price is not None and abs(float(take_profit_price) - float(entry_price)) <= 0:
        return False, 'take_profit_distance_non_positive', diagnostics

    metrics = compute_live_position_metrics(
        symbol=symbol,
        qty=float(qty),
        entry_price=float(entry_price),
        specs=specs,
        leverage=leverage,
    )
    diagnostics['contract_size'] = float(metrics.contract_size)
    diagnostics['estimated_notional'] = float(metrics.notional_value_usd)
    diagnostics['estimated_margin'] = float(metrics.required_margin_usd)

    logging.info(
        "LIVE METRICS | symbol=%s qty=%.4f entry=%.6f contract_size=%.2f notional=%.2f margin=%.2f",
        symbol,
        float(metrics.qty),
        float(metrics.entry_price),
        float(metrics.contract_size),
        float(metrics.notional_value_usd),
        float(metrics.required_margin_usd),
    )

    if equity > 0 and metrics.required_margin_usd > equity:
        return False, 'insufficient_margin', diagnostics
    return True, 'ok', diagnostics


def compute_effective_volatility(bars: pd.DataFrame, latest_filter: Any | None = None, window: int = VOLATILITY_LOOKBACK) -> float:
    close = pd.Series(bars['close'].tail(max(window, 20)), dtype=float).dropna()
    if len(close) >= 20:
        prices = close.to_numpy(dtype=float)
        returns = np.diff(prices) / np.maximum(prices[:-1], 1e-9)
        volatility = float(np.std(returns)) if len(returns) else 0.0
        volatility = max(volatility, 0.00001)
        logging.info("VOLATILITY FIXED | value=%.8f", volatility)
        return volatility
    if len(close) >= 2:
        last_price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2])
        volatility = abs(last_price - prev_price) / max(last_price, 1e-9)
        volatility = max(float(volatility), 0.00001)
        logging.info("VOLATILITY FIXED | value=%.8f", volatility)
        return volatility
    volatility = 0.00001
    logging.info("VOLATILITY FIXED | value=%.8f", volatility)
    return volatility


def get_symbol_universe(symbols_arg: str | None) -> list[str]:
    raw = symbols_arg or ",".join(DEFAULT_SYMBOLS)
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    return symbols or list(DEFAULT_SYMBOLS)


def compute_symbol_volatility(prices: np.ndarray | list[float], *, floor: float = 0.00001) -> float:
    arr = np.asarray(prices, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size >= 20:
        returns = np.diff(arr) / np.maximum(arr[:-1], 1e-9)
        volatility = float(np.std(returns)) if returns.size else 0.0
    elif arr.size >= 2:
        volatility = abs(float(arr[-1]) - float(arr[-2])) / max(float(arr[-1]), 1e-9)
    else:
        volatility = 0.0
    return max(float(volatility), float(floor))


def detect_market_regime(volatility: float) -> str:
    if float(volatility) > 0.0003:
        return "HIGH_VOL"
    if float(volatility) < 0.00008:
        return "LOW_VOL"
    return "NORMAL"


def compute_adaptive_threshold(base: float, volatility: float, regime: str, performance_score: float) -> float:
    resolved_base = max(float(base), 1e-9)
    vol_factor = float(volatility) / resolved_base if resolved_base > 0 else 1.0

    normalized_regime = str(regime or "NORMAL").upper()
    if normalized_regime == "HIGH_VOL":
        regime_factor = 1.3
    elif normalized_regime == "LOW_VOL":
        regime_factor = 0.6
    else:
        regime_factor = 1.0

    perf_factor = 0.8 if float(performance_score) < 0.0 else 1.1
    adaptive = resolved_base * vol_factor * regime_factor * perf_factor
    lower = resolved_base * 0.4
    upper = resolved_base * 1.8
    return float(max(lower, min(adaptive, upper)))


def resolve_dynamic_symbol_volatility_threshold(
    symbol: str,
    volatility: float,
    base_threshold: float,
    history: deque[float],
) -> float:
    resolved_base_threshold = float(base_threshold)
    history_values = np.asarray([float(v) for v in history if np.isfinite(v)], dtype=float)
    if history_values.size < 10:
        return resolved_base_threshold
    rolling_median = float(np.median(history_values))
    rolling_p35 = float(np.percentile(history_values, 35))
    anchored = (base_threshold * 0.45) + (rolling_median * 0.35) + (rolling_p35 * 0.20)
    min_threshold = resolved_base_threshold * 0.55
    max_threshold = resolved_base_threshold * 1.1
    dynamic_threshold = float(np.clip(anchored, min_threshold, max_threshold))
    logging.info(
        "DYNAMIC THRESHOLD | symbol=%s vol=%.8f base=%.8f dynamic=%.8f",
        symbol,
        float(volatility),
        resolved_base_threshold,
        dynamic_threshold,
    )
    return dynamic_threshold


def symbol_is_eligible(symbol: str, volatility: float, threshold: float) -> bool:
    eligible = float(volatility) >= float(threshold)
    logging.info(
        "SYMBOL VOL CHECK | symbol=%s volatility=%.8f threshold=%.8f eligible=%s",
        symbol,
        volatility,
        threshold,
        str(eligible).lower(),
    )
    return eligible


def detect_market_regime(volatility: float) -> str:
    if float(volatility) > 0.0003:
        return "HIGH_VOL"
    if float(volatility) < 0.00008:
        return "LOW_VOL"
    return "NORMAL"


def compute_adaptive_threshold(base: float, volatility: float, regime: str, performance_score: float) -> float:
    resolved_base = max(float(base), 1e-12)
    vol_factor = float(volatility) / resolved_base if resolved_base > 0 else 1.0
    if regime == "HIGH_VOL":
        regime_factor = 1.3
    elif regime == "LOW_VOL":
        regime_factor = 0.6
    else:
        regime_factor = 1.0
    perf_factor = 0.8 if float(performance_score) < 0 else 1.1
    adaptive = resolved_base * vol_factor * regime_factor * perf_factor
    lower = resolved_base * 0.4
    upper = resolved_base * 1.8
    return max(lower, min(adaptive, upper))


def apply_auto_relax(adaptive_threshold: float, trades_last_5min: int, loops_without_trade: int) -> float:
    adjusted = float(adaptive_threshold)
    if int(trades_last_5min) == 0:
        adjusted *= 0.7
    if int(trades_last_5min) == 0 and int(loops_without_trade) > 20:
        adjusted *= 0.5
    if AGGRESSION_MODE:
        adjusted *= 0.8
    return float(adjusted)


def compute_entry_quality_score(
    *,
    signal_score: float,
    realized_volatility: float,
    spread_ratio: float,
    regime: str,
    symbol_rank_score: float,
) -> float:
    base = float(signal_score) * 0.72 + float(symbol_rank_score) * 0.28
    vol_component = float(np.clip(float(realized_volatility) * 3500.0, 0.0, 0.35))
    spread_penalty = float(np.clip(spread_ratio * 2.5, 0.0, 1.0))
    normalized_regime = str(regime or "NORMAL").upper()
    if normalized_regime == "LOW_VOL":
        regime_multiplier = 0.75
    elif normalized_regime == "HIGH_VOL":
        regime_multiplier = 1.15
    else:
        regime_multiplier = 1.0
    quality = ((base * regime_multiplier) + vol_component) - spread_penalty
    return float(np.clip(quality, 0.03, 2.0))


def compute_symbol_priority(
    *,
    signal_score: float,
    volatility: float,
    spread_ratio: float,
    current_regime: str,
    already_eligible: bool,
) -> float:
    if not already_eligible:
        return -1.0
    priority = float(signal_score) * 0.78 + float(np.clip(float(volatility) * 1200.0, 0.0, 0.22))
    spread_penalty = float(np.clip(spread_ratio * 1.5, 0.0, 0.5))
    regime = str(current_regime or "NORMAL").upper()
    if regime == "LOW_VOL":
        regime_bias = -0.05
    elif regime == "HIGH_VOL":
        regime_bias = 0.15
    else:
        regime_bias = 0.05
    priority = priority + regime_bias - spread_penalty
    return float(np.clip(priority, -0.1, 1.0))


def apply_symbol_balance(symbol: str, priority: float) -> float:
    normalized_symbol = str(symbol).upper()
    if normalized_symbol.startswith("XAU"):
        return float(priority * XAU_PRIORITY_MULTIPLIER)
    return float(priority + NON_XAU_DIVERSITY_BOOST)


def is_fx_symbol(symbol: str) -> bool:
    s = str(symbol).upper()
    if len(s) != 6:
        return False
    majors = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "CAD", "NZD"}
    return s[:3] in majors and s[3:] in majors


def _parse_session_hhmm(value: str, fallback_hour: int, fallback_minute: int = 0) -> tuple[int, int]:
    raw = str(value or "").strip()
    if ":" not in raw:
        return fallback_hour, fallback_minute
    try:
        hh, mm = raw.split(":", 1)
        hour = max(0, min(23, int(hh)))
        minute = max(0, min(59, int(mm)))
        return hour, minute
    except Exception:
        return fallback_hour, fallback_minute


def _is_in_utc_window(ts: datetime | None, start_hm: tuple[int, int], end_hm: tuple[int, int]) -> bool:
    if ts is None:
        return True
    current_minutes = int(ts.hour) * 60 + int(ts.minute)
    start_minutes = int(start_hm[0]) * 60 + int(start_hm[1])
    end_minutes = int(end_hm[0]) * 60 + int(end_hm[1])
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def entry_filter_gate(
    *,
    symbol: str,
    side: str,
    signal_score: float,
    entry_quality: float,
    realized_volatility: float,
    spread_ratio: float,
    regime: str,
    recent_bars: pd.DataFrame | None,
    timestamp: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    symbol_upper = str(symbol or "").upper()
    side_upper = str(side or "").upper()
    bars = recent_bars if isinstance(recent_bars, pd.DataFrame) else pd.DataFrame()
    now_utc = timestamp if isinstance(timestamp, datetime) else datetime.utcnow()
    strictness = max(
        0.5,
        float(_safe_env_float(f"{symbol_upper}_FILTER_STRICTNESS", _safe_env_float("DEFAULT_FILTER_STRICTNESS", 1.0))),
    )

    min_vol = _safe_env_float(f"MIN_VOL_{symbol_upper}", _safe_env_float("MIN_VOL_DEFAULT", 0.00005))
    impulse_body_ratio_min = _safe_env_float("IMPULSE_BODY_RATIO_MIN", 1.20)
    impulse_close_extreme_min = _safe_env_float("IMPULSE_CLOSE_EXTREME_MIN", 0.65)
    followthrough_min_progress = _safe_env_float("FOLLOWTHROUGH_MIN_PROGRESS", 0.00012)
    enable_session_filter = str(os.getenv("ENABLE_SESSION_FILTER", "true")).lower() in ("1", "true", "yes")

    lookback = bars.tail(20).copy() if not bars.empty else pd.DataFrame()
    last_bar = lookback.iloc[-1] if len(lookback) > 0 else None
    prev_bars = lookback.iloc[:-1] if len(lookback) > 1 else pd.DataFrame()

    last_open = float(last_bar.get("open", 0.0)) if last_bar is not None else 0.0
    last_close = float(last_bar.get("close", 0.0)) if last_bar is not None else 0.0
    last_high = float(last_bar.get("high", max(last_open, last_close))) if last_bar is not None else 0.0
    last_low = float(last_bar.get("low", min(last_open, last_close))) if last_bar is not None else 0.0
    last_body = abs(last_close - last_open)
    last_range = max(last_high - last_low, 1e-9)

    recent_bodies = (
        (prev_bars["close"].astype(float) - prev_bars["open"].astype(float)).abs().tail(8)
        if not prev_bars.empty and {"open", "close"}.issubset(prev_bars.columns)
        else pd.Series(dtype=float)
    )
    avg_body = float(recent_bodies.mean()) if len(recent_bodies) > 0 else max(last_body, 1e-9)
    impulse_ratio = float(last_body / max(avg_body, 1e-9))

    if side_upper == "LONG":
        close_extreme = float((last_close - last_low) / last_range)
    else:
        close_extreme = float((last_high - last_close) / last_range)

    continuation_bars = lookback.tail(4)
    continuation_move = 0.0
    expansion_ratio = 1.0
    if len(continuation_bars) >= 3:
        first_open = float(continuation_bars.iloc[0].get("open", last_open))
        latest_close = float(continuation_bars.iloc[-1].get("close", last_close))
        raw_move = (latest_close - first_open) / max(abs(first_open), 1e-9)
        continuation_move = raw_move if side_upper == "LONG" else -raw_move
        newer = continuation_bars.tail(2)
        older = continuation_bars.head(2)
        newer_body = (newer["close"].astype(float) - newer["open"].astype(float)).abs().mean()
        older_body = (older["close"].astype(float) - older["open"].astype(float)).abs().mean()
        expansion_ratio = float(newer_body / max(float(older_body), 1e-9))

    session_windows: list[bool] = []
    if enable_session_filter and is_fx_symbol(symbol_upper):
        london_start = _parse_session_hhmm(os.getenv("SESSION_LONDON_START", "07:00"), 7, 0)
        london_end = _parse_session_hhmm(os.getenv("SESSION_LONDON_END", "11:30"), 11, 30)
        ny_start = _parse_session_hhmm(os.getenv("SESSION_NY_START", "13:00"), 13, 0)
        ny_end = _parse_session_hhmm(os.getenv("SESSION_NY_END", "17:00"), 17, 0)
        in_london = _is_in_utc_window(now_utc, london_start, london_end)
        in_ny = _is_in_utc_window(now_utc, ny_start, ny_end)
        session_windows = [in_london, in_ny]

    diagnostics: dict[str, Any] = {
        "symbol": symbol_upper,
        "regime": str(regime or "UNKNOWN").upper(),
        "strictness": float(strictness),
        "signal_score": float(signal_score),
        "entry_quality": float(entry_quality),
        "realized_volatility": float(realized_volatility),
        "min_required_volatility": float(min_vol),
        "spread_ratio": float(spread_ratio),
        "impulse_body_ratio": float(impulse_ratio),
        "impulse_body_ratio_min": float(impulse_body_ratio_min * strictness),
        "close_extreme_ratio": float(close_extreme),
        "close_extreme_min": float(impulse_close_extreme_min * min(1.2, strictness)),
        "continuation_move": float(continuation_move),
        "followthrough_min_progress": float(followthrough_min_progress * strictness),
        "expansion_ratio": float(expansion_ratio),
        "timestamp_utc": now_utc.isoformat(timespec="seconds"),
    }

    if float(realized_volatility) < float(min_vol * strictness):
        return False, "low_volatility", diagnostics

    if float(impulse_ratio) < float(impulse_body_ratio_min * strictness):
        return False, "weak_impulse", diagnostics

    if float(close_extreme) < float(impulse_close_extreme_min * min(1.2, strictness)):
        return False, "poor_close_location", diagnostics

    if float(continuation_move) < float(followthrough_min_progress * strictness) or float(expansion_ratio) < 0.9:
        return False, "poor_followthrough_context", diagnostics

    if enable_session_filter and is_fx_symbol(symbol_upper):
        if session_windows and not any(session_windows):
            diagnostics["session_windows_ok"] = False
            return False, "bad_session", diagnostics
        diagnostics["session_windows_ok"] = True

    strictness_quality_floor = min(1.2, 0.55 + max(0.0, strictness - 1.0) * 0.25)
    blended_quality = float(signal_score) * 0.5 + float(entry_quality) * 0.5
    diagnostics["blended_quality"] = float(blended_quality)
    diagnostics["strictness_quality_floor"] = float(strictness_quality_floor)
    if float(blended_quality) < float(strictness_quality_floor):
        return False, "symbol_strictness_block", diagnostics

    return True, "ok", diagnostics


def compute_rotation_penalty(symbol: str) -> tuple[float, int]:
    if not selected_symbol_memory:
        return 0.0, 0
    normalized_symbol = str(symbol).upper()
    recent_symbols = list(selected_symbol_memory)[-SYMBOL_ROTATION_LOOKBACK:]
    repeat_count = sum(1 for item in recent_symbols if str(item).upper() == normalized_symbol)
    if repeat_count <= 2:
        penalty = 0.0
    elif repeat_count == 3:
        penalty = 0.04
    elif repeat_count == 4:
        penalty = 0.08
    else:
        penalty = 0.10
    penalty = min(float(penalty), 0.10)
    return float(penalty), int(repeat_count)


def compute_smart_entry_timing(
    signal_score: float,
    volatility: float,
    regime: str,
    consecutive_losses: int,
    recent_execution_rate: float,
) -> dict[str, float]:
    min_time = 22.0
    cooldown = 65.0
    if signal_score >= 1.0 and volatility >= 0.00012:
        min_time -= 8.0
        cooldown -= 12.0
    elif signal_score >= 0.85 and volatility >= 0.00010:
        min_time -= 5.0
        cooldown -= 8.0
    if signal_score <= 0.25 and volatility <= 0.00008:
        min_time += 9.0
        cooldown += 20.0
    normalized_regime = str(regime or "NORMAL").upper()
    if normalized_regime == "HIGH_VOL":
        min_time -= 3.0
    elif normalized_regime == "LOW_VOL":
        min_time += 4.0
        cooldown += 6.0
    loss_count = max(0, int(consecutive_losses))
    if loss_count > 0:
        cooldown += min(55.0, float(loss_count) * 15.0)
    if recent_execution_rate < 0.25 and loss_count == 0:
        cooldown -= 8.0
    min_time = _clamp(min_time, LEVEL7_MIN_TIME_FLOOR_SECONDS, LEVEL7_MIN_TIME_CEILING_SECONDS)
    cooldown = _clamp(cooldown, LEVEL7_COOLDOWN_FLOOR_SECONDS, LEVEL7_COOLDOWN_CEILING_SECONDS)
    return {
        "min_time_seconds": float(min_time),
        "cooldown_seconds": float(cooldown),
    }


def can_override_cooldown(
    signal_score: float,
    quality_score: float,
    spread_ratio: float,
    recent_losses: int,
) -> bool:
    return (
        float(signal_score) >= 1.0
        and float(quality_score) >= 1.1
        and float(spread_ratio) <= COOLDOWN_OVERRIDE_SPREAD_FACTOR
        and int(recent_losses) == 0
    )


def is_level7_fast_lane(
    *,
    quality_score: float,
    signal_score: float,
    spread_ratio: float,
    allowed_spread_ratio: float,
    realized_volatility: float,
) -> bool:
    if not np.isfinite(float(quality_score)):
        return False
    if not np.isfinite(float(signal_score)):
        return False
    if float(allowed_spread_ratio) <= 0:
        return False
    return (
        float(quality_score) >= LEVEL7_FAST_QUALITY_MIN
        and float(signal_score) >= LEVEL7_FAST_SIGNAL_MIN
        and float(spread_ratio) <= float(allowed_spread_ratio) * LEVEL7_MAX_SPREAD_USAGE
        and float(realized_volatility) >= LEVEL7_FAST_VOL_MIN
    )


def is_level7_ultra_fast_lane(
    *,
    quality_score: float,
    signal_score: float,
    spread_ratio: float,
    allowed_spread_ratio: float,
    realized_volatility: float,
) -> bool:
    if float(allowed_spread_ratio) <= 0:
        return False
    return (
        float(quality_score) >= LEVEL7_ULTRA_QUALITY_MIN
        and float(signal_score) >= LEVEL7_ULTRA_SIGNAL_MIN
        and float(spread_ratio) <= float(allowed_spread_ratio) * LEVEL7_ULTRA_MAX_SPREAD_USAGE
        and float(realized_volatility) >= LEVEL7_ULTRA_VOL_MIN
    )


def compute_level7_timing(
    *,
    base_min_time: int,
    base_cooldown: int,
    fast_lane: bool,
    ultra_fast_lane: bool,
    consecutive_losses: int,
) -> tuple[int, int]:
    min_time = int(base_min_time)
    cooldown = int(base_cooldown)

    if fast_lane:
        min_time = int(round(min_time * 0.65))
        cooldown = int(round(cooldown * 0.80))

    if ultra_fast_lane:
        min_time = int(round(min_time * 0.70))
        cooldown = int(round(cooldown * 0.85))

    if int(consecutive_losses) > 0:
        loss_penalty = min(45, int(consecutive_losses) * 10)
        cooldown += loss_penalty

    min_time = int(np.clip(min_time, LEVEL7_MIN_TIME_FLOOR_SECONDS, LEVEL7_MIN_TIME_CEILING_SECONDS))
    cooldown = int(np.clip(cooldown, LEVEL7_COOLDOWN_FLOOR_SECONDS, LEVEL7_COOLDOWN_CEILING_SECONDS))
    return min_time, cooldown


def get_dynamic_weights(regime: str) -> dict[str, float]:
    regime_norm = str(regime).upper()
    if regime_norm == "HIGH_VOL":
        weights = {"signal": 0.55, "vol": 0.20, "priority": 0.18, "context": 0.07}
    elif regime_norm == "LOW_VOL":
        weights = {"signal": 0.72, "vol": 0.08, "priority": 0.14, "context": 0.06}
    else:
        weights = {"signal": 0.62, "vol": 0.15, "priority": 0.17, "context": 0.06}
    total = max(sum(float(v) for v in weights.values()), 1e-9)
    return {k: float(v) / total for k, v in weights.items()}


def soft_cap_score(x: float) -> float:
    value = float(x)
    return value / (1.0 + abs(value))


def compute_context_score(state: Any, positions: list[Any] | None = None) -> float:
    score = 0.0
    if bool(getattr(state, "recent_position_closed", False)):
        score += 0.08
    position_rows = list(positions or [])
    if any(bool(getattr(p, "runner_active", False)) for p in position_rows):
        score += 0.12
    if any(bool(getattr(p, "closed_partial", False)) for p in position_rows):
        score += 0.08
    return float(min(score, 0.25))


def compute_portfolio_pressure(symbol: str, positions: list[Any] | None = None) -> float:
    position_rows = list(positions or [])
    if not position_rows:
        return 0.0
    symbol_norm = str(symbol).upper()
    total_positions = max(1, len(position_rows))
    same_symbol_positions = sum(1 for p in position_rows if str(getattr(p, "symbol", "")).upper() == symbol_norm)
    pressure = float(same_symbol_positions) / float(total_positions)
    return float(min(pressure * 0.20, 0.20))


def get_dynamic_signal_floor(recent_signal_scores: list[float] | Any, fallback_floor: float = 0.05) -> float:
    values: list[float] = []
    for item in list(recent_signal_scores or []):
        try:
            value = float(item)
        except Exception:
            continue
        if np.isfinite(value):
            values.append(value)
    if len(values) < 10:
        return float(fallback_floor)
    percentile_floor = float(np.percentile(np.asarray(values, dtype=float), 30))
    return float(max(float(fallback_floor), percentile_floor))


def compute_final_execution_decision(
    *,
    signal_score: float,
    signal_executable: bool,
    execution_threshold: float,
    dynamic_signal_floor: float,
    quality_score: float,
    quality_floor: float,
    signal_decay_blocked: bool,
    bypass_dynamic_floor: bool = False,
) -> tuple[bool, str]:

    logging.info(
        "CENTRAL EXEC CHECK | score=%.4f exec_threshold=%.4f dyn_floor=%.4f quality=%.4f quality_floor=%.4f bypass=%s",
        float(signal_score),
        float(execution_threshold),
        float(dynamic_signal_floor),
        float(quality_score),
        float(quality_floor),
        str(bool(bypass_dynamic_floor)).lower(),
    )

    if not signal_executable:
        return False, "not_executable"

    if signal_decay_blocked:
        return False, "signal_decay_blocked"

    if float(signal_score) < float(execution_threshold):
        return False, "below_execution_threshold"

    if not bypass_dynamic_floor and float(signal_score) < float(dynamic_signal_floor):
        return False, "below_dynamic_floor"

    if float(quality_score) < float(quality_floor):
        return False, "below_quality_floor"

    return True, "ok"


def compute_activity_mode_adjustment(
    *,
    no_trade_loops: int,
    base_execution_threshold: float,
    base_dynamic_floor: float,
    base_quality_floor: float,
    regime: str,
) -> tuple[bool, float, float, float, float, str]:
    loops = int(max(0, int(no_trade_loops)))
    exec_th = float(base_execution_threshold)
    dyn_floor = float(base_dynamic_floor)
    qual_floor = float(base_quality_floor)
    size_mult = 1.0
    reason = "inactive"

    if loops < 15:
        return False, exec_th, dyn_floor, qual_floor, size_mult, reason

    if loops < 30:
        exec_th *= 0.92
        dyn_floor *= 0.92
        qual_floor = min(qual_floor, 0.48)
        size_mult = 0.75
        reason = "stage1"
    elif loops < 60:
        exec_th *= 0.85
        dyn_floor *= 0.85
        qual_floor = min(qual_floor, 0.44)
        size_mult = 0.60
        reason = "stage2"
    else:
        exec_th *= 0.78
        dyn_floor *= 0.80
        qual_floor = min(qual_floor, 0.40)
        size_mult = 0.45
        reason = "stage3"

    if str(regime).upper() == "LOW_VOL":
        exec_th *= 0.92
        dyn_floor *= 0.90
        reason += "_low_vol"

    exec_th = max(0.08, exec_th)
    dyn_floor = max(0.05, dyn_floor)
    qual_floor = max(0.35, qual_floor)

    return True, exec_th, dyn_floor, qual_floor, size_mult, reason


def evaluate_activity_trade_filters(
    *,
    quality_score: float,
    quality_floor: float,
    signal_score: float,
    predicted_pnl: float,
    min_expected_profit: float,
    relative_volume: float,
    spread: float,
    adaptive_spread: float,
) -> tuple[bool, str]:
    if float(quality_score) < float(quality_floor):
        return False, "quality_below_floor"
    if float(signal_score) < 0.08:
        return False, "signal_below_minimum"
    if float(predicted_pnl) < float(min_expected_profit) and float(signal_score) < 0.20:
        return False, "weak_signal_negative_edge"
    if float(relative_volume) <= 0.0:
        return False, "non_positive_relative_volume"
    if float(spread) > float(adaptive_spread) * 1.25:
        return False, "spread_above_activity_guard"
    return True, "ok"


def record_trade_performance(regime: str, pnl: float) -> None:
    regime_norm = str(regime or "NORMAL").upper()
    if regime_norm not in performance_tracker:
        performance_tracker[regime_norm] = []
    performance_tracker[regime_norm].append(float(pnl))
    if len(performance_tracker[regime_norm]) > 200:
        performance_tracker[regime_norm] = performance_tracker[regime_norm][-200:]


def optimize_weights_for_regime(regime: str) -> dict[str, float] | None:
    regime_norm = str(regime or "NORMAL").upper()
    pnl_samples = list(performance_tracker.get(regime_norm, []))[-50:]
    if len(pnl_samples) < 20:
        return None
    wins = sum(1 for pnl in pnl_samples if float(pnl) > 0.0)
    winrate = float(wins) / float(len(pnl_samples))
    avg_pnl = float(np.mean(np.asarray(pnl_samples, dtype=float)))

    weights = get_dynamic_weights(regime_norm)
    if winrate < 0.45 or avg_pnl < 0.0:
        weights["signal"] += 0.05
        weights["vol"] = max(0.01, weights["vol"] - 0.05)
    elif winrate > 0.60 and avg_pnl > 0.0:
        weights["signal"] = max(0.40, weights["signal"] - 0.04)
        weights["vol"] += 0.04

    total = max(sum(float(v) for v in weights.values()), 1e-9)
    return {k: float(v) / total for k, v in weights.items()}


def rank_symbols(
    candidates: list[dict[str, float]],
    state: Any = None,
    positions: list[Any] | None = None,
    evo_state: dict[str, Any] | None = None,
) -> list[dict[str, float]]:
    ranked: list[dict[str, float]] = []
    recent_committed = [str(item).upper() for item in list(selected_symbol_memory)[-FRESH_SYMBOL_LOOKBACK:]]
    context_score = compute_context_score(state, positions=positions)

    for c in candidates:
        signal_score = float(c.get("signal_score", 0.0))
        if signal_score < 0.02:
            continue
        candidate_eligible = bool(c.get("eligible", True)) and signal_score >= 0.02
        regime = str(c.get("regime", "NORMAL")).upper()
        signal_strength = float(c.get("signal_strength", signal_score))
        if signal_strength < 0.05:
            signal_strength *= 1.5

        volatility = float(c.get("volatility", 0.0))
        normalized_volatility = float(np.log1p(max(0.0, volatility) * 1500.0) / np.log(1.0 + 1500.0))
        normalized_volatility = float(np.clip(normalized_volatility, 0.0, 0.60))

        weights = get_dynamic_weights(regime)
        optimized_weights = optimize_weights_for_regime(regime)
        if optimized_weights is not None:
            weights = optimized_weights

        symbol_priority = compute_symbol_priority(
            signal_score=signal_score,
            volatility=volatility,
            spread_ratio=float(c.get("spread_ratio", 0.0)),
            current_regime=regime,
            already_eligible=candidate_eligible,
        )
        v24_rank_score, v24_rank_breakdown = compute_symbol_rank_v24(
            {
                **c,
                "execution_quality": float(c.get("execution_quality", c.get("fill_quality", EVO_V24_CONFIG.missing_execution_rate_fallback))),
                "regime_alignment": float(c.get("regime_alignment", 0.5)),
                "volatility_suitability": float(c.get("volatility_suitability", 0.5)),
                "symbol_quality": float(c.get("symbol_quality", EVO_V24_CONFIG.missing_symbol_quality_fallback)),
            },
            EVO_V24_CONFIG,
        )
        if EVO_V24_CONFIG.symbol_quality_filter_enabled and v24_rank_breakdown["symbol_quality"] < EVO_V24_CONFIG.min_symbol_quality_score:
            logging.info(
                "SYMBOL FILTERED | symbol=%s reason=symbol_quality score=%.4f min=%.4f",
                str(c.get("symbol", "")),
                float(v24_rank_breakdown["symbol_quality"]),
                float(EVO_V24_CONFIG.min_symbol_quality_score),
            )
            continue
        priority_bonus = 0.0
        if _evo2_enabled() and evo_state is not None:
            evo_symbol_state = evo_get_symbol_state(evo_state, str(c.get("symbol", "")))
            max_bonus = abs(_safe_float(os.getenv("EVO2_MAX_PRIORITY_BONUS", "0.08"), 0.08))
            priority_bonus = float(
                np.clip(
                    _safe_float(evo_symbol_state.get("adaptive_priority_bonus", 0.0), 0.0),
                    -max_bonus,
                    max_bonus,
                )
            )
            logging.info(
                "EVO2 PRIORITY | symbol=%s base=%.4f bonus=%.4f final=%.4f",
                str(c.get("symbol", "")),
                float(symbol_priority),
                float(priority_bonus),
                float(symbol_priority + priority_bonus),
            )
            symbol_priority += priority_bonus
        symbol_priority = apply_symbol_balance(str(c.get("symbol", "")), symbol_priority)
        rotation_penalty, repeat_count = compute_rotation_penalty(str(c.get("symbol", "")))
        rotation_penalty = min(float(rotation_penalty), 0.10)
        portfolio_penalty = compute_portfolio_pressure(str(c.get("symbol", "")), positions=positions)

        base_score = (
            signal_score * float(weights["signal"])
            + normalized_volatility * float(weights["vol"])
            + symbol_priority * float(weights["priority"])
            + context_score * float(weights["context"])
        )

        fresh_boost = 0.0
        symbol_name = str(c.get("symbol", "")).upper()
        if symbol_name and symbol_name not in recent_committed:
            fresh_boost = FRESH_SYMBOL_BOOST_MAX

        raw_score = base_score + fresh_boost - rotation_penalty - portfolio_penalty
        v24_mix = 0.30 if EVO_V24_CONFIG.symbol_rotation_enabled else 0.0
        raw_score = (raw_score * (1.0 - v24_mix)) + (v24_rank_score * v24_mix)

        quality_score = compute_entry_quality_score(
            signal_score=signal_score,
            realized_volatility=volatility,
            spread_ratio=float(c.get("spread_ratio", 0.0)),
            regime=regime,
            symbol_rank_score=raw_score,
        )
        fast_lane = quality_score >= LEVEL7_FAST_QUALITY_MIN and signal_strength >= LEVEL7_FAST_SIGNAL_MIN
        if fast_lane:
            raw_score += FAST_LANE_RANK_BONUS

        if symbol_name.startswith("XAU") and signal_score < 0.05:
            raw_score *= 0.5

        final_score = soft_cap_score(raw_score)
        if is_fx_symbol(symbol_name):
            final_score = max(final_score, 0.01)
            logging.info(
                "FINAL SCORE CLAMP | symbol=%s final=%.6f",
                symbol_name,
                final_score,
            )

        logging.info(
            "FRESH BOOST | symbol=%s boost=%.6f lookback=%d",
            symbol_name,
            fresh_boost,
            FRESH_SYMBOL_LOOKBACK,
        )
        logging.info(
            "RANK V4 | symbol=%s regime=%s signal=%.4f vol_norm=%.4f priority=%.4f context=%.4f fresh=%.4f rotation=%.4f portfolio=%.4f raw=%.6f final=%.6f",
            symbol_name,
            regime,
            signal_score,
            normalized_volatility,
            symbol_priority,
            context_score,
            fresh_boost,
            rotation_penalty,
            portfolio_penalty,
            raw_score,
            final_score,
        )
        logging.info(
            "ROTATION PENALTY SOFTENED | symbol=%s repeats=%d penalty=%.4f",
            symbol_name,
            repeat_count,
            rotation_penalty,
        )
        logging.info(
            "RANK AFTER ROTATION | symbol=%s raw=%.6f penalty=%.4f final=%.6f",
            symbol_name,
            raw_score,
            rotation_penalty,
            final_score,
        )
        logging.info(
            "RANK DEBUG | symbol=%s signal=%.4f final=%.6f",
            symbol_name,
            signal_score,
            final_score,
        )
        logging.info(
            "SYMBOL PRIORITY | symbol=%s priority=%.6f eligible=%s spread=%.6f vol=%.6f",
            str(c.get("symbol", "")),
            symbol_priority,
            str(candidate_eligible).lower(),
            float(c.get("spread_ratio", 0.0)),
            volatility,
        )
        logging.info(
            "ROTATION PENALTY | symbol=%s repeats=%d lookback=%d penalty=%.6f",
            str(c.get("symbol", "")),
            repeat_count,
            SYMBOL_ROTATION_LOOKBACK,
            rotation_penalty,
        )
        logging.info(
            "SYMBOL RANK | symbol=%s score=%.6f signal=%.6f vol=%.6f norm_vol=%.3f",
            str(c.get("symbol", "")),
            final_score,
            signal_strength,
            volatility,
            normalized_volatility,
        )
        logging.info(
            "SYMBOL RANK V24 | symbol=%s rank_score=%.6f mix=%.2f breakdown=%s",
            str(c.get("symbol", "")),
            float(v24_rank_score),
            float(v24_mix),
            json.dumps({k: round(float(v), 4) for k, v in v24_rank_breakdown.items()}, sort_keys=True),
        )
        ranked.append(
            {
                **c,
                "eligible": candidate_eligible,
                "score": float(final_score),
                "priority": float(symbol_priority),
                "quality_score": float(quality_score),
                "symbol_rank_v24": float(v24_rank_score),
                "symbol_rank_breakdown": v24_rank_breakdown,
            }
        )

    ranked.sort(key=lambda x: float(x["score"]), reverse=True)
    if len(ranked) >= 2:
        top = ranked[0]
        second = ranked[1]
        top_score = float(top.get("score", 0.0))
        second_score = float(second.get("score", 0.0))
        score_diff = abs(top_score - second_score)
        if score_diff <= SELECTION_TIEBREAK_DELTA:
            recent_symbols = [str(item).upper() for item in list(selected_symbol_memory)[-SYMBOL_ROTATION_LOOKBACK:]]
            top_symbol = str(top.get("symbol", "")).upper()
            second_symbol = str(second.get("symbol", "")).upper()
            top_recent_count = sum(1 for item in recent_symbols if item == top_symbol)
            second_recent_count = sum(1 for item in recent_symbols if item == second_symbol)
            rotation_choice = second_symbol if second_recent_count < top_recent_count else top_symbol
            logging.info(
                "SELECTION TIEBREAK | top=%s top_score=%.6f second=%s second_score=%.6f diff=%.6f threshold=%.6f",
                top_symbol,
                top_score,
                second_symbol,
                second_score,
                score_diff,
                SELECTION_TIEBREAK_DELTA,
            )
            if score_diff < 0.03:
                chosen_symbol = rotation_choice
            else:
                chosen_symbol = top_symbol
            if chosen_symbol == second_symbol:
                ranked[0], ranked[1] = ranked[1], ranked[0]
            chosen = str(ranked[0].get("symbol", "")).upper()
            logging.info(
                "ROTATION FIX | top=%.4f second=%.4f diff=%.4f chosen=%s",
                top_score,
                second_score,
                score_diff,
                chosen_symbol,
            )
            logging.info(
                "ROTATION DECISION | chosen=%s top_recent=%d second_recent=%d lookback=%d",
                chosen,
                top_recent_count,
                second_recent_count,
                SYMBOL_ROTATION_LOOKBACK,
            )
    return ranked

def scale_portfolio_risk(base_risk_per_trade_usd: float, active_symbol_count: int) -> float:
    safe_count = max(1, int(active_symbol_count))
    adjusted = float(base_risk_per_trade_usd) / float(np.sqrt(safe_count))
    logging.info(
        "PORTFOLIO RISK SCALE | active=%d adjusted=%.6f base=%.6f",
        safe_count,
        adjusted,
        float(base_risk_per_trade_usd),
    )
    return adjusted


def compute_capital_allocation_weights(symbol_states: dict[str, "SymbolRuntimeState"]) -> dict[str, float]:
    eligible_symbols = [symbol for symbol, st in symbol_states.items() if not bool(st.disabled)]
    if not eligible_symbols:
        return {}
    raw_weights = {
        symbol: max(
            0.1,
            float(symbol_states[symbol].performance.get("avg_pnl", 0.0)) + float(symbol_states[symbol].performance.get("win_rate", 0.0)),
        )
        for symbol in eligible_symbols
    }
    total = sum(raw_weights.values())
    if total <= 0:
        even_weight = 1.0 / float(len(eligible_symbols))
        return {symbol: even_weight for symbol in eligible_symbols}
    return {symbol: float(value) / float(total) for symbol, value in raw_weights.items()}


def is_market_data_ready(symbol_states: list[dict[str, Any]]) -> bool:
    return any(
        float(s.get("volatility", 0.0)) > 0 and float(s.get("signal", s.get("signal_score", 0.0))) > 0
        for s in symbol_states
    )


def is_market_ready(symbol_states: list[dict[str, Any]]) -> bool:
    return any(
        float(s.get("volatility", 0.0)) > 0.0 and float(s.get("signal", 0.0)) > 0.0
        for s in symbol_states
    )


trade_memory: deque[dict[str, Any]] = deque(maxlen=500)
last_trade_timestamp: float = 0.0
consecutive_losses: int = 0
last_symbol: str | None = None
last_direction: str | None = None
last_closed_trade_timestamp: float = 0.0


def predict_trade_success(current_features: dict[str, Any], *, top_n: int = 20) -> tuple[float, float, float]:
    if not trade_memory:
        return 0.5, 0.0, 0.5
    scored: list[tuple[float, dict[str, Any]]] = []
    current_signal = float(current_features.get("signal_score", 0.0))
    current_vol = float(current_features.get("volatility", 0.0))
    current_spread = float(current_features.get("spread", 0.0))
    current_regime = str(current_features.get("regime", "UNKNOWN"))
    for item in trade_memory:
        if current_regime != str(item.get("regime", "UNKNOWN")):
            continue
        similarity = (
            abs(current_signal - float(item.get("signal_score", 0.0)))
            + abs(current_vol - float(item.get("volatility", 0.0)))
            + abs(current_spread - float(item.get("spread", 0.0)))
        )
        scored.append((similarity, item))
    if not scored:
        for item in trade_memory:
            similarity = (
                abs(current_signal - float(item.get("signal_score", 0.0)))
                + abs(current_vol - float(item.get("volatility", 0.0)))
                + abs(current_spread - float(item.get("spread", 0.0)))
            )
            scored.append((similarity, item))
    scored.sort(key=lambda x: x[0])
    selected = [item for _, item in scored[:max(1, min(top_n, len(scored)))]]
    predicted_win_rate = float(np.mean([1.0 if bool(x.get("win", False)) else 0.0 for x in selected])) if selected else 0.5
    predicted_pnl = float(np.mean([float(x.get("result", 0.0)) for x in selected])) if selected else 0.0
    confidence_score = (predicted_win_rate * 0.6) + (predicted_pnl * 0.4)
    return predicted_win_rate, predicted_pnl, float(_clamp(confidence_score, 0.0, 1.0))


def should_apply_volume_filter(*, data_backend: str, symbol: str) -> bool:
    if str(data_backend).lower() == 'mt5':
        return False
    return True


def _safe_trend_strength_from_bars(bars: pd.DataFrame, fallback: float = 0.0) -> float:
    close = pd.Series(bars.get('close', []), dtype=float).dropna()
    if len(close) >= 2:
        last_price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2])
        if not np.isfinite(last_price) or not np.isfinite(prev_price):
            return float(max(0.0, fallback))
        delta = last_price - prev_price
        trend = float(np.sign(delta))
        return abs(trend)
    return float(max(0.0, fallback))


@dataclass
class ActiveTrade:
    instrument: str
    side: str
    entry_price: float
    entry_time: datetime
    entry_index: int
    signal_score: float
    tp_pct: float
    sl_pct: float
    trailing_activation_pct: float
    trailing_offset_pct: float
    max_hold_seconds: float
    source: str
    exit_tier: str = EXIT_TIER_MEDIUM
    volatility_factor: float = 1.0
    flat_exit_threshold_pct: float = 0.0003
    no_follow_through_min_bars: int = 2
    no_follow_through_min_seconds: float = 10.0
    no_follow_through_progress_pct: float = 0.0004
    winner_extension_profit_pct: float = 0.0
    winner_extension_seconds: float = 0.0
    base_max_hold_seconds: float = 0.0
    exit_profile: str = "partial_runner"
    break_even_armed: bool = False
    break_even_trigger_pct: float = 0.0
    fast_spike_trigger_pct: float = 0.0
    fast_spike_window_seconds: float = 20.0
    partial_tp_armed: bool = False
    partial_tp_pct: float = 0.0
    partial_tp_fraction: float = 0.0
    market_regime: str = 'UNKNOWN'
    trailing_stop_price: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    force_close_reason: str | None = None
    trailing_active: bool = False
    peak_price: float | None = None
    trough_price: float | None = None
    winner_extension_active: bool = False
    entry_volatility: float = 0.0
    entry_spread: float = 0.0
    entry_regime: str = 'UNKNOWN'
    stack_count: int = 0
    recovered_at: datetime | None = None
    recovered_qty: float | None = None


@dataclass
class SymbolRuntimeState:
    buffer: pd.DataFrame
    adaptive_filters: Any
    recent_symbol_volatility: deque[float] = field(default_factory=lambda: deque(maxlen=SYMBOL_VOLATILITY_LOOKBACK))
    active_trade: ActiveTrade | None = None
    active_position_scale: float = 1.0
    active_notional_usd: float = 0.0
    loop_count_without_trade: int = 0
    adaptive_threshold_relax: float = 0.0
    last_trade_signature: tuple[float, float, float] | None = None
    last_execution_index: int = -1
    no_trade_snapshot_for_active_trade: int = 0
    performance: dict[str, float | int] = field(default_factory=lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "win_rate": 0.0, "avg_pnl": 0.0})
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_win_pnl: float = 0.0
    gross_loss_pnl: float = 0.0
    max_equity: float = 0.0
    current_drawdown: float = 0.0
    equity_history: list[float] = field(default_factory=list)
    threshold_bias: float = 1.0
    disabled: bool = False
    blocked_patterns: list[tuple[float, float]] = field(default_factory=list)
    blocked_patterns_expires_at: dict[tuple[float, float], int] = field(default_factory=dict)
    recent_loss_patterns: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=30))
    ai_blocks: int = 0
    last_trade_closed_timestamp: float | None = None
    recent_close_timer: int = 0
    flat_confirm_loops: int = 0
    flat_since: datetime | None = None
    blocked_while_flat_loops: int = 0
    last_flat_recovery_at: datetime | None = None
    reversal_pending_close: bool = False
    last_entry_commit_timestamp: float | None = None
    last_close_timestamp: float | None = None


@dataclass
class ExitEvaluation:
    exit_price: float
    exit_reason: str
    hold_seconds: float
    pnl_ratio: float
    bars_held: int
    explicit_exit_triggered: bool
    exit_index: int


def _append_csv_row(file_path: str, fieldnames: list[str], row: dict[str, Any]) -> None:
    try:
        file_exists = os.path.exists(file_path)
        with open(file_path, "a", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logging.warning("CSV LOGGING WARNING | file=%s error=%s", file_path, exc)


def update_equity_tracking(
    *,
    state: SymbolRuntimeState,
    symbol: str,
    equity: float | None,
    csv_logging_enabled: bool = False,
) -> None:
    equity_value = _safe_positive_float(equity)
    if equity_value is None:
        return
    state.max_equity = max(float(state.max_equity), float(equity_value))
    state.current_drawdown = max(0.0, float(state.max_equity) - float(equity_value))
    state.equity_history.append(float(equity_value))
    logging.info(
        "EQUITY TRACK | symbol=%s equity=%.2f max=%.2f drawdown=%.2f",
        symbol,
        float(equity_value),
        float(state.max_equity),
        float(state.current_drawdown),
    )
    if csv_logging_enabled:
        _append_csv_row(
            "equity_log.csv",
            ["timestamp", "symbol", "equity", "max_equity", "drawdown", "total_trades", "wins", "losses", "total_pnl"],
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "equity": f"{float(equity_value):.8f}",
                "max_equity": f"{float(state.max_equity):.8f}",
                "drawdown": f"{float(state.current_drawdown):.8f}",
                "total_trades": int(state.total_trades),
                "wins": int(state.wins),
                "losses": int(state.losses),
                "total_pnl": f"{float(state.total_pnl):.8f}",
            },
        )


def update_closed_trade_performance(
    *,
    state: SymbolRuntimeState,
    symbol: str,
    realized_pnl: float,
    account_equity_usd: float | None,
    csv_logging_enabled: bool = False,
) -> dict[str, float]:
    pnl = float(realized_pnl)
    state.total_trades += 1
    state.total_pnl += pnl
    if pnl > 0:
        state.wins += 1
        state.gross_win_pnl += pnl
    elif pnl < 0:
        state.losses += 1
        state.gross_loss_pnl += abs(pnl)
    winrate = float(state.wins) / float(state.total_trades) if state.total_trades > 0 else 0.0
    avg_win = float(state.gross_win_pnl) / float(state.wins) if state.wins > 0 else 0.0
    avg_loss = float(state.gross_loss_pnl) / float(state.losses) if state.losses > 0 else 0.0
    pnl_per_trade = float(state.total_pnl) / float(state.total_trades) if state.total_trades > 0 else 0.0
    current_drawdown = max(0.0, float(state.max_equity) - float(account_equity_usd or 0.0))
    state.current_drawdown = current_drawdown
    state.performance.update(
        {
            "trades": int(state.total_trades),
            "wins": int(state.wins),
            "losses": int(state.losses),
            "pnl": float(state.total_pnl),
            "win_rate": float(winrate),
            "avg_pnl": float(pnl_per_trade),
        }
    )
    logging.info(
        "PERFORMANCE | symbol=%s trades=%d wins=%d losses=%d winrate=%.4f pnl=%.2f avg_win=%.2f avg_loss=%.2f pnl_per_trade=%.2f drawdown=%.2f",
        symbol,
        int(state.total_trades),
        int(state.wins),
        int(state.losses),
        float(winrate),
        float(state.total_pnl),
        float(avg_win),
        float(avg_loss),
        float(pnl_per_trade),
        float(current_drawdown),
    )
    if csv_logging_enabled:
        _append_csv_row(
            "trade_performance_log.csv",
            ["timestamp", "symbol", "realized_pnl", "total_trades", "wins", "losses", "winrate", "total_pnl"],
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "realized_pnl": f"{pnl:.8f}",
                "total_trades": int(state.total_trades),
                "wins": int(state.wins),
                "losses": int(state.losses),
                "winrate": f"{float(winrate):.8f}",
                "total_pnl": f"{float(state.total_pnl):.8f}",
            },
        )
    return {
        "winrate": float(winrate),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "pnl_per_trade": float(pnl_per_trade),
        "drawdown": float(current_drawdown),
    }


@dataclass
class RuntimeTradeManagerResult:
    trade: ActiveTrade | None
    position_scale: float
    active_notional_usd: float
    no_trade_snapshot: int
    exited: bool = False
    exit_reason: str | None = None
    realized_pnl: float = 0.0
    pnl_ratio: float = 0.0
    hold_seconds: float = 0.0
    closed_trade_context: dict[str, Any] = field(default_factory=dict)


def _log_invalid_trade_state(error_code: str, trade: ActiveTrade, context: str) -> None:
    side = str(getattr(trade, 'side', 'UNKNOWN')).upper()
    entry_price = getattr(trade, 'entry_price', None)
    if error_code == 'invalid_entry_price':
        logging.critical(
            'INVALID ENTRY PRICE | context=%s side=%s entry_price=%r trailing_stop=%r',
            context,
            side,
            entry_price,
            getattr(trade, 'trailing_stop_price', None),
        )
    elif error_code == 'invalid_trade_side':
        logging.critical(
            'INVALID TRADE SIDE | context=%s side=%r entry_price=%r trailing_stop=%r',
            context,
            getattr(trade, 'side', None),
            entry_price,
            getattr(trade, 'trailing_stop_price', None),
        )


def _runtime_guard_result(
    *,
    trade: ActiveTrade,
    error_code: str,
    context: str,
    active_position_scale: float,
    active_notional_usd: float,
    no_trade_snapshot_for_active_trade: int,
) -> RuntimeTradeManagerResult:
    _log_invalid_trade_state(error_code, trade, context)
    return RuntimeTradeManagerResult(
        trade=None,
        position_scale=float(active_position_scale),
        active_notional_usd=float(active_notional_usd),
        no_trade_snapshot=no_trade_snapshot_for_active_trade,
        exited=True,
        exit_reason=error_code,
    )


class AdaptiveFilterProtocol(Protocol):
    session_drawdown_usd: float

    def record_trade_feedback(self, pnl_usd: float, latest_bar_time: datetime, bars: pd.DataFrame, exit_reason: str) -> None: ...


class BybitHTTPProtocol(Protocol):
    def get_instruments_info(self, **kwargs: Any) -> dict[str, Any]: ...
    def place_order(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_positions(self, **kwargs: Any) -> dict[str, Any]: ...
    def set_trading_stop(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_open_orders(self, **kwargs: Any) -> dict[str, Any]: ...


BrokerSession = BrokerAdapter | BybitHTTPProtocol


def _safe_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(parsed) or parsed <= 0:
        return None
    return float(parsed)


def fetch_live_account_equity(session: BrokerSession | None) -> dict[str, float | str | None]:
    if session is None:
        return {'balance': None, 'equity': None, 'free_margin': None, 'source': 'session_none', 'reason': 'session_missing'}

    if isinstance(session, MT5BrokerAdapter):
        snapshot = session.get_mt5_account_snapshot()
        return {
            'balance': _safe_positive_float(snapshot.get('balance')),
            'equity': _safe_positive_float(snapshot.get('equity')),
            'free_margin': _safe_positive_float(snapshot.get('free_margin')),
            'source': 'broker',
            'reason': None if bool(snapshot.get('connected')) else str(snapshot.get('reason') or 'mt5_not_connected'),
        }

    get_wallet_balance = getattr(session, 'get_wallet_balance', None)
    if callable(get_wallet_balance):
        try:
            wallet = get_wallet_balance(accountType="UNIFIED")
            wallet_row = ((wallet or {}).get('result', {}).get('list', [{}]) or [{}])[0]
            return {
                'balance': _safe_positive_float(wallet_row.get('totalWalletBalance')),
                'equity': _safe_positive_float(wallet_row.get('totalEquity')),
                'free_margin': _safe_positive_float(wallet_row.get('totalAvailableBalance')),
                'source': 'broker',
                'reason': None,
            }
        except Exception as exc:
            return {'balance': None, 'equity': None, 'free_margin': None, 'source': 'broker_error', 'reason': str(exc)}

    return {'balance': None, 'equity': None, 'free_margin': None, 'source': 'unknown_session', 'reason': 'unsupported_session_type'}


def resolve_account_equity(session: BrokerSession | None, fallback_env_value: float, *, minimum_fallback: float = 50.0) -> AccountEquityResolution:
    global ACCOUNT_EQUITY_CACHE_USD
    account = fetch_live_account_equity(session)
    balance = _safe_positive_float(account.get('balance'))
    equity = _safe_positive_float(account.get('equity'))
    free_margin = _safe_positive_float(account.get('free_margin'))
    fallback_equity = _safe_positive_float(fallback_env_value)

    if equity is not None:
        ACCOUNT_EQUITY_CACHE_USD = float(equity)
        logging.info(
            'ACCOUNT SYNC | equity=%.2f balance=%.2f free_margin=%.2f source=%s',
            float(equity),
            float(balance or 0.0),
            float(free_margin or 0.0),
            'live_session',
        )
        return AccountEquityResolution(equity=float(equity), source='live_session')
    if balance is not None:
        ACCOUNT_EQUITY_CACHE_USD = float(balance)
        logging.warning(
            'ACCOUNT SYNC WARNING | fallback_used=%s reason=%s',
            'broker_balance',
            'broker_equity_missing_or_invalid',
        )
        logging.info(
            'ACCOUNT SYNC | equity=%.2f balance=%.2f free_margin=%.2f source=%s',
            float(balance),
            float(balance),
            float(free_margin or 0.0),
            'cached',
        )
        return AccountEquityResolution(
            equity=float(balance),
            source='cached',
            warning='broker_equity_missing_or_invalid',
        )

    if fallback_env_value is not None:
        effective_fallback = max(float(minimum_fallback), float(fallback_equity if fallback_equity is not None else minimum_fallback))
        logging.warning(
            'ACCOUNT SYNC WARNING | fallback_used=%s reason=%s',
            'env_account_equity_usd' if fallback_equity is not None else 'default_50',
            str(account.get('reason') or 'invalid_broker_equity'),
        )
        logging.info(
            'ACCOUNT SYNC | equity=%.2f balance=%.2f free_margin=%.2f source=%s',
            float(effective_fallback),
            float(balance or 0.0),
            float(free_margin or 0.0),
            'env_fallback',
        )
        return AccountEquityResolution(
            equity=float(effective_fallback),
            source='env_fallback',
            warning=str(account.get('reason') or 'invalid_broker_equity'),
        )

    if ACCOUNT_EQUITY_CACHE_USD is not None and ACCOUNT_EQUITY_CACHE_USD > 0:
        logging.warning(
            'ACCOUNT SYNC WARNING | fallback_used=%s reason=%s',
            'cached_equity',
            str(account.get('reason') or 'invalid_broker_equity'),
        )
        return AccountEquityResolution(
            equity=float(ACCOUNT_EQUITY_CACHE_USD),
            source='cached',
            warning=str(account.get('reason') or 'invalid_broker_equity'),
        )

    logging.warning(
        'ACCOUNT SYNC WARNING | fallback_used=%s reason=%s',
        'default_50',
        str(account.get('reason') or 'invalid_broker_equity'),
    )
    return AccountEquityResolution(
        equity=float(minimum_fallback),
        source='unknown',
        warning=str(account.get('reason') or 'invalid_broker_equity'),
    )


def resolve_effective_account_equity(session: BrokerSession | None, fallback_env_value: float, *, minimum_fallback: float = 50.0) -> float:
    return resolve_account_equity(session, fallback_env_value, minimum_fallback=minimum_fallback).equity


def _enforce_mt5_live_session_health(session: BrokerSession | None, safety_controller: LiveSafetyController, *, reason_prefix: str) -> None:
    if not isinstance(session, MT5BrokerAdapter):
        return
    try:
        session.validate_trading_session()
    except Exception as exc:
        logging.critical('MT5 HEARTBEAT FAILURE | phase=%s error=%s', reason_prefix, exc)
        safety_controller.activate_kill_switch(f'{reason_prefix}:{exc}')
        raise SystemExit(f'MT5 heartbeat failure during {reason_prefix}; kill switch engaged') from exc


def _enforce_mt5_order_channel(session: BrokerSession, safety_controller: LiveSafetyController, *, symbol: str, side: str | None = None, qty: float | None = None, position_idx: int | None = None) -> None:
    if not isinstance(session, MT5BrokerAdapter):
        return
    try:
        positions_total = session.validate_order_channel()
        logging.info('MT5 ORDER CHANNEL VALIDATION | symbol=%s positions_total=%s', symbol, positions_total)
    except Exception as exc:
        logging.critical('MT5 GHOST TRADE PREVENTION | symbol=%s error=%s', symbol, exc)
        safety_controller.activate_kill_switch(f'mt5_positions_total_failed:{exc}', session=session, symbol=symbol, side=side, qty=qty, position_idx=position_idx)
        raise SystemExit('MT5 order channel validation failed; kill switch engaged') from exc


def create_broker_adapter() -> BrokerAdapter | None:
    backend = str(os.getenv('BROKER_BACKEND') or '').strip().lower()
    if backend == 'mt5':
        adapter = MT5BrokerAdapter()
        try:
            connected = adapter.connect()
        except Exception as exc:
            adapter.connected = False
            adapter.last_error = str(exc)
            logging.critical('LIVE MODE | failed to initialize broker adapter backend=mt5 error=%s', exc)
            return None
        if not connected:
            logging.critical('LIVE MODE | failed to initialize broker adapter backend=mt5 error=%s', adapter.last_error or 'unknown')
            return None
        logging.info('BROKER ADAPTER | backend=mt5')
        return adapter
    return None


LiveOrderResult = BrokerOrderResult


@dataclass
class ExchangePosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    position_idx: int | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    broker_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> float:
        return self.qty

    @property
    def is_open(self) -> bool:
        return self.qty > 0.0 and self.side in {'LONG', 'SHORT'}

    @classmethod
    def from_normalized(cls, position: NormalizedPosition) -> 'ExchangePosition':
        return cls(
            symbol=position.symbol,
            side=position.side,
            qty=position.size,
            entry_price=position.entry_price,
            position_idx=position.position_idx,
            take_profit=position.take_profit,
            stop_loss=position.stop_loss,
            broker_id=position.broker_id,
            raw=position.raw,
        )


@dataclass(frozen=True)
class SpreadSnapshot:
    bid: float
    ask: float
    spread: float
    spread_ratio: float


def compute_live_spread_snapshot(session: BrokerSession, symbol: str) -> SpreadSnapshot:
    if not isinstance(session, BrokerAdapter):
        return SpreadSnapshot(bid=0.0, ask=0.0, spread=0.0, spread_ratio=0.0)
    tick = session.fetch_current_tick(symbol)
    bid = float(tick.get('bid') or 0.0)
    ask = float(tick.get('ask') or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError(f'invalid MT5 tick for spread gate: bid={bid} ask={ask}')
    spread = ask - bid
    midpoint = max((ask + bid) / 2.0, 1e-9)
    return SpreadSnapshot(bid=bid, ask=ask, spread=spread, spread_ratio=spread / midpoint)


def enforce_live_spread_gate(*, session: BrokerSession, symbol: str, trade: ActiveTrade, specs: SymbolSpecs, max_spread_ratio: float, safety_controller: LiveSafetyController) -> bool:
    if not isinstance(session, BrokerAdapter):
        return True
    try:
        spread_snapshot = compute_live_spread_snapshot(session, symbol)
    except Exception as exc:
        logging.critical('LIVE SPREAD CHECK FAILED | symbol=%s reason=%s', symbol, exc)
        safety_controller.register_execution_failure(f'spread_check_failed:{exc}')
        safety_controller.activate_kill_switch(f'spread_check_failed:{exc}')
        return False
    tick_floor = max(float(specs.tick_size), 1e-9)
    min_ratio_floor = tick_floor / max(spread_snapshot.ask, tick_floor)
    allowed_ratio = max(float(max_spread_ratio), min_ratio_floor)
    if spread_snapshot.spread_ratio > allowed_ratio:
        logging.warning('LIVE SPREAD BLOCK | symbol=%s side=%s bid=%.6f ask=%.6f spread=%.6f spread_ratio=%.6f allowed=%.6f reason=abnormally_high_spread', symbol, trade.side, spread_snapshot.bid, spread_snapshot.ask, spread_snapshot.spread, spread_snapshot.spread_ratio, allowed_ratio)
        return False
    logging.info('LIVE SPREAD OK | symbol=%s side=%s bid=%.6f ask=%.6f spread=%.6f spread_ratio=%.6f allowed=%.6f', symbol, trade.side, spread_snapshot.bid, spread_snapshot.ask, spread_snapshot.spread, spread_snapshot.spread_ratio, allowed_ratio)
    return True


@dataclass(frozen=True)
class TrailingPolicy:
    mode: str
    allow_internal: bool
    exchange_native: bool


@dataclass
class SyncResult:
    active_trade: ActiveTrade | None
    active_notional_usd: float
    active_position_scale: float
    safe_mode_triggered: bool
    mismatch: bool
    recovered: bool
    exchange_position: ExchangePosition


@dataclass
class LiveSafetyController:
    max_session_drawdown_pct: float = 0.08
    max_daily_loss_pct: float = 0.05
    max_consecutive_live_losses: int = 3
    max_position_value_usd: float = 250.0
    max_position_qty: float = 0.05
    max_exchange_desync_count: int = 2
    max_consecutive_execution_failures: int = 3
    max_unresolved_confirmations: int = 2
    max_protection_failures: int = 1
    live_trading_halted: bool = False
    halt_reason: str | None = None
    consecutive_live_losses: int = 0
    exchange_desync_count: int = 0
    consecutive_execution_failures: int = 0
    unresolved_confirmation_count: int = 0
    protection_failure_count: int = 0
    halt_count: int = 0
    last_order_link_id: str | None = None

    @classmethod
    def from_env(cls) -> 'LiveSafetyController':
        return cls(
            max_session_drawdown_pct=float(os.getenv('MAX_SESSION_DRAWDOWN_PCT', '0.08')),
            max_daily_loss_pct=float(os.getenv('MAX_DAILY_LOSS_PCT', '0.05')),
            max_consecutive_live_losses=int(os.getenv('MAX_CONSECUTIVE_LIVE_LOSSES', '3')),
            max_position_value_usd=float(os.getenv('MAX_POSITION_VALUE_USD', os.getenv('MAX_POSITION_NOTIONAL_USDT', '250'))),
            max_position_qty=float(os.getenv('MAX_POSITION_QTY', '0.05')),
            max_exchange_desync_count=int(os.getenv('MAX_EXCHANGE_DESYNC_COUNT', '2')),
            max_consecutive_execution_failures=int(os.getenv('MAX_CONSECUTIVE_EXECUTION_FAILURES', '3')),
            max_unresolved_confirmations=int(os.getenv('MAX_UNRESOLVED_CONFIRMATIONS', '2')),
            max_protection_failures=int(os.getenv('MAX_PROTECTION_FAILURES', '1')),
        )

    def activate_kill_switch(self, reason: str, *, session: BybitHTTPProtocol | None = None, symbol: str | None = None, side: str | None = None, qty: float | None = None, position_idx: int | None = None) -> None:
        self.halt_reason = str(reason or self.halt_reason or 'kill_switch_triggered')
        self.halt_count += 1
        logging.error('KILL SWITCH TRIGGERED | symbol=%s reason=%s', symbol or 'unknown', self.halt_reason)
        logging.warning('EXECUTION CONTINUE | reason=%s', self.halt_reason)
        self.live_trading_halted = True
        normalized_side = str(side or '').upper()
        normalized_qty = float(qty or 0.0)
        if session is None or not symbol or normalized_side not in {'LONG', 'SHORT'} or normalized_qty <= 0:
            return
        try:
            close_result = close_bybit_position(session, symbol, normalized_side, normalized_qty, position_idx=position_idx)
            if close_result.success:
                logging.info('KILL SWITCH SAFE EXIT | symbol=%s side=%s qty=%.12f result=success', symbol, normalized_side, normalized_qty)
            else:
                logging.warning('KILL SWITCH SAFE EXIT | symbol=%s side=%s qty=%.12f result=failed reason=%s', symbol, normalized_side, normalized_qty, close_result.reason or 'unknown')
        except Exception as exc:
            logging.warning('KILL SWITCH SAFE EXIT FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, normalized_side, normalized_qty, exc)

    def register_live_trade_result(self, pnl_usd: float, position_value_usd: float, daily_pnl_usd: float, session_drawdown_usd: float) -> None:
        if pnl_usd < 0:
            self.consecutive_live_losses += 1
        else:
            self.consecutive_live_losses = 0
        session_limit_usd = abs(position_value_usd) * abs(self.max_session_drawdown_pct)
        daily_limit_usd = abs(position_value_usd) * abs(self.max_daily_loss_pct)
        if session_limit_usd > 0 and abs(session_drawdown_usd) >= session_limit_usd:
            self.activate_kill_switch(f'session_drawdown_limit:{session_drawdown_usd:.4f}')
        if daily_limit_usd > 0 and abs(min(0.0, daily_pnl_usd)) >= daily_limit_usd:
            self.activate_kill_switch(f'daily_loss_limit:{daily_pnl_usd:.4f}')
        if self.consecutive_live_losses >= self.max_consecutive_live_losses:
            self.activate_kill_switch(f'consecutive_live_losses:{self.consecutive_live_losses}')

    def register_desync(self, reason: str) -> None:
        self.exchange_desync_count += 1
        logging.warning('EXCHANGE DESYNC | count=%d reason=%s', self.exchange_desync_count, reason)
        if self.exchange_desync_count >= self.max_exchange_desync_count:
            logging.error('EXECUTION GUARD | reason=exchange_desync_threshold_reached threshold=%d', self.max_exchange_desync_count)
            self.activate_kill_switch(f'exchange_desync_limit:{reason}')

    def clear_desync(self) -> None:
        self.exchange_desync_count = 0

    def register_execution_failure(self, reason: str) -> None:
        if "missing_confirmation" in reason:
            logging.warning('IGNORED FAILURE | reason=%s', reason)
            return
        self.consecutive_execution_failures += 1
        logging.warning('EXECUTION FAILURE | count=%d reason=%s', self.consecutive_execution_failures, reason)
        if self.consecutive_execution_failures >= self.max_consecutive_execution_failures:
            logging.error('EXECUTION GUARD | reason=execution_failure_threshold_reached threshold=%d', self.max_consecutive_execution_failures)
            self.activate_kill_switch(f'execution_failure_limit:{reason}')

    def register_execution_success(self) -> None:
        self.consecutive_execution_failures = 0
        self.unresolved_confirmation_count = 0

    def register_unresolved_confirmation(self, reason: str) -> None:
        self.unresolved_confirmation_count += 1
        logging.warning(
            'UNRESOLVED CONFIRMATION | count=%s reason=%s',
            self.unresolved_confirmation_count,
            reason,
        )
        if self.unresolved_confirmation_count >= self.max_unresolved_confirmations:
            self.activate_kill_switch(f'execution_confirmation_limit:{reason}')

    def clear_execution_failures(self) -> None:
        self.consecutive_execution_failures = 0

    def register_protection_failure(self, reason: str) -> None:
        self.protection_failure_count += 1
        logging.critical('PROTECTION FAILURE | count=%d reason=%s', self.protection_failure_count, reason)
        if self.protection_failure_count >= self.max_protection_failures:
            self.activate_kill_switch(f'protection_failure_limit:{reason}')

    def clear_protection_failures(self) -> None:
        self.protection_failure_count = 0

    def enforce_position_limits(self, *, position_value_usd: float, qty: float) -> bool:
        if position_value_usd > self.max_position_value_usd:
            logging.critical('POSITION LIMIT BREACH | reason=position_value_cap position_value_usd=%.4f limit=%.4f', position_value_usd, self.max_position_value_usd)
            self.activate_kill_switch(f'position_value_limit:{position_value_usd:.4f}')
            return False
        if qty > self.max_position_qty:
            logging.critical('POSITION LIMIT BREACH | reason=qty_cap qty=%.12f limit=%.12f', qty, self.max_position_qty)
            self.activate_kill_switch(f'position_qty_limit:{qty:.12f}')
            return False
        return True


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _decimal_from_number(value: float | str) -> Decimal:
    return Decimal(str(value))


def create_bybit_session() -> HTTP | None:
    if HTTP is None:
        logging.warning('LIVE MODE | pybit unavailable; live execution disabled')
        return None
    api_key = os.getenv('BYBIT_API_KEY', '').strip()
    api_secret = os.getenv('BYBIT_API_SECRET', '').strip()
    if not api_key or not api_secret:
        logging.warning('LIVE MODE | missing BYBIT_API_KEY/BYBIT_API_SECRET; staying paper-only')
        return None
    testnet = _env_flag('BYBIT_TESTNET', True)
    try:
        session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
    except Exception as exc:
        logging.warning('LIVE MODE | failed to create Bybit session: %s', exc)
        return None
    logging.info('LIVE MODE | enabled=true testnet=%s', str(testnet).lower())
    return session


def get_live_trailing_policy() -> TrailingPolicy:
    mode = str(os.getenv('BYBIT_LIVE_TRAILING_MODE', 'disabled')).strip().lower()
    if mode not in {'disabled', 'internal', 'exchange'}:
        logging.warning('LIVE TRAILING | invalid mode=%s fallback=disabled', mode)
        mode = 'disabled'
    policy = TrailingPolicy(
        mode=mode,
        allow_internal=mode == 'internal',
        exchange_native=mode == 'exchange',
    )
    logging.info(
        'LIVE TRAILING | mode=%s allow_internal=%s exchange_native=%s',
        policy.mode,
        str(policy.allow_internal).lower(),
        str(policy.exchange_native).lower(),
    )
    return policy


def fetch_symbol_specs(session: BrokerSession, symbol: str) -> SymbolSpecs:
    if isinstance(session, BrokerAdapter):
        return session.get_symbol_specs(symbol)
    response = session.get_instruments_info(category='linear', symbol=symbol)
    rows = response.get('result', {}).get('list', [])
    if not rows:
        raise RuntimeError(f'No instrument info returned for {symbol}')
    row = rows[0]
    lot = row.get('lotSizeFilter', {})
    price_filter = row.get('priceFilter', {})
    return SymbolSpecs(
        symbol=symbol,
        category='linear',
        qty_step=float(lot.get('qtyStep') or 0.0),
        min_qty=float(lot.get('minOrderQty') or 0.0),
        tick_size=float(price_filter.get('tickSize') or 0.0),
    )


def round_qty_to_step(qty: float, step: float) -> float:
    return round_size_to_step(qty, step)


def enforce_min_qty(qty: float, min_qty: float) -> float:
    if qty <= 0:
        return 0.0
    if min_qty > 0 and qty < min_qty:
        logging.warning('ORDER BLOCKED | qty=%.12f below min_qty=%.12f', qty, min_qty)
        return 0.0
    return qty


def calc_order_qty(position_value_usd: float, position_scale: float, entry_price: float, step: float, min_qty: float) -> float:
    raw_qty = (float(position_value_usd) * float(position_scale)) / float(entry_price) if entry_price > 0 else 0.0
    rounded_qty = calculate_order_size_from_notional(position_value_usd, position_scale, entry_price, step, min_qty)
    logging.info('QTY CALC | position_value_usd=%.4f candidate_scale=%.4f entry_price=%.6f raw_qty=%.12f rounded_qty=%.12f', position_value_usd, position_scale, entry_price, raw_qty, rounded_qty)
    return rounded_qty


def validate_order_qty(qty: float | None) -> bool:
    if qty is None:
        logging.critical('ORDER VALIDATION FAILED | reason=qty_none side=none qty=none price=none')
        return False
    try:
        parsed_qty = float(qty)
    except (TypeError, ValueError):
        logging.critical('ORDER VALIDATION FAILED | reason=qty_non_numeric side=none qty=%r price=none', qty)
        return False
    if parsed_qty <= 0:
        logging.critical('ORDER VALIDATION FAILED | reason=qty_non_positive side=none qty=%.12f price=none', parsed_qty)
        return False
    return True


def is_valid_trade_state(trade: ActiveTrade | None) -> bool:
    if trade is None:
        logging.critical('TRADE VALIDATION FAILED | reason=missing_trade side=none qty=none price=none')
        return False
    if str(getattr(trade, 'side', '')).upper() not in {'LONG', 'SHORT'}:
        logging.critical('TRADE VALIDATION FAILED | reason=invalid_side side=%r qty=none price=%r', getattr(trade, 'side', None), getattr(trade, 'entry_price', None))
        return False
    entry_price = float(getattr(trade, 'entry_price', 0.0) or 0.0)
    if entry_price <= 0:
        logging.critical('TRADE VALIDATION FAILED | reason=invalid_entry_price side=%s qty=none price=%r', str(getattr(trade, 'side', 'UNKNOWN')).upper(), getattr(trade, 'entry_price', None))
        return False
    return True


def _round_price_to_tick(price: float, tick_size: float) -> float:
    if price <= 0 or tick_size <= 0:
        return float(price)
    rounded = (_decimal_from_number(price) / _decimal_from_number(tick_size)).to_integral_value(rounding=ROUND_DOWN) * _decimal_from_number(tick_size)
    return float(rounded)


def _build_order_link_id(symbol: str, trade: ActiveTrade, latest_bar_id: str | None) -> str:
    timestamp = str(int(time.time() * 1000))
    fingerprint = '|'.join(
        [
            str(symbol).upper(),
            str(trade.side).upper(),
            str(latest_bar_id or 'no-bar'),
            timestamp,
            f'{float(getattr(trade, "signal_score", 0.0)):.6f}',
            f'{float(getattr(trade, "entry_price", 0.0)):.6f}',
        ]
    )
    digest = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:24]
    suffix = uuid.uuid4().hex[:6]
    return f'hft-{digest}-{suffix}'


def _has_duplicate_open_order(session: BrokerSession, *, symbol: str, side: str, order_link_id: str) -> bool:
    # MT5 / BrokerAdapter → skip duplicate check (veilig)
    if isinstance(session, BrokerAdapter):
        return False

    try:
        response = session.get_open_orders(category='linear', symbol=symbol)
        orders = response.get('result', {}).get('list', [])
    except Exception as exc:
        logging.warning('OPEN ORDER CHECK FAILED | symbol=%s reason=%s action=skip_duplicate_guard', symbol, exc)
        return False

    normalized_side = 'Buy' if str(side).upper() == 'LONG' else 'Sell'

    for order in orders:
        if isinstance(order, dict):
            reduce_only = bool(order.get('reduceOnly'))
            open_order_link_id = str(order.get('orderLinkId') or '')
            order_symbol = str(order.get('symbol') or symbol)
            order_side = str(order.get('side') or '')
        else:
            reduce_only = bool(getattr(order, 'reduceOnly', False))
            open_order_link_id = str(getattr(order, 'orderLinkId', '') or '')
            order_symbol = str(getattr(order, 'symbol', symbol) or symbol)
            order_side = str(getattr(order, 'side', '') or '')

        if reduce_only:
            continue

        if open_order_link_id == order_link_id:
            return True

        if order_symbol == symbol and order_side == normalized_side:
            return True

    return False


def _validate_exchange_position_reconstruction(exchange_position: ExchangePosition) -> bool:
    return (
        exchange_position.is_open
        and exchange_position.entry_price > 0
        and exchange_position.qty > 0
        and exchange_position.side in {'LONG', 'SHORT'}
    )


def _protection_matches_expected(
    *,
    exchange_position: ExchangePosition,
    trade: ActiveTrade,
    expected_tp: float,
    expected_sl: float,
    tick_size: float,
) -> bool:
    actual_tp = float(exchange_position.take_profit or 0.0)
    actual_sl = float(exchange_position.stop_loss or 0.0)
    expected_tp = _round_price_to_tick(expected_tp, tick_size)
    expected_sl = _round_price_to_tick(expected_sl, tick_size)
    tolerance = max(float(tick_size) * 0.5, 1e-9)
    if actual_tp <= 0 or actual_sl <= 0:
        return False
    if abs(actual_tp - expected_tp) > tolerance or abs(actual_sl - expected_sl) > tolerance:
        return False
    entry_price = float(exchange_position.entry_price or trade.entry_price)
    if trade.side == 'LONG':
        return actual_sl < entry_price < actual_tp
    return actual_tp < entry_price < actual_sl


def execute_bybit_market_order(session: BrokerSession, symbol: str, side: str, qty: float, reduce_only: bool = False, position_idx: int | None = None, order_link_id: str | None = None, safety_controller: LiveSafetyController | None = None) -> LiveOrderResult:
    if qty <= 0:
        return LiveOrderResult(False, None, str(side).upper(), float(qty), None, {'validation': 'qty_must_be_positive'}, 'qty_must_be_positive')
    logging.info('ORDER SUBMIT | side=%s qty=%.12f symbol=%s reduce_only=%s', str(side).upper(), qty, symbol, str(reduce_only).lower())
    if safety_controller is not None:
        _enforce_mt5_order_channel(session, safety_controller, symbol=symbol, side=str(side).upper(), qty=float(qty), position_idx=position_idx)
    if isinstance(session, BrokerAdapter):
        if reduce_only:
            return session.close_position(symbol, str(side).upper(), qty, position=position_idx, order_link_id=order_link_id)
        return session.place_market_order(symbol, str(side).upper(), qty, position=position_idx, order_link_id=order_link_id)
    normalized_side = 'Buy' if str(side).upper() in {'LONG', 'BUY'} else 'Sell'
    try:
        payload: dict[str, Any] = {
            'category': 'linear',
            'symbol': symbol,
            'side': normalized_side,
            'orderType': 'Market',
            'qty': str(qty),
            'reduceOnly': reduce_only,
        }
        if order_link_id:
            payload['orderLinkId'] = order_link_id
        if position_idx is not None:
            payload['positionIdx'] = position_idx
        response = session.place_order(**payload)
        order = response.get('result', {})
        success = (
            str(response.get('retMsg', 'OK')).lower() in {'ok', 'success', ''}
            and int(response.get('retCode', 0)) == 0
            and bool(order.get('orderId'))
        )
        avg_price = order.get('avgPrice') or order.get('price')
        result = LiveOrderResult(success=success, order_id=order.get('orderId'), side=str(side).upper(), qty=float(qty), avg_price=float(avg_price) if avg_price not in (None, '') else None, raw_response=response, reason=None if success else str(response.get('retMsg') or 'exchange_rejected'))
    except Exception as exc:
        logging.error('ORDER FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, side, qty, exc)
        return LiveOrderResult(False, None, str(side).upper(), float(qty), None, {'exception': str(exc)}, str(exc))
    if result.success:
        logging.info('ORDER FILLED | order_id=%s avg_price=%s exchange_confirmed_qty=%.12f', result.order_id, 'none' if result.avg_price is None else f'{result.avg_price:.6f}', result.qty)
    else:
        logging.error('ORDER FAILED | symbol=%s side=%s qty=%.12f reason=%s', symbol, side, qty, result.reason)
    return result


def set_bybit_position_protection(session: BrokerSession, symbol: str, side: str, take_profit_price: float, stop_loss_price: float, trailing_stop: float | None = None, trailing_active_price: float | None = None, tick_size: float = 0.0) -> bool:
    tp_price = _round_price_to_tick(take_profit_price, tick_size)
    sl_price = _round_price_to_tick(stop_loss_price, tick_size)
    logging.info('PROTECTION SUBMIT | symbol=%s side=%s tp=%.6f sl=%.6f trailing_stop=%s', symbol, side, tp_price, sl_price, 'none' if trailing_stop is None else f'{trailing_stop:.6f}')
    if isinstance(session, BrokerAdapter):
        return session.set_protection(symbol, side, tp_price, sl_price, trailing_stop=trailing_stop, trailing_active_price=trailing_active_price)
    try:
        payload: dict[str, Any] = dict(
            category='linear',
            symbol=symbol,
            takeProfit=str(tp_price),
            stopLoss=str(sl_price),
            tpslMode='Full',
        )
        if trailing_stop is not None and trailing_stop > 0:
            payload['trailingStop'] = str(_round_price_to_tick(trailing_stop, tick_size))
        if trailing_active_price is not None and trailing_active_price > 0:
            payload['activePrice'] = str(_round_price_to_tick(trailing_active_price, tick_size))
        response = session.set_trading_stop(**payload)
    except Exception as exc:
        logging.critical('PROTECTION FAILED | symbol=%s reason=%s', symbol, exc)
        return False
    success = int(response.get('retCode', 0)) == 0
    if success:
        if trailing_stop is None:
            logging.info('PROTECTION SET | tp=%.6f sl=%.6f trailing=paper_only', tp_price, sl_price)
        else:
            logging.info('PROTECTION SET | tp=%.6f sl=%.6f trailing=paper_only_requested_%.6f', tp_price, sl_price, trailing_stop)
        return True
    logging.critical('PROTECTION FAILED | symbol=%s reason=%s', symbol, response.get('retMsg'))
    return False


def fetch_open_position(session: BrokerSession, symbol: str) -> ExchangePosition:
    if isinstance(session, BrokerAdapter):
        return ExchangePosition.from_normalized(session.fetch_open_position(symbol))
    response = session.get_positions(category='linear', symbol=symbol)
    rows = response.get('result', {}).get('list', [])
    for row in rows:
        qty = float(row.get('size') or 0.0)
        if qty <= 0:
            continue
        raw_side = str(row.get('side') or '').upper()
        side = 'LONG' if raw_side == 'BUY' else 'SHORT' if raw_side == 'SELL' else 'FLAT'
        return ExchangePosition(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=float(row.get('avgPrice') or 0.0),
            position_idx=int(row.get('positionIdx')) if row.get('positionIdx') not in (None, '') else None,
            take_profit=float(row.get('takeProfit')) if row.get('takeProfit') not in (None, '') else None,
            stop_loss=float(row.get('stopLoss')) if row.get('stopLoss') not in (None, '') else None,
            raw=row,
        )
    return ExchangePosition(symbol=symbol, side='FLAT', qty=0.0, entry_price=0.0, raw={})


def confirm_position_open(session: BrokerSession, symbol: str, timeout: float = 3.0) -> ExchangePosition | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            position = fetch_open_position(session, symbol)
            if position and getattr(position, "is_open", False):
                return position
        except Exception:
            pass
        _sleep()
    return None


def _positions_match(active_trade: ActiveTrade, exchange_position: ExchangePosition) -> bool:
    if str(active_trade.side).upper() != exchange_position.side:
        return False
    if exchange_position.qty <= 0:
        return False
    entry_delta = abs(float(active_trade.entry_price) - exchange_position.entry_price)
    price_tolerance = max(exchange_position.entry_price * 0.002, 1e-9)
    return entry_delta <= price_tolerance


def is_opposite_side(active_side: str, new_side: str) -> bool:
    return str(active_side).upper() != str(new_side).upper()


def sync_with_exchange_position(*, session: BybitHTTPProtocol, symbol: str, active_trade: ActiveTrade | None, active_notional_usd: float, active_position_scale: float, risk_state: RiskState, safety_controller: LiveSafetyController, specs: SymbolSpecs | None = None, leverage: float | None = None) -> SyncResult:
    if safety_controller.live_trading_halted:
        exchange_position = ExchangePosition(symbol=symbol, side='FLAT', qty=0.0, entry_price=0.0, raw={})
        try:
            exchange_position = fetch_open_position(session, symbol)
        except Exception:
            pass
        return SyncResult(active_trade, active_notional_usd, active_position_scale, True, False, False, exchange_position)
    try:
        exchange_position = fetch_open_position(session, symbol)
    except Exception as exc:
        safety_controller.register_desync(f'position_fetch_failed:{exc}')
        logging.critical('EXCHANGE SYNC | fetch_failed=true reason=%s', exc)
        return SyncResult(active_trade, active_notional_usd, active_position_scale, True, True, False, ExchangePosition(symbol=symbol, side='FLAT', qty=0.0, entry_price=0.0, raw={}))
    if exchange_position.is_open and not _validate_exchange_position_reconstruction(exchange_position):
        safety_controller.register_desync('startup_reconstruction_insufficient_data')
        logging.critical('EXCHANGE SYNC | invalid_open_position_data=true side=%s qty=%.12f price=%.6f', exchange_position.side, exchange_position.qty, exchange_position.entry_price)
        return SyncResult(active_trade, active_notional_usd, active_position_scale, True, True, False, exchange_position)
    bot_open = active_trade is not None and risk_state.open_positions > 0
    mismatch = False
    recovered = False
    if not exchange_position.is_open and bot_open:
        logging.warning('SYNC FIX | reason=exchange_flat_internal_open side=%s qty=none price=%s', 'none' if active_trade is None else str(active_trade.side).upper(), 'none' if active_trade is None else f'{active_trade.entry_price:.6f}')
        risk_state.open_positions = 0
        return SyncResult(None, active_notional_usd, 1.0, False, False, False, exchange_position)
    if exchange_position.is_open and not bot_open:
        if not _validate_exchange_position_reconstruction(exchange_position):
            safety_controller.register_desync('exchange_recovery_invalid_data')
            return SyncResult(active_trade, active_notional_usd, active_position_scale, True, True, False, exchange_position)
        recovered_trade = compute_exit_plan(signal_score=1.0, volatility=0.001, side=exchange_position.side, entry_price=exchange_position.entry_price, source='exchange_recovery')
        recovered_trade.entry_time = datetime.now(timezone.utc)
        risk_state.open_positions = 1
        recovered = True
        resolved_specs = specs if specs is not None else fetch_symbol_specs(session, symbol)
        resolved_leverage = float(leverage if leverage is not None else os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0)
        recovered_metrics = compute_live_position_metrics(
            symbol=symbol,
            qty=float(exchange_position.qty),
            entry_price=float(exchange_position.entry_price),
            specs=resolved_specs,
            leverage=resolved_leverage,
        )
        logging.info(
            'SYNC BASIS | symbol=%s expected_qty=%.12f broker_qty=%.12f expected_notional=%.6f',
            symbol,
            recovered_metrics.qty,
            float(exchange_position.qty),
            recovered_metrics.notional_value_usd,
        )
        logging.info('SYNC RESULT | aligned=true reason=recovered_exchange_position')
        logging.warning('SYNC FIX | reason=recovered_exchange_position side=%s qty=%.12f price=%.6f', exchange_position.side, exchange_position.qty, exchange_position.entry_price)
        return SyncResult(recovered_trade, recovered_metrics.notional_value_usd, active_position_scale, False, False, True, exchange_position)
    if exchange_position.is_open and bot_open:
        bot_side = str(active_trade.side).upper()
        exchange_qty = float(exchange_position.qty)
        resolved_specs = specs if specs is not None else fetch_symbol_specs(session, symbol)
        resolved_leverage = float(leverage if leverage is not None else os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0)
        expected_metrics = compute_live_position_metrics(
            symbol=symbol,
            qty=float(exchange_qty),
            entry_price=float(exchange_position.entry_price),
            specs=resolved_specs,
            leverage=resolved_leverage,
        )
        expected_qty = float(expected_metrics.qty)
        expected_notional = float(expected_metrics.notional_value_usd)
        logging.info(
            'SYNC BASIS | symbol=%s expected_qty=%.12f broker_qty=%.12f expected_notional=%.6f',
            symbol,
            expected_qty,
            exchange_qty,
            expected_notional,
        )
        qty_tolerance = max(exchange_qty * 0.01, 1e-9)
        qty_mismatch = abs(expected_qty - exchange_qty) > qty_tolerance
        if not _positions_match(active_trade, exchange_position) or qty_mismatch:
            mismatch = True
            reason = f'bot_side={bot_side} exchange_side={exchange_position.side} expected_qty={expected_qty:.12f} exchange_qty={exchange_qty:.12f}'
            safety_controller.register_desync(reason)
            logging.info('SYNC RESULT | aligned=false reason=%s', reason)
            logging.critical('EXCHANGE SYNC | mismatch=true side=%s qty=%.12f price=%.6f reason=%s', exchange_position.side, exchange_position.qty, exchange_position.entry_price, reason)
            return SyncResult(active_trade, active_notional_usd, active_position_scale, True, True, False, exchange_position)
    safety_controller.clear_desync()
    logging.info('SYNC RESULT | aligned=true reason=positions_aligned')
    logging.info('EXCHANGE SYNC | mismatch=false side=%s qty=%.12f price=%.6f reason=positions_aligned', exchange_position.side, exchange_position.qty, exchange_position.entry_price)
    return SyncResult(active_trade, active_notional_usd, active_position_scale, False, False, False, exchange_position)


def sync_bot_with_exchange_position(**kwargs: Any) -> SyncResult:
    return sync_with_exchange_position(**kwargs)


def fetch_open_positions_from_broker(session: Any) -> list[Any]:
    fetcher = getattr(session, 'fetch_open_positions', None)
    if not callable(fetcher):
        return []
    try:
        positions = fetcher()
    except TypeError:
        positions = fetcher(None)
    return list(positions or [])

def count_symbol_open_positions(session: Any, symbol: str) -> int:
    positions = fetch_open_positions_from_broker(session)
    symbol_upper = str(symbol).upper()
    count = 0
    for pos in positions:
        pos_symbol = str(getattr(pos, "symbol", "") or "").upper()
        pos_qty = float(getattr(pos, "qty", 0.0) or 0.0)
        pos_side = str(getattr(pos, "side", "FLAT") or "FLAT").upper()
        if pos_symbol != symbol_upper:
            continue
        if pos_qty > 0.0 and pos_side in {"LONG", "SHORT", "BUY", "SELL"}:
            count += 1
    return count


def perform_hard_position_sync(
    *,
    symbol: str,
    state: SymbolRuntimeState,
    risk_state: RiskState,
    broker_positions: int,
    exchange_position: ExchangePosition | None,
    now: datetime,
) -> None:
    if int(broker_positions) > 0:
        if exchange_position is None or not getattr(exchange_position, "is_open", False):
            logging.warning(
                "HARD SYNC SKIPPED | broker_positions>0 but symbol flat | symbol=%s broker_positions=%d",
                symbol,
                int(broker_positions),
            )
            return
        risk_state.open_positions = int(broker_positions)
        if state.active_trade is None:
            recovered_side = (exchange_position.side if exchange_position is not None else 'LONG')
            recovered_price = float(exchange_position.entry_price) if exchange_position is not None else 0.0
            recovered_qty = float(exchange_position.qty) if exchange_position is not None else 0.0
            if recovered_side not in {'LONG', 'SHORT'}:
                recovered_side = 'LONG'
            if recovered_qty <= 0.0:
                logging.error(
                    "HARD SYNC FAILED | invalid recovered qty | symbol=%s broker_qty=%s",
                    symbol,
                    recovered_qty,
                )
                return
            if recovered_price <= 0.0 and exchange_position is not None:
                broker_raw = exchange_position.raw if isinstance(exchange_position.raw, dict) else {}
                for key in ('mark_price', 'last_price', 'price', 'avg_price', 'current_price'):
                    candidate = broker_raw.get(key)
                    try:
                        candidate_value = float(candidate)
                    except (TypeError, ValueError):
                        continue
                    if candidate_value > 0.0:
                        recovered_price = candidate_value
                        break
            if recovered_price <= 0.0 and isinstance(state.buffer, pd.DataFrame) and 'close' in state.buffer:
                close_series = pd.to_numeric(state.buffer['close'], errors='coerce').dropna()
                if len(close_series) > 0:
                    recovered_price = float(close_series.iloc[-1])
            if recovered_price <= 0.0:
                logging.error(
                    "HARD SYNC FAILED | invalid recovered entry price | symbol=%s broker_price=%s",
                    symbol,
                    recovered_price,
                )
                return
            recovered_trade = compute_exit_plan(
                signal_score=1.0,
                volatility=0.001,
                side=recovered_side,
                entry_price=recovered_price,
                source='recovered_trade',
            )
            recovered_trade.entry_time = now
            recovered_trade.source = 'recovered_trade'
            recovered_trade.recovered_at = now
            recovered_trade.recovered_qty = float(recovered_qty)
            state.active_trade = recovered_trade
            state.active_notional_usd = 0.0
            state.active_position_scale = 1.0
            logging.info(
                "HARD SYNC RECOVERED | symbol=%s side=%s entry_price=%.6f qty=%.6f",
                symbol,
                recovered_side,
                recovered_price,
                recovered_qty,
            )
            logging.warning(
                "HARD SYNC | recovered broker position | symbol=%s side=%s positions=%d",
                symbol,
                recovered_side,
                int(broker_positions),
            )
        return
    risk_state.open_positions = 0
    state.active_trade = None
    state.active_notional_usd = 0.0
    state.active_position_scale = 0.0
    state.no_trade_snapshot_for_active_trade = 0
    state.last_entry_commit_timestamp = 0.0
    state.last_close_timestamp = 0.0
    risk_state.last_entry_time = None
    if hasattr(risk_state, "symbol_last_trade_time") and isinstance(risk_state.symbol_last_trade_time, dict):
        risk_state.symbol_last_trade_time.pop(str(symbol).upper(), None)
    logging.info(
        "HARD SYNC | broker flat confirmed | resetting state | symbol=%s",
        symbol,
    )


def reset_flat_internal_state(
    *,
    symbol: str,
    state: SymbolRuntimeState,
    risk_state: RiskState,
    reason: str,
    now: datetime,
    force_reentry_ready: bool = False,
) -> bool:
    stale_active_trade = state.active_trade is not None
    stale_notional = abs(float(state.active_notional_usd)) > 1e-9
    stale_position_scale = abs(float(state.active_position_scale) - 1.0) > 1e-9
    stale_open_positions = int(getattr(risk_state, 'open_positions', 0)) > 0
    stale_snapshot = int(getattr(state, 'no_trade_snapshot_for_active_trade', 0)) != 0
    stale_same_bar = (not bool(risk_state.same_bar_entry_allowed)) and force_reentry_ready
    has_stale_state = stale_active_trade or stale_notional or stale_position_scale or stale_open_positions or stale_snapshot or stale_same_bar
    if not has_stale_state:
        return False
    logging.warning(
        'FLAT HARD RESET | symbol=%s reason=%s active_trade=%s open_positions=%d same_bar_allowed_before=%s',
        symbol,
        reason,
        str(stale_active_trade).lower(),
        int(getattr(risk_state, 'open_positions', 0)),
        str(bool(risk_state.same_bar_entry_allowed)).lower(),
    )
    state.active_trade = None
    state.active_notional_usd = 0.0
    state.active_position_scale = 1.0
    state.no_trade_snapshot_for_active_trade = 0
    risk_state.open_positions = 0
    state.flat_confirm_loops = 0
    state.blocked_while_flat_loops = 0
    if force_reentry_ready and not bool(risk_state.same_bar_entry_allowed):
        risk_state.same_bar_entry_allowed = True
    state.last_flat_recovery_at = now
    logging.warning(
        'RE-ENTRY READY | symbol=%s broker_flat=true active_trade=false open_positions=0 same_bar_allowed=%s reason=%s',
        symbol,
        str(bool(risk_state.same_bar_entry_allowed)).lower(),
        reason,
    )
    return True


def recover_if_stale_flat_state(
    *,
    symbol: str,
    state: SymbolRuntimeState,
    risk_state: RiskState,
    exchange_position: ExchangePosition,
    now: datetime,
) -> bool:
    if exchange_position.is_open:
        state.flat_confirm_loops = 0
        state.flat_since = None
        state.blocked_while_flat_loops = 0
        return False
    if state.flat_since is None:
        state.flat_since = now
    if (
        state.active_trade is None
        and getattr(risk_state, 'open_positions', 0) == 0
        and not exchange_position.is_open
    ):
        flat_seconds = max(0.0, (now - state.flat_since).total_seconds())
        if (
            flat_seconds >= FLAT_RECOVERY_SAME_BAR_RESET_SECONDS
            and not bool(risk_state.same_bar_entry_allowed)
        ):
            risk_state.same_bar_entry_allowed = True
            logging.warning('SAME BAR RESET | symbol=%s reason=stale_flat_state', symbol)
            logging.warning(
                'RE-ENTRY READY | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=true reason=stale_same_bar_gate_released',
                symbol,
                'false',
                0,
            )
        logging.debug(
            'RECOVERY SKIPPED | symbol=%s state already clean',
            symbol,
        )
        return False
    stale_active_trade = state.active_trade is not None
    stale_open_positions = int(getattr(risk_state, 'open_positions', 0)) > 0
    if stale_active_trade or stale_open_positions:
        state.flat_confirm_loops += 1
    else:
        state.flat_confirm_loops = 0
    flat_seconds = max(0.0, (now - state.flat_since).total_seconds())
    recovered = False
    if state.flat_confirm_loops >= FLAT_RECOVERY_CONFIRM_LOOPS or flat_seconds >= FLAT_RECOVERY_STALE_SECONDS:
        logging.warning(
            'FLAT RECOVERY | symbol=%s reason=broker_flat_internal_stale active_trade=%s open_positions=%d',
            symbol,
            'true' if stale_active_trade else 'false',
            int(getattr(risk_state, 'open_positions', 0)),
        )
        recovered = reset_flat_internal_state(
            symbol=symbol,
            state=state,
            risk_state=risk_state,
            reason='broker_flat_internal_stale',
            now=now,
            force_reentry_ready=True,
        )
    if (
        flat_seconds >= FLAT_RECOVERY_SAME_BAR_RESET_SECONDS
        and not bool(risk_state.same_bar_entry_allowed)
    ):
        risk_state.same_bar_entry_allowed = True
        logging.warning('SAME BAR RESET | symbol=%s reason=stale_flat_state', symbol)
        logging.warning(
            'RE-ENTRY READY | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=true reason=stale_same_bar_gate_released',
            symbol,
            str(state.active_trade is not None).lower(),
            int(getattr(risk_state, 'open_positions', 0)),
        )
    return recovered


def update_no_entry_watchdog(
    *,
    symbol: str,
    state: SymbolRuntimeState,
    risk_state: RiskState,
    broker_flat: bool,
    signal_generated: bool,
    signal_executable: bool,
    cooldown_remaining: int,
    reason: str,
) -> bool:
    if broker_flat and state.active_trade is None and int(risk_state.open_positions) <= 0 and (signal_generated or signal_executable):
        state.blocked_while_flat_loops += 1
        if state.blocked_while_flat_loops >= NO_ENTRY_WATCHDOG_LOOPS:
            logging.warning(
                'NO ENTRY WATCHDOG | symbol=%s broker_flat=true internal_flat=true loops=%d generated=%s executable=%s cooldown=%d reason=%s',
                symbol,
                state.blocked_while_flat_loops,
                str(signal_generated).lower(),
                str(signal_executable).lower(),
                int(cooldown_remaining),
                reason,
            )
            return True
        return False
    state.blocked_while_flat_loops = 0
    return False


def close_bybit_position(session: BybitHTTPProtocol, symbol: str, side: str, qty: float, position_idx: int | None = None) -> LiveOrderResult:
    if not validate_order_qty(qty):
        logging.critical('POSITION CLOSE BLOCKED | symbol=%s qty=%.12f reason=invalid_qty', symbol, qty)
        return LiveOrderResult(False, None, str(side).upper(), float(qty), None, {'validation': 'close_qty_must_be_positive'}, 'close_qty_must_be_positive')
    resolved_position_idx = position_idx
    if isinstance(session, BrokerAdapter) and resolved_position_idx is None:
        current_position = session.fetch_open_position(symbol)
        if current_position.broker_id is not None:
            resolved_position_idx = int(current_position.broker_id)
    close_side = 'SHORT' if str(side).upper() == 'LONG' else 'LONG'
    logging.info('POSITION CLOSE SUBMIT | symbol=%s side=%s qty=%.12f position=%s', symbol, close_side, qty, resolved_position_idx)
    result = execute_bybit_market_order(session, symbol, close_side, qty, reduce_only=True, position_idx=resolved_position_idx)
    if not isinstance(session, BrokerAdapter):
        return result
    try:
        remaining_position = fetch_open_position(session, symbol)
    except Exception as exc:
        logging.critical('POSITION CLOSE VERIFY FAILED | symbol=%s reason=%s', symbol, exc)
        return LiveOrderResult(False, result.order_id, str(side).upper(), float(qty), result.avg_price, {**result.raw_response, 'close_verify_exception': str(exc)}, 'close_verify_failed')
    if remaining_position.is_open:
        if remaining_position.side == str(side).upper() and remaining_position.qty < float(qty):
            logging.warning(
                'POSITION CLOSE PARTIAL | symbol=%s side=%s requested_qty=%.12f remaining_qty=%.12f position=%s',
                symbol,
                side,
                qty,
                remaining_position.qty,
                remaining_position.position_idx,
            )
            return LiveOrderResult(
                False,
                result.order_id,
                str(side).upper(),
                float(qty),
                result.avg_price,
                {
                    **result.raw_response,
                    'remaining_position': remaining_position.raw,
                    'remaining_qty': remaining_position.qty,
                    'remaining_side': remaining_position.side,
                },
                'close_position_partial',
            )
        logging.critical('POSITION CLOSE AMBIGUOUS | symbol=%s side=%s qty=%.12f remaining_side=%s remaining_qty=%.12f position=%s', symbol, side, qty, remaining_position.side, remaining_position.qty, remaining_position.position_idx)
        return LiveOrderResult(False, result.order_id, str(side).upper(), float(qty), result.avg_price, {**result.raw_response, 'remaining_position': remaining_position.raw, 'remaining_qty': remaining_position.qty, 'remaining_side': remaining_position.side}, 'close_position_still_open')
    if result.success:
        logging.info('POSITION CLOSE VERIFIED | symbol=%s requested_side=%s qty=%.12f', symbol, side, qty)
        return result
    logging.info('POSITION CLOSE CONFIRMED | symbol=%s requested_side=%s qty=%.12f reason=position_flat_after_failure', symbol, side, qty)
    return LiveOrderResult(True, result.order_id, str(side).upper(), float(qty), result.avg_price, {**result.raw_response, 'close_recheck': 'position_flat_after_failure'}, None)


def ensure_exchange_protection(*, session: BybitHTTPProtocol, symbol: str, trade: ActiveTrade, exchange_position: ExchangePosition, specs: SymbolSpecs, safety_controller: LiveSafetyController, trailing_policy: TrailingPolicy | None = None) -> bool:
    trailing_policy = trailing_policy or TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False)
    entry_price = exchange_position.entry_price if exchange_position.entry_price > 0 else trade.entry_price
    if not is_valid_trade_state(trade):
        safety_controller.activate_kill_switch('invalid_trade_state', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else None, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    if trade.side == 'LONG':
        tp_price = entry_price * (1.0 + trade.tp_pct)
        sl_price = entry_price * (1.0 - trade.sl_pct)
        trailing_active_price = entry_price * (1.0 + trade.trailing_activation_pct) if trailing_policy.exchange_native else None
    else:
        tp_price = entry_price * (1.0 - trade.tp_pct)
        sl_price = entry_price * (1.0 + trade.sl_pct)
        trailing_active_price = entry_price * (1.0 - trade.trailing_activation_pct) if trailing_policy.exchange_native else None
    trailing_distance = (entry_price * trade.trailing_offset_pct) if trailing_policy.exchange_native else None
    if trailing_policy.exchange_native and (trailing_distance is None or trailing_distance <= 0):
        logging.critical('PROTECTION VALIDATION FAILED | side=%s qty=%.12f price=%.6f reason=invalid_exchange_trailing_distance', trade.side, exchange_position.qty, entry_price)
        safety_controller.register_protection_failure('invalid_exchange_trailing_distance')
        safety_controller.activate_kill_switch('invalid_exchange_trailing_distance', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    if tp_price <= 0 or sl_price <= 0:
        logging.critical('PROTECTION VALIDATION FAILED | side=%s qty=%.12f price=%.6f reason=non_positive_tp_sl', trade.side, exchange_position.qty, entry_price)
        safety_controller.activate_kill_switch('tp_sl_invalid_values', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    if trade.side == 'LONG' and not (sl_price < entry_price < tp_price):
        logging.critical('PROTECTION VALIDATION FAILED | side=%s qty=%.12f price=%.6f reason=inconsistent_long_tp_sl', trade.side, exchange_position.qty, entry_price)
        safety_controller.activate_kill_switch('tp_sl_inconsistent', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    if trade.side == 'SHORT' and not (tp_price < entry_price < sl_price):
        logging.critical('PROTECTION VALIDATION FAILED | side=%s qty=%.12f price=%.6f reason=inconsistent_short_tp_sl', trade.side, exchange_position.qty, entry_price)
        safety_controller.activate_kill_switch('tp_sl_inconsistent', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    if exchange_position.take_profit and exchange_position.stop_loss and (not trailing_policy.exchange_native or trailing_distance is None):
        already_verified = _protection_matches_expected(
            exchange_position=exchange_position,
            trade=trade,
            expected_tp=tp_price,
            expected_sl=sl_price,
            tick_size=specs.tick_size,
        )
        logging.info('PROTECTION VERIFY | symbol=%s side=%s existing_result=%s', symbol, trade.side, str(already_verified).lower())
        if already_verified:
            safety_controller.clear_protection_failures()
            return True
    protection_ok = set_bybit_position_protection(
        session,
        symbol,
        trade.side,
        tp_price,
        sl_price,
        trailing_stop=trailing_distance,
        trailing_active_price=trailing_active_price,
        tick_size=specs.tick_size,
    )
    if not protection_ok:
        safety_controller.register_protection_failure('tp_sl_submit_failed')
        safety_controller.activate_kill_switch('tp_sl_failed', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    try:
        verified_position = fetch_open_position(session, symbol)
    except Exception as exc:
        logging.critical('PROTECTION VERIFY FAILED | symbol=%s reason=%s', symbol, exc)
        safety_controller.register_protection_failure(f'protection_verify_fetch_failed:{exc}')
        safety_controller.activate_kill_switch('tp_sl_verify_failed', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    protection_verified = _protection_matches_expected(
        exchange_position=verified_position,
        trade=trade,
        expected_tp=tp_price,
        expected_sl=sl_price,
        tick_size=specs.tick_size,
    )
    logging.info(
        'PROTECTION VERIFY | symbol=%s side=%s tp=%s sl=%s result=%s',
        symbol,
        trade.side,
        verified_position.take_profit,
        verified_position.stop_loss,
        str(protection_verified).lower(),
    )
    if not protection_verified:
        safety_controller.register_protection_failure('tp_sl_verify_mismatch')
        safety_controller.activate_kill_switch('tp_sl_verify_mismatch', session=session, symbol=symbol, side=exchange_position.side if exchange_position.is_open else trade.side, qty=exchange_position.qty if exchange_position.qty > 0 else None, position_idx=exchange_position.position_idx)
        return False
    safety_controller.clear_protection_failures()
    return protection_ok


def can_open_live_entry(safety_controller: LiveSafetyController) -> bool:
    return not safety_controller.live_trading_halted


def execute_live_entry_flow(*, session: BybitHTTPProtocol, symbol: str, trade: ActiveTrade, position_value_usd: float, position_scale: float, specs: SymbolSpecs, risk_state: RiskState, safety_controller: LiveSafetyController, trailing_policy: TrailingPolicy, latest_bar_id: str | None = None, risk_per_trade_usd: float | None = None, risk_pct: float | None = None, account_equity_usd: float | None = None, use_fx_risk_model: bool | None = None) -> tuple[LiveOrderResult | None, ExchangePosition | None, float | None]:
    if not can_open_live_entry(safety_controller):
        logging.critical('LIVE ENTRY BLOCKED | side=%s qty=none price=%s reason=kill_switch_active', getattr(trade, 'side', 'UNKNOWN'), getattr(trade, 'entry_price', None))
        return None, None, None
    if not is_valid_trade_state(trade):
        safety_controller.activate_kill_switch('invalid_trade_state')
        return None, None, None
    pre_entry_sync = sync_with_exchange_position(
        session=session,
        symbol=symbol,
        active_trade=None,
        active_notional_usd=0.0,
        active_position_scale=1.0,
        risk_state=replace(risk_state, open_positions=0),
        safety_controller=safety_controller,
    )
    if pre_entry_sync.safe_mode_triggered or pre_entry_sync.exchange_position.is_open:
        reason = 'pre_entry_sync_halt' if pre_entry_sync.safe_mode_triggered else 'entry_blocked_exchange_not_flat'
        safety_controller.activate_kill_switch(reason, session=session, symbol=symbol, side=pre_entry_sync.exchange_position.side if pre_entry_sync.exchange_position.is_open else None, qty=pre_entry_sync.exchange_position.qty if pre_entry_sync.exchange_position.qty > 0 else None, position_idx=pre_entry_sync.exchange_position.position_idx)
        return None, None, None
    if not enforce_live_spread_gate(session=session, symbol=symbol, trade=trade, specs=specs, max_spread_ratio=0.003, safety_controller=safety_controller):
        return None, None, None
    is_adapter_session = isinstance(session, BrokerAdapter)
    resolved_account_equity = _safe_positive_float(account_equity_usd)
    if resolved_account_equity is None and is_adapter_session:
        try:
            resolved_account_equity = _safe_positive_float(resolve_account_equity(session, float(account_equity_usd or 0.0)).equity)
        except Exception:
            resolved_account_equity = None
    effective_account_equity_usd = float(resolved_account_equity or 0.0)
    use_fx_risk_model = _env_flag('USE_FX_RISK_MODEL', False) if use_fx_risk_model is None else bool(use_fx_risk_model)
    if use_fx_risk_model:
        stop_loss_price = float(trade.entry_price) * (1.0 - float(trade.sl_pct)) if str(trade.side).upper() == 'LONG' else float(trade.entry_price) * (1.0 + float(trade.sl_pct))
        effective_risk_budget = float(risk_per_trade_usd or 0.0)
        if effective_risk_budget <= 0 and risk_pct is not None and risk_pct > 0:
            effective_risk_budget = float(effective_account_equity_usd or 0.0) * float(risk_pct)
        pip_size = float(specs.pip_size or (0.01 if int(specs.digits) <= 3 else 0.0001))
        stop_distance = abs(float(trade.entry_price) - float(stop_loss_price))
        stop_pips = stop_distance / pip_size if pip_size > 0 else 0.0
        try:
            pip_value_per_lot = resolve_fx_pip_value_per_lot(specs=specs, entry_price=float(trade.entry_price))
        except Exception:
            pip_value_per_lot = 0.0
        raw_lot_size = (effective_risk_budget / (stop_pips * pip_value_per_lot)) if stop_pips > 0 and pip_value_per_lot > 0 else 0.0
        raw_lot_size_before_floor = float(raw_lot_size)
        broker_min_lot = float(specs.minimum_size)
        lot_step = float(specs.size_step)
        if raw_lot_size < broker_min_lot:
            logging.warning("FX RISK BOOST | forcing minimum tradable lot")
            raw_lot_size = broker_min_lot
        rounded_lot_size = compute_safe_fx_lot(
            raw_lot=raw_lot_size,
            min_lot=broker_min_lot,
            lot_step=lot_step,
        )
        logging.info(
            "FX LOT FIX | raw=%.5f adjusted=%.5f min=%.5f step=%.5f",
            raw_lot_size,
            rounded_lot_size,
            broker_min_lot,
            lot_step,
        )
        adjusted_risk_per_trade_usd = risk_per_trade_usd
        if stop_pips > 0 and pip_value_per_lot > 0 and raw_lot_size_before_floor < float(specs.minimum_size):
            required_risk = float(specs.minimum_size) * stop_pips * pip_value_per_lot
            max_risk_cap = float(os.getenv('MAX_RISK_PER_TRADE_USD', '10000') or 10000.0)
            clamped_required_risk = max(0.0, min(required_risk, max_risk_cap))
            old_risk = effective_risk_budget
            if clamped_required_risk > old_risk:
                adjusted_risk_per_trade_usd = clamped_required_risk
                effective_risk_budget = clamped_required_risk
                raw_lot_size = effective_risk_budget / (stop_pips * pip_value_per_lot)
                if raw_lot_size < broker_min_lot:
                    logging.warning("FX RISK BOOST | forcing minimum tradable lot")
                    raw_lot_size = broker_min_lot
                rounded_lot_size = compute_safe_fx_lot(
                    raw_lot=raw_lot_size,
                    min_lot=broker_min_lot,
                    lot_step=lot_step,
                )
                logging.info(
                    "FX LOT FIX | raw=%.5f adjusted=%.5f min=%.5f step=%.5f",
                    raw_lot_size,
                    rounded_lot_size,
                    broker_min_lot,
                    lot_step,
                )
                logging.info(
                    'PRE-ADJUST LOT | old_risk=%.4f new_risk=%.4f required_risk=%.4f raw_lot=%.6f min_lot=%.6f',
                    old_risk,
                    adjusted_risk_per_trade_usd,
                    required_risk,
                    raw_lot_size,
                    float(specs.minimum_size),
                )
        try:
            safe_qty = calculate_fx_lot_size(
                risk_per_trade_usd=adjusted_risk_per_trade_usd,
                risk_pct=risk_pct,
                account_equity_usd=float(effective_account_equity_usd or 0.0),
                entry_price=float(trade.entry_price),
                stop_loss_price=stop_loss_price,
                specs=specs,
            )
        except Exception as exc:
            error_text = str(exc)
            logging.critical(
                'LIVE ENTRY BLOCKED | side=%s price=%.6f reason=fx_risk_sizing_failed error=%s risk_per_trade_usd=%s risk_pct=%s account_equity_usd=%s',
                trade.side,
                float(trade.entry_price),
                exc,
                'none' if adjusted_risk_per_trade_usd is None else f'{float(adjusted_risk_per_trade_usd):.4f}',
                'none' if risk_pct is None else f'{float(risk_pct):.6f}',
                'none' if effective_account_equity_usd <= 0 else f'{float(effective_account_equity_usd):.4f}',
            )
            safety_controller.register_execution_failure(f'fx_risk_sizing_failed:{exc}')
            return None, None, None
        logging.info(
            'QTY CALC | mode=fx_risk risk_per_trade_usd=%s risk_pct=%s account_equity_usd=%s entry_price=%.6f stop_loss_price=%.6f rounded_qty=%.12f',
            'none' if adjusted_risk_per_trade_usd is None else f'{float(adjusted_risk_per_trade_usd):.4f}',
            'none' if risk_pct is None else f'{float(risk_pct):.6f}',
            'none' if effective_account_equity_usd <= 0 else f'{float(effective_account_equity_usd):.4f}',
            float(trade.entry_price),
            stop_loss_price,
            safe_qty,
        )
    else:
        safe_qty = calc_order_qty(position_value_usd, 1.0, float(trade.entry_price), specs.qty_step, specs.min_qty)
    spec_min_lot = float(getattr(specs, 'minimum_size', getattr(specs, 'min_qty', 0.0)) or 0.0)
    spec_lot_step = float(getattr(specs, 'size_step', getattr(specs, 'qty_step', 0.0)) or 0.0)
    spec_max_lot = float(getattr(specs, 'max_lot', 0.0) or 0.0)
    min_lot = spec_min_lot if spec_min_lot > 0 else 0.01
    lot_step = spec_lot_step if spec_lot_step > 0 else min_lot
    max_lot = spec_max_lot if spec_max_lot > 0 else 0.0
    if safe_qty is None or safe_qty <= 0:
        logging.error("ENTRY BLOCKED | qty_non_positive")
        return None, None, 0.0

    safe_qty = float(safe_qty)
    if max_lot > 0:
        safe_qty = min(float(safe_qty), float(max_lot))
    safe_qty = _round_down_to_step(float(safe_qty), float(lot_step)) if lot_step > 0 else float(safe_qty)
    if safe_qty < min_lot:
        logging.error(
            "ENTRY BLOCKED | qty_below_min_lot qty=%.6f min_lot=%.6f",
            float(safe_qty),
            float(min_lot),
        )
        return None, None, 0.0
    if not validate_order_qty(safe_qty):
        safety_controller.register_execution_failure('entry_qty_invalid')
        logging.warning('ENTRY BLOCKED | symbol=%s side=%s reason=entry_qty_invalid qty=%.12f', symbol, trade.side, float(safe_qty))
        return None, None, 0.0
    position_value_for_limits = float(position_value_usd)
    if is_adapter_session and str(getattr(specs, 'category', '') or '').lower() == 'fx':
        position_value_for_limits = 0.0
    if not safety_controller.enforce_position_limits(position_value_usd=position_value_for_limits, qty=safe_qty):
        return None, None, None
    order_link_id = _build_order_link_id(symbol, trade, latest_bar_id)
    if safety_controller.last_order_link_id == order_link_id or _has_duplicate_open_order(session, symbol=symbol, side=trade.side, order_link_id=order_link_id):
        logging.critical('DUPLICATE ORDER ATTEMPT | symbol=%s side=%s order_link_id=%s latest_bar_id=%s', symbol, trade.side, order_link_id, latest_bar_id)
        safety_controller.register_execution_failure(f'duplicate_order_attempt:{order_link_id}')
        safety_controller.activate_kill_switch(f'duplicate_order_attempt:{order_link_id}')
        return None, None, None
    leverage = float(os.getenv("ACCOUNT_LEVERAGE", "100"))
    margin_limits_active = False
    if is_adapter_session:
        mt5_contract_size = float(getattr(specs, "contract_size", 0.0) or 0.0)
        contract_size = resolve_contract_size(symbol, specs)
        if mt5_contract_size <= 0:
            logging.warning("MT5 CONTRACT SIZE UNKNOWN | fallback used")
        if contract_size <= 0:
            logging.warning(
                "INVALID CONTRACT SIZE | forcing fallback=100000 | symbol=%s",
                symbol,
            )
            contract_size = 100000.0
        logging.info(
            "BROKER CHECK | symbol=%s mt5_contract_size=%.2f resolved_contract_size=%.2f leverage=%.2f min_lot=%.4f max_lot=%.4f lot_step=%.4f",
            symbol,
            mt5_contract_size,
            contract_size,
            leverage,
            float(min_lot),
            float(max_lot),
            float(lot_step),
        )
        equity_usd = float(effective_account_equity_usd or 0.0)
        entry_price_live = float(trade.entry_price)
        if equity_usd > 0:
            margin_limits_active = True
            max_lot_from_margin = _max_lot_by_margin(
                symbol=symbol,
                equity_usd=float(equity_usd),
                entry_price=float(entry_price_live),
                leverage=float(leverage),
                contract_size=float(contract_size),
                min_lot=float(min_lot),
                lot_step=float(lot_step),
                max_lot=float(max_lot if max_lot > 0 else max(float(safe_qty), float(min_lot))),
                margin_buffer=_safe_float(os.getenv("MARGIN_BUFFER_RATIO", "0.85"), 0.85),
            )
            logging.info(
                "MARGIN MODEL | equity=%.2f leverage=%.2f entry_price=%.5f contract_size=%.2f max_lot=%.4f",
                equity_usd,
                leverage,
                entry_price_live,
                contract_size,
                max_lot_from_margin,
            )
            if max_lot_from_margin <= 0:
                logging.error("MARGIN FAILURE | computed max_lot=0 -> BLOCK")
                return None, None, 0.0
            if max_lot_from_margin < min_lot:
                logging.warning(
                    "ENTRY BLOCKED | margin_below_min_lot max_lot=%.4f min_lot=%.4f",
                    max_lot_from_margin,
                    min_lot,
                )
                return None, None, 0.0
            if lot_step > 0:
                safe_qty = math.floor(min(float(safe_qty), float(max_lot_from_margin)) / float(lot_step)) * float(lot_step)
            else:
                safe_qty = min(float(safe_qty), float(max_lot_from_margin))
            safe_qty = round(max(float(safe_qty), 0.0), 8)
            logging.info(
                "FINAL QTY CHECK | symbol=%s qty=%.8f min_lot=%.8f max_allowed=%.8f",
                symbol,
                safe_qty,
                min_lot,
                max_lot_from_margin,
            )
            logging.info(
                "QTY AFTER MARGIN | symbol=%s qty=%.8f max_lot_by_margin=%.8f min_lot=%.8f",
                symbol,
                safe_qty,
                max_lot_from_margin,
                min_lot,
            )
            if safe_qty < min_lot:
                logging.warning(
                    "ENTRY BLOCKED | qty_below_min_after_margin qty=%.8f min_lot=%.8f",
                    safe_qty,
                    min_lot,
                )
                return None, None, 0.0
        else:
            logging.info("MARGIN MODEL | skipped=true reason=equity_unknown")
    side_upper = str(trade.side).upper()
    entry_price_live = float(trade.entry_price)
    stop_loss_price = entry_price_live * (1.0 - float(trade.sl_pct)) if side_upper == 'LONG' else entry_price_live * (1.0 + float(trade.sl_pct))
    take_profit_pct = float(getattr(trade, 'tp_pct', 0.0) or 0.0)
    take_profit_price: float | None = None
    if take_profit_pct > 0:
        take_profit_price = entry_price_live * (1.0 + take_profit_pct) if side_upper == 'LONG' else entry_price_live * (1.0 - take_profit_pct)
    protection_ok, protection_reason = _validate_protective_prices(
        side=side_upper,
        entry_price=entry_price_live,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
    )
    if not protection_ok:
        diagnostics: dict[str, float | str] = {
            'qty': float(safe_qty),
            'entry_price': float(entry_price_live),
            'stop_loss_price': float(stop_loss_price),
            'take_profit_price': float(take_profit_price) if take_profit_price is not None else 'none',
        }
        logging.warning(
            "ENTRY PREFLIGHT BLOCKED | symbol=%s side=%s reason=%s diagnostics=%s",
            symbol,
            side_upper,
            protection_reason,
            diagnostics,
        )
        return None, None, 0.0
    preflight_ok, preflight_reason, preflight_diagnostics = mt5_preflight_check(
        session=session,
        symbol=symbol,
        side=side_upper,
        qty=float(safe_qty),
        entry_price=entry_price_live,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        specs=specs,
        account_equity_usd=effective_account_equity_usd,
    )
    logging.info(
        "PREFLIGHT CHECK | symbol=%s side=%s qty=%.4f ok=%s reason=%s",
        symbol,
        side_upper,
        float(safe_qty),
        str(preflight_ok).lower(),
        preflight_reason,
    )
    if not preflight_ok:
        logging.warning(
            "ENTRY PREFLIGHT BLOCKED | symbol=%s side=%s reason=%s diagnostics=%s",
            symbol,
            side_upper,
            preflight_reason,
            preflight_diagnostics,
        )
        return None, None, 0.0
    entry_metrics = compute_live_position_metrics(
        symbol=symbol,
        qty=float(safe_qty),
        entry_price=float(entry_price_live),
        specs=specs,
        leverage=leverage,
    )
    notional_value = float(entry_metrics.notional_value_usd)
    required_margin = float(entry_metrics.required_margin_usd)
    logging.info(
        "LIVE METRICS | symbol=%s qty=%.4f entry=%.6f contract_size=%.2f notional=%.2f margin=%.2f",
        symbol,
        float(entry_metrics.qty),
        float(entry_metrics.entry_price),
        float(entry_metrics.contract_size),
        float(entry_metrics.notional_value_usd),
        float(entry_metrics.required_margin_usd),
    )
    logging.info(
        "MT5 RAW SPECS | contract_size=%.2f tick_size=%.6f tick_value=%.6f min_lot=%.4f",
        float(getattr(specs, "contract_size", 0.0) or 0.0),
        float(getattr(specs, "tick_size", 0.0) or 0.0),
        float(getattr(specs, "tick_value", 0.0) or 0.0),
        float(getattr(specs, "min_qty", 0.0) or 0.0),
    )
    if margin_limits_active:
        margin_buffer = _safe_float(os.getenv("MARGIN_BUFFER_RATIO", "0.85"), 0.85)
        max_allowed_margin = float(effective_account_equity_usd or 0.0) * float(margin_buffer)
        if required_margin > max_allowed_margin:
            scale_factor = max_allowed_margin / max(required_margin, 1e-9)
            new_qty = float(safe_qty) * float(scale_factor)
            new_qty = max(float(min_lot), round(new_qty / float(lot_step)) * float(lot_step)) if float(lot_step) > 0 else max(float(min_lot), float(new_qty))
            logging.warning(
                "AUTO SIZE REDUCE | old_qty=%.4f new_qty=%.4f required_margin=%.2f allowed=%.2f",
                float(safe_qty),
                float(new_qty),
                required_margin,
                max_allowed_margin,
            )
            safe_qty = float(new_qty)
            entry_metrics = compute_live_position_metrics(
                symbol=symbol,
                qty=float(safe_qty),
                entry_price=float(entry_price_live),
                specs=specs,
                leverage=leverage,
            )
            notional_value = float(entry_metrics.notional_value_usd)
            required_margin = float(entry_metrics.required_margin_usd)
        if required_margin > max_allowed_margin:
            logging.warning(
                "ENTRY BLOCKED | insufficient margin required=%.2f equity=%.2f allowed=%.2f notional=%.2f",
                required_margin,
                float(effective_account_equity_usd or 0.0),
                max_allowed_margin,
                notional_value,
            )
            return None, None, 0.0
    order_result = execute_bybit_market_order(session, symbol, trade.side, safe_qty, order_link_id=order_link_id, safety_controller=safety_controller)
    if not order_result.success:
        raw_reason = order_result.reason or "broker_rejected"
        classified_reason = classify_execution_failure(raw_reason)
        safety_controller.register_execution_failure(classified_reason)
        logging.error("ENTRY FAILED CLASSIFIED | class=%s raw_reason=%s", classified_reason, raw_reason)
        logging.error(
            "ENTRY FAILED | symbol=%s side=%s qty=%.4f reason=%s",
            symbol,
            trade.side,
            safe_qty,
            raw_reason,
        )
        logging.warning(
            'EXECUTION ABORT CLEAN | symbol=%s reason=%s broker_flat=%s',
            symbol,
            classified_reason,
            'true',
        )
        return order_result, None, safe_qty
    confirmed_position = confirm_position_open(session, symbol)
    if confirmed_position:
        safety_controller.register_execution_success()
    else:
        logging.warning('PENDING CONFIRMATION | symbol=%s', symbol)
        position_retry = confirm_position_open(session, symbol, timeout=1.5)
        if position_retry:
            confirmed_position = position_retry
            safety_controller.register_execution_success()
        else:
            safety_controller.register_unresolved_confirmation('missing_confirmation')
            logging.warning('UNRESOLVED CONFIRMATION | symbol=%s', symbol)
            clean_abort_requested = 'clean' in str(latest_bar_id or '').lower()
            logging.warning(
                'EXECUTION ABORT CLEAN | symbol=%s reason=%s broker_flat=%s',
                symbol,
                'missing_confirmation',
                'true',
            )
            if clean_abort_requested:
                return order_result, None, safe_qty
            safety_controller.activate_kill_switch('missing_confirmation', session=session, symbol=symbol, side=trade.side, qty=safe_qty)
            return order_result, ExchangePosition(symbol=symbol, side='FLAT', qty=0.0, entry_price=0.0, raw={'reason': 'missing_confirmation'}), safe_qty
    logging.info('EXECUTION CONFIRMED | symbol=%s', symbol)
    if order_result.avg_price is None or order_result.avg_price <= 0:
        if confirmed_position.entry_price > 0:
            order_result.avg_price = float(confirmed_position.entry_price)
        else:
            order_result.avg_price = float(trade.entry_price)
    if not order_result.order_id:
        order_result.order_id = str(confirmed_position.position_idx) if confirmed_position.position_idx is not None else f'{symbol}-confirmed'
    safety_controller.last_order_link_id = order_link_id
    safety_controller.register_execution_success()
    trade.entry_price = float(order_result.avg_price)
    confirmed_metrics = compute_live_position_metrics(
        symbol=symbol,
        qty=float(confirmed_position.qty),
        entry_price=float(confirmed_position.entry_price),
        specs=specs,
        leverage=leverage,
    )
    logging.info(
        "COMMIT METRICS | symbol=%s qty=%.4f notional=%.2f",
        symbol,
        float(confirmed_metrics.qty),
        float(confirmed_metrics.notional_value_usd),
    )
    post_entry_sync = sync_with_exchange_position(
        session=session,
        symbol=symbol,
        active_trade=trade,
        active_notional_usd=float(confirmed_metrics.notional_value_usd),
        active_position_scale=position_scale,
        risk_state=replace(risk_state, open_positions=1),
        safety_controller=safety_controller,
        specs=specs,
        leverage=leverage,
    )
    if post_entry_sync.safe_mode_triggered or not post_entry_sync.exchange_position.is_open:
        safety_controller.register_execution_failure('post_entry_sync_failed')
        safety_controller.activate_kill_switch('post_entry_sync_failed', session=session, symbol=symbol, side=trade.side, qty=safe_qty, position_idx=post_entry_sync.exchange_position.position_idx)
        logging.warning(
            'EXECUTION ABORT CLEAN | symbol=%s reason=%s broker_flat=%s',
            symbol,
            'post_entry_sync_failed',
            str(not post_entry_sync.exchange_position.is_open).lower(),
        )
        return order_result, post_entry_sync.exchange_position, safe_qty
    if not ensure_exchange_protection(
        session=session,
        symbol=symbol,
        trade=trade,
        exchange_position=post_entry_sync.exchange_position,
        specs=specs,
        safety_controller=safety_controller,
        trailing_policy=trailing_policy,
    ):
        return order_result, post_entry_sync.exchange_position, safe_qty
    logging.info('LIVE ENTRY | side=%s qty=%.12f price=%.6f reason=exchange_confirmed', trade.side, post_entry_sync.exchange_position.qty, trade.entry_price)
    return order_result, post_entry_sync.exchange_position, safe_qty


@dataclass(frozen=True)
class ExecutionDecision:
    should_execute: bool
    position_scale: float
    adjusted_score: float
    dynamic_profit_floor: float
    reason: str
    execution_probability: float = 0.0
    size_tier: str | None = None
    post_exit_guard_blocked: bool = False
    low_volume_blocked: bool = False
    reason_code: str = 'decision_ok'
    outcome: str = 'ENTRY_APPROVED'
    hard_block: bool = False
    soft_penalties: tuple[str, ...] = ()
    layer_trace: dict[str, Any] | None = None


def _classify_exit_tier(signal_score: float) -> str:
    if signal_score < 1.0:
        return EXIT_TIER_WEAK
    if signal_score < 1.25:
        return EXIT_TIER_MEDIUM
    if signal_score < 1.5:
        return EXIT_TIER_HIGH
    return EXIT_TIER_ELITE


def _tier_is_strong(exit_tier: str) -> bool:
    return exit_tier in {EXIT_TIER_HIGH, EXIT_TIER_ELITE}


def _signed_progress_ratio(trade: ActiveTrade, *, high_price: float, low_price: float, close_price: float) -> float:
    if trade.side == 'LONG':
        favorable_price = max(high_price, close_price)
        return (favorable_price / max(trade.entry_price, 1e-9)) - 1.0
    favorable_price = min(low_price, close_price)
    return (trade.entry_price / max(favorable_price, 1e-9)) - 1.0


def _safe_pct(value: float, minimum: float, maximum: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(minimum)
    if not np.isfinite(numeric):
        return float(minimum)
    return float(_clamp(numeric, minimum, maximum))


def compute_exit_plan(signal_score: float, volatility: float, side: str, entry_price: float, source: str) -> ActiveTrade:
    vol = float(volatility)

    exit_tier = _classify_exit_tier(signal_score)
    vol_factor = _clamp(volatility / 0.0025, 0.75, 1.5)
    tier_profiles = {
        EXIT_TIER_WEAK: {
            'tp_pct': 0.0012,
            'sl_pct': 0.00075,
            'trailing_activation_pct': 0.00075,
            'trailing_offset_pct': 0.00045,
            'max_hold_seconds': 35.0,
            'flat_exit_threshold_pct': 0.00018,
            'no_follow_through_min_bars': 1,
            'no_follow_through_min_seconds': 5.0,
            'no_follow_through_progress_pct': 0.00022,
            'winner_extension_profit_pct': 0.0,
            'winner_extension_seconds': 0.0,
        },
        EXIT_TIER_MEDIUM: {
            'tp_pct': 0.0015,
            'sl_pct': 0.00085,
            'trailing_activation_pct': 0.00095,
            'trailing_offset_pct': 0.00055,
            'max_hold_seconds': 55.0,
            'flat_exit_threshold_pct': 0.00022,
            'no_follow_through_min_bars': 2,
            'no_follow_through_min_seconds': 10.0,
            'no_follow_through_progress_pct': 0.00032,
            'winner_extension_profit_pct': 0.0,
            'winner_extension_seconds': 0.0,
        },
        EXIT_TIER_HIGH: {
            'tp_pct': 0.0019,
            'sl_pct': 0.00095,
            'trailing_activation_pct': 0.00115,
            'trailing_offset_pct': 0.0007,
            'max_hold_seconds': 75.0,
            'flat_exit_threshold_pct': 0.00026,
            'no_follow_through_min_bars': 2,
            'no_follow_through_min_seconds': 12.0,
            'no_follow_through_progress_pct': 0.00038,
            'winner_extension_profit_pct': 0.0010,
            'winner_extension_seconds': 20.0,
        },
        EXIT_TIER_ELITE: {
            'tp_pct': 0.0023,
            'sl_pct': 0.00105,
            'trailing_activation_pct': 0.0013,
            'trailing_offset_pct': 0.0008,
            'max_hold_seconds': 95.0,
            'flat_exit_threshold_pct': 0.0003,
            'no_follow_through_min_bars': 3,
            'no_follow_through_min_seconds': 15.0,
            'no_follow_through_progress_pct': 0.00045,
            'winner_extension_profit_pct': 0.00125,
            'winner_extension_seconds': 30.0,
        },
    }
    profile = tier_profiles[exit_tier]
    tp_scale = 0.88 + (0.18 * vol_factor)
    trailing_scale = 0.92 + (0.16 * vol_factor)
    tp_pct = _safe_pct(profile['tp_pct'] * tp_scale, 0.0007, 0.0062)
    tp_upper_bound = 0.0038
    sl_pct = _clamp(profile['sl_pct'] * (0.9 + (0.15 * vol_factor)), 0.00055, 0.0018)
    trailing_activation_pct = _safe_pct(profile['trailing_activation_pct'] * trailing_scale, 0.0003, 0.0035)
    trailing_offset_pct = _safe_pct(profile['trailing_offset_pct'] * trailing_scale, 0.00016, 0.0025)
    max_hold_seconds = _clamp(profile['max_hold_seconds'] * (0.9 + (0.2 * vol_factor)), 25.0, 150.0)
    flat_exit_threshold_pct = _clamp(profile['flat_exit_threshold_pct'] * (0.9 + (0.2 * vol_factor)), 0.00012, 0.00055)
    no_follow_progress_pct = _clamp(profile['no_follow_through_progress_pct'] * (0.9 + (0.15 * vol_factor)), 0.00012, 0.00075)
    no_follow_min_seconds = float(profile['no_follow_through_min_seconds'])
    no_follow_min_bars = int(profile['no_follow_through_min_bars'])
    winner_extension_profit_pct = float(profile['winner_extension_profit_pct'])
    winner_extension_seconds = float(profile['winner_extension_seconds'])
    break_even_trigger_pct = 0.0007
    fast_spike_trigger_pct = 0.00095

    if signal_score < 0.75:
        no_follow_min_seconds = _clamp(min(no_follow_min_seconds, 8.0), 3.0, 20.0)
        no_follow_min_bars = int(_clamp(min(float(no_follow_min_bars), 2.0), 1.0, 4.0))
        no_follow_progress_pct = _safe_pct(no_follow_progress_pct * 0.8, 0.0001, 0.0009)
        break_even_trigger_pct = 0.0005
        fast_spike_trigger_pct = 0.0006
        logging.info(
            'PNL ENGINE | weak_follow_through_exit_tuned score=%.4f seconds=%.1f bars=%d progress=%.5f',
            signal_score,
            no_follow_min_seconds,
            no_follow_min_bars,
            no_follow_progress_pct,
        )
    elif signal_score >= 1.1:
        no_follow_min_seconds = _clamp(no_follow_min_seconds * 1.2, 5.0, 30.0)
        no_follow_progress_pct = _safe_pct(no_follow_progress_pct * 1.1, 0.00012, 0.00095)
        break_even_trigger_pct = 0.0009
        fast_spike_trigger_pct = 0.0010
        logging.info(
            'PNL ENGINE | weak_follow_through_exit_tuned score=%.4f seconds=%.1f bars=%d progress=%.5f',
            signal_score,
            no_follow_min_seconds,
            no_follow_min_bars,
            no_follow_progress_pct,
        )

    if signal_score >= 1.0:
        tp_pct = _safe_pct(tp_pct, 0.0008, 0.0055)
        max_hold_seconds = _clamp(max_hold_seconds * 1.15, 25.0, 220.0)
        if winner_extension_profit_pct > 0.0:
            winner_extension_profit_pct = _safe_pct(winner_extension_profit_pct * 0.9, 0.0004, 0.003)
        winner_extension_seconds = _clamp(max(winner_extension_seconds, 10.0) * 1.25, 10.0, 120.0)
    if signal_score >= 1.25:
        tp_pct = _safe_pct(tp_pct * 1.06, 0.0008, 0.0062)
        trailing_offset_pct = _safe_pct(trailing_offset_pct * 1.08, 0.00022, 0.0022)
        trailing_activation_pct = _safe_pct(trailing_activation_pct * 0.9, 0.00035, 0.0025)
    elif signal_score < 0.75:
        tp_pct = _safe_pct(tp_pct * 0.9, 0.0007, 0.0032)
        trailing_offset_pct = _safe_pct(trailing_offset_pct * 0.9, 0.0002, 0.0012)
        max_hold_seconds = _clamp(max_hold_seconds * 0.85, 18.0, 120.0)

    logging.info(
        'PNL ENGINE | winner_mode score=%.4f tp=%.5f trailing_activation=%.5f trailing_offset=%.5f hold=%.1f',
        signal_score,
        tp_pct,
        trailing_activation_pct,
        trailing_offset_pct,
        max_hold_seconds,
    )
    logging.info(
        'PNL ENGINE | partial_tp_ready armed=%s partial_tp_pct=%.5f partial_tp_fraction=%.2f',
        'false',
        0.0,
        0.0,
    )
    if signal_score >= 1.25:
        previous_tp_pct = tp_pct
        tp_upper_bound = 0.0055
        tp_pct = _clamp(tp_pct * 1.05, 0.0008, tp_upper_bound)
        logging.info(
            'TP BOOST active | signal_score=%.3f tier=%s tp_old=%.5f tp_new=%.5f tp_cap=%.5f',
            signal_score,
            exit_tier,
            previous_tp_pct,
            tp_pct,
            tp_upper_bound,
        )
    elif signal_score >= 1.10:
        previous_tp_pct = tp_pct
        tp_upper_bound = 0.0046
        tp_pct = _clamp(tp_pct * 1.03, 0.0008, tp_upper_bound)
        logging.info(
            'TP BOOST active | signal_score=%.3f tier=%s tp_old=%.5f tp_new=%.5f tp_cap=%.5f',
            signal_score,
            exit_tier,
            previous_tp_pct,
            tp_pct,
            tp_upper_bound,
        )
    exit_profile = str(os.getenv("FX_EXIT_PROFILE", "partial_runner") or "partial_runner").strip().lower()
    if exit_profile not in {"fixed_rr", "partial_runner", "delayed_trailing"}:
        exit_profile = "partial_runner"
    return ActiveTrade(
        instrument='UNKNOWN',
        side=str(side).upper(),
        entry_price=float(entry_price),
        entry_time=datetime.now(timezone.utc),
        entry_index=0,
        signal_score=float(signal_score),
        tp_pct=float(tp_pct),
        sl_pct=float(sl_pct),
        trailing_activation_pct=float(trailing_activation_pct),
        trailing_offset_pct=float(trailing_offset_pct),
        max_hold_seconds=float(max_hold_seconds),
        source=str(source),
        exit_tier=exit_tier,
        volatility_factor=float(vol_factor),
        flat_exit_threshold_pct=float(flat_exit_threshold_pct),
        no_follow_through_min_bars=int(no_follow_min_bars),
        no_follow_through_min_seconds=float(no_follow_min_seconds),
        no_follow_through_progress_pct=float(no_follow_progress_pct),
        winner_extension_profit_pct=float(winner_extension_profit_pct),
        winner_extension_seconds=float(winner_extension_seconds),
        base_max_hold_seconds=float(max_hold_seconds),
        exit_profile=exit_profile,
        break_even_trigger_pct=float(_safe_pct(break_even_trigger_pct, 0.0003, 0.0022)),
        fast_spike_trigger_pct=float(_safe_pct(fast_spike_trigger_pct, 0.0004, 0.0025)),
        peak_price=float(entry_price),
        trough_price=float(entry_price),
    )


def sync_trade_with_risk_state(trade: ActiveTrade | None, risk_state: RiskState) -> ActiveTrade | None:
    if trade is None:
        if risk_state.open_positions != 0:
            logging.warning(
                'TRADE STATE SYNC | trade=none risk_open_positions=%d action=reset_flat',
                risk_state.open_positions,
            )
            risk_state.open_positions = 0
        return None

    if risk_state.open_positions <= 0:
        logging.warning(
            'TRADE STATE SYNC | trade_exists=true risk_open_positions=%d action=force_open_position',
            risk_state.open_positions,
        )
        risk_state.open_positions = 1
    # DISABLED: allow multi-position per symbol
    if False:
        pass
    return trade


def _calculate_trade_pnl_ratio(trade: ActiveTrade, exit_price: float) -> float:
    if trade.side == 'LONG':
        return (float(exit_price) / max(float(trade.entry_price), 1e-9)) - 1.0
    return (float(trade.entry_price) / max(float(exit_price), 1e-9)) - 1.0


def manage_active_trade(
    *,
    trade: ActiveTrade | None,
    risk_state: RiskState,
    risk_cfg: RiskConfig,
    latest_price: float,
    latest_bar_time: datetime,
    loop_now: datetime,
    active_notional_usd: float,
    active_position_scale: float,
    no_trade_snapshot_for_active_trade: int,
    same_bar_entry_allowed_before_loop: bool,
    trades_file: Path,
    symbol: str,
    bars: pd.DataFrame,
    adaptive_filters: AdaptiveFilterProtocol,
    latest_signal_score: float = 0.0,
    latest_volatility: float = 0.0,
    latest_spread: float = 0.0,
    latest_regime: str = "UNKNOWN",
    live_session: BybitHTTPProtocol | None = None,
    live_position_qty: float = 0.0,
    live_position_idx: int | None = None,
    live_safety_controller: LiveSafetyController | None = None,
    live_trailing_policy: TrailingPolicy | None = None,
) -> RuntimeTradeManagerResult:
    global consecutive_losses, last_symbol, last_direction, last_closed_trade_timestamp
    trade = sync_trade_with_risk_state(trade, risk_state)
    if trade is None:
        return RuntimeTradeManagerResult(
            trade=None,
            position_scale=1.0,
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot=no_trade_snapshot_for_active_trade,
        )

    if float(getattr(trade, 'entry_price', 0.0) or 0.0) <= 0.0:
        logging.error(
            "EXIT BLOCKED | invalid entry price | symbol=%s source=%s entry_price=%.6f",
            symbol,
            getattr(trade, "source", "unknown"),
            float(getattr(trade, 'entry_price', 0.0) or 0.0),
        )
        if live_safety_controller is not None:
            live_safety_controller.activate_kill_switch(
                'invalid_entry_price',
                session=live_session,
                symbol=symbol if live_session is not None else None,
                side=str(getattr(trade, 'side', '')).upper() if str(getattr(trade, 'side', '')).upper() in {'LONG', 'SHORT'} else None,
                qty=live_position_qty if live_position_qty > 0 else None,
                position_idx=live_position_idx,
            )
        return _runtime_guard_result(
            trade=trade,
            error_code='invalid_entry_price',
            context='manage_active_trade_runtime',
            active_position_scale=float(active_position_scale),
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot_for_active_trade=no_trade_snapshot_for_active_trade,
        )
    if str(getattr(trade, 'side', '')).upper() not in {'LONG', 'SHORT'}:
        if live_safety_controller is not None:
            live_safety_controller.activate_kill_switch(
                'invalid_trade_state',
                session=live_session,
                symbol=symbol if live_session is not None else None,
                side=None,
                qty=live_position_qty if live_position_qty > 0 else None,
                position_idx=live_position_idx,
            )
        return _runtime_guard_result(
            trade=trade,
            error_code='invalid_trade_side',
            context='manage_active_trade_runtime',
            active_position_scale=float(active_position_scale),
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot_for_active_trade=no_trade_snapshot_for_active_trade,
        )
    entry_time = trade.entry_time
    elapsed_seconds = _safe_elapsed_seconds(entry_time, loop_now)
    logging.info(
        'EXIT LOOP | side=%s entry_price=%.6f current_price=%.6f loop_time=%s entry_time=%s trailing_active=%s trailing_stop=%s risk_open_positions=%d',
        trade.side,
        trade.entry_price,
        latest_price,
        loop_now.isoformat(),
        entry_time.isoformat() if isinstance(entry_time, datetime) else 'none',
        str(trade.trailing_active).lower(),
        'none' if trade.trailing_stop_price is None else f'{trade.trailing_stop_price:.6f}',
        risk_state.open_positions,
    )
    logging.info(
        'EXIT CLOCK | entry_time=%s loop_now=%s latest_bar_time=%s elapsed_seconds=%.2f',
        entry_time.isoformat() if isinstance(entry_time, datetime) else 'none',
        loop_now.isoformat(),
        latest_bar_time.isoformat(),
        elapsed_seconds,
    )
    if str(getattr(trade, 'source', '')).strip().lower() == 'recovered_trade':
        recovered_at = getattr(trade, 'recovered_at', None)
        if isinstance(recovered_at, datetime):
            recovered_elapsed = max(0.0, (loop_now - recovered_at).total_seconds())
            if recovered_elapsed < 2.0:
                logging.info("RECOVERY EXIT GRACE | symbol=%s elapsed=%.2f", symbol, recovered_elapsed)
                return RuntimeTradeManagerResult(
                    trade=trade,
                    position_scale=float(active_position_scale),
                    active_notional_usd=float(active_notional_usd),
                    no_trade_snapshot=no_trade_snapshot_for_active_trade,
                    exited=False,
                    exit_reason='recovery_exit_grace',
                )

    if (
        live_session is not None
        and live_trailing_policy is not None
        and live_trailing_policy.allow_internal
        and trade.trailing_active
        and float(getattr(trade, 'trailing_stop_price', 0.0) or 0.0) <= 0.0
    ):
        logging.critical('TRAILING STATE INVALID | side=%s entry_price=%.6f latest_price=%.6f mode=%s', trade.side, trade.entry_price, latest_price, live_trailing_policy.mode)
        if live_safety_controller is not None:
            live_safety_controller.activate_kill_switch('invalid_internal_trailing_state', session=live_session, symbol=symbol, side=trade.side, qty=live_position_qty if live_position_qty > 0 else None, position_idx=live_position_idx)
        return RuntimeTradeManagerResult(
            trade=trade,
            position_scale=float(active_position_scale),
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot=no_trade_snapshot_for_active_trade,
            exited=False,
            exit_reason='close_failed',
        )
    should_exit, exit_reason = evaluate_exit(
        trade,
        latest_price,
        loop_now,
        allow_trailing=(live_session is None or bool((live_trailing_policy or TrailingPolicy('disabled', False, False)).allow_internal)),
    )
    trade.exit_reason = exit_reason
    if not should_exit:
        logging.info(
            'TRADE STATE SYNC | action=hold side=%s risk_open_positions=%d trailing_active=%s trailing_stop=%s',
            trade.side,
            risk_state.open_positions,
            str(trade.trailing_active).lower(),
            'none' if trade.trailing_stop_price is None else f'{trade.trailing_stop_price:.6f}',
        )
        return RuntimeTradeManagerResult(
            trade=trade,
            position_scale=float(active_position_scale),
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot=no_trade_snapshot_for_active_trade,
        )

    if risk_state.open_positions <= 0:
        logging.warning(
            'EXIT LOOP | duplicated_exit_guard reason=%s risk_open_positions=%d action=drop_trade',
            exit_reason,
            risk_state.open_positions,
        )
        return RuntimeTradeManagerResult(
            trade=None,
            position_scale=1.0,
            active_notional_usd=float(active_notional_usd),
            no_trade_snapshot=0,
            exited=False,
            exit_reason='duplicated_exit_guard',
        )

    exit_price = float(latest_price)
    live_exit_result: LiveOrderResult | None = None
    if live_session is not None:
        close_qty = float(live_position_qty) if validate_order_qty(live_position_qty) else 0.0
        if close_qty <= 0.0:
            recovered_qty = float(getattr(trade, 'recovered_qty', 0.0) or 0.0)
            if recovered_qty > 0.0:
                close_qty = recovered_qty
            else:
                logging.error(
                    "CLOSE BLOCKED | symbol=%s reason=missing_recovered_qty",
                    symbol,
                )
        if close_qty <= 0.0:
            logging.error(
                "EXIT BLOCKED | invalid close qty | symbol=%s source=%s qty=%s",
                symbol,
                getattr(trade, "source", "unknown"),
                close_qty,
            )
            return RuntimeTradeManagerResult(
                trade=trade,
                position_scale=float(active_position_scale),
                active_notional_usd=float(active_notional_usd),
                no_trade_snapshot=no_trade_snapshot_for_active_trade,
                exited=False,
                exit_reason='close_qty_invalid',
            )
        max_close_attempts = 3
        for attempt in range(1, max_close_attempts + 1):
            live_exit_result = close_bybit_position(live_session, symbol, trade.side, close_qty, position_idx=live_position_idx)
            if live_exit_result.success:
                break
            try:
                remaining_position_after_attempt = fetch_open_position(live_session, symbol)
            except Exception as exc:
                remaining_position_after_attempt = ExchangePosition(symbol=symbol, side='FLAT', qty=0.0, entry_price=0.0, raw={'error': str(exc)})
            if not remaining_position_after_attempt.is_open:
                if isinstance(live_session, BrokerAdapter):
                    logging.info('LIVE EXIT RECHECK | attempt=%d/%d result=already_closed reason=%s', attempt, max_close_attempts, live_exit_result.reason or 'unknown')
                    live_exit_result = LiveOrderResult(
                        True,
                        live_exit_result.order_id,
                        live_exit_result.side,
                        live_exit_result.qty,
                        live_exit_result.avg_price,
                        {**live_exit_result.raw_response, 'close_recheck': 'position_flat_after_failure'},
                        None,
                    )
                else:
                    logging.warning('LIVE EXIT RECHECK | attempt=%d/%d result=flat_after_failure_preserved reason=%s', attempt, max_close_attempts, live_exit_result.reason or 'unknown')
                break
            close_qty = float(remaining_position_after_attempt.qty)
            if close_qty <= 0.0:
                logging.error(
                    "EXIT BLOCKED | invalid close qty | symbol=%s source=%s qty=%s",
                    symbol,
                    getattr(trade, "source", "unknown"),
                    close_qty,
                )
                return RuntimeTradeManagerResult(
                    trade=trade,
                    position_scale=float(active_position_scale),
                    active_notional_usd=float(active_notional_usd),
                    no_trade_snapshot=no_trade_snapshot_for_active_trade,
                    exited=False,
                    exit_reason='close_qty_invalid',
                )
            live_position_idx = remaining_position_after_attempt.position_idx
            logging.error('LIVE EXIT RETRY | attempt=%d/%d reason=%s side=%s qty=%.12f price=%.6f', attempt, max_close_attempts, live_exit_result.reason or 'unknown', trade.side, close_qty, latest_price)
            if attempt < max_close_attempts:
                _sleep()
        if not live_exit_result.success:
            if live_safety_controller is not None:
                live_safety_controller.register_execution_failure(f"close_failure:{live_exit_result.reason or 'unknown'}")
                live_safety_controller.activate_kill_switch(f"close_failure:{live_exit_result.reason or 'unknown'}", session=live_session, symbol=symbol, side=trade.side, qty=live_position_qty, position_idx=live_position_idx)
            return RuntimeTradeManagerResult(
                trade=trade,
                position_scale=float(active_position_scale),
                active_notional_usd=float(active_notional_usd),
                no_trade_snapshot=no_trade_snapshot_for_active_trade,
                exited=False,
                exit_reason='close_failed',
            )
        if live_exit_result.avg_price is not None:
            exit_price = float(live_exit_result.avg_price)
        try:
            post_close_position = fetch_open_position(live_session, symbol)
        except Exception as exc:
            if live_safety_controller is not None:
                live_safety_controller.register_desync(f'post_close_fetch_failed:{exc}')
                live_safety_controller.activate_kill_switch(f'post_close_fetch_failed:{exc}')
            return RuntimeTradeManagerResult(
                trade=trade,
                position_scale=float(active_position_scale),
                active_notional_usd=float(active_notional_usd),
                no_trade_snapshot=no_trade_snapshot_for_active_trade,
                exited=False,
                exit_reason='close_failed',
            )
        if post_close_position.is_open:
            if live_safety_controller is not None:
                live_safety_controller.register_desync(f'post_close_position_still_open:{post_close_position.qty:.12f}')
                live_safety_controller.activate_kill_switch('post_close_position_still_open', session=live_session, symbol=symbol, side=post_close_position.side, qty=post_close_position.qty, position_idx=post_close_position.position_idx)
            return RuntimeTradeManagerResult(
                trade=trade,
                position_scale=float(active_position_scale),
                active_notional_usd=float(active_notional_usd),
                no_trade_snapshot=no_trade_snapshot_for_active_trade,
                exited=False,
                exit_reason='close_failed',
            )
    trade.exit_price = exit_price
    pnl_ratio = _calculate_trade_pnl_ratio(trade, exit_price)
    pnl_usd = pnl_ratio * float(active_notional_usd)
    pnl = float(exit_price - trade.entry_price)
    # =========================================
    # SWEEP CLOSE DEBUG (EDGE TRUTH)
    # =========================================
    logging.info(
        "SWEEP CLOSE DEBUG | setup=%s pnl_usd=%.4f exit_reason=%s",
        getattr(trade, "setup_type", "unknown"),
        pnl_usd,
        exit_reason,
    )
    # =========================================
    # SWEEP TRACKING (CLOSE STATS)
    # =========================================
    if str(getattr(trade, "setup_type", "unknown")) == "sweep_only":
        try:
            pnl_value = float(pnl_usd)
            sweep_stats["pnl_total"] += pnl_value
            if pnl_value > 0:
                sweep_stats["wins"] += 1
            else:
                sweep_stats["losses"] += 1
        except Exception:
            pass
    if pnl_usd > 0:
        result = "WIN"
        consecutive_losses = 0
    elif pnl_usd < 0:
        result = "LOSS"
        consecutive_losses += 1
    else:
        result = "FLAT"
    logging.info("TRADE RESULT FIX | pnl_usd=%.4f result=%s", pnl_usd, result)
    last_symbol = str(symbol).upper()
    last_direction = str(trade.side).upper()
    last_closed_trade_timestamp = time.time()
    hold_seconds = elapsed_seconds
    register_exit(risk_state, pnl_usd, when=loop_now, reason=exit_reason, symbol=symbol)
    risk_state.same_bar_entry_allowed = bool(same_bar_entry_allowed_before_loop)
    logging.info(
        'LIVE EXIT | reason=%s side=%s qty=%.12f price=%.6f pnl=%.4f',
        exit_reason,
        trade.side,
        live_position_qty,
        exit_price,
        pnl_usd,
    )
    if maybe_pause_after_consecutive_losses(risk_state, risk_cfg, when=loop_now):
        logging.info(
            'Loss-streak pause armed | consecutive_losses=%d pause_until=%s',
            risk_state.consecutive_losses,
            risk_state.trading_paused_until.isoformat() if risk_state.trading_paused_until else 'none',
        )
    with trades_file.open('a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([int(time.time()), symbol, trade.side, trade.entry_price, exit_price, exit_reason, pnl_usd, risk_state.daily_pnl_usd])
    adaptive_filters.record_trade_feedback(pnl_usd, latest_bar_time, bars, exit_reason)
    if live_safety_controller is not None and live_session is not None:
        live_safety_controller.register_live_trade_result(pnl_usd, float(active_notional_usd), risk_state.daily_pnl_usd, adaptive_filters.session_drawdown_usd)
    logging.info(
        "TRADE CLOSED | result=%s pnl=%.6f reason=%s",
        result,
        pnl_usd,
        trade.exit_reason,
    )
    performance_memory.append({
        "result": result,
        "reason": trade.exit_reason or "",
        "pnl": pnl,
    })
    confidence_memory.append({
        "score": float(trade.signal_score),
        "result": result,
    })
    logging.info(
        'EXIT FILLED | entry_price=%.6f exit_price=%.6f pnl_usd=%.4f pnl_ratio=%.5f exit_reason=%s hold_seconds=%.1f side=%s signal_score=%.4f tp_pct=%.5f sl_pct=%.5f',
        trade.entry_price,
        exit_price,
        pnl_usd,
        pnl_ratio,
        exit_reason,
        hold_seconds,
        trade.side,
        trade.signal_score,
        trade.tp_pct,
        trade.sl_pct,
    )
    logging.info(
        'TRADE STATE SYNC | action=exit_complete risk_open_positions=%d same_bar_entry_allowed=%s daily_pnl=%.4f',
        risk_state.open_positions,
        str(risk_state.same_bar_entry_allowed).lower(),
        risk_state.daily_pnl_usd,
    )
    try:
        stop_price = float(getattr(trade, "stop_price", getattr(trade, "sl_price", 0.0)) or 0.0)
        tp_price = float(getattr(trade, "take_profit_price", getattr(trade, "tp_price", 0.0)) or 0.0)
        risk_per_unit = abs(float(trade.entry_price) - stop_price)
        pnl_r = float(pnl_ratio) / max((risk_per_unit / max(float(trade.entry_price), 1e-9)), 1e-9) if risk_per_unit > 0 else 0.0
        trade_context = getattr(trade, "context", {}) if isinstance(getattr(trade, "context", {}), dict) else {}
        opened_at = trade.entry_time.isoformat() if isinstance(trade.entry_time, datetime) else datetime.now(timezone.utc).isoformat()
        closed_at = loop_now.isoformat() if isinstance(loop_now, datetime) else datetime.now(timezone.utc).isoformat()
        FX_TRADE_AUDIT_LOGGER.append(
            FXTradeAuditRecord(
                symbol=str(symbol).upper(),
                setup_name=str(getattr(trade, "setup_type", "unknown")),
                execution_authority=str(getattr(trade, "source", "unknown")),
                ict_reason=str(trade_context.get("ict_reason", "")),
                ict_confidence=float(trade_context.get("ict_confidence", 0.0) or 0.0),
                ict_genome=str(trade_context.get("ict_genome", "unknown")),
                regime=str(getattr(trade, "entry_regime", latest_regime)),
                signal_score=float(getattr(trade, "signal_score", latest_signal_score)),
                threshold_required=float(trade_context.get("threshold_required", 0.0) or 0.0),
                signal_delta=float(trade_context.get("signal_delta", 0.0) or 0.0),
                entry_quality=float(trade_context.get("entry_quality", 0.0) or 0.0),
                spread=float(getattr(trade, "entry_spread", latest_spread)),
                volatility=float(getattr(trade, "entry_volatility", latest_volatility)),
                override_used=bool(trade_context.get("override_used", False)),
                force_enabled=bool(trade_context.get("force_enabled", False)),
                side=str(trade.side).upper(),
                entry_price=float(trade.entry_price),
                stop_price=float(stop_price),
                tp_price=float(tp_price),
                exit_price=float(exit_price),
                exit_reason=str(exit_reason),
                hold_seconds=float(hold_seconds),
                pnl_usd=float(pnl_usd),
                pnl_r=float(pnl_r),
                result=result,
                opened_at=opened_at,
                closed_at=closed_at,
                exit_profile=str(getattr(trade, "exit_profile", "partial_runner")),
            )
        )
    except Exception as audit_exc:
        logging.warning("FX TRADE AUDIT WRITE FAILED | symbol=%s error=%s", symbol, audit_exc)
    return RuntimeTradeManagerResult(
        trade=None,
        position_scale=1.0,
        active_notional_usd=float(risk_cfg.notional_usd),
        no_trade_snapshot=0,
        exited=True,
        exit_reason=exit_reason,
        realized_pnl=float(pnl_usd),
        pnl_ratio=float(pnl_ratio),
        hold_seconds=float(hold_seconds),
        closed_trade_context={
            "symbol": str(symbol),
            "signal_score": float(getattr(trade, "signal_score", latest_signal_score)),
            "volatility": float(getattr(trade, "entry_volatility", latest_volatility)),
            "spread": float(getattr(trade, "entry_spread", latest_spread)),
            "regime": str(getattr(trade, "entry_regime", latest_regime)),
        },
    )


def evaluate_exit(trade: ActiveTrade, current_price: float, now: datetime, allow_trailing: bool = True) -> tuple[bool, str]:
    side = str(trade.side).upper()
    entry_price_raw = trade.entry_price
    entry_time = trade.entry_time
    trailing_active = bool(trade.trailing_active)
    trailing_stop = getattr(trade, 'trailing_stop_price', None)
    trailing_activation_pct = trade.trailing_activation_pct
    trailing_offset_pct = trade.trailing_offset_pct
    take_profit_pct = trade.tp_pct
    stop_loss_pct = trade.sl_pct
    max_hold_seconds = trade.max_hold_seconds
    peak_price = getattr(trade, 'peak_price', None)
    trough_price = getattr(trade, 'trough_price', None)

    if side not in {'LONG', 'SHORT'}:
        logging.critical(
            'INVALID TRADE SIDE | context=evaluate_exit side=%r entry_price=%r current_price=%r',
            getattr(trade, 'side', None),
            entry_price_raw,
            current_price,
        )
        return False, 'invalid_trade_side'
    if now is None or not isinstance(now, datetime):
        logging.error('EXIT CHECK FAILED | invalid timestamp=%r', now)
        return False, 'invalid_time'
    if entry_time is None or not isinstance(entry_time, datetime):
        logging.error('EXIT CHECK FAILED | missing entry_time=%r', entry_time)
        return False, 'missing_entry_time'

    try:
        entry_price = float(entry_price_raw)
        current_price = float(current_price)
    except (TypeError, ValueError):
        logging.error('EXIT CHECK FAILED | non-numeric price entry=%r current=%r', entry_price_raw, current_price)
        return False, 'invalid_price'

    if entry_price <= 0.0 or current_price <= 0.0:
        logging.critical(
            'INVALID ENTRY PRICE | context=evaluate_exit side=%s entry_price=%.10f current_price=%.10f',
            side,
            entry_price,
            current_price,
        )
        return False, 'invalid_price'
    force_close_reason = str(getattr(trade, 'force_close_reason', '') or '').strip()
    if force_close_reason:
        logging.info('EXIT OVERRIDE | reason=%s side=%s', force_close_reason, side)
        return True, force_close_reason

    elapsed = max(0.0, (now - entry_time).total_seconds())
    seconds_open = elapsed
    progress = abs(current_price - entry_price)
    signal_score = float(getattr(trade, 'signal_score', 0.0))
    partial_tp_triggered = bool(getattr(trade, 'partial_tp_triggered', False))
    tp = abs(entry_price) * float(getattr(trade, 'tp_pct', 0.0) or 0.0)
    if side == 'LONG':
        profit_pct = (current_price - entry_price) / entry_price
        profit_points = current_price - entry_price
        peak_price = current_price if peak_price is None else max(float(peak_price), current_price)
        trough_price = current_price if trough_price is None else min(float(trough_price), current_price)
        trade.peak_price = peak_price
        trade.trough_price = trough_price
    else:
        profit_pct = (entry_price - current_price) / entry_price
        profit_points = entry_price - current_price
        trough_price = current_price if trough_price is None else min(float(trough_price), current_price)
        peak_price = current_price if peak_price is None else max(float(peak_price), current_price)
        trade.trough_price = trough_price
        trade.peak_price = peak_price

    logging.info(
        'EXIT DEBUG | profit=%.5f trailing_active=%s trailing_stop=%s elapsed=%.2f',
        profit_pct,
        trailing_active,
        'None' if trailing_stop is None else f'{float(trailing_stop):.10f}',
        elapsed,
    )
    logging.info(
        'EXIT CLOCK | entry_time=%s loop_now=%s latest_bar_time=%s elapsed_seconds=%.2f',
        entry_time.isoformat(),
        now.isoformat(),
        now.isoformat(),
        elapsed,
    )
    pnl_ratio = float(profit_pct)
    in_early_grace = elapsed < EARLY_EXIT_GRACE_SECONDS
    if in_early_grace:
        blocked_early_loss_cut = not (pnl_ratio <= -0.0006)
        logging.info(
            "EARLY EXIT GRACE | symbol=%s elapsed=%.2f pnl=%.5f blocked=%s",
            str(getattr(trade, 'symbol', 'unknown')),
            elapsed,
            pnl_ratio,
            str(blocked_early_loss_cut).lower(),
        )
    if elapsed < 20.0 and profit_points < -0.5:
        if in_early_grace and pnl_ratio > -0.0006:
            logging.info('EXIT BLOCKED | reason=early_loss_grace side=%s pnl_ratio=%.5f elapsed=%.2f', side, pnl_ratio, elapsed)
        else:
            logging.warning("EARLY LOSS CUT")
            return True, "EARLY_LOSS_CUT"

    if max_hold_seconds is not None:
        try:
            max_hold_seconds = max(0.0, float(max_hold_seconds))
        except (TypeError, ValueError):
            logging.warning('EXIT CHECK WARN | invalid max_hold_seconds=%r', max_hold_seconds)
            max_hold_seconds = None

    try:
        trailing_activation_pct = None if trailing_activation_pct is None else max(0.0, float(trailing_activation_pct))
    except (TypeError, ValueError):
        logging.warning('EXIT CHECK WARN | invalid trailing_activation_pct=%r', trailing_activation_pct)
        trailing_activation_pct = None
    try:
        trailing_offset_pct = None if trailing_offset_pct is None else max(0.0, float(trailing_offset_pct))
    except (TypeError, ValueError):
        logging.warning('EXIT CHECK WARN | invalid trailing_offset_pct=%r', trailing_offset_pct)
        trailing_offset_pct = None
    try:
        take_profit_pct = None if take_profit_pct is None else max(0.0, float(take_profit_pct))
    except (TypeError, ValueError):
        logging.warning('EXIT CHECK WARN | invalid take_profit_pct=%r', take_profit_pct)
        take_profit_pct = None
    try:
        stop_loss_pct = None if stop_loss_pct is None else max(0.0, float(stop_loss_pct))
    except (TypeError, ValueError):
        logging.warning('EXIT CHECK WARN | invalid stop_loss_pct=%r', stop_loss_pct)
        stop_loss_pct = None

    if allow_trailing and not trailing_active and trailing_activation_pct is not None and trailing_offset_pct is not None:
        if profit_pct >= trailing_activation_pct:
            trailing_active = True
            trade.trailing_active = True
            logging.info(
                'TRAILING ACTIVATED | side=%s profit_pct=%.5f activation_pct=%.5f',
                side,
                profit_pct,
                trailing_activation_pct,
            )
        else:
            logging.info(
                'TRAILING NOT ACTIVE | side=%s profit_pct=%.5f activation_pct=%.5f',
                side,
                profit_pct,
                trailing_activation_pct,
            )

    if allow_trailing and trailing_active and trailing_offset_pct is not None:
        if side == 'LONG':
            reference_price = float(trade.peak_price if trade.peak_price is not None else current_price)
            candidate_stop = reference_price * (1.0 - trailing_offset_pct)
            if candidate_stop <= 0.0:
                logging.critical('TRAILING INVALID | side=%s reference_price=%.10f trailing_offset_pct=%.10f candidate_stop=%.10f', side, reference_price, trailing_offset_pct, candidate_stop)
                candidate_stop = max(reference_price, 1e-9)
            trailing_stop = candidate_stop if trailing_stop is None or float(trailing_stop) <= 0.0 else max(float(trailing_stop), candidate_stop)
        else:
            reference_price = float(trade.trough_price if trade.trough_price is not None else current_price)
            candidate_stop = reference_price * (1.0 + trailing_offset_pct)
            if candidate_stop <= 0.0:
                logging.critical('TRAILING INVALID | side=%s reference_price=%.10f trailing_offset_pct=%.10f candidate_stop=%.10f', side, reference_price, trailing_offset_pct, candidate_stop)
                candidate_stop = max(reference_price, 1e-9)
            trailing_stop = candidate_stop if trailing_stop is None or float(trailing_stop) <= 0.0 else min(float(trailing_stop), candidate_stop)
        trade.trailing_stop_price = trailing_stop
        logging.info(
            'TRAILING UPDATED | side=%s reference_price=%.10f trailing_stop=%.10f',
            side,
            reference_price,
            trailing_stop,
        )
    risk_unit = abs(entry_price * float(stop_loss_pct or 0.0))
    profit_lock_stop: float | None = None
    if risk_unit > 0.0 and profit_points > (1.5 * risk_unit):
        if side == 'LONG':
            profit_lock_stop = entry_price + (profit_points * 0.5)
        else:
            profit_lock_stop = entry_price - (profit_points * 0.5)
    elif risk_unit > 0.0 and profit_points > (0.5 * risk_unit):
        profit_lock_stop = entry_price
    if risk_unit > 0.0 and profit_points > (2.0 * risk_unit):
        if side == 'LONG':
            profit_lock_stop = max(entry_price + risk_unit, profit_lock_stop or (entry_price + risk_unit))
        else:
            profit_lock_stop = min(entry_price - risk_unit, profit_lock_stop or (entry_price - risk_unit))
    elif risk_unit > 0.0 and profit_points > (1.0 * risk_unit):
        profit_lock_stop = entry_price
    if profit_lock_stop is not None:
        if side == 'LONG':
            trailing_stop = profit_lock_stop if trailing_stop is None else max(float(trailing_stop), float(profit_lock_stop))
        else:
            trailing_stop = profit_lock_stop if trailing_stop is None else min(float(trailing_stop), float(profit_lock_stop))
        trade.trailing_stop_price = float(trailing_stop)
        logging.info("PROFIT LOCK ENGAGED")

    if stop_loss_pct is not None:
        if side == 'LONG' and current_price <= entry_price * (1.0 - stop_loss_pct):
            logging.info('EXIT TRIGGER | reason=stop_loss side=%s current=%.10f', side, current_price)
            return True, 'stop_loss'
        if side == 'SHORT' and current_price >= entry_price * (1.0 + stop_loss_pct):
            logging.info('EXIT TRIGGER | reason=stop_loss side=%s current=%.10f', side, current_price)
            return True, 'stop_loss'

    # --- volatility adaptive timing ---
    volatility = float(getattr(trade, "volatility", 0.0) or 0.0)
    if volatility > 0.003:
        min_time = 15.0
    elif volatility < 0.001:
        min_time = 45.0
    else:
        min_time = float(os.getenv("MIN_PROGRESS_TIME", "30"))

    # --- RR-based progress ---
    if stop_loss_pct is not None:
        stop_loss_price = (
            entry_price * (1.0 - stop_loss_pct)
            if side == 'LONG'
            else entry_price * (1.0 + stop_loss_pct)
        )
        risk = abs(entry_price - stop_loss_price)
    else:
        risk = 0.0
    reward = abs(current_price - entry_price)
    rr_progress = (reward / risk) if risk > 0.0 else 0.0

    if seconds_open > min_time:
        if rr_progress < 0.3:
            if pnl_ratio > -0.0005:
                logging.info(
                    "EXIT BLOCKED | low_rr_but_healthy rr:%.2f pnl=%.5f",
                    rr_progress,
                    pnl_ratio,
                )
            else:
                logging.info(
                    "EXIT | no_progress rr:%.2f pnl=%.5f",
                    rr_progress,
                    pnl_ratio,
                )
                return True, "EXIT_NO_PROGRESS"

    if progress > tp * 0.4 and trailing_activation_pct is not None:
        trailing_activation_pct = min(trailing_activation_pct, tp * 0.6)

    if risk_unit > 0.0 and profit_points >= risk_unit and not partial_tp_triggered:
        logging.info("PARTIAL TP HIT | fraction=0.50 action=arm_runner_be")
        trade.partial_tp_triggered = True
        trade.trailing_active = True
        if side == 'LONG':
            trade.trailing_stop_price = max(float(trade.trailing_stop_price or entry_price), float(entry_price))
        else:
            trade.trailing_stop_price = min(float(trade.trailing_stop_price or entry_price), float(entry_price))

    if take_profit_pct is not None:
        if side == 'LONG' and current_price >= entry_price * (1.0 + take_profit_pct):
            logging.info('EXIT TRIGGER | reason=take_profit side=%s current=%.10f', side, current_price)
            return True, 'take_profit'
        if side == 'SHORT' and current_price <= entry_price * (1.0 - take_profit_pct):
            logging.info('EXIT TRIGGER | reason=take_profit side=%s current=%.10f', side, current_price)
            return True, 'take_profit'

    if allow_trailing and trailing_stop is not None:
        if side == 'LONG' and current_price <= float(trailing_stop):
            logging.info('EXIT TRIGGER | reason=trailing_stop side=%s current=%.10f stop=%.10f', side, current_price, float(trailing_stop))
            return True, 'trailing_stop'
        if side == 'SHORT' and current_price >= float(trailing_stop):
            logging.info('EXIT TRIGGER | reason=trailing_stop side=%s current=%.10f stop=%.10f', side, current_price, float(trailing_stop))
            return True, 'trailing_stop'

    if max_hold_seconds is not None and elapsed >= max_hold_seconds:
        logging.info('EXIT TRIGGER | reason=max_hold_time side=%s elapsed=%.2f limit=%.2f', side, elapsed, max_hold_seconds)
        return True, 'max_hold_time'

    logging.info('EXIT HOLD | side=%s profit_pct=%.5f elapsed=%.2f', side, profit_pct, elapsed)
    return False, ''


def build_entry_candidate(
    signal: int,
    latest_price: float,
    latest_index: int,
    signal_score: float,
    volatility: float,
    timestamp: datetime,
    source: str,
    churn_pressure: float = 0.0,
) -> ActiveTrade | None:
    if float(latest_price) <= 0.0:
        logging.critical(
            'INVALID ENTRY PRICE | context=build_entry_candidate side=%s entry_price=%.10f source=%s',
            'LONG' if signal > 0 else 'SHORT',
            float(latest_price),
            source,
        )
        return None
    side = 'LONG' if signal > 0 else 'SHORT'
    if side not in ('LONG', 'SHORT'):
        logging.critical(
            'INVALID TRADE SIDE | context=build_entry_candidate side=%r entry_price=%.10f source=%s',
            side,
            float(latest_price),
            source,
        )
        return None
    trade = compute_exit_plan(signal_score=signal_score, volatility=volatility, side=side, entry_price=latest_price, source=source)
    trade.entry_time = timestamp
    trade.entry_index = int(latest_index)
    trade.peak_price = float(latest_price)
    trade.trough_price = float(latest_price)
    if trade.exit_tier == EXIT_TIER_WEAK and churn_pressure > 0.0:
        tighten_factor = 1.0 - (0.15 * churn_pressure)
        trade.max_hold_seconds = float(_clamp(trade.max_hold_seconds * tighten_factor, 20.0, trade.max_hold_seconds))
        trade.trailing_activation_pct = float(_clamp(trade.trailing_activation_pct * tighten_factor, 0.0004, trade.trailing_activation_pct))
        trade.no_follow_through_progress_pct = float(_clamp(trade.no_follow_through_progress_pct * (1.0 + (0.2 * churn_pressure)), 0.00015, 0.0009))
    logging.info(
        'EXIT PLAN CREATED | exit_tier=%s tp_pct=%.5f sl_pct=%.5f trailing_activation_pct=%.5f trailing_offset_pct=%.5f max_hold_seconds=%.1f volatility_factor=%.3f',
        trade.exit_tier,
        trade.tp_pct,
        trade.sl_pct,
        trade.trailing_activation_pct,
        trade.trailing_offset_pct,
        trade.max_hold_seconds,
        trade.volatility_factor,
    )
    return trade


def evaluate_exit_engine(
    trade: ActiveTrade,
    latest_bar: pd.Series,
    latest_index: int,
    bar_seconds: float,
) -> ExitEvaluation | None:
    if float(getattr(trade, 'entry_price', 0.0) or 0.0) <= 0.0:
        _log_invalid_trade_state('invalid_entry_price', trade, 'evaluate_exit_engine')
        return ExitEvaluation(
            exit_price=max(float(latest_bar.get('close', 0.0) or 0.0), 0.0),
            exit_reason='invalid_entry_price',
            hold_seconds=0.0,
            pnl_ratio=0.0,
            bars_held=0,
            explicit_exit_triggered=True,
            exit_index=int(latest_index),
        )
    if str(getattr(trade, 'side', '')).upper() not in {'LONG', 'SHORT'}:
        _log_invalid_trade_state('invalid_trade_side', trade, 'evaluate_exit_engine')
        return ExitEvaluation(
            exit_price=max(float(latest_bar.get('close', 0.0) or 0.0), 0.0),
            exit_reason='invalid_trade_side',
            hold_seconds=0.0,
            pnl_ratio=0.0,
            bars_held=0,
            explicit_exit_triggered=True,
            exit_index=int(latest_index),
        )
    if latest_index <= trade.entry_index:
        return None

    high_price = float(latest_bar['high'])
    low_price = float(latest_bar['low'])
    close_price = float(latest_bar['close'])
    trade.peak_price = high_price if trade.peak_price is None else max(trade.peak_price, high_price)
    trade.trough_price = low_price if trade.trough_price is None else min(trade.trough_price, low_price)
    hold_seconds = max(0.0, float(latest_index - trade.entry_index) * float(bar_seconds))
    bars_held = max(1, int(latest_index - trade.entry_index))
    exit_profile = str(getattr(trade, "exit_profile", "partial_runner") or "partial_runner").strip().lower()
    if exit_profile not in {"fixed_rr", "partial_runner", "delayed_trailing"}:
        exit_profile = "partial_runner"
    progress_ratio = _signed_progress_ratio(
        trade,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
    )
    market_regime = str(getattr(trade, 'market_regime', 'UNKNOWN') or 'UNKNOWN').upper()

    hold_multiplier = 1.0
    if trade.signal_score < 0.7:
        hold_multiplier *= 0.8
    elif trade.signal_score > 1.1:
        hold_multiplier *= 1.2
    if market_regime in {'LOW_VOL', 'LOW_ACTIVITY'}:
        hold_multiplier *= 0.9
    if abs(progress_ratio) < max(0.00005, trade.no_follow_through_progress_pct * 0.35):
        hold_multiplier *= 0.9
    base_max_hold_seconds = float(getattr(trade, 'base_max_hold_seconds', 0.0) or 0.0)
    configured_max_hold_seconds = float(getattr(trade, 'max_hold_seconds', 0.0) or 0.0)
    hold_anchor_seconds = base_max_hold_seconds if base_max_hold_seconds > 0.0 else configured_max_hold_seconds
    manual_hold_override = (
        configured_max_hold_seconds > 0.0
        and not bool(getattr(trade, 'winner_extension_active', False))
        and (hold_anchor_seconds <= 0.0 or not math.isclose(configured_max_hold_seconds, hold_anchor_seconds, rel_tol=0.0, abs_tol=1e-9))
    )
    if manual_hold_override:
        hold_anchor_seconds = configured_max_hold_seconds
    if hold_anchor_seconds <= 0.0:
        hold_anchor_seconds = 35.0
    if manual_hold_override:
        adapted_max_hold_seconds = float(max(0.0, hold_anchor_seconds))
    else:
        adapted_max_hold_seconds = float(_clamp(hold_anchor_seconds * hold_multiplier, 15.0, 220.0))
    if bool(getattr(trade, 'winner_extension_active', False)) and configured_max_hold_seconds > adapted_max_hold_seconds:
        adapted_max_hold_seconds = configured_max_hold_seconds
    trade.max_hold_seconds = adapted_max_hold_seconds
    logging.info(
        'PNL ENGINE | hold_time_adapted score=%.4f regime=%s max_hold=%.1f',
        trade.signal_score,
        market_regime,
        trade.max_hold_seconds,
    )

    if (
        not trade.winner_extension_active
        and _tier_is_strong(trade.exit_tier)
        and progress_ratio >= trade.winner_extension_profit_pct
    ):
        trade.winner_extension_active = True
        trade.max_hold_seconds = float(_clamp(
            trade.base_max_hold_seconds + trade.winner_extension_seconds,
            trade.base_max_hold_seconds,
            trade.base_max_hold_seconds + max(trade.winner_extension_seconds, 0.0),
        ))
        logging.info(
            'WINNER EXTENSION ACTIVE | side=%s signal_score=%.4f hold_extension=%.1f',
            trade.side,
            trade.signal_score,
            trade.max_hold_seconds - trade.base_max_hold_seconds,
        )

    adaptive_activation_pct = _safe_pct(trade.trailing_activation_pct, 0.00025, 0.0035)
    adaptive_offset_pct = _safe_pct(trade.trailing_offset_pct, 0.00012, 0.0035)
    trailing_params_locked = bool(trade.trailing_active and getattr(trade, 'trailing_stop_price', None) is not None)
    if not trailing_params_locked:
        if trade.signal_score < 0.75:
            adaptive_offset_pct = _safe_pct(adaptive_offset_pct * 0.85, 0.0001, 0.0025)
        elif trade.signal_score > 1.1:
            adaptive_offset_pct = _safe_pct(adaptive_offset_pct * 1.15, 0.00012, 0.0032)
    logging.info(
        'PNL ENGINE | smart_trailing score=%.4f activation=%.5f offset=%.5f',
        trade.signal_score,
        adaptive_activation_pct,
        adaptive_offset_pct,
    )
    if not trailing_params_locked:
        trade.trailing_activation_pct = adaptive_activation_pct
        trade.trailing_offset_pct = adaptive_offset_pct

    if (not trailing_params_locked) and hold_seconds <= float(max(getattr(trade, 'fast_spike_window_seconds', 20.0), 1.0)):
        fast_spike_trigger = _safe_pct(getattr(trade, 'fast_spike_trigger_pct', 0.0008), 0.00035, 0.0025)
        if progress_ratio >= fast_spike_trigger:
            adaptive_activation_pct = min(adaptive_activation_pct, fast_spike_trigger * 0.8)
            adaptive_offset_pct = _safe_pct(adaptive_offset_pct * 0.85, 0.0001, 0.0025)
            trade.trailing_activation_pct = adaptive_activation_pct
            trade.trailing_offset_pct = adaptive_offset_pct
            logging.info(
                'PNL ENGINE | fast_spike_lock activated profit_pct=%.5f seconds_since_entry=%.1f',
                progress_ratio,
                hold_seconds,
            )

    break_even_trigger_pct = _safe_pct(getattr(trade, 'break_even_trigger_pct', 0.0007), 0.0003, 0.0022)
    if not bool(getattr(trade, 'break_even_armed', False)) and progress_ratio >= break_even_trigger_pct:
        trade.break_even_armed = True
        break_even_buffer = 0.0 if trade.signal_score >= 1.05 else 0.00003
        existing_stop = getattr(trade, 'trailing_stop_price', None)
        if trade.side == 'LONG':
            be_stop = trade.entry_price * (1.0 + break_even_buffer)
            if existing_stop is not None and float(existing_stop) > 0.0:
                trade.trailing_stop_price = max(float(existing_stop), be_stop)
        else:
            be_stop = trade.entry_price * (1.0 - break_even_buffer)
            if existing_stop is not None and float(existing_stop) > 0.0:
                trade.trailing_stop_price = min(float(existing_stop), be_stop)
        logging.info(
            'PNL ENGINE | break_even_armed entry=%.6f trigger=%.5f side=%s',
            trade.entry_price,
            break_even_trigger_pct,
            trade.side,
        )

    progress_required = _safe_pct(trade.no_follow_through_progress_pct, 0.00008, 0.0012)
    no_follow_min_seconds = float(_clamp(float(trade.no_follow_through_min_seconds), 2.0, 60.0))
    no_follow_min_bars = int(_clamp(float(trade.no_follow_through_min_bars), 1.0, 10.0))
    if trade.signal_score < 0.75:
        no_follow_min_seconds = _clamp(min(no_follow_min_seconds, 8.0), 2.0, 60.0)
        no_follow_min_bars = int(_clamp(min(float(no_follow_min_bars), 2.0), 1.0, 10.0))
        progress_required = _safe_pct(progress_required * 1.15, 0.0001, 0.0015)
    elif trade.signal_score >= 1.1:
        no_follow_min_seconds = _clamp(no_follow_min_seconds * 1.2, 2.0, 60.0)
        progress_required = _safe_pct(progress_required * 0.9, 0.00008, 0.0012)
    trade.no_follow_through_min_seconds = float(no_follow_min_seconds)
    trade.no_follow_through_min_bars = int(no_follow_min_bars)
    trade.no_follow_through_progress_pct = float(progress_required)
    logging.info(
        'PNL ENGINE | weak_follow_through_exit_tuned score=%.4f seconds=%.1f bars=%d progress=%.5f',
        trade.signal_score,
        trade.no_follow_through_min_seconds,
        trade.no_follow_through_min_bars,
        trade.no_follow_through_progress_pct,
    )

    if trade.side == 'LONG':
        tp_price = trade.entry_price * (1.0 + trade.tp_pct)
        sl_price = trade.entry_price * (1.0 - trade.sl_pct)
        trailing_ready = exit_profile != "fixed_rr"
        if exit_profile == "delayed_trailing":
            trailing_ready = hold_seconds >= max(12.0, float(trade.max_hold_seconds) * 0.20)
        if not trade.trailing_active and trailing_ready and trade.peak_price >= trade.entry_price * (1.0 + adaptive_activation_pct):
            trade.trailing_active = True
            logging.info(
                'TRAILING ACTIVATED | side=%s peak=%.6f stop=%.6f',
                trade.side,
                trade.peak_price,
                trade.peak_price * (1.0 - adaptive_offset_pct),
            )
        if trade.trailing_active:
            if trade.trailing_stop_price is None:
                logging.error(
                    'TRAILING STOP MISSING | context=evaluate_exit_engine side=%s entry_price=%.6f mark_price=%.6f',
                    trade.side,
                    trade.entry_price,
                    close_price,
                )
            else:
                candidate_stop = trade.peak_price * (1.0 - adaptive_offset_pct)
                new_stop = max(trade.trailing_stop_price, candidate_stop)
                if new_stop > trade.trailing_stop_price:
                    logging.info(
                        'TRAILING UPDATED | side=%s old_stop=%.6f new_stop=%.6f',
                        trade.side,
                        trade.trailing_stop_price,
                        new_stop,
                    )
                trade.trailing_stop_price = new_stop
        trailing_hit = (
            (trade.trailing_stop_price is not None and low_price <= trade.trailing_stop_price)
            or (trade.trailing_active and trade.trailing_stop_price is None)
        ) if exit_profile != "fixed_rr" else False
        tp_hit = high_price >= tp_price
        sl_hit = low_price <= sl_price
        mark_price = close_price
        if sl_hit:
            exit_price, exit_reason = sl_price, 'stop_loss'
        elif tp_hit:
            exit_price, exit_reason = tp_price, 'take_profit'
        elif trailing_hit:
            trailing_stop_price = getattr(trade, 'trailing_stop_price', None)
            if trailing_stop_price is None:
                logging.error(
                    'TRAILING STOP MISSING | context=evaluate_exit_engine side=%s entry_price=%.6f mark_price=%.6f',
                    trade.side,
                    trade.entry_price,
                    mark_price,
                )
                exit_price = float(mark_price)
            else:
                exit_price = float(trailing_stop_price)
            exit_reason = 'trailing_stop'
        else:
            exit_price, exit_reason = mark_price, 'max_hold_time'
        pnl_ratio = (exit_price / max(trade.entry_price, 1e-9)) - 1.0
    else:
        tp_price = trade.entry_price * (1.0 - trade.tp_pct)
        sl_price = trade.entry_price * (1.0 + trade.sl_pct)
        trailing_ready = exit_profile != "fixed_rr"
        if exit_profile == "delayed_trailing":
            trailing_ready = hold_seconds >= max(12.0, float(trade.max_hold_seconds) * 0.20)
        if not trade.trailing_active and trailing_ready and trade.trough_price <= trade.entry_price * (1.0 - adaptive_activation_pct):
            trade.trailing_active = True
            logging.info(
                'TRAILING ACTIVATED | side=%s trough=%.6f stop=%.6f',
                trade.side,
                trade.trough_price,
                trade.trough_price * (1.0 + adaptive_offset_pct),
            )
        if trade.trailing_active:
            if trade.trailing_stop_price is None:
                logging.error(
                    'TRAILING STOP MISSING | context=evaluate_exit_engine side=%s entry_price=%.6f mark_price=%.6f',
                    trade.side,
                    trade.entry_price,
                    close_price,
                )
            else:
                candidate_stop = trade.trough_price * (1.0 + adaptive_offset_pct)
                new_stop = min(trade.trailing_stop_price, candidate_stop)
                if new_stop < trade.trailing_stop_price:
                    logging.info(
                        'TRAILING UPDATED | side=%s old_stop=%.6f new_stop=%.6f',
                        trade.side,
                        trade.trailing_stop_price,
                        new_stop,
                    )
                trade.trailing_stop_price = new_stop
        trailing_hit = (
            (trade.trailing_stop_price is not None and high_price >= trade.trailing_stop_price)
            or (trade.trailing_active and trade.trailing_stop_price is None)
        ) if exit_profile != "fixed_rr" else False
        tp_hit = low_price <= tp_price
        sl_hit = high_price >= sl_price
        mark_price = close_price
        if sl_hit:
            exit_price, exit_reason = sl_price, 'stop_loss'
        elif tp_hit:
            exit_price, exit_reason = tp_price, 'take_profit'
        elif trailing_hit:
            trailing_stop_price = getattr(trade, 'trailing_stop_price', None)
            if trailing_stop_price is None:
                logging.error(
                    'TRAILING STOP MISSING | context=evaluate_exit_engine side=%s entry_price=%.6f mark_price=%.6f',
                    trade.side,
                    trade.entry_price,
                    mark_price,
                )
                exit_price = float(mark_price)
            else:
                exit_price = float(trailing_stop_price)
            exit_reason = 'trailing_stop'
        else:
            exit_price, exit_reason = mark_price, 'max_hold_time'
        pnl_ratio = (trade.entry_price / max(exit_price, 1e-9)) - 1.0

    no_follow_due = (
        exit_reason == 'max_hold_time'
        and
        bars_held >= trade.no_follow_through_min_bars
        and hold_seconds >= trade.no_follow_through_min_seconds
        and progress_ratio < trade.no_follow_through_progress_pct
    )
    if no_follow_due:
        pnl_ratio_now = float((mark_price / max(trade.entry_price, 1e-9)) - 1.0 if trade.side == 'LONG' else (trade.entry_price / max(mark_price, 1e-9)) - 1.0)
        explicit_fast_no_follow_profile = (
            int(getattr(trade, 'no_follow_through_min_bars', 0) or 0) <= 1
            and float(getattr(trade, 'no_follow_through_min_seconds', 0.0) or 0.0) <= EARLY_EXIT_GRACE_SECONDS
        )
        if hold_seconds < EARLY_EXIT_GRACE_SECONDS and pnl_ratio_now > -0.0006 and not explicit_fast_no_follow_profile:
            logging.info(
                "EARLY EXIT GRACE | symbol=%s elapsed=%.2f pnl=%.5f blocked=%s",
                str(getattr(trade, 'symbol', 'unknown')),
                hold_seconds,
                pnl_ratio_now,
                'true',
            )
        else:
            logging.info(
                'NO FOLLOW THROUGH EXIT | side=%s progress=%.5f required_progress=%.5f',
                trade.side,
                progress_ratio,
                trade.no_follow_through_progress_pct,
            )
            return ExitEvaluation(
                exit_price=float(mark_price),
                exit_reason='no_follow_through',
                hold_seconds=float(hold_seconds),
                pnl_ratio=float(pnl_ratio_now),
                bars_held=bars_held,
                explicit_exit_triggered=False,
                exit_index=int(latest_index),
            )

    normalized_progress = progress_ratio / max(trade.no_follow_through_progress_pct, 1e-9)
    profit_component = _clamp(pnl_ratio / max(trade.flat_exit_threshold_pct, 1e-9), -1.5, 2.0)
    follow_through_component = _clamp(normalized_progress, -1.0, 1.5)
    signal_component = _clamp((trade.signal_score - 0.8) * 0.9, -0.5, 0.8)
    stagnation_penalty = _clamp(hold_seconds / max(trade.max_hold_seconds, 1e-9), 0.0, 1.3) * (0.55 if progress_ratio > 0 else 0.8)
    reversal_penalty = _clamp((trade.peak_price - close_price) / max(trade.entry_price * max(adaptive_offset_pct, 1e-9), 1e-9), 0.0, 1.5) if trade.side == 'LONG' else _clamp((close_price - trade.trough_price) / max(trade.entry_price * max(adaptive_offset_pct, 1e-9), 1e-9), 0.0, 1.5)
    exit_quality_score = profit_component + follow_through_component + signal_component - stagnation_penalty - (0.45 * reversal_penalty)
    logging.info(
        'PNL ENGINE | exit_quality score=%.4f profit_pct=%.5f age=%.1f follow_through=%.4f',
        exit_quality_score,
        pnl_ratio,
        hold_seconds,
        normalized_progress,
    )
    if exit_reason == 'max_hold_time':
        if hold_seconds < trade.max_hold_seconds:
            if hold_seconds >= max(8.0, trade.no_follow_through_min_seconds) and exit_quality_score < -0.35:
                logging.info(
                    'EXIT QUALITY | reason=quality_decay score=%.4f age=%.1f pnl=%.5f',
                    exit_quality_score,
                    hold_seconds,
                    pnl_ratio,
                )
                return ExitEvaluation(
                    exit_price=float(mark_price),
                    exit_reason='exit_quality_decay',
                    hold_seconds=float(hold_seconds),
                    pnl_ratio=float((mark_price / max(trade.entry_price, 1e-9)) - 1.0 if trade.side == 'LONG' else (trade.entry_price / max(mark_price, 1e-9)) - 1.0),
                    bars_held=bars_held,
                    explicit_exit_triggered=False,
                    exit_index=int(latest_index),
                )
            return None
        if abs(pnl_ratio) < trade.flat_exit_threshold_pct:
            exit_reason = 'time_exit_flat'
        elif pnl_ratio > 0:
            exit_reason = 'time_exit_profit'
        else:
            exit_reason = 'time_exit_loss'
        logging.info(
            'TIME EXIT | side=%s pnl_ratio=%.5f hold_seconds=%.1f reason=%s',
            trade.side,
            pnl_ratio,
            hold_seconds,
            exit_reason,
        )
        return ExitEvaluation(
            exit_price=float(mark_price),
            exit_reason=exit_reason,
            hold_seconds=float(hold_seconds),
            pnl_ratio=float(pnl_ratio),
            bars_held=bars_held,
            explicit_exit_triggered=False,
            exit_index=int(latest_index),
        )
    explicit_exit_triggered = True
    return ExitEvaluation(
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        hold_seconds=float(hold_seconds),
        pnl_ratio=float(pnl_ratio),
        bars_held=bars_held,
        explicit_exit_triggered=explicit_exit_triggered,
        exit_index=int(latest_index),
    )


def compute_adaptive_trade_cap(regime: str, win_rate: float, drawdown: float, exec_rate: float, trades_last_5min: int) -> int:
    normalized_regime = str(regime or MarketRegime.NORMAL.value).upper()
    base = 120.0

    if normalized_regime == MarketRegime.HIGH_VOLATILITY.value:
        base = 300.0
    elif normalized_regime == 'TRENDING':
        base = 220.0
    elif normalized_regime == MarketRegime.NORMAL.value:
        base = 150.0

    if win_rate > 0.65 and drawdown < 0.05:
        base *= 1.8
    elif win_rate > 0.55:
        base *= 1.4

    if exec_rate < 0.40 and trades_last_5min > 20:
        base *= 1.5

    return int(min(base, MAX_ADAPTIVE_MAX_TRADES_PER_DAY))


def enrich_signal_strength_context(runtime_context: dict[str, Any]) -> None:
    enrich_runtime_signal_strength_context(runtime_context)


def setups_are_similar(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a.get('side') == b.get('side')
        and a.get('regime') == b.get('regime')
        and abs(float(a.get('score', 0.0)) - float(b.get('score', 0.0))) <= 0.01
        and abs(float(a.get('signal_strength', 0.0)) - float(b.get('signal_strength', 0.0))) <= 0.02
        and abs(float(a.get('volume_ratio', 0.0)) - float(b.get('volume_ratio', 0.0))) <= 0.10
        and abs(float(a.get('spread', 0.0)) - float(b.get('spread', 0.0))) <= 0.0015
    )


def compute_signal_score(signal: float, threshold: float, prev_score: float | None = None) -> float:
    score = signal / (threshold + 1e-8)
    score = max(0.0, min(score, 2.0))
    if prev_score is not None:
        score = (0.7 * score) + (0.3 * prev_score)
        score = max(0.0, min(score, 2.0))
    logging.info('SCORE FIX signal=%.4f threshold=%.4f score=%.4f', signal, threshold, score)
    return score


def evaluate_signal_decay_guard(
    signal_strength_series: Any,
    latest_signal_score: float,
    lookback: int = SIGNAL_DECAY_LOOKBACK,
) -> tuple[bool, float, float]:
    if signal_strength_series is None:
        return False, 0.0, 1.0
    try:
        arr = np.asarray(signal_strength_series, dtype=float)
    except Exception:
        return False, 0.0, 1.0
    arr = arr[np.isfinite(arr)]
    if arr.size < max(3, int(lookback)):
        return False, 0.0, 1.0
    window = arr[-int(lookback):]
    current = float(window[-1])
    previous_peak = float(np.max(window[:-1]))
    if previous_peak <= 1e-9:
        return False, 0.0, 1.0
    absolute_drop = max(0.0, previous_peak - current)
    decay_ratio = current / previous_peak
    blocked = (
        float(latest_signal_score) < SIGNAL_PRIORITY_BYPASS_THRESHOLD
        and absolute_drop >= SIGNAL_DECAY_MAX_DROP
        and decay_ratio <= SIGNAL_DECAY_RATIO_FLOOR
    )
    return bool(blocked), float(absolute_drop), float(decay_ratio)


def classify_signal_priority(signal_score: float) -> str:
    if signal_score >= SIGNAL_PRIORITY_BYPASS_THRESHOLD:
        return 'high'
    if signal_score >= SIGNAL_PRIORITY_EXECUTE_THRESHOLD:
        return 'medium'
    return 'low'


def compute_execution_score_threshold(
    market_regime: str,
    volatility: float,
    signal_strength: float = 0.0,
    signal_strength_threshold: float = 1.0,
    soft_trend_applied: bool = False,
    consecutive_losses: int = 0,
    performance_score: float = 0.0,
    trades_last_5min: int = 0,
    loops_without_trade: int = 0,
    aggression_mode: bool = AGGRESSION_MODE,
) -> float:
    normalized_regime = str(market_regime or MarketRegime.NORMAL.value).upper()
    regime_base_threshold = {
        MarketRegime.HIGH_VOLATILITY.value: 0.78,
        MarketRegime.NORMAL.value: 0.80,
        MarketRegime.LOW_ACTIVITY.value: 0.82,
        'TRENDING': 0.79,
    }.get(normalized_regime, 0.81)
    normalized_signal_strength = _clamp(
        float(signal_strength) / max(float(signal_strength_threshold), 1e-6),
        0.0,
        2.0,
    )
    volatility_relief = _clamp(float(volatility) * 8.75, 0.0, 0.018)
    regime_relief = {
        MarketRegime.HIGH_VOLATILITY.value: 0.0,
        'TRENDING': 0.0005,
        MarketRegime.NORMAL.value: 0.001,
        MarketRegime.LOW_ACTIVITY.value: 0.002,
    }.get(normalized_regime, 0.001)
    signal_relief = _clamp(max(0.0, normalized_signal_strength - 1.0) * 0.02, 0.0, 0.03)
    activity_relief = 0.004 if int(trades_last_5min) == 0 and int(loops_without_trade) > 20 else 0.0
    performance_tightening = 0.008 if float(performance_score) < 0.0 else 0.0
    effective_exec_threshold = _clamp(
        regime_base_threshold
        - volatility_relief
        - regime_relief
        - signal_relief
        - activity_relief
        + performance_tightening,
        EXECUTION_THRESHOLD_FLOOR,
        EXECUTION_THRESHOLD_CEILING,
    )
    if soft_trend_applied:
        effective_exec_threshold = _clamp(
            effective_exec_threshold + 0.02,
            EXECUTION_THRESHOLD_FLOOR,
            EXECUTION_THRESHOLD_CEILING,
        )
    if consecutive_losses >= 2:
        effective_exec_threshold = _clamp(
            effective_exec_threshold + 0.05,
            EXECUTION_THRESHOLD_FLOOR,
            EXECUTION_THRESHOLD_CEILING,
        )
    logging.info(
        "ADAPTIVE | symbol=%s regime=%s vol=%.6f threshold=%.6f",
        "GLOBAL",
        normalized_regime,
        float(volatility),
        float(effective_exec_threshold),
    )
    return effective_exec_threshold


def post_exit_guard_block(
    risk_state: RiskState,
    now: datetime,
    adjusted_score: float,
    relative_volume: float,
) -> tuple[bool, str, float]:
    """Return whether the post-exit cooldown blocks a fresh execution attempt.

    This helper uses ``risk_state`` as the single source of truth for the latest
    exit context so execution gating stays deterministic and testable.
    """
    block_seconds = 0.0
    normalized_reason = str(risk_state.last_exit_reason or 'none').strip().lower()
    if normalized_reason == 'stop_loss':
        block_seconds = 8.0
    elif normalized_reason == 'trailing_stop':
        block_seconds = 4.0
    elif normalized_reason == 'take_profit':
        block_seconds = 0.0
    elif risk_state.last_exit_time is not None:
        block_seconds = 8.0

    seconds_since_exit = float('inf')
    if risk_state.last_exit_time is not None:
        seconds_since_exit = max(0.0, (now - risk_state.last_exit_time).total_seconds())

    override = (
        adjusted_score >= 1.45
        and relative_volume >= 0.9
        and risk_state.consecutive_losses < 2
    )
    blocked = (
        risk_state.last_exit_time is not None
        and block_seconds > 0.0
        and seconds_since_exit < block_seconds
        and not override
    )
    logging.info(
        'POST EXIT GUARD | seconds_since_exit=%s block_seconds=%d last_exit_reason=%s override=%s',
        'none' if seconds_since_exit == float('inf') else f'{seconds_since_exit:.2f}',
        int(block_seconds),
        normalized_reason,
        str(override).lower(),
    )
    return blocked, 'post_exit_cooldown' if blocked else 'ok', block_seconds


@dataclass
class AdaptiveFilterController:
    min_volume_ratio: float
    min_trend_strength: float
    base_min_volume_ratio: float
    base_min_trend_strength: float
    min_volume_ratio_min: float = MIN_VOLUME_RATIO_MIN
    min_volume_ratio_max: float = MIN_VOLUME_RATIO_MAX
    no_trade_relax_loops: int = NO_TRADE_RELAX_LOOPS
    no_trade_disable_loops: int = NO_TRADE_DISABLE_LOOPS
    overtrading_threshold_5min: int = OVERTRADING_THRESHOLD_5MIN
    win_rate_window: int = WIN_RATE_WINDOW
    max_spread_ratio: float = 0.003
    spread_multiplier: float = DEFAULT_SPREAD_MULTIPLIER
    current_market_regime: str = MarketRegime.NORMAL.value
    adaptive_cooldown_seconds: int = DEFAULT_BASE_COOLDOWN_SECONDS
    adaptive_min_time_between_trades_seconds: int = DEFAULT_BASE_COOLDOWN_SECONDS
    base_cooldown_seconds: int = DEFAULT_BASE_COOLDOWN_SECONDS
    min_cooldown_seconds: int = DEFAULT_MIN_COOLDOWN_SECONDS
    max_cooldown_seconds: int = DEFAULT_MAX_COOLDOWN_SECONDS
    signals_generated_last_100: deque[int] = field(default_factory=lambda: deque(maxlen=100))
    signals_executable_last_100: deque[int] = field(default_factory=lambda: deque(maxlen=100))
    signals_executed_last_100: deque[int] = field(default_factory=lambda: deque(maxlen=100))
    filter_disable_until_trade: bool = False
    volume_filter_disabled_until_trade: bool = False
    recent_trade_pnls: deque[float] = field(default_factory=lambda: deque(maxlen=WIN_RATE_WINDOW))
    recent_exit_reasons: deque[str] = field(default_factory=lambda: deque(maxlen=WIN_RATE_WINDOW))
    recent_trade_times: deque[datetime] = field(default_factory=deque)
    rolling_spread_ratios: deque[float] = field(default_factory=lambda: deque(maxlen=ROLLING_SPREAD_WINDOW))
    execution_history: deque[int] = field(default_factory=lambda: deque(maxlen=100))
    recent_signal_scores: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    last_state_log_ts: float = 0.0
    last_no_trade_warning_bucket: int = -1
    last_no_trade_adjust_bucket: int = -1
    adaptive_max_trades_per_day: int = MIN_ADAPTIVE_MAX_TRADES_PER_DAY
    aggression_scale: float = 1.0
    current_position_size_scale: float = 1.0
    session_peak_pnl_usd: float = 0.0
    session_drawdown_usd: float = 0.0
    previous_drawdown_usd: float = 0.0
    daily_stop_triggered: bool = False
    last_block_reason: str = 'none'
    last_soft_limit_triggered: bool = False
    recent_setups: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=10))
    recent_scores: deque[float] = field(default_factory=lambda: deque(maxlen=5))
    last_entry_side: str | None = None
    last_entry_price: float | None = None
    last_entry_at: datetime | None = None
    last_entry_bar_id: str | None = None
    decision_counters: dict[str, int] = field(
        default_factory=lambda: {
            'pre_exec_block_count': 0,
            'final_block_count': 0,
            'executed_count': 0,
        }
    )
    execution_funnel_counters: dict[str, int] = field(
        default_factory=lambda: {
            'signals_generated': 0,
            'signals_after_filters': 0,
            'signals_after_profit_gate': 0,
            'executed_trades': 0,
        }
    )
    last_decision_counter_log_ts: float = 0.0

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> 'AdaptiveFilterController':
        if 'min_volume' in params and 'min_volume_ratio' not in params:
            logging.warning('Legacy min_volume detected in strategy params; converting to normalized min_volume_ratio=%.2f', DEFAULT_MIN_VOLUME_RATIO)
        base_min_volume_ratio = _clamp(_safe_param(params, 'min_volume_ratio', DEFAULT_MIN_VOLUME_RATIO), MIN_VOLUME_RATIO_MIN, MIN_VOLUME_RATIO_MAX)
        base_min_trend = _safe_param(params, 'min_trend_strength', DEFAULT_MIN_TREND_STRENGTH)
        base_max_trades = int(_safe_param(params, 'max_trades_per_day', 24))
        return cls(
            min_volume_ratio=base_min_volume_ratio,
            min_trend_strength=base_min_trend,
            base_min_volume_ratio=base_min_volume_ratio,
            base_min_trend_strength=base_min_trend,
            max_spread_ratio=_clamp(_safe_param(params, "max_spread_ratio", 0.003), MIN_SAFE_SPREAD_RATIO, MAX_SAFE_SPREAD_RATIO),
            spread_multiplier=max(1.0, _safe_param(params, "spread_multiplier", DEFAULT_SPREAD_MULTIPLIER)),
            adaptive_cooldown_seconds=int(_safe_param(params, 'base_cooldown', DEFAULT_BASE_COOLDOWN_SECONDS)),
            adaptive_min_time_between_trades_seconds=int(_safe_param(params, 'min_time_between_trades_seconds', DEFAULT_BASE_COOLDOWN_SECONDS)),
            base_cooldown_seconds=int(_safe_param(params, 'base_cooldown', DEFAULT_BASE_COOLDOWN_SECONDS)),
            min_cooldown_seconds=int(_safe_param(params, 'min_cooldown', DEFAULT_MIN_COOLDOWN_SECONDS)),
            max_cooldown_seconds=int(_safe_param(params, 'max_cooldown', DEFAULT_MAX_COOLDOWN_SECONDS)),
            adaptive_max_trades_per_day=max(MIN_ADAPTIVE_MAX_TRADES_PER_DAY, min(MAX_ADAPTIVE_MAX_TRADES_PER_DAY, base_max_trades)),
        )

    def current_params(self, params: dict[str, Any]) -> dict[str, Any]:
        live_params = dict(params)
        live_params['min_volume_ratio'] = round(_clamp(self.min_volume_ratio, self.min_volume_ratio_min, self.min_volume_ratio_max), 8)
        live_params.pop('min_volume', None)
        live_params['min_trend_strength'] = round(max(0.0, self.min_trend_strength), 8)
        live_params['max_spread_ratio'] = round(_clamp(self.max_spread_ratio, MIN_SAFE_SPREAD_RATIO, MAX_SAFE_SPREAD_RATIO), 8)
        regime = MarketRegime(self.current_market_regime)
        live_params['spread_multiplier'] = round(spread_multiplier_for_regime(self.spread_multiplier, regime), 8)
        live_params['min_volume_ratio'] = round(volume_threshold_for_regime(live_params['min_volume_ratio'], regime), 8)
        live_params['spread_multiplier'] = round(_clamp(live_params['spread_multiplier'], 1.0, DEFAULT_SPREAD_MULTIPLIER_MAX), 8)
        live_params['base_cooldown'] = int(self.base_cooldown_seconds)
        live_params['min_cooldown'] = int(self.min_cooldown_seconds)
        live_params['max_cooldown'] = int(self.max_cooldown_seconds)
        live_params['cooldown_seconds'] = int(self.adaptive_cooldown_seconds)
        live_params['min_time_between_trades_seconds'] = int(self.adaptive_min_time_between_trades_seconds)
        if self.volume_filter_disabled_until_trade:
            live_params['min_volume_ratio'] = 0.0
        if self.filter_disable_until_trade:
            live_params['min_atr_threshold'] = 0.0
            live_params['min_trend_strength'] = 0.0
            live_params['min_volume_ratio'] = 0.0
        return live_params

    def trades_last_5min(self, now: datetime) -> int:
        cutoff = now.timestamp() - 300.0
        while self.recent_trade_times and self.recent_trade_times[0].timestamp() < cutoff:
            self.recent_trade_times.popleft()
        return len(self.recent_trade_times)

    def maybe_log_state(self, now: datetime) -> None:
        if (now.timestamp() - self.last_state_log_ts) < FILTER_STATE_LOG_INTERVAL_SECONDS:
            return
        self.last_state_log_ts = now.timestamp()
        logging.info(
            'Execution stats | exec_rate=%.4f generated=%d executable=%d executed=%d win_rate=%.4f pnl=%.4f drawdown=%.4f adaptive_max_trades=%d aggression=%.4f position_scale=%.4f cooldown=%d regime=%s trades_last_5min=%d',
            self.execution_rate(),
            sum(self.signals_generated_last_100),
            sum(self.signals_executable_last_100),
            sum(self.signals_executed_last_100),
            self.win_rate(),
            sum(self.recent_trade_pnls),
            self.session_drawdown_usd,
            self.adaptive_max_trades_per_day,
            self.aggression_scale,
            self.current_position_size_scale,
            self.adaptive_cooldown_seconds,
            self.current_market_regime,
            self.trades_last_5min(now),
        )
        logging.info(
            'CURRENT FILTER STATE: min_volume_ratio=%.4f min_trend_strength=%.8f max_spread_ratio=%.6f spread_multiplier=%.4f filters_relaxed=%s volume_filter_disabled=%s',
            self.min_volume_ratio,
            self.min_trend_strength,
            self.max_spread_ratio,
            self.spread_multiplier,
            self.filter_disable_until_trade,
            self.volume_filter_disabled_until_trade,
        )
        self.maybe_log_decision_counters(now)

    def _adjust_volume(self, factor: float, reason: str, upper_bound: float | None = None) -> None:
        previous = self.min_volume_ratio
        upper = self.min_volume_ratio_max if upper_bound is None else min(self.min_volume_ratio_max, float(upper_bound))
        lower_bound = self.min_volume_ratio_min
        if reason == 'low_volume_block_decay':
            lower_bound = max(lower_bound, MIN_VOLUME_DECAY_FLOOR)
        self.min_volume_ratio = _clamp(self.min_volume_ratio * factor, lower_bound, upper)
        if reason == 'low_volume_block_decay':
            logging.info(
                'Adaptive change | param=min_volume_ratio old=%.4f new=%.4f floor=%.4f reason=%s',
                previous,
                self.min_volume_ratio,
                MIN_VOLUME_DECAY_FLOOR,
                reason,
            )
        else:
            logging.info('Adaptive change | param=min_volume_ratio old=%.4f new=%.4f reason=%s', previous, self.min_volume_ratio, reason)

    def _adjust_trend(self, factor: float, reason: str) -> None:
        previous = self.min_trend_strength
        self.min_trend_strength = max(0.0, self.min_trend_strength * factor)
        logging.info('Adaptive change | param=min_trend_strength old=%.8f new=%.8f reason=%s', previous, self.min_trend_strength, reason)

    def _adjust_spread(self, factor: float, reason: str) -> None:
        previous = self.spread_multiplier
        self.spread_multiplier = _clamp(self.spread_multiplier * factor, 1.0, DEFAULT_SPREAD_MULTIPLIER_MAX)
        logging.info('Adaptive change | param=spread_multiplier old=%.4f new=%.4f reason=%s', previous, self.spread_multiplier, reason)

    def record_block_reason(self, reason: str) -> None:
        self.last_block_reason = str(reason or 'unknown')
        if self.last_block_reason == 'low_volume':
            self._adjust_volume(0.98, 'low_volume_block_decay', upper_bound=0.55)

    def increment_decision_counter(self, counter_key: str) -> None:
        if counter_key not in self.decision_counters:
            self.decision_counters[counter_key] = 0
        self.decision_counters[counter_key] += 1

    def _position_state(self, risk_state: RiskState) -> str:
        return 'OPEN' if getattr(risk_state, 'open_positions', 0) > 0 else 'FLAT'

    def _cooldown_remaining(self, risk_state: RiskState, risk_cfg: RiskConfig, now: datetime, symbol: str | None = None) -> int:
        last_trade_time = getattr(risk_state, 'last_trade_time', None)
        if symbol:
            symbol_times = getattr(risk_state, 'symbol_last_trade_time', {})
            last_trade_time = symbol_times.get(symbol)
        if last_trade_time is None:
            return 0
        remaining = float(risk_cfg.cooldown_seconds) - (now - last_trade_time).total_seconds()
        return max(0, int(remaining))

    def log_pre_exec_block(
        self,
        *,
        reason: str,
        side: str,
        score: float,
        volume_ratio: float,
        spread: float,
        regime: str,
    ) -> None:
        self.increment_decision_counter('pre_exec_block_count')
        logging.info(
            'FILTER BLOCK | stage=pre_exec reason=%s side=%s score=%.3f volume=%.3f spread=%.6f regime=%s',
            reason,
            side,
            score,
            volume_ratio,
            spread,
            regime,
        )

    def log_final_block(
        self,
        *,
        reason: str,
        side: str,
        score: float,
        cooldown_remaining: int,
        position_state: str,
    ) -> None:
        self.increment_decision_counter('final_block_count')
        logging.info(
            'EXEC BLOCKED FINAL | reason=%s side=%s score=%.3f cooldown=%d position=%s',
            reason,
            side,
            score,
            cooldown_remaining,
            position_state,
        )

    def maybe_log_decision_counters(self, now: datetime) -> None:
        if (now.timestamp() - self.last_decision_counter_log_ts) < DECISION_COUNTER_LOG_INTERVAL_SECONDS:
            return
        self.last_decision_counter_log_ts = now.timestamp()
        logging.info(
            'Decision counters | pre_exec=%d final_block=%d executed=%d',
            self.decision_counters.get('pre_exec_block_count', 0),
            self.decision_counters.get('final_block_count', 0),
            self.decision_counters.get('executed_count', 0),
        )
        logging.info(
            'EXECUTION FUNNEL | signals_generated=%d signals_after_filters=%d signals_after_profit_gate=%d executed_trades=%d',
            self.execution_funnel_counters.get('signals_generated', 0),
            self.execution_funnel_counters.get('signals_after_filters', 0),
            self.execution_funnel_counters.get('signals_after_profit_gate', 0),
            self.execution_funnel_counters.get('executed_trades', 0),
        )

    def _normalize_market_regime(self, regime: Any) -> str:
        raw_regime = str(regime or MarketRegime.HIGH_VOLATILITY.value).upper()
        aliases = {
            'HIGH_IMPACT': MarketRegime.HIGH_VOLATILITY.value,
            'VOLATILE': MarketRegime.HIGH_VOLATILITY.value,
            'BREAKOUT': 'TRENDING',
            'QUIET': MarketRegime.LOW_ACTIVITY.value,
            'LOW_VOL': MarketRegime.LOW_ACTIVITY.value,
        }
        normalized = aliases.get(raw_regime, raw_regime)
        valid_regimes = {item.value for item in MarketRegime}
        if normalized not in valid_regimes and normalized != 'TRENDING':
            logging.warning('Unknown market regime %s detected; falling back to %s', raw_regime, MarketRegime.HIGH_VOLATILITY.value)
            return MarketRegime.HIGH_VOLATILITY.value
        return normalized

    def update_from_snapshot(self, latest_filter: Any | None) -> None:
        if latest_filter is None:
            return
        self.current_market_regime = self._normalize_market_regime(getattr(latest_filter, 'current_market_regime', MarketRegime.NORMAL.value))
        self.adaptive_cooldown_seconds = int(getattr(latest_filter, 'cooldown_seconds', self.base_cooldown_seconds))
        self.adaptive_min_time_between_trades_seconds = int(getattr(latest_filter, 'min_time_between_trades_seconds', self.base_cooldown_seconds))
        logging.info('Market regime: %s', self.current_market_regime)

    def record_signal(self, generated: bool, executable: bool, executed: bool, signal_score: float = 0.0) -> None:
        self.signals_generated_last_100.append(1 if generated else 0)
        self.signals_executable_last_100.append(1 if executable else 0)
        self.signals_executed_last_100.append(1 if executed else 0)
        self.execution_history.append(1 if executed else 0)
        if generated:
            self.execution_funnel_counters['signals_generated'] += 1
            self.recent_signal_scores.append(float(signal_score))
        if executable:
            self.execution_funnel_counters['signals_after_filters'] += 1

    def record_profit_gate_passed(self) -> None:
        # This funnel stage counts signals that passed the execution engine itself
        # (decide_execution_v2 -> should_execute=True), even if the later risk/timer
        # gate still blocks the trade before commit.
        self.execution_funnel_counters['signals_after_profit_gate'] += 1

    def execution_rate(self) -> float:
        return sum(self.signals_executed_last_100) / max(sum(self.signals_executable_last_100), 1)

    def win_rate(self) -> float:
        if not self.recent_trade_pnls:
            return 0.0
        return sum(1 for pnl in self.recent_trade_pnls if pnl > 0.0) / len(self.recent_trade_pnls)

    def current_drawdown_ratio(self, notional_usd: float) -> float:
        return abs(self.session_drawdown_usd) / max(abs(notional_usd), 1.0)

    def adaptive_trade_limit(self) -> int:
        return int(_clamp(self.adaptive_max_trades_per_day, MIN_ADAPTIVE_MAX_TRADES_PER_DAY, MAX_ADAPTIVE_MAX_TRADES_PER_DAY))

    def effective_position_scale(self) -> float:
        return _clamp(self.current_position_size_scale * self.aggression_scale, 0.25, STRONG_SIGNAL_POSITION_SCALE)

    def update_trade_capacity(self, daily_pnl_usd: float, drawdown_ratio: float) -> None:
        win_rate = self.win_rate()
        exec_rate = self.execution_rate()
        trades_last_5min = self.trades_last_5min(datetime.now(timezone.utc))
        previous = self.adaptive_max_trades_per_day
        dynamic_cap = compute_adaptive_trade_cap(
            regime=self.current_market_regime,
            win_rate=win_rate,
            drawdown=drawdown_ratio,
            exec_rate=exec_rate,
            trades_last_5min=trades_last_5min,
        )
        self.adaptive_max_trades_per_day = max(MIN_ADAPTIVE_MAX_TRADES_PER_DAY, min(MAX_ADAPTIVE_MAX_TRADES_PER_DAY, dynamic_cap))
        if self.adaptive_max_trades_per_day != previous:
            logging.info(
                'Adaptive change | param=max_trades_per_day old=%d new=%d reason=dynamic_trade_cap regime=%s win_rate=%.4f drawdown=%.4f exec_rate=%.4f trades_last_5min=%d daily_pnl=%.4f',
                previous,
                self.adaptive_max_trades_per_day,
                self.current_market_regime,
                win_rate,
                drawdown_ratio,
                exec_rate,
                trades_last_5min,
                daily_pnl_usd,
            )


    def _regime_min_time_factor(self, regime: str) -> float:
        normalized_regime = self._normalize_market_regime(regime)
        if normalized_regime == 'TRENDING':
            return 0.75
        if normalized_regime == MarketRegime.HIGH_VOLATILITY.value:
            return 0.6
        if normalized_regime == MarketRegime.LOW_ACTIVITY.value:
            return 1.25
        return 1.0

    def _signal_min_time_factor(self, signal_score: float) -> float:
        if signal_score > 0.9:
            return 0.3
        if signal_score > 0.8:
            return 0.5
        if signal_score > 0.75:
            return 0.7
        return 0.7

    def adaptive_min_time_between_trades(
        self,
        base_min_time: int,
        volatility: float,
        signal_score: float,
        market_regime: str,
        consecutive_losses: int,
        no_trade_loops: int,
    ) -> tuple[int, dict[str, float]]:
        normalized_regime = self._normalize_market_regime(market_regime)
        if signal_score > 0.9:
            tier_base = 8.0
        elif signal_score > 0.8:
            tier_base = 12.0
        else:
            tier_base = 24.0
        volatility_factor = 1.25
        if volatility >= 0.004:
            volatility_factor = 0.55
        elif volatility >= 0.002:
            volatility_factor = 0.72
        elif volatility <= 0.0005:
            volatility_factor = 1.45
        elif volatility <= 0.001:
            volatility_factor = 1.2
        signal_factor = self._signal_min_time_factor(signal_score)
        regime_factor = self._regime_min_time_factor(normalized_regime)
        loss_factor = 1.5 if consecutive_losses >= 3 else _clamp(1.0 + (consecutive_losses * 0.1), 1.0, 1.3)
        aggression_factor = _clamp(1.0 / max(self.aggression_scale, 1e-9), 0.9, 1.8)
        recovery_factor = 0.5 if no_trade_loops > RECOVERY_MIN_TIME_TRIGGER_LOOPS else 1.0
        base_anchor = max(float(base_min_time), float(self.base_cooldown_seconds), 10.0)
        base_factor = _clamp(base_anchor / 60.0, 0.75, 1.25)
        adaptive = int(round(tier_base * volatility_factor * signal_factor * regime_factor * loss_factor * aggression_factor * recovery_factor * base_factor))
        adaptive = int(_clamp(adaptive, ADAPTIVE_MIN_TIME_FLOOR_SECONDS, ADAPTIVE_MIN_TIME_CEILING_SECONDS))
        diagnostics = {
            'normalized_regime': normalized_regime,
            'tier_base': tier_base,
            'volatility_factor': volatility_factor,
            'signal_factor': signal_factor,
            'regime_factor': regime_factor,
            'loss_factor': loss_factor,
            'aggression_factor': aggression_factor,
            'recovery_factor': recovery_factor,
            'base_factor': base_factor,
        }
        return adaptive, diagnostics

    def adaptive_cooldown(
        self,
        volatility: float,
        consecutive_losses: int,
        signal_score: float,
        market_regime: str,
        daily_pnl_usd: float,
    ) -> tuple[int, dict[str, float]]:
        normalized_regime = self._normalize_market_regime(market_regime)
        base = max(10, int(self.base_cooldown_seconds))
        volatility_factor = 1.2
        if volatility >= 0.004:
            volatility_factor = 0.55
        elif volatility >= 0.002:
            volatility_factor = 0.75
        elif volatility <= 0.0005:
            volatility_factor = 1.35
        elif volatility <= 0.001:
            volatility_factor = 1.15
        signal_factor = 0.3 if signal_score > FORCE_EXECUTE_SIGNAL_THRESHOLD else (0.5 if signal_score > 0.8 else (0.75 if signal_score > 0.7 else 1.0))
        pnl_factor = 0.85 if daily_pnl_usd > 0 else (1.15 if daily_pnl_usd < 0 else 1.0)
        loss_factor = 1.6 if consecutive_losses >= 3 else _clamp(1.0 + (consecutive_losses * 0.2), 1.0, 1.4)
        regime_factor = 0.8 if normalized_regime in {'TRENDING', MarketRegime.HIGH_VOLATILITY.value} else (1.15 if normalized_regime == MarketRegime.LOW_ACTIVITY.value else 1.0)
        aggression_factor = _clamp(1.0 / max(self.aggression_scale, 1e-9), 0.9, 1.9)
        cooldown = int(round(base * volatility_factor * signal_factor * pnl_factor * loss_factor * regime_factor * aggression_factor))
        cooldown = int(_clamp(cooldown, SMART_COOLDOWN_FLOOR_SECONDS, SMART_COOLDOWN_CEILING_SECONDS))
        diagnostics = {
            'normalized_regime': normalized_regime,
            'volatility_factor': volatility_factor,
            'signal_factor': signal_factor,
            'pnl_factor': pnl_factor,
            'loss_factor': loss_factor,
            'regime_factor': regime_factor,
            'aggression_factor': aggression_factor,
        }
        return cooldown, diagnostics

    def _safe_execution_metric(self, value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(default)
        if not np.isfinite(numeric):
            numeric = float(default)
        return float(_clamp(numeric, minimum, maximum))

    def _normalize_signal_score_for_v24(self, value: float) -> float:
        # Legacy runtime gebruikt intern vaak score-ranges > 1.0 (tot ~3.0).
        # EVO v24 gating gebruikt expliciet genormaliseerde 0..1 input.
        return float(_clamp(float(value) / 3.0, 0.0, 1.0))

    def _decision_signal_components(
        self,
        *,
        signal_score: float,
        adjusted_score: float,
        relative_volume: float,
        signal_strength: float,
        spread: float,
        spread_threshold: float,
        volatility: float,
        fill_quality: float,
        market_regime: str,
    ) -> dict[str, float]:
        spread_pressure = _clamp(spread / max(spread_threshold, 1e-9), 0.0, 2.0)
        volatility_pressure = _clamp(volatility / 0.0035, 0.0, 2.0)
        regime_penalty = 0.14 if market_regime == MarketRegime.LOW_ACTIVITY.value else 0.0
        if market_regime == MarketRegime.HIGH_VOLATILITY.value:
            regime_penalty += 0.03

        return {
            'raw_signal_norm': self._safe_execution_metric(signal_score / 1.6, 0.0, 0.0, 1.0),
            'adjusted_signal_norm': self._safe_execution_metric(adjusted_score / 1.7, 0.0, 0.0, 1.0),
            'volume_norm': self._safe_execution_metric(relative_volume / 1.4, 0.0, 0.0, 1.0),
            'strength_norm': self._safe_execution_metric(signal_strength / 0.25, 0.0, 0.0, 1.0),
            'spread_quality_norm': self._safe_execution_metric(1.0 - (spread_pressure * 0.55), 0.0, 0.0, 1.0),
            'volatility_quality_norm': self._safe_execution_metric(1.0 - abs(volatility_pressure - 0.8), 0.0, 0.0, 1.0),
            'fill_quality_norm': self._safe_execution_metric(fill_quality, 0.75, 0.0, 1.0),
            'regime_penalty': self._safe_execution_metric(regime_penalty, 0.0, 0.0, 0.40),
        }

    def _composite_quality_score(
        self,
        *,
        components: dict[str, float],
        consecutive_losses: int,
        drawdown_ratio: float,
        no_trade_loops: int,
        execution_rate: float,
        win_rate: float,
    ) -> tuple[float, list[str], float]:
        penalties: list[str] = []
        weighted = (
            (components['raw_signal_norm'] * 0.20)
            + (components['adjusted_signal_norm'] * 0.25)
            + (components['volume_norm'] * 0.12)
            + (components['strength_norm'] * 0.10)
            + (components['spread_quality_norm'] * 0.12)
            + (components['volatility_quality_norm'] * 0.09)
            + (components['fill_quality_norm'] * 0.12)
        ) - components['regime_penalty']

        quality_floor = 0.50
        if consecutive_losses >= 3:
            quality_floor += 0.08
            penalties.append('loss_streak_tightening')
        elif consecutive_losses == 2:
            quality_floor += 0.04
            penalties.append('loss_streak_caution')

        if drawdown_ratio >= 0.08:
            quality_floor += 0.07
            penalties.append('drawdown_tightening')
        elif drawdown_ratio >= 0.05:
            quality_floor += 0.03
            penalties.append('drawdown_caution')

        if no_trade_loops >= 120:
            quality_floor -= 0.02
            penalties.append('stalled_loop_relax')

        if execution_rate < 0.03:
            quality_floor -= 0.015
            penalties.append('execution_recovery_relax')

        if win_rate >= 0.62 and drawdown_ratio < 0.04:
            quality_floor -= 0.01
            penalties.append('high_winrate_relax')

        composite_quality = self._safe_execution_metric(weighted, 0.0, 0.0, 1.0)
        quality_floor = self._safe_execution_metric(quality_floor, 0.45, 0.45, 0.72)
        return composite_quality, penalties, quality_floor

    def _log_decision_layers(self, payload: dict[str, Any]) -> None:
        logging.info('DECISION LAYERS | %s', json.dumps(payload, sort_keys=True, default=str))

    def decide_execution_v2(
        self,
        risk_state: RiskState,
        risk_cfg: RiskConfig,
        *args: Any,
        symbol: str | None = None,
        signal_priority: str | None = None,
        signal_score: float | None = None,
        latest_filter: Any = None,
        now: datetime | None = None,
        volatility: float | None = None,
        no_trade_loops: int | None = None,
        side: str | None = None,
        entry_price: float | None = None,
        spread: float | None = None,
        latest_bar_id: str | None = None,
    ) -> ExecutionDecision:
        if args:
            if len(args) == 7:
                parsed_symbol, parsed_signal_priority, parsed_signal_score, parsed_latest_filter, parsed_now, parsed_volatility, parsed_no_trade_loops = args
                symbol = symbol if symbol is not None else parsed_symbol
                signal_priority = signal_priority if signal_priority is not None else parsed_signal_priority
                signal_score = signal_score if signal_score is not None else parsed_signal_score
                latest_filter = latest_filter if latest_filter is not None else parsed_latest_filter
                now = now if now is not None else parsed_now
                volatility = volatility if volatility is not None else parsed_volatility
                no_trade_loops = no_trade_loops if no_trade_loops is not None else parsed_no_trade_loops
            elif len(args) == 6:
                parsed_signal_priority, parsed_signal_score, parsed_latest_filter, parsed_now, parsed_volatility, parsed_no_trade_loops = args
                signal_priority = signal_priority if signal_priority is not None else parsed_signal_priority
                signal_score = signal_score if signal_score is not None else parsed_signal_score
                latest_filter = latest_filter if latest_filter is not None else parsed_latest_filter
                now = now if now is not None else parsed_now
                volatility = volatility if volatility is not None else parsed_volatility
                no_trade_loops = no_trade_loops if no_trade_loops is not None else parsed_no_trade_loops
            else:
                raise TypeError('decide_execution_v2 expected 6 or 7 positional arguments after risk_cfg')

        if signal_priority is None or signal_score is None or latest_filter is None or now is None or volatility is None or no_trade_loops is None or side is None or entry_price is None or spread is None:
            raise TypeError('decide_execution_v2 missing required arguments')

        resolved_symbol = symbol or getattr(risk_state, 'symbol', None) or 'unknown'
        symbol = str(resolved_symbol)

        is_stacking_entry = int(getattr(risk_state, "open_positions", 0)) > 0

        logging.info(
            "SYMBOL CONTEXT | symbol=%s source=decide_execution_v2",
            symbol,
        )
        drawdown_ratio = self.current_drawdown_ratio(risk_cfg.notional_usd)
        self.update_trade_capacity(risk_state.daily_pnl_usd, drawdown_ratio)
        market_regime = self._normalize_market_regime(getattr(latest_filter, 'current_market_regime', self.current_market_regime))
        self.current_market_regime = market_regime
        dynamic_cooldown, cooldown_diag = self.adaptive_cooldown(
            volatility,
            risk_state.consecutive_losses,
            signal_score,
            market_regime,
            risk_state.daily_pnl_usd,
        )
        base_min_time = int(getattr(latest_filter, 'min_time_between_trades_seconds', risk_cfg.min_time_between_trades_seconds or self.base_cooldown_seconds))
        adaptive_min_time, min_time_diag = self.adaptive_min_time_between_trades(
            base_min_time=base_min_time,
            volatility=volatility,
            signal_score=signal_score,
            market_regime=market_regime,
            consecutive_losses=risk_state.consecutive_losses,
            no_trade_loops=no_trade_loops,
        )
        self.adaptive_cooldown_seconds = dynamic_cooldown
        self.adaptive_min_time_between_trades_seconds = adaptive_min_time
        if EVO_V24_CONFIG.adaptive_cooldown_enabled:
            self.adaptive_cooldown_seconds = int(
                _clamp(
                    self.adaptive_cooldown_seconds,
                    max(0, int(self.min_cooldown_seconds)),
                    EVO_V24_CONFIG.max_cooldown_seconds,
                )
            )
        if EVO_V24_CONFIG.adaptive_trade_spacing_enabled:
            self.adaptive_min_time_between_trades_seconds = int(
                _clamp(
                    self.adaptive_min_time_between_trades_seconds,
                    0,
                    EVO_V24_CONFIG.max_time_between_trades_seconds,
                )
            )

        raw_signal_score = self._safe_execution_metric(signal_score, 0.0, 0.0, 3.0)
        adjusted_score = self._safe_execution_metric(getattr(latest_filter, 'adjusted_signal_score', signal_score), raw_signal_score, 0.0, 3.0)
        normalized_raw_signal = self._normalize_signal_score_for_v24(raw_signal_score)
        normalized_adjusted_signal = self._normalize_signal_score_for_v24(adjusted_score)
        relative_volume = self._safe_execution_metric(getattr(latest_filter, 'relative_volume', 0.0), 0.0, 0.0, 10.0)
        signal_strength = self._safe_execution_metric(getattr(latest_filter, 'signal_strength', 0.0), 0.0, 0.0, 1.0)
        spread_threshold = self._safe_execution_metric(getattr(latest_filter, 'spread_threshold', spread), spread, 1e-9, 1.0)
        fill_quality = self._safe_execution_metric(getattr(latest_filter, 'fill_quality', 0.75), 0.75, 0.0, 1.0)
        execution_quality = self._safe_execution_metric(
            getattr(latest_filter, 'execution_quality', fill_quality),
            EVO_V24_CONFIG.missing_execution_quality_fallback,
            0.0,
            1.0,
        )
        symbol_quality = self._safe_execution_metric(
            getattr(latest_filter, 'symbol_quality', EVO_V24_CONFIG.missing_symbol_quality_fallback),
            EVO_V24_CONFIG.missing_symbol_quality_fallback,
            0.0,
            1.0,
        )
        explicit_spread_proxy = getattr(latest_filter, 'spread_proxy_score', None)
        if explicit_spread_proxy is None:
            explicit_spread_proxy = getattr(latest_filter, 'spread_proxy', None)
        spread_proxy_metric = None if explicit_spread_proxy is None else self._safe_execution_metric(explicit_spread_proxy, 0.0, 0.0, 2.0)
        spread_proxy = 0.0 if spread_proxy_metric is None else float(spread_proxy_metric)
        executable_history = int(sum(self.signals_executable_last_100))
        execution_metrics_immature = executable_history < 20
        exec_rate_raw = self.execution_rate()
        exec_rate = exec_rate_raw if not execution_metrics_immature else EVO_V24_CONFIG.missing_execution_rate_fallback
        adaptive_exec_rate = exec_rate_raw
        explicit_execution_rate = getattr(latest_filter, 'execution_rate', None)
        if explicit_execution_rate is None:
            explicit_execution_rate = getattr(latest_filter, 'execution_rate_score', None)
        has_explicit_v24_quality_metrics = any(
            getattr(latest_filter, attr, None) is not None
            for attr in (
                'fill_quality',
                'execution_quality',
                'symbol_quality',
                'regime_alignment',
                'volatility_suitability',
                'trend_bias',
            )
        )
        win_rate = self.win_rate()
        trades_today = max(risk_state.trades_today, risk_state.daily_trade_count)
        trades_last_5min = self.trades_last_5min(now)
        adaptive_trade_limit = self.adaptive_trade_limit()
        dynamic_profit_floor = float(getattr(latest_filter, 'signal_strength_threshold', 1.0))
        dynamic_profit_floor = max(dynamic_profit_floor, 0.8)
        if adaptive_exec_rate < 0.05:
            dynamic_profit_floor *= 0.85
        if adaptive_exec_rate < 0.02:
            dynamic_profit_floor *= 0.7
        if risk_state.daily_pnl_usd < 0:
            dynamic_profit_floor *= 1.1
        dynamic_profit_floor = _clamp(dynamic_profit_floor, 0.6, 1.2)
        signal_components = self._decision_signal_components(
            signal_score=raw_signal_score,
            adjusted_score=adjusted_score,
            relative_volume=relative_volume,
            signal_strength=signal_strength,
            spread=spread,
            spread_threshold=spread_threshold,
            volatility=volatility,
            fill_quality=fill_quality,
            market_regime=market_regime,
        )
        composite_quality, soft_penalties, min_composite_quality = self._composite_quality_score(
            components=signal_components,
            consecutive_losses=risk_state.consecutive_losses,
            drawdown_ratio=drawdown_ratio,
            no_trade_loops=no_trade_loops,
            execution_rate=adaptive_exec_rate,
            win_rate=win_rate,
        )
        v24_assessment = evaluate_composite_assessment(
            metrics={
                'raw_signal_score': normalized_raw_signal,
                'adjusted_signal_score': normalized_adjusted_signal,
                'signal_strength': signal_strength,
                'regime_alignment': self._safe_execution_metric(getattr(latest_filter, 'regime_alignment', 0.5), 0.5, 0.0, 1.0),
                'volatility_suitability': self._safe_execution_metric(getattr(latest_filter, 'volatility_suitability', 0.5), 0.5, 0.0, 1.0),
                'trend_bias': self._safe_execution_metric(getattr(latest_filter, 'trend_bias', 0.5), 0.5, 0.0, 1.0),
                'relative_volume': self._safe_execution_metric(relative_volume / 2.0, 0.0, 0.0, 1.0),
                'execution_quality': execution_quality,
                'fill_quality': fill_quality,
                'symbol_quality': symbol_quality,
            },
            config=EVO_V24_CONFIG,
            regime_label=market_regime,
            consecutive_losses=risk_state.consecutive_losses,
            drawdown_ratio=drawdown_ratio,
            no_trade_loops=no_trade_loops,
            win_rate=win_rate,
        )
        execution_assessment = evaluate_execution_assessment(
            metrics={
                'execution_rate': None if execution_metrics_immature or explicit_execution_rate is None else self._safe_execution_metric(explicit_execution_rate, EVO_V24_CONFIG.missing_execution_rate_fallback, 0.0, 1.0),
                'fill_quality': None if execution_metrics_immature else fill_quality,
                'execution_quality': None if execution_metrics_immature else execution_quality,
                'spread_proxy': spread_proxy_metric,
                'slippage_bps': None if execution_metrics_immature else self._safe_execution_metric(getattr(latest_filter, 'slippage_bps', 0.0), 0.0, 0.0, 250.0),
            },
            config=EVO_V24_CONFIG,
        )
        if has_explicit_v24_quality_metrics:
            composite_quality = _clamp((composite_quality * 0.55) + (v24_assessment.score * 0.45), 0.0, 1.0)
            soft_penalties.extend(list(v24_assessment.soft_penalties))
        soft_penalties.extend(list(execution_assessment.soft_penalties))
        if has_explicit_v24_quality_metrics:
            min_composite_quality = max(min_composite_quality, v24_assessment.quality_floor)
        execution_probability = _clamp(adjusted_score / max(dynamic_profit_floor, 1e-9), 0.0, 1.0)
        cooldown_remaining = self._cooldown_remaining(risk_state, risk_cfg, now)
        position_state = self._position_state(risk_state)
        layer_trace: dict[str, Any] = {
            'symbol': symbol,
            'market_eligibility': {'allow': True, 'reason': 'ok'},
            'regime_filter': {'allow': True, 'regime': market_regime, 'reason': 'ok'},
            'volatility_filter': {'allow': True, 'reason': 'ok', 'volatility': round(float(volatility), 8)},
            'execution_quality_filter': {'allow': True, 'reason': 'ok'},
            'profit_gate': {
                'allow': True,
                'reason': 'ok',
                'execution_probability': round(float(execution_probability), 6),
                'dynamic_profit_floor': round(float(dynamic_profit_floor), 6),
            },
            'risk_gate': {'allow': True, 'reason': 'ok'},
            'final_approval': {'allow': False, 'reason': 'pending'},
            'soft_penalties': list(soft_penalties),
            'quality': {
                'composite': round(float(composite_quality), 6),
                'minimum': round(float(min_composite_quality), 6),
                'components': {k: round(float(v), 6) for k, v in signal_components.items()},
            },
            'quality_v24': {
                'composite': round(float(v24_assessment.score), 6),
                'quality_floor': round(float(v24_assessment.quality_floor), 6),
                'breakdown': {k: round(float(v), 6) for k, v in v24_assessment.breakdown.items()},
                'bonuses': {k: round(float(v), 6) for k, v in v24_assessment.applied_bonuses.items()},
                'penalties': {k: round(float(v), 6) for k, v in v24_assessment.applied_penalties.items()},
                'hard_blocks': list(v24_assessment.hard_blocks),
                'soft_penalties': list(v24_assessment.soft_penalties),
                'critical_missing': list(v24_assessment.critical_missing),
            },
            'execution_v24': {
                'allow': execution_assessment.passed,
                'hard_blocks': list(execution_assessment.hard_blocks),
                'soft_penalties': list(execution_assessment.soft_penalties),
                'diagnostics': {k: round(float(v), 6) for k, v in execution_assessment.diagnostics.items()},
            },
        }

        logging.info(
            'RISK GATE DEBUG | consecutive_losses=%d pause_until=%s cooldown_effective=%d min_time_effective=%d',
            risk_state.consecutive_losses,
            risk_state.trading_paused_until.isoformat() if risk_state.trading_paused_until else 'none',
            self.adaptive_cooldown_seconds,
            self.adaptive_min_time_between_trades_seconds,
        )
        logging.info(
            'PRE-EXEC SNAPSHOT | side=%s score=%.3f volume=%.3f spread=%.6f regime=%s cooldown=%d position=%s',
            side,
            adjusted_score,
            relative_volume,
            spread,
            market_regime,
            cooldown_remaining,
            position_state,
        )
        logging.info(
            'Adaptive min_time | base=%d adaptive=%d volatility=%.6f signal=%.4f regime=%s',
            base_min_time,
            self.adaptive_min_time_between_trades_seconds,
            volatility,
            adjusted_score,
            market_regime,
        )
        logging.info('Adaptive min_time diagnostics | %s', _round_dict({k: v for k, v in min_time_diag.items() if isinstance(v, (int, float))}))
        logging.info('Adaptive cooldown diagnostics | %s', _round_dict({k: v for k, v in cooldown_diag.items() if isinstance(v, (int, float))}))

        def blocked(
            reason: str,
            *,
            post_exit_blocked: bool = False,
            low_volume_blocked: bool = False,
            candidate_scale: float = 0.0,
            hard_block: bool = True,
            reason_code: str = 'hard_block',
            stage: str = 'risk_gate',
        ) -> ExecutionDecision:
            layer_trace[stage] = {'allow': False, 'reason': reason}
            layer_trace['final_approval'] = {'allow': False, 'reason': reason}
            layer_trace['stage'] = stage
            layer_trace['reason_code'] = reason_code
            layer_trace['outcome'] = map_stage_to_outcome(stage)
            self._log_decision_layers(layer_trace)
            logging.info(
                'EXEC BLOCKED FINAL | reason=%s side=%s score=%.3f cooldown=%d position=%s',
                reason,
                side,
                adjusted_score,
                cooldown_remaining,
                position_state,
            )
            logging.info(
                'EXECUTION FUNNEL | probability=%.3f size_tier=%s candidate_scale=%.3f final_scale=0.000 should_execute=false reason=%s',
                execution_probability,
                'none',
                candidate_scale,
                reason,
            )
            logging.info(
                'EXEC DECISION V2 | score=%.4f dynamic_floor=%.4f probability=%.4f position_scale=%.4f executed=false reason=%s',
                adjusted_score,
                dynamic_profit_floor,
                execution_probability,
                0.0,
                reason,
            )
            return ExecutionDecision(
                should_execute=False,
                position_scale=0.0,
                adjusted_score=adjusted_score,
                dynamic_profit_floor=dynamic_profit_floor,
                reason=reason,
                execution_probability=execution_probability,
                size_tier=None,
                post_exit_guard_blocked=post_exit_blocked,
                low_volume_blocked=low_volume_blocked,
                reason_code=reason_code,
                outcome=map_stage_to_outcome(stage),
                hard_block=hard_block,
                soft_penalties=tuple(soft_penalties),
                layer_trace=layer_trace,
            )

        def _v24_hard_block_route(hard_blocks: tuple[str, ...]) -> tuple[str, str]:
            if any(code.startswith('regime_') for code in hard_blocks):
                return 'regime_filter', 'regime_v24'
            if any(code.startswith('volatility_') for code in hard_blocks):
                return 'volatility_filter', 'volatility_v24'
            return 'execution_quality_filter', 'quality_hard'

        if spread > (spread_threshold * 1.45):
            layer_trace['market_eligibility'] = {'allow': False, 'reason': 'spread_market_ineligible'}
            return blocked('spread_market_ineligible', stage='market_eligibility', reason_code='eligibility_spread')
        if has_explicit_v24_quality_metrics and v24_assessment.hard_blocks:
            block_stage, block_reason_code = _v24_hard_block_route(v24_assessment.hard_blocks)
            layer_trace[block_stage] = {
                'allow': False,
                'reason': ','.join(v24_assessment.hard_blocks),
            }
            layer_trace['execution_quality_filter'] = {
                'allow': block_stage == 'execution_quality_filter',
                'reason': 'v24_hard_block_routed',
            }
            return blocked('v24_hard_block', stage=block_stage, reason_code=block_reason_code)
        if not execution_assessment.passed:
            layer_trace['execution_quality_filter'] = {
                'allow': False,
                'reason': ','.join(execution_assessment.hard_blocks),
                'diagnostics': {k: round(float(v), 6) for k, v in execution_assessment.diagnostics.items()},
            }
            return blocked('execution_quality_hard_block', stage='execution_quality_filter', reason_code='execution_quality')
        if trades_last_5min > 30:
            return blocked('overtrading_hard_block', reason_code='risk_overtrade')
        if drawdown_ratio > 0.12:
            return blocked('drawdown_hard_block', reason_code='risk_drawdown')
        if trades_today > adaptive_trade_limit * 1.5:
            return blocked('adaptive_trade_limit_reached', reason_code='risk_trade_limit')
        if risk_state.consecutive_losses >= 3 and adjusted_score < 1.45:
            return blocked('recovery_mode_low_quality', reason_code='risk_recovery_quality')
        if market_regime == MarketRegime.LOW_ACTIVITY.value and adjusted_score < 0.9:
            layer_trace['regime_filter'] = {'allow': False, 'regime': market_regime, 'reason': 'regime_chop_low_score'}
            return blocked('regime_chop_low_score', stage='regime_filter', reason_code='regime_chop')
        if has_explicit_v24_quality_metrics and market_regime == MarketRegime.HIGH_VOLATILITY.value and not EVO_V24_CONFIG.allow_stress_regime:
            layer_trace['regime_filter'] = {'allow': False, 'regime': market_regime, 'reason': 'regime_stress_block'}
            return blocked('regime_stress_block', stage='regime_filter', reason_code='regime_stress')
        if volatility <= 0.0002 and adjusted_score < 1.15:
            layer_trace['volatility_filter'] = {'allow': False, 'reason': 'volatility_too_low_for_setup', 'volatility': round(float(volatility), 8)}
            return blocked('volatility_too_low_for_setup', stage='volatility_filter', reason_code='volatility_low')
        if volatility >= 0.0095 and adjusted_score < 1.25:
            layer_trace['volatility_filter'] = {'allow': False, 'reason': 'volatility_too_high_for_score', 'volatility': round(float(volatility), 8)}
            return blocked('volatility_too_high_for_score', stage='volatility_filter', reason_code='volatility_high')
        volatility_score = self._safe_execution_metric(getattr(latest_filter, 'volatility_suitability', 0.5), 0.5, 0.0, 1.0)
        if EVO_V24_CONFIG.volatility_filter_enabled and volatility_score < EVO_V24_CONFIG.min_volatility_score:
            layer_trace['volatility_filter'] = {'allow': False, 'reason': 'volatility_score_low', 'volatility_score': round(float(volatility_score), 6)}
            return blocked('volatility_score_low', stage='volatility_filter', reason_code='volatility_score_low')
        if EVO_V24_CONFIG.volatility_filter_enabled and volatility_score > EVO_V24_CONFIG.max_volatility_score and EVO_V24_CONFIG.extreme_vol_hard_block:
            layer_trace['volatility_filter'] = {'allow': False, 'reason': 'volatility_score_high', 'volatility_score': round(float(volatility_score), 6)}
            return blocked('volatility_score_high', stage='volatility_filter', reason_code='volatility_score_high')
        if (has_explicit_v24_quality_metrics and not v24_assessment.passed) or composite_quality < min_composite_quality:
            layer_trace['execution_quality_filter'] = {
                'allow': False,
                'reason': 'weak_composite_quality',
                'composite': round(float(composite_quality), 6),
                'minimum': round(float(min_composite_quality), 6),
            }
            return blocked('weak_composite_quality', stage='execution_quality_filter', reason_code='quality_composite')

        strong_volume_override = adjusted_score >= 1.6 and relative_volume >= 1.0 and risk_state.consecutive_losses < 2
        extreme_low_volume_floor = 0.35
        execution_min_relative_volume = 0.55 if adjusted_score < 1.6 else (0.40 if adjusted_score >= 1.8 else 0.45)
        if relative_volume < extreme_low_volume_floor:
            layer_trace['execution_quality_filter'] = {'allow': False, 'reason': 'low_volume_execution_block'}
            return blocked('low_volume_execution_block', low_volume_blocked=True, stage='execution_quality_filter', reason_code='liquidity_hard')

        override_requested = bool(getattr(latest_filter, 'profit_override', False))
        override_reason = str(getattr(latest_filter, 'profit_override_reason', '') or '').strip().lower()
        override_reason_explicit = override_reason not in {'', 'none', 'manual'}
        profit_gate = evaluate_profit_gate(
            config=EVO_V24_CONFIG,
            composite_score=composite_quality,
            execution_probability=execution_probability,
            signal_score=normalized_adjusted_signal,
            override_requested=override_requested,
            override_reason=override_reason,
        )
        effective_override_used = bool(
            override_requested
            and override_reason_explicit
            and normalized_adjusted_signal >= EVO_V24_CONFIG.profit_override_min_signal
            and 'profit_override_rejected' in profit_gate.reason_codes
            and not profit_gate.blocked
        )
        layer_trace['profit_gate'] = {
            'allow': profit_gate.passed or effective_override_used,
            'reason': 'ok' if profit_gate.passed else 'failed',
            'reason_codes': list(profit_gate.reason_codes),
            'threshold': round(float(profit_gate.threshold), 6),
            'expectancy_proxy': round(float(profit_gate.expectancy_proxy), 6),
        }
        if profit_gate.blocked or (override_requested and 'profit_override_rejected' in profit_gate.reason_codes and not (profit_gate.override_used or effective_override_used)):
            borderline_expectancy_soft_pass = (
                not override_requested
                and execution_metrics_immature
                and bool({'profit_signal_floor_failed', 'profit_composite_floor_failed'} & set(profit_gate.reason_codes))
                and execution_probability >= 0.6
                and adjusted_score >= 0.4
                and composite_quality >= max(min_composite_quality - 0.02, 0.45)
                and not has_explicit_v24_quality_metrics
            )
            if borderline_expectancy_soft_pass:
                layer_trace['profit_gate'] = {
                    'allow': True,
                    'reason': 'borderline_expectancy_soft_pass',
                    'reason_codes': list(profit_gate.reason_codes),
                    'threshold': round(float(profit_gate.threshold), 6),
                    'expectancy_proxy': round(float(profit_gate.expectancy_proxy), 6),
                }
                soft_penalties.append('borderline_expectancy_soft_pass')
            else:
                blocked_reason = 'profit_gate_blocked'
                reason_code = 'profit_gate'
                if 'profit_override_rejected' in profit_gate.reason_codes:
                    blocked_reason = 'profit_override_rejected'
                    reason_code = 'profit_override_rejected'
                elif 'profit_expectancy_floor_failed' in profit_gate.reason_codes:
                    if execution_probability < 0.5:
                        blocked_reason = 'below_soft_probability_floor'
                        reason_code = 'profit_probability_floor'
                    else:
                        blocked_reason = 'expectancy_proxy_too_low'
                        reason_code = 'profit_expectancy'
                return blocked(
                    blocked_reason,
                    stage='profit_gate',
                    reason_code=reason_code,
                    hard_block=blocked_reason not in {'below_soft_probability_floor', 'expectancy_proxy_too_low'},
                )
        if profit_gate.override_used or effective_override_used:
            layer_trace['profit_gate'] = {
                'allow': True,
                'reason': 'profit_override_approved',
                'override_reason': override_reason,
            }
            soft_penalties.append('profit_override_applied')
        if execution_probability < 0.8:
            size_tier_name = 'reduced_half_size'
            size_tier_scale = 0.5
        else:
            size_tier_name = 'full_size'
            size_tier_scale = 1.0

        score_factor = _clamp(adjusted_score, 0.6, 1.35)
        overtrade_factor = 0.7 if trades_last_5min > self.overtrading_threshold_5min else 1.0
        drawdown_factor = 0.55 if drawdown_ratio >= 0.10 else (0.70 if drawdown_ratio >= 0.08 else (0.85 if drawdown_ratio >= 0.05 else 1.0))
        volatility_factor = 0.85 if _clamp(volatility / 0.003, 0.5, 1.5) > 1.1 else 1.0
        win_rate_factor = 1.05 if win_rate >= 0.6 else (0.9 if win_rate and win_rate < 0.4 else 1.0)
        adaptive_limit_factor = 0.7 if trades_today >= adaptive_trade_limit else 1.0
        self.last_soft_limit_triggered = trades_today >= adaptive_trade_limit
        pre_scale = size_tier_scale * score_factor * overtrade_factor * drawdown_factor * volatility_factor * win_rate_factor * adaptive_limit_factor
        final_position_scale = _clamp(pre_scale * self.current_position_size_scale, 0.20, 1.25)

        post_exit_blocked, post_exit_reason, _ = post_exit_guard_block(
            risk_state,
            now,
            adjusted_score,
            relative_volume,
        )
        if post_exit_blocked:
            layer_trace['risk_gate'] = {'allow': False, 'reason': post_exit_reason or 'post_exit_cooldown'}
            return blocked(
                post_exit_reason or 'post_exit_cooldown',
                post_exit_blocked=True,
                candidate_scale=pre_scale,
                stage='risk_gate',
                reason_code='risk_post_exit',
                hard_block=False,
            )
        # Liquidity is gated in two layers:
        # 1) extreme_low_volume_floor: hard block for thin liquidity, never overridden.
        # 2) execution_min_relative_volume: normal execution floor that strong signals may override.
        if relative_volume < execution_min_relative_volume and not strong_volume_override:
            layer_trace['execution_quality_filter'] = {'allow': False, 'reason': 'low_volume_execution_block'}
            return blocked(
                'low_volume_execution_block',
                low_volume_blocked=True,
                candidate_scale=pre_scale,
                stage='execution_quality_filter',
                reason_code='liquidity_soft_floor',
            )

        setup_key = (
            side,
            round(adjusted_score, 3),
            round(signal_strength, 3),
            round(relative_volume, 2),
            round(spread, 5),
            market_regime,
        )
        current_setup = {
            'side': side,
            'score': adjusted_score,
            'signal_strength': signal_strength,
            'volume_ratio': relative_volume,
            'spread': spread,
            'regime': market_regime,
            'timestamp': now,
            'key': setup_key,
        }
        self.recent_setups.append(current_setup)
        while self.recent_setups and (now - self.recent_setups[0]['timestamp']).total_seconds() > REPEATED_SETUP_LOOKBACK_SECONDS:
            self.recent_setups.popleft()
        similar_count = sum(
            1
            for setup in self.recent_setups
            if (now - setup['timestamp']).total_seconds() <= REPEATED_SETUP_LOOKBACK_SECONDS and setups_are_similar(setup, current_setup)
        )
        max_repeated_setups = int(os.getenv('MAX_REPEATED_SETUPS', str(min(MAX_REPEATED_SETUPS_DEFAULT, 5))))
        repeated_setup_lockout = similar_count >= max_repeated_setups
        self.recent_scores.append(adjusted_score)
        score_variance = pvariance(self.recent_scores) if len(self.recent_scores) >= 2 else 0.0
        score_stagnation_block = (
            len(self.recent_scores) >= SCORE_STAGNATION_MIN_WINDOW
            and score_variance <= SCORE_STAGNATION_VARIANCE_THRESHOLD
            and (max(self.recent_scores) - min(self.recent_scores)) < 0.008
        )
        logging.info(
            'SETUP DEBUG | side=%s score=%.3f signal=%.3f volume=%.3f spread=%.5f regime=%s similar_count=%d',
            side,
            adjusted_score,
            signal_strength,
            relative_volume,
            spread,
            market_regime,
            similar_count,
        )

        same_bar_block = (
            not risk_state.same_bar_entry_allowed
            and latest_bar_id is not None
            and latest_bar_id == self.last_entry_bar_id
            and adjusted_score < 1.0
        )
        min_reentry_move = max(entry_price * (0.0002 if adjusted_score > 1.2 else 0.0004), spread * 1.5)
        reentry_block = False
        post_loss_reentry_block = False
        if self.last_entry_side == side and self.last_entry_price is not None and self.last_entry_at is not None:
            if abs(entry_price - self.last_entry_price) < min_reentry_move and (now - self.last_entry_at) < timedelta(seconds=120):
                reentry_block = True
            if risk_state.last_exit_reason in {'stop_loss', 'trailing_stop'} and risk_state.last_exit_time is not None:
                displacement_threshold = max(entry_price * 0.0006, spread * 2.0)
                if (now - risk_state.last_exit_time) < timedelta(seconds=20) and abs(entry_price - self.last_entry_price) < displacement_threshold:
                    post_loss_reentry_block = True

        if same_bar_block:
            return blocked('same_bar_entry_blocked', candidate_scale=pre_scale, reason_code='risk_same_bar')
        if post_loss_reentry_block:
            return blocked('post_loss_reentry_not_displaced', candidate_scale=pre_scale, reason_code='risk_post_loss_reentry')
        # =========================================
        # 🔥 FIX 4 — DISABLE REENTRY BLOCK
        # =========================================
        disable_reentry_block = str(os.getenv('DISABLE_REENTRY_BLOCK', 'true')).lower() in ('1', 'true', 'yes')

        if reentry_block:
            # use global stacking state (do not redeclare)
            # is_stacking_entry already defined above
            if is_stacking_entry:
                logging.warning(
                    "STACK REENTRY OVERRIDE | allowing same price zone | symbol=%s",
                    symbol,
                )
            elif disable_reentry_block:
                logging.warning(
                    'REENTRY BLOCK DISABLED → forcing execution | symbol=%s',
                    symbol,
                )
            else:
                return blocked('reentry_price_not_displaced', candidate_scale=pre_scale, reason_code='risk_reentry_displacement')
        # ================================
        # REPEATED SETUP LOCKOUT FIX
        # ================================
        if similar_count >= max_repeated_setups:
            if FORCE_MODE:
                logging.warning(
                    'FORCE MODE → bypass repeated setup hard lock | count=%d max=%d',
                    similar_count,
                    max_repeated_setups,
                )
            else:
                self.adaptive_cooldown_seconds = max(self.adaptive_cooldown_seconds, 60)
                logging.warning(
                    'REPEATED SETUP HARD BLOCK | side=%s score=%.3f signal=%.3f volume=%.3f spread=%.5f similar=%d max=%d',
                    side,
                    adjusted_score,
                    signal_strength,
                    relative_volume,
                    spread,
                    similar_count,
                    max_repeated_setups,
                )
                logging.warning('FORCED COOLDOWN due to repeated setup | cooldown=%d', self.adaptive_cooldown_seconds)
                return blocked('repeated_setup_lockout', candidate_scale=pre_scale, reason_code='risk_repeated_setup')
        if score_stagnation_block:
            return blocked('score_stagnation_block', candidate_scale=pre_scale, reason_code='risk_score_stagnation')

        if final_position_scale < 0.05:
            logging.warning("MIN SCALE FLOOR ACTIVATED")
            final_position_scale = 0.05

        logging.info(
            'EXECUTION FUNNEL | probability=%.3f size_tier=%s candidate_scale=%.3f final_scale=%.3f should_execute=true reason=%s',
            execution_probability,
            size_tier_name,
            pre_scale,
            final_position_scale,
            'ok',
        )
        logging.info(
            'EXEC DECISION V2 | score=%.4f dynamic_floor=%.4f probability=%.4f position_scale=%.4f executed=true reason=%s',
            adjusted_score,
            dynamic_profit_floor,
            execution_probability,
            final_position_scale,
            'ok',
        )
        layer_trace['risk_gate'] = {'allow': True, 'reason': 'ok'}
        layer_trace['final_approval'] = {'allow': True, 'reason': 'ok', 'size_tier': size_tier_name}
        layer_trace['stage'] = 'approved'
        layer_trace['reason_code'] = 'decision_ok'
        layer_trace['outcome'] = 'ENTRY_APPROVED'
        self._log_decision_layers(layer_trace)
        return ExecutionDecision(
            should_execute=True,
            position_scale=final_position_scale,
            adjusted_score=adjusted_score,
            dynamic_profit_floor=dynamic_profit_floor,
            reason='ok',
            execution_probability=execution_probability,
            size_tier=size_tier_name,
            reason_code='decision_ok',
            outcome='ENTRY_APPROVED',
            hard_block=False,
            soft_penalties=tuple(dict.fromkeys(soft_penalties)),
            layer_trace=layer_trace,
        )

    def commit_execution(self, decision: ExecutionDecision) -> None:
        if not decision.should_execute:
            return
        self.current_position_size_scale = float(decision.position_scale)
        self.execution_funnel_counters['executed_trades'] += 1
        self.increment_decision_counter('executed_count')
        logging.info(
            'EXECUTION COMMIT | reason=%s size_tier=%s committed_scale=%.3f',
            decision.reason,
            decision.size_tier or 'none',
            self.current_position_size_scale,
        )

    def allow_trade(
        self,
        risk_state: RiskState,
        risk_cfg: RiskConfig,
        signal_priority: str,
        signal_score: float,
        latest_filter: Any,
        now: datetime,
        volatility: float,
        no_trade_loops: int,
        *,
        side: str,
        entry_price: float,
        spread: float,
        latest_bar_id: str | None,
        execution_decision: ExecutionDecision,
        symbol: str,
        quality_score: float = 0.0,
    ) -> tuple[bool, str, int]:
        effective_risk_cfg = replace(risk_cfg)
        signal_strength = float(getattr(latest_filter, 'signal_strength', signal_score))

        if signal_score >= 1.3:
            effective_risk_cfg.cooldown_seconds = 0
            effective_risk_cfg.min_time_between_trades_seconds = 0
        else:
            effective_risk_cfg.cooldown_seconds = min(effective_risk_cfg.cooldown_seconds, self.adaptive_cooldown_seconds)
            effective_risk_cfg.min_time_between_trades_seconds = min(
                effective_risk_cfg.min_time_between_trades_seconds,
                self.adaptive_min_time_between_trades_seconds,
            )
        if signal_strength > 0.15:
            effective_risk_cfg.cooldown_seconds = int(round(effective_risk_cfg.cooldown_seconds * 0.3))
        elif signal_strength > 0.08:
            effective_risk_cfg.cooldown_seconds = int(round(effective_risk_cfg.cooldown_seconds * 0.6))
        effective_risk_cfg.cooldown_seconds = max(0, int(effective_risk_cfg.cooldown_seconds))
        effective_risk_cfg.max_trades_per_day = MAX_DAILY_TRADES_HARD_CAP
        risk_state.max_trades_per_day = effective_risk_cfg.max_trades_per_day

        if len(performance_memory) > 5:
            recent_losses = sum(
                1 for t in performance_memory[-5:] if t["result"] == "LOSS"
            )
            if recent_losses >= 3:
                logging.warning("COOLDOWN ACTIVATED")
                return False, 'cooldown_activated', self.adaptive_cooldown_seconds

        max_positions_per_symbol = 2
        if int(getattr(risk_state, "open_positions", 0)) > max_positions_per_symbol:
            logging.warning(
                "STACK LIMIT HIT | symbol=%s positions=%d max=%d",
                symbol,
                int(getattr(risk_state, "open_positions", 0)),
                max_positions_per_symbol,
            )
            return False, "max_positions_reached", self.adaptive_cooldown_seconds

        ok, reason = can_open_new_position(risk_state, effective_risk_cfg, now=now, symbol=symbol)
        allowed_spread_ratio = max(float(getattr(latest_filter, 'spread_threshold', spread)), 1e-9)
        fast_lane = is_level7_fast_lane(
            quality_score=quality_score,
            signal_score=signal_score,
            spread_ratio=float(spread),
            allowed_spread_ratio=allowed_spread_ratio,
            realized_volatility=float(volatility),
        )
        logging.info(
            "LEVEL7 OVERRIDE | symbol=%s allowed=false reason=%s",
            symbol,
            reason,
        )
        is_stacking_entry = int(getattr(risk_state, "open_positions", 0)) > 0
        if reason == 'min_time_between_trades_active':
            is_stacking_entry = int(getattr(risk_state, "open_positions", 0)) > 0
            if is_stacking_entry:
                logging.warning(
                    "STACKING OVERRIDE | bypass min_time | symbol=%s",
                    symbol,
                )
                min_time_remaining = 0.0
                effective_cooldown = 0
                ok = True
                reason = "stacking_override"
            elif (
                signal_score >= 1.0
                and float(getattr(latest_filter, 'relative_volume', 0.0) or 0.0) >= 1.25
                and risk_state.consecutive_losses < 2
            ):
                logging.info(
                    "HIGH QUALITY TIMER OVERRIDE | symbol=%s score=%.3f volume=%.3f",
                    symbol,
                    signal_score,
                    float(getattr(latest_filter, 'relative_volume', 0.0) or 0.0),
                )
                effective_cooldown = 0
                ok = True
                reason = "high_quality_override"

        if not ok:
            if reason == 'cooldown_active':
                cooldown_remaining_seconds = self._cooldown_remaining(risk_state, risk_cfg, now, symbol=symbol)
                logging.info(
                    "EXECUTION BLOCKED | reason=cooldown symbol=%s remaining=%.2f",
                    symbol,
                    float(cooldown_remaining_seconds),
                )
            self.log_final_block(
                reason=reason,
                side=side,
                score=execution_decision.adjusted_score,
                cooldown_remaining=self._cooldown_remaining(risk_state, risk_cfg, now, symbol=symbol),
                position_state=self._position_state(risk_state),
            )
            return False, reason, self.adaptive_cooldown_seconds

        logging.info(
            'EXEC FINAL | score=%.3f side=%s position_scale=%.4f dynamic_floor=%.4f execution_reason=%s size_tier=%s risk_reason=%s',
            execution_decision.adjusted_score,
            side,
            execution_decision.position_scale,
            execution_decision.dynamic_profit_floor,
            execution_decision.reason,
            execution_decision.size_tier or 'none',
            reason,
        )
        risk_state.same_bar_entry_allowed = signal_score >= 1.0
        return True, 'ok', self.adaptive_cooldown_seconds

    def maybe_override_filter(self, latest_filter: Any, signal_priority: str, signal_score: float) -> bool:
        if latest_filter.passed:
            return True
        if signal_priority != 'high':
            return False
        if latest_filter.reason_code == 'wide_spread' and latest_filter.spread_ratio <= (latest_filter.spread_threshold * HIGH_CONFIDENCE_SPREAD_OVERRIDE_FACTOR):
            logging.info('Adaptive change | param=spread_override old=%.6f new=%.6f reason=high_confidence_signal', latest_filter.spread_threshold, latest_filter.spread_ratio)
            return True
        if signal_score > HIGH_QUALITY_OVERRIDE_THRESHOLD and latest_filter.reason_code == 'poor_fill_risk':
            logging.info('Adaptive change | param=fill_risk_override old=%.4f new=%.4f reason=%s', float(getattr(latest_filter, 'fill_risk_score', 0.0)), signal_score, latest_filter.reason_code)
            return True
        volume_override_allowed = (
            signal_score >= 1.25
            and float(getattr(latest_filter, 'relative_volume', 0.0)) >= 0.95
        )
        if latest_filter.reason_code == 'low_volume':
            logging.info(
                'LOW VOLUME override check | signal_score=%.4f relative_volume=%.4f required_signal=1.2500 required_volume=0.9500 allowed=%s',
                signal_score,
                float(getattr(latest_filter, 'relative_volume', 0.0)),
                str(volume_override_allowed).lower(),
            )
            if signal_score >= SIGNAL_PRIORITY_FILTER_OVERRIDE_THRESHOLD and volume_override_allowed:
                logging.info('Adaptive change | param=filter_override old=0.0000 new=%.4f reason=%s', signal_score, latest_filter.reason_code)
                return True
            return False
        if signal_score >= SIGNAL_PRIORITY_FILTER_OVERRIDE_THRESHOLD and latest_filter.reason_code in {'low_trend_strength', 'weak_trend'}:
            logging.info('Adaptive change | param=filter_override old=0.0000 new=%.4f reason=%s', signal_score, latest_filter.reason_code)
            return True
        return False

    def update_profit_protection(self, risk_state: RiskState, risk_cfg: RiskConfig) -> None:
        self.session_peak_pnl_usd = max(self.session_peak_pnl_usd, risk_state.daily_pnl_usd)
        self.previous_drawdown_usd = self.session_drawdown_usd
        self.session_drawdown_usd = min(0.0, risk_state.daily_pnl_usd - self.session_peak_pnl_usd)
        daily_loss_limit_usd = abs(risk_cfg.notional_usd) * abs(risk_cfg.max_daily_loss_pct) * DAILY_LOSS_STOP_BUFFER
        drawdown_breach = risk_state.daily_pnl_usd <= -daily_loss_limit_usd
        self.daily_stop_triggered = drawdown_breach
        drawdown_ratio = self.current_drawdown_ratio(risk_cfg.notional_usd)
        previous_aggression = self.aggression_scale
        self.aggression_scale = _clamp(1.0 - (drawdown_ratio * 8.0), DRAWDOWN_AGGRESSION_FLOOR, 1.1)
        if self.aggression_scale != previous_aggression:
            logging.info('Adaptive change | param=aggression_scale old=%.4f new=%.4f reason=drawdown_control', previous_aggression, self.aggression_scale)
        if drawdown_breach:
            previous_scale = self.current_position_size_scale
            self.current_position_size_scale = max(0.25, self.current_position_size_scale * 0.5)
            if previous_scale != self.current_position_size_scale:
                logging.info('Adaptive change | param=position_size_scale old=%.4f new=%.4f reason=adaptive_daily_stop_scaling', previous_scale, self.current_position_size_scale)
        elif self.session_drawdown_usd < self.previous_drawdown_usd:
            previous_scale = self.current_position_size_scale
            self.current_position_size_scale = max(0.25, self.current_position_size_scale * 0.9)
            self.aggression_scale = max(DRAWDOWN_AGGRESSION_FLOOR, self.aggression_scale * 0.92)
            if previous_scale != self.current_position_size_scale:
                logging.info('Adaptive change | param=position_size_scale old=%.4f new=%.4f reason=drawdown_increasing', previous_scale, self.current_position_size_scale)


    def on_loop(self, loop_count_without_trade: int, now: datetime, latest_filter: Any | None = None) -> None:
        self.update_from_snapshot(latest_filter)
        recovery_active = bool(
            latest_filter is not None
            and getattr(latest_filter, 'reason_code', 'none') != 'no_signal'
            and loop_count_without_trade > RECOVERY_MIN_TIME_TRIGGER_LOOPS
        )
        logging.info(
            'Recovery system | active=%s reason=%s loops_without_trade=%d threshold=%d',
            str(recovery_active).lower(),
            getattr(latest_filter, 'reason_code', 'none') if recovery_active else 'none',
            loop_count_without_trade,
            RECOVERY_MIN_TIME_TRIGGER_LOOPS,
        )
        if recovery_active:
            previous_min_time = self.adaptive_min_time_between_trades_seconds
            recovered_min_time = max(
                ADAPTIVE_MIN_TIME_FLOOR_SECONDS,
                int(round(self.adaptive_min_time_between_trades_seconds * 0.5)),
            )
            if recovered_min_time != previous_min_time:
                self.adaptive_min_time_between_trades_seconds = recovered_min_time
                logging.info(
                    'Recovery triggered | active=true reason=%s previous_min_time=%d new_min_time=%d',
                    getattr(latest_filter, 'reason_code', 'unknown'),
                    previous_min_time,
                    self.adaptive_min_time_between_trades_seconds,
                )
        if loop_count_without_trade > 30:
            self._adjust_volume(0.9, 'no_trade_recovery_boost', upper_bound=0.55)
        if loop_count_without_trade > 0 and loop_count_without_trade % self.no_trade_relax_loops == 0:
            bucket = loop_count_without_trade // self.no_trade_relax_loops
            if bucket != self.last_no_trade_adjust_bucket:
                self.last_no_trade_adjust_bucket = bucket
                self.min_volume_ratio *= 0.9
                self.spread_multiplier *= 1.1
                self.min_volume_ratio = max(0.2, self.min_volume_ratio)
                self.spread_multiplier = min(5.0, self.spread_multiplier)
                self._adjust_trend(0.8, 'no_trade_recovery')
                previous_cooldown = self.adaptive_cooldown_seconds
                self.adaptive_cooldown_seconds = max(SMART_COOLDOWN_FLOOR_SECONDS, int(round(self.adaptive_cooldown_seconds * 0.5)))
                self.adaptive_min_time_between_trades_seconds = max(ADAPTIVE_MIN_TIME_FLOOR_SECONDS, int(round(self.adaptive_min_time_between_trades_seconds * 0.5)))
                logging.info('Adaptive recovery triggered | min_volume_ratio=%.4f spread_multiplier=%.4f cooldown=%d', self.min_volume_ratio, self.spread_multiplier, self.adaptive_cooldown_seconds)
                logging.info('Recovery system | active=true reason=no_trade_recovery previous_cooldown=%d adjusted_cooldown=%d', previous_cooldown, self.adaptive_cooldown_seconds)
        if loop_count_without_trade > self.no_trade_relax_loops:
            warn_bucket = loop_count_without_trade // self.no_trade_relax_loops
            if warn_bucket != self.last_no_trade_warning_bucket:
                self.last_no_trade_warning_bucket = warn_bucket
                logging.warning('No trades detected -> relaxing filters')
                if latest_filter is not None:
                    logging.warning(
                        'Latest blocked filter snapshot | reason=%s current_volume=%.4f normalized_volume=%.4f rolling_mean=%.4f relative_volume=%.4f threshold=%.4f adaptive_threshold=%.4f trend_strength=%.8f spread_ratio=%.6f spread_threshold=%.6f rolling_median=%.6f multiplier=%.4f',
                        getattr(latest_filter, 'reason_code', 'unknown'),
                        float(getattr(latest_filter, 'volume', 0.0)),
                        float(getattr(latest_filter, 'normalized_volume', 0.0)),
                        float(getattr(latest_filter, 'volume_mean', 0.0)),
                        float(getattr(latest_filter, 'relative_volume', 0.0)),
                        float(getattr(latest_filter, 'min_volume_ratio', 0.0)),
                        float(getattr(latest_filter, 'adaptive_volume_threshold', 0.0)),
                        float(getattr(latest_filter, 'trend_strength', 0.0)),
                        float(getattr(latest_filter, 'spread_ratio', 0.0)),
                        float(getattr(latest_filter, 'spread_threshold', 0.0)),
                        float(getattr(latest_filter, 'rolling_median_spread_ratio', 0.0)),
                        float(getattr(latest_filter, 'spread_multiplier', 0.0)),
                    )
        if loop_count_without_trade >= self.no_trade_disable_loops and not self.volume_filter_disabled_until_trade:
            self.volume_filter_disabled_until_trade = True
            logging.warning('Volume filter temporarily disabled due to no-trade condition')
        if loop_count_without_trade > self.no_trade_disable_loops and not self.filter_disable_until_trade:
            self.filter_disable_until_trade = True
            logging.warning('No trades for %d loops -> temporarily disabling filters except spread', loop_count_without_trade)

        trades_last_5min = self.trades_last_5min(now)
        if trades_last_5min > self.overtrading_threshold_5min:
            self._adjust_volume(1.03, 'overtrading_guard', upper_bound=0.55)
            self.min_volume_ratio = min(self.min_volume_ratio, 0.60)
            logging.info('Overtrading guard | active=true trades_last_5min=%d min_volume_ratio=%.4f', trades_last_5min, self.min_volume_ratio)
        execution_rate = self.execution_rate()
        win_rate = self.win_rate()
        if win_rate > 0.6:
            previous_volume_ratio = self.min_volume_ratio
            self.min_volume_ratio = _clamp(self.min_volume_ratio * 0.98, self.min_volume_ratio_min, self.min_volume_ratio_max)
            if previous_volume_ratio != self.min_volume_ratio:
                logging.info('Overtrading guard | active=false decay=true win_rate=%.4f min_volume_ratio_old=%.4f min_volume_ratio_new=%.4f', win_rate, previous_volume_ratio, self.min_volume_ratio)
        if sum(self.signals_executable_last_100) >= 20 and execution_rate < EXECUTION_RATE_RELAX_THRESHOLD:
            self._adjust_spread(1.15, 'low_execution_rate_relax')
            self._adjust_volume(0.94, 'low_execution_rate_relax', upper_bound=0.55)
            self.adaptive_cooldown_seconds = max(self.min_cooldown_seconds, int(round(self.adaptive_cooldown_seconds * 0.9)))
            self.adaptive_min_time_between_trades_seconds = max(self.min_cooldown_seconds, int(round(self.adaptive_min_time_between_trades_seconds * 0.9)))
            logging.info('Recovery system | active=true reason=low_execution_rate execution_rate=%.4f cooldown=%d min_time=%d', execution_rate, self.adaptive_cooldown_seconds, self.adaptive_min_time_between_trades_seconds)
        elif sum(self.signals_executable_last_100) >= 20 and execution_rate > EXECUTION_RATE_TIGHTEN_THRESHOLD:
            self._adjust_spread(0.98, 'high_execution_rate_tighten')
            self._adjust_volume(1.03, 'high_execution_rate_tighten', upper_bound=0.55)
            self._adjust_trend(1.02, 'high_execution_rate_tighten')
            logging.info('Adaptive change | param=execution_rate_control old=%.4f new=%.4f reason=high_execution_rate cooldown=%d min_time=%d', execution_rate, execution_rate, self.adaptive_cooldown_seconds, self.adaptive_min_time_between_trades_seconds)

    def _apply_live_feedback(self) -> None:
        if len(self.recent_trade_pnls) < min(5, self.win_rate_window):
            return
        wins = sum(1 for pnl in self.recent_trade_pnls if pnl > 0)
        win_rate = wins / len(self.recent_trade_pnls)
        pnl_last = sum(self.recent_trade_pnls)
        if win_rate < 0.4:
            self._adjust_volume(1.05, 'low_win_rate', upper_bound=0.55)
            self._adjust_trend(1.05, 'low_win_rate')
            logging.info('Live feedback tightened filters | win_rate_last_%d=%.3f pnl_last_%d=%.4f', len(self.recent_trade_pnls), win_rate, len(self.recent_trade_pnls), pnl_last)
        elif win_rate > 0.7:
            self._adjust_volume(0.95, 'high_win_rate', upper_bound=0.55)
            self._adjust_trend(0.95, 'high_win_rate')
            logging.info('Live feedback loosened filters | win_rate_last_%d=%.3f pnl_last_%d=%.4f', len(self.recent_trade_pnls), win_rate, len(self.recent_trade_pnls), pnl_last)

    def _apply_market_regime_adjustment(self, bars: pd.DataFrame) -> None:
        if len(bars) < MARKET_REGIME_VOL_LOOKBACK + 1:
            return
        close = bars['close'].tail(MARKET_REGIME_VOL_LOOKBACK + 1).to_numpy(dtype=float)
        returns = close[1:] / close[:-1] - 1.0
        volatility = float(pd.Series(returns).std(ddof=0)) if len(returns) else 0.0
        if not pd.notna(volatility):
            return
        if volatility < 0.0005:
            self._adjust_volume(0.97, 'low_volatility_regime', upper_bound=0.55)
            self._adjust_trend(0.97, 'low_volatility_regime')
        elif volatility > 0.003:
            self._adjust_volume(1.03, 'high_volatility_regime', upper_bound=0.55)
            self._adjust_trend(1.03, 'high_volatility_regime')

    def on_trade(self, pnl_usd: float, now: datetime, bars: pd.DataFrame) -> None:
        self.recent_trade_pnls.append(float(pnl_usd))
        self.recent_trade_times.append(now)
        self.filter_disable_until_trade = False
        self.volume_filter_disabled_until_trade = False
        if pnl_usd < 0.0:
            previous_scale = self.current_position_size_scale
            self.current_position_size_scale = max(0.35, self.current_position_size_scale * LOSS_SIZE_REDUCTION_FACTOR)
            if previous_scale != self.current_position_size_scale:
                logging.info('Adaptive change | param=position_size_scale old=%.4f new=%.4f reason=post_loss_deleveraging', previous_scale, self.current_position_size_scale)
        else:
            self.current_position_size_scale = min(STRONG_SIGNAL_POSITION_SCALE, (self.current_position_size_scale * 1.03) + 0.02)
        trailing = list(self.recent_trade_pnls)
        if len(trailing) >= 3 and all(value < 0.0 for value in trailing[-3:]):
            previous_min_time = self.adaptive_min_time_between_trades_seconds
            previous_aggression = self.aggression_scale
            self.adaptive_min_time_between_trades_seconds = int(_clamp(
                round(self.adaptive_min_time_between_trades_seconds * 1.5),
                ADAPTIVE_MIN_TIME_FLOOR_SECONDS,
                ADAPTIVE_MIN_TIME_CEILING_SECONDS,
            ))
            self.aggression_scale = max(DRAWDOWN_AGGRESSION_FLOOR, self.aggression_scale * 0.85)
            logging.info('Adaptive change | param=min_time_between_trades old=%d new=%d reason=three_consecutive_losses', previous_min_time, self.adaptive_min_time_between_trades_seconds)
            logging.info('Adaptive change | param=aggression_scale old=%.4f new=%.4f reason=three_consecutive_losses', previous_aggression, self.aggression_scale)
        self._apply_live_feedback()
        self._apply_market_regime_adjustment(bars)

    def record_trade_feedback(self, pnl_usd: float, now: datetime, bars: pd.DataFrame, exit_reason: str) -> None:
        self.recent_exit_reasons.append(str(exit_reason))
        self.on_trade(pnl_usd, now, bars)

    def profit_churn_pressure(self) -> float:
        recent = list(self.recent_exit_reasons)[-5:]
        if not recent:
            return 0.0
        churn_count = sum(1 for reason in recent if reason in {'time_exit_flat', 'time_exit_loss', 'no_follow_through'})
        return _clamp(churn_count / len(recent), 0.0, 1.0)


def _build_safe_stream_handler() -> logging.StreamHandler:
    stream_encoding = getattr(sys.stdout, 'encoding', None) or 'cp1252'
    if hasattr(sys.stdout, 'buffer'):
        safe_stream = io.TextIOWrapper(sys.stdout.buffer, encoding=stream_encoding, errors='replace', line_buffering=True)
        return logging.StreamHandler(safe_stream)
    return logging.StreamHandler(sys.stdout)


def configure_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = WindowsSafeFormatter('%(asctime)s | %(levelname)s | %(message)s')
    file_handler = logging.FileHandler(log_file, encoding='cp1252', errors='replace')
    stream_handler = _build_safe_stream_handler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, stream_handler],
        force=True,
    )


def fetch_bybit_testnet_trades(symbol: str, limit: int = 1000) -> pd.DataFrame:
    url = f"{TESTNET_URL}?{urllib.parse.urlencode({'category': 'linear', 'symbol': symbol, 'limit': limit})}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode('utf-8'))
    items = payload.get('result', {}).get('list', [])
    rows = [{'timestamp': int(x['time']), 'price': float(x['price']), 'size': float(x['size'])} for x in items]
    if not rows:
        return pd.DataFrame(columns=['timestamp', 'price', 'size'])
    df = pd.DataFrame(rows)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    return df.sort_values('timestamp').drop_duplicates()


def resolve_mt5_symbol(symbol: str) -> str | None:
    try:
        if mt5 is None:
            logging.warning("MT5 SYMBOL RESOLVE FAILED | mt5_unavailable")
            return None

        requested = str(symbol).upper().strip()
        available = mt5.symbols_get()
        if not available:
            logging.warning("MT5 SYMBOL RESOLVE FAILED | symbol=%s reason=no_symbols_from_terminal", requested)
            return None

        names = [str(s.name) for s in available if getattr(s, "name", None)]

        for name in names:
            if name.upper() == requested:
                return name

        startswith_matches = [name for name in names if name.upper().startswith(requested)]
        if startswith_matches:
            chosen = sorted(startswith_matches, key=len)[0]
            logging.warning(
                "SYMBOL AUTO-RESOLVED | requested=%s resolved=%s method=startswith",
                requested,
                chosen,
            )
            return chosen

        contains_matches = [name for name in names if requested in name.upper()]
        if contains_matches:
            chosen = sorted(contains_matches, key=len)[0]
            logging.warning(
                "SYMBOL AUTO-RESOLVED | requested=%s resolved=%s method=contains",
                requested,
                chosen,
            )
            return chosen

        logging.warning("SYMBOL AUTO-RESOLVE FAILED | requested=%s reason=no_match", requested)
        return None

    except Exception as exc:
        logging.warning("SYMBOL RESOLVE FAILED | symbol=%s error=%s", symbol, str(exc))
        return None


# ===============================
# 🔥 MT5 BAR FETCH (CORE FIX)
# ===============================
def fetch_mt5_bars(symbol: str, timeframe_seconds: int = 60, count: int = 200):
    try:
        if mt5 is None:
            logging.warning("MT5 NOT AVAILABLE")
            return None

        timeframe_map = {
            60: mt5.TIMEFRAME_M1,
            300: mt5.TIMEFRAME_M5,
            900: mt5.TIMEFRAME_M15,
            1800: mt5.TIMEFRAME_M30,
            3600: mt5.TIMEFRAME_H1,
        }

        tf = timeframe_map.get(int(timeframe_seconds), mt5.TIMEFRAME_M1)

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, int(count))

        if rates is None or len(rates) == 0:
            logging.warning("MT5 NO DATA | symbol=%s", symbol)
            return None

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])

        logging.info("MT5 DATA LOADED | symbol=%s bars=%d", symbol, len(df))

        return df

    except Exception as e:
        logging.warning("MT5 FETCH FAILED | %s", str(e))
        return None


def preload_historical_bars(symbol: str, limit: int = 200) -> pd.DataFrame | None:
    try:
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=1&limit={limit}"
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        klines = payload.get('result', {}).get('list', [])
        if not klines:
            return None
        rows = []
        for kline in klines:
            rows.append(
                {
                    'timestamp': pd.to_datetime(int(kline[0]), unit='ms', utc=True),
                    'open': float(kline[1]),
                    'high': float(kline[2]),
                    'low': float(kline[3]),
                    'close': float(kline[4]),
                    'volume': float(kline[5]),
                    'price': float(kline[4]),
                    'size': float(kline[5]),
                }
            )
        frame = pd.DataFrame(rows).sort_values('timestamp')
        logging.info('HISTORICAL DATA LOADED | symbol=%s bars=%d', symbol, len(frame))
        return frame
    except Exception as exc:
        logging.warning('HISTORICAL PRELOAD FAILED | symbol=%s error=%s', symbol, exc)
        return None


def merge_bars(existing: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    try:
        if existing is None or existing.empty:
            return new
        merged = pd.concat([existing, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=['timestamp']).sort_values('timestamp')
        if len(merged) > 2000:
            merged = merged.tail(2000).copy()
        return merged
    except Exception:
        return existing if existing is not None else new


def fetch_mt5_ticks(symbol: str) -> dict[str, float]:
    if mt5 is None:
        raise RuntimeError('MetaTrader5 package unavailable')
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f'MT5 symbol_info_tick unavailable for {symbol}')
    bid = float(getattr(tick, 'bid', 0.0) or 0.0)
    ask = float(getattr(tick, 'ask', 0.0) or 0.0)
    last_value = getattr(tick, 'last', 0.0)
    last = float(last_value) if last_value not in (None, 0, 0.0) else (bid + ask) / 2.0
    if bid <= 0.0 or ask <= 0.0:
        raise RuntimeError(f'invalid MT5 tick for {symbol}: bid={bid} ask={ask}')
    return {
        'bid': bid,
        'ask': ask,
        'last': last,
    }


def load_strategy_and_params(strategy_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    strategy_file = Path(strategy_path)
    logging.info("STRATEGY LOAD | path=%s", strategy_file)

    fallback_strategy = {
        "name": "fallback_runtime_strategy",
        "strategy_type": "default",
        "strategy_module": "",
        "module": "",
        "timeframe": None,
        "entry_type": None,
        "phase": None,
        "ob_tradability": None,
        "filters": {},
        "params_json": {},
        "metadata": {
            "source": "fallback",
            "reason": "strategy_file_missing_or_invalid",
        },
    }

    strategy: dict[str, Any]
    if not strategy_file.exists():
        logging.warning(
            "STRATEGY FILE MISSING | path=%s | using fallback defaults",
            strategy_file,
        )
        strategy = dict(fallback_strategy)
    else:
        try:
            raw_text = strategy_file.read_text(encoding="utf-8")
            loaded = json.loads(raw_text)
            if not isinstance(loaded, dict):
                raise TypeError("strategy_json_not_dict")
            strategy = loaded
        except Exception as exc:
            logging.warning(
                "STRATEGY FILE INVALID | path=%s | error=%s | using fallback defaults",
                strategy_file,
                exc,
            )
            strategy = dict(fallback_strategy)

    params = strategy.get("params_json", {})
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception as exc:
            logging.warning("PARAMS JSON INVALID | source=strategy.params_json | error=%s", exc)
            params = {}

    if not isinstance(params, dict):
        logging.warning("PARAMS JSON INVALID | source=strategy.params_json | reason=not_dict")
        params = {}

    params = dict(params)
    has_legacy_min_volume = "min_volume" in params
    original_min_trend_strength = params.get("min_trend_strength")

    filter_config_path = Path("results/best_filter_config.json")
    if filter_config_path.exists():
        try:
            best_filter_config = json.loads(filter_config_path.read_text(encoding="utf-8"))
            if isinstance(best_filter_config, dict):
                filters = best_filter_config.get("filters", {})
                if isinstance(filters, dict):
                    params.update(filters)
                    logging.info("Loaded best filter config from %s", filter_config_path)
                else:
                    logging.warning("FILTER CONFIG INVALID | path=%s | reason=filters_not_dict", filter_config_path)
            else:
                logging.warning("FILTER CONFIG INVALID | path=%s | reason=not_dict", filter_config_path)
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logging.warning("Filter config missing/broken, using safe defaults (%s)", exc)
    else:
        logging.info("FILTER CONFIG MISSING | path=%s | using strategy/default params", filter_config_path)

    if has_legacy_min_volume:
        logging.warning(
            "Legacy min_volume found in config; forcing safe min_volume_ratio=%.2f",
            DEFAULT_MIN_VOLUME_RATIO,
        )
        params["min_volume_ratio"] = DEFAULT_MIN_VOLUME_RATIO
    params.pop("min_volume", None)

    min_volume_ratio = params.get("min_volume_ratio")
    if not isinstance(min_volume_ratio, (int, float)) or min_volume_ratio <= 0.0:
        min_volume_ratio = DEFAULT_MIN_VOLUME_RATIO
    params["min_volume_ratio"] = float(
        _clamp(float(min_volume_ratio), MIN_VOLUME_RATIO_MIN, MIN_VOLUME_RATIO_MAX)
    )

    min_trend_strength = params.get("min_trend_strength")
    if not isinstance(original_min_trend_strength, (int, float)) or original_min_trend_strength <= 0.0:
        min_trend_strength = DEFAULT_MIN_TREND_STRENGTH
    if not isinstance(min_trend_strength, (int, float)) or min_trend_strength <= 0.0:
        min_trend_strength = DEFAULT_MIN_TREND_STRENGTH
    params["min_trend_strength"] = float(min_trend_strength)
    params["max_spread_ratio"] = _clamp(
        _safe_param(params, "max_spread_ratio", 0.003),
        MIN_SAFE_SPREAD_RATIO,
        MAX_SAFE_SPREAD_RATIO,
    )
    params["spread_multiplier"] = max(
        1.0,
        _safe_param(params, "spread_multiplier", DEFAULT_SPREAD_MULTIPLIER),
    )
    params["base_cooldown"] = int(
        _safe_param(params, "base_cooldown", DEFAULT_BASE_COOLDOWN_SECONDS)
    )
    params["min_cooldown"] = int(
        _safe_param(params, "min_cooldown", DEFAULT_MIN_COOLDOWN_SECONDS)
    )
    params["max_cooldown"] = int(
        _safe_param(params, "max_cooldown", DEFAULT_MAX_COOLDOWN_SECONDS)
    )

    strategy.setdefault("name", "loaded_runtime_strategy")
    strategy.setdefault("strategy_type", "default")
    strategy.setdefault("strategy_module", strategy.get("module", "") or "")
    strategy.setdefault("module", strategy.get("strategy_module", "") or "")
    strategy.setdefault("entry_type", None)
    strategy.setdefault("phase", None)
    strategy.setdefault("filters", {})
    strategy.setdefault("params_json", {})
    strategy.setdefault("metadata", {})
    strategy["metadata"]["source"] = (
        "fallback"
        if strategy.get("name") == "fallback_runtime_strategy"
        else "file"
    )

    logging.info(
        "STRATEGY ACTIVE | source=%s name=%s strategy_type=%s strategy_module=%s",
        strategy["metadata"]["source"],
        strategy.get("name"),
        strategy.get("strategy_type"),
        strategy.get("strategy_module") or strategy.get("module") or "",
    )
    logging.info(
        "STRATEGY PARAMS ACTIVE | min_volume_ratio=%.4f min_trend_strength=%.4f max_spread_ratio=%.6f spread_multiplier=%.4f base_cooldown=%s min_cooldown=%s max_cooldown=%s",
        float(params["min_volume_ratio"]),
        float(params["min_trend_strength"]),
        float(params["max_spread_ratio"]),
        float(params["spread_multiplier"]),
        int(params["base_cooldown"]),
        int(params["min_cooldown"]),
        int(params["max_cooldown"]),
    )
    return strategy, params





def ensure_trade_log(trades_file: Path) -> None:
    if not trades_file.exists():
        trades_file.parent.mkdir(parents=True, exist_ok=True)
        with trades_file.open('w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(['timestamp', 'symbol', 'side', 'entry', 'exit', 'reason', 'pnl', 'running_daily_pnl'])


def run_adaptive_timing_validation(controller: AdaptiveFilterController) -> None:
    scenarios = [
        ('high_vol_high_signal', 0.0045, 0.92, MarketRegime.HIGH_VOLATILITY.value, 0, 0, 25.0),
        ('high_vol_low_signal', 0.0045, 0.35, MarketRegime.HIGH_VOLATILITY.value, 0, 0, -5.0),
        ('low_vol_high_signal', 0.0003, 0.92, MarketRegime.LOW_ACTIVITY.value, 0, 0, 25.0),
        ('losing_streak_scenario', 0.0003, 0.68, MarketRegime.LOW_ACTIVITY.value, 3, 35, -10.0),
    ]
    print('ADAPTIVE TIMING VALIDATION')
    for name, volatility, signal_score, regime, consecutive_losses, no_trade_loops, daily_pnl in scenarios:
        min_time, _ = controller.adaptive_min_time_between_trades(
            base_min_time=controller.base_cooldown_seconds,
            volatility=volatility,
            signal_score=signal_score,
            market_regime=regime,
            consecutive_losses=consecutive_losses,
            no_trade_loops=no_trade_loops,
        )
        cooldown, _ = controller.adaptive_cooldown(
            volatility=volatility,
            consecutive_losses=consecutive_losses,
            signal_score=signal_score,
            market_regime=regime,
            daily_pnl_usd=daily_pnl,
        )
        print(
            f'{name}: volatility={volatility:.4f} signal={signal_score:.2f} regime={regime} '
            f'losses={consecutive_losses} no_trade_loops={no_trade_loops} -> '
            f'min_time={min_time}s cooldown={cooldown}s'
        )
    print('EXPECTED: high-volatility/high-signal executes fastest; low-volatility/low-signal stays throttled; recovery scenario halves min_time.')


def main() -> None:

    # =========================================
    # MT5 INIT (SAFE DISABLED)
    # =========================================
    mt5_enabled = (
        (not DISABLE_MT5)
        and IS_LIVE_MODE
        and LIVE_EXECUTION_ENABLED
    )
    if mt5_enabled and DATA_BACKEND == "mt5":
        if not init_mt5_connection():
            logging.warning("MT5 INIT FAILED → fallback to pure paper mode")
            mt5_enabled = False
    elif not mt5_enabled:
        logging.warning("MT5 DISABLED | running in pure paper mode")

    # =========================================
    # 🔥 FIX CORE: ACCOUNT SYNC (SAFE)
    # =========================================
    try:
        if mt5_enabled and mt5 is not None:
            account_info = _mt5_safe("account_info")
        else:
            account_info = None

        if account_info is not None:
            mt5_equity = float(getattr(account_info, "equity", 0.0) or 0.0)
            mt5_balance = float(getattr(account_info, "balance", 0.0) or 0.0)
            mt5_free_margin = float(getattr(account_info, "margin_free", 0.0) or 0.0)
            account_source = "mt5"
        else:
            raise ValueError("MT5 unavailable (paper defaults)")

    except Exception as e:
        logging.warning("ACCOUNT SYNC FAILED | %s", str(e))
        mt5_equity = 100.0
        mt5_balance = 100.0
        mt5_free_margin = 100.0
        account_source = "fallback"

    env_balance = float(os.getenv("ACCOUNT_EQUITY_USD", "0") or 0.0)

    # 🔥 FINAL BALANCE SELECTION
    effective_balance = mt5_balance if mt5_balance > 0 else env_balance

    if effective_balance <= 0:
        effective_balance = 100.0  # absolute safety fallback

    # 🔥 GLOBAL EQUITY SYNC (CRITICAL)
    account_equity_usd = float(effective_balance)

    logging.info(
        "ACCOUNT SYNC | equity=%.2f balance=%.2f free_margin=%.2f source=%s",
        mt5_equity,
        mt5_balance,
        mt5_free_margin,
        account_source,
    )

    logging.info(
        "BALANCE CHECK | mt5=%.2f env=%.2f used=%.2f",
        mt5_balance,
        env_balance,
        effective_balance,
    )
    global last_trade_timestamp, consecutive_losses, last_symbol, last_direction, last_closed_trade_timestamp
    p = argparse.ArgumentParser(description='Paper trader with explicit opt-in Bybit live execution safety layer')
    p.add_argument('--strategy', default='results/best_strategy.json')
    p.add_argument('--symbol', default='BTCUSDT')
    p.add_argument('--symbols', default=",".join(DEFAULT_SYMBOLS))
    p.add_argument('--bar-seconds', type=int, default=5)
    p.add_argument('--log-file', default='results/paper_trader.log')
    p.add_argument('--trades-file', default='results/paper_trades.csv')
    p.add_argument('--notional-usd', type=float, default=100)
    p.add_argument('--fee-rate', type=float, default=0.00055)
    p.add_argument('--slippage-rate', type=float, default=0.00015)
    p.add_argument('--max-open-positions', type=int, default=1)
    p.add_argument('--max-trades-per-day', type=int, default=10)
    p.add_argument('--max-daily-loss-pct', '--max-daily-loss', dest='max_daily_loss_pct', type=float, default=None)
    p.add_argument('--risk-per-trade-pct', '--risk-per-trade', dest='risk_per_trade_pct', type=float, default=None)
    p.add_argument('--cooldown-seconds', '--cooldown', dest='cooldown_seconds', type=int, default=None)
    p.add_argument('--min-time-between-trades-seconds', type=int, default=None)
    p.add_argument('--live-execution', action='store_true', help='Enable live Bybit execution when env credentials are present')
    args = p.parse_args()

    # =========================================
    # 🔥 FORCE ONE SINGLE LOG TARGET
    # =========================================
    args.log_file = RUNTIME_LOG_FILE
    logging.info("RUNTIME LOG TARGET FORCED | file=%s", args.log_file)

    profit_lock_engine = ProfitLockEngine()
    risk_engine = RiskScalingEngine()
    cooldown_remaining = 0.0
    min_time_remaining = 0.0
    symbol_universe = get_symbol_universe(args.symbols or args.symbol)
    allowed_symbols = get_allowed_symbols()
    symbol_universe = [s for s in symbol_universe if str(s).upper() in allowed_symbols]
    logging.info("ACTIVE SYMBOLS | %s", symbol_universe)

    live_execution_enabled = bool(args.live_execution) or LIVE_EXECUTION_ENABLED_ENV
    print('LIVE EXECUTION ENABLED' if live_execution_enabled else 'PAPER MODE ONLY / NO LIVE ORDERS')
    try:
        configure_logging(args.log_file)
        logging.info("LOGGING RECONFIRMED | file=%s", args.log_file)
    except Exception as e:
        logging.warning("LOGGING RECONFIG SKIPPED | reason=%s", str(e))

    active_risk_config = load_risk_config(
        cli_overrides={
            'risk_per_trade_pct': args.risk_per_trade_pct,
            'max_daily_loss_pct': args.max_daily_loss_pct,
            'cooldown_seconds': args.cooldown_seconds,
            'min_time_between_trades_seconds': args.min_time_between_trades_seconds,
        },
    )
    risk_per_trade_pct = risk_engine.get_risk()
    max_daily_loss_pct = active_risk_config['max_daily_loss_pct']
    cooldown_seconds = active_risk_config['cooldown_seconds']
    cooldown_seconds = int(cooldown_seconds * 0.5)
    min_time_between_trades_seconds = active_risk_config['min_time_between_trades_seconds']
    min_time_between_trades_seconds = int(min_time_between_trades_seconds * 0.5)

    print(f'FINAL ACTIVE CONFIG: {active_risk_config}')
    logging.info("PROFIT MODE ACTIVE")
    evo_threshold_enabled = str(os.getenv("EVO_THRESHOLD_ENABLED", "true")).lower() == "true"
    evo_threshold = EvoThresholdEngine() if evo_threshold_enabled else None
    evo_engine_state: dict[str, Any] = load_evo_state(_evo2_state_path()) if _evo2_enabled() else {}

    strategy, params = load_strategy_and_params(args.strategy)
    find_setups_fn = resolve_runtime_find_setups_fn(strategy)
    if find_setups_fn is not None:
        validate_strategy_api(find_setups_fn)
    scan_health_tracker = ScanHealthTracker(
        disable_threshold=int(os.getenv('SCAN_FAILURE_DISABLE_THRESHOLD', '3') or 3),
        cooldown_bars=int(os.getenv('SCAN_DISABLE_COOLDOWN_BARS', '4') or 4),
        repeat_log_every=int(os.getenv('SCAN_REPEAT_LOG_EVERY', '10') or 10),
    )
    runtime_scan_research = RuntimeScanResearchTracker(logs_dir=log_dir)
    params['base_cooldown'] = cooldown_seconds or DEFAULT_BASE_COOLDOWN_SECONDS
    params['cooldown_seconds'] = params['base_cooldown']
    params['min_cooldown'] = int(_safe_param(params, 'min_cooldown', DEFAULT_MIN_COOLDOWN_SECONDS))
    params['max_cooldown'] = int(_safe_param(params, 'max_cooldown', DEFAULT_MAX_COOLDOWN_SECONDS))
    params['min_time_between_trades_seconds'] = min_time_between_trades_seconds or params['base_cooldown']
    params['fee_rate'] = args.fee_rate
    params['slippage_rate'] = args.slippage_rate
    params['max_trades_per_day'] = args.max_trades_per_day
    evolution_engine = EvolutionEngine()
    run_adaptive_timing_validation(AdaptiveFilterController.from_params(params))

    trades_file = Path(args.trades_file)
    ensure_trade_log(trades_file)

    risk_cfg = RiskConfig(
        notional_usd=args.notional_usd,
        max_open_positions=3,
        max_trades_per_day=MAX_DAILY_TRADES_HARD_CAP,
        cooldown_seconds=cooldown_seconds,
        min_time_between_trades_seconds=min_time_between_trades_seconds,
        max_daily_loss_pct=max_daily_loss_pct,
        risk_per_trade_pct=risk_per_trade_pct,
    )
    logging.info(
        'Risk config | max_open_positions=%s max_trades_per_day=%s max_daily_loss_pct=%s risk_per_trade_pct=%s cooldown_seconds=%s min_time_between_trades_seconds=%s',
        risk_cfg.max_open_positions,
        risk_cfg.max_trades_per_day,
        risk_cfg.max_daily_loss_pct,
        risk_cfg.risk_per_trade_pct,
        risk_cfg.cooldown_seconds,
        risk_cfg.min_time_between_trades_seconds,
    )
    risk_state = RiskState(max_trades_per_day=risk_cfg.max_trades_per_day)
    is_stacking_entry = int(getattr(risk_state, "open_positions", 0)) > 0
    broker_backend = os.getenv('BROKER_BACKEND')
    if not broker_backend:
        logging.warning("Missing BROKER_BACKEND in environment -> defaulting to mt5")
        os.environ["BROKER_BACKEND"] = "mt5"
        broker_backend = "mt5"
    live_backend = str(broker_backend or 'mt5').strip().lower()
    live_session: BrokerSession | None = None
    live_safety_controller = LiveSafetyController.from_env()
    if live_execution_enabled:
        if live_backend == 'mt5':
            logging.info('EXECUTION BACKEND LOCKED | backend=mt5 no_fallback=true')
            live_session = create_broker_adapter()
            if live_session is None:
                logging.critical('MT5 backend failed - aborting live execution')
                live_safety_controller.activate_kill_switch('mt5_backend_failed')
                raise SystemExit('MT5 live connection failed; kill switch engaged')
        else:
            live_session = create_broker_adapter() or create_bybit_session()
    if live_execution_enabled and live_backend == 'mt5' and live_session is None:
        live_safety_controller.activate_kill_switch('mt5_connection_required')
        raise SystemExit('MT5 connection required for live execution')
    data_backend = DATA_BACKEND
    params['data_backend'] = data_backend
    symbols_before_sanitize = list(symbol_universe)
    if data_backend == 'mt5':
        crypto_prefixes = (
            'BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'LTC', 'BCH', 'BNB', 'AVAX', 'DOT', 'MATIC', 'LINK', 'TRX', 'XLM',
        )
        symbol_universe = [
            s for s in symbol_universe
            if not (
                str(s).upper() == 'BTCUSDT'
                or str(s).upper().endswith('USDT')
                or str(s).upper().startswith(crypto_prefixes)
            )
        ]
    logging.info('SYMBOL SANITIZED | before=%s after=%s', len(symbols_before_sanitize), len(symbol_universe))
    if data_backend == 'mt5':
        logging.info('DATA SOURCE LOCKED | source=mt5')
    live_trailing_policy = get_live_trailing_policy() if live_execution_enabled else TrailingPolicy(mode='disabled', allow_internal=False, exchange_native=False)
    live_symbol_specs: SymbolSpecs | None = None
    live_position_qty = 0.0
    live_position_idx: int | None = None
    if live_execution_enabled and live_session is None:
        logging.warning('LIVE MODE DISABLED | reason=missing_broker_session')
        live_execution_enabled = False

    if data_backend == 'mt5':
        resolved_symbol_universe: list[str] = []
        for raw_symbol in symbol_universe:
            requested_symbol = str(raw_symbol).upper().strip()
            resolved_symbol = resolve_mt5_symbol(requested_symbol)
            if resolved_symbol is None:
                logging.warning(
                    'SYMBOL REGISTRATION SKIPPED | backend=mt5 symbol=%s reason=not_found',
                    requested_symbol,
                )
                continue

            try:
                selected_ok = bool(mt5.symbol_select(resolved_symbol, True)) if mt5 is not None else False
            except Exception:
                selected_ok = False

            if not selected_ok:
                logging.warning(
                    'SYMBOL REGISTRATION SKIPPED | backend=mt5 symbol=%s resolved=%s reason=symbol_select_failed',
                    requested_symbol,
                    resolved_symbol,
                )
                continue

            resolved_symbol_universe.append(resolved_symbol)
            logging.info(
                'SYMBOL REGISTERED | backend=mt5 requested=%s resolved=%s',
                requested_symbol,
                resolved_symbol,
            )

        deduped_resolved_symbol_universe: list[str] = []
        seen_symbols: set[str] = set()
        for s in resolved_symbol_universe:
            su = str(s).upper()
            if su in seen_symbols:
                continue
            seen_symbols.add(su)
            deduped_resolved_symbol_universe.append(s)
        symbol_universe = deduped_resolved_symbol_universe

        if len(symbol_universe) == 0:
            raise SystemExit('No valid symbols available')

    def _normalize_expected_mt5_value(value):
        """
        Normalize optional MT5 expectation fields so placeholders do not trigger
        false startup mismatches.
        Treat None / empty / unset / 0 / "0" as not configured.
        """
        if value is None:
            return None
        text = str(value).strip()
        if text == "":
            return None
        if text.lower() in {"unset", "none", "null", "0", "false"}:
            return None
        return text

    if isinstance(live_session, MT5BrokerAdapter):
        startup_snapshot = live_session.get_mt5_account_snapshot()
        actual_login = startup_snapshot.get('login')
        actual_server = startup_snapshot.get('server')
        raw_expected_login = getattr(live_session, 'login', None)
        raw_expected_server = getattr(live_session, 'server', None)

        expected_login = _normalize_expected_mt5_value(raw_expected_login)
        expected_server = _normalize_expected_mt5_value(raw_expected_server)
        actual_login_normalized = _normalize_expected_mt5_value(actual_login)
        actual_server_normalized = _normalize_expected_mt5_value(actual_server)
        logging.info(
            'MT5 STARTUP CHECKLIST | path=%s login=%s server=%s balance=%s connected=%s reason=%s expected_login=%s expected_server=%s',
            live_session.path or 'unset',
            actual_login,
            actual_server,
            startup_snapshot['balance'],
            startup_snapshot['connected'],
            startup_snapshot['reason'],
            expected_login if expected_login is not None else 'unset',
            expected_server if expected_server is not None else 'unset',
        )

        startup_reason: str | None = None
        if not bool(startup_snapshot.get('connected')):
            startup_reason = str(startup_snapshot.get('reason') or 'mt5_not_connected')
        elif actual_login_normalized is None:
            startup_reason = 'mt5_no_login'
        elif (
            expected_login is not None
            and actual_login_normalized is not None
            and expected_login != actual_login_normalized
        ):
            startup_reason = 'mt5_login_mismatch'
        elif (
            expected_server is not None
            and actual_server_normalized is not None
            and expected_server != actual_server_normalized
        ):
            startup_reason = 'mt5_server_mismatch'

        if startup_reason is not None:
            live_safety_controller.activate_kill_switch(startup_reason)
            raise SystemExit(f'MT5 startup checklist incomplete ({startup_reason}); kill switch engaged')
    data_symbols = list(symbol_universe)
    execution_candidates = list(symbol_universe)

    if live_execution_enabled and isinstance(live_session, MT5BrokerAdapter):
        mt5_ready_execution_symbols: list[str] = []
        for symbol in execution_candidates:
            resolved_symbol = symbol
            try:
                resolved_symbol = resolve_mt5_symbol(symbol) or symbol
                selected = mt5.symbol_select(resolved_symbol, True) if mt5 is not None else False
            except Exception:
                selected = False
            if not selected:
                logging.warning('SYMBOL REGISTRATION SKIPPED | backend=mt5 symbol=%s resolved=%s reason=symbol_select_failed', symbol, resolved_symbol)
                continue
            logging.info('SYMBOL REGISTERED | backend=mt5 symbol=%s', resolved_symbol)
            mt5_ready_execution_symbols.append(resolved_symbol)
        execution_candidates = mt5_ready_execution_symbols

    symbol_states: dict[str, SymbolRuntimeState] = {}
    for symbol in data_symbols:
        symbol_states[symbol] = SymbolRuntimeState(
            buffer=pd.DataFrame(columns=['timestamp', 'price', 'size']),
            adaptive_filters=AdaptiveFilterController.from_params(copy.deepcopy(params)),
            active_trade=None,
            active_position_scale=1.0,
            active_notional_usd=float(args.notional_usd),
        )
    previous_daily_pnl_usd = float(risk_state.daily_pnl_usd)
    live_symbol_specs_map: dict[str, SymbolSpecs] = {}
    execution_symbols: list[str] = []

    if live_session is not None:
        for symbol in execution_candidates:
            if symbol not in live_symbol_specs_map:
                try:
                    live_symbol_specs_map[symbol] = fetch_symbol_specs(live_session, symbol)
                    specs = live_symbol_specs_map[symbol]
                    execution_symbols.append(symbol)
                    logging.info('LIVE MODE | enabled=true backend=%s symbol=%s qty_step=%.12f min_qty=%.12f tick_size=%.12f', 'mt5' if isinstance(live_session, BrokerAdapter) else 'bybit', symbol, specs.qty_step, specs.min_qty, specs.tick_size)
                except Exception as exc:
                    logging.warning('LIVE MODE | failed to load symbol specs for %s: %s', symbol, exc)
                    continue
            symbol_state = symbol_states[symbol]
            specs = live_symbol_specs_map.get(symbol)
            if specs is None:
                continue
            startup_sync = sync_bot_with_exchange_position(
                session=live_session,
                symbol=symbol,
                active_trade=symbol_state.active_trade,
                active_notional_usd=symbol_state.active_notional_usd,
                active_position_scale=symbol_state.active_position_scale,
                risk_state=risk_state,
                safety_controller=live_safety_controller,
                specs=specs,
                leverage=float(os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0),
            )
            symbol_state.active_trade = startup_sync.active_trade
            symbol_state.active_notional_usd = startup_sync.active_notional_usd
            symbol_state.active_position_scale = startup_sync.active_position_scale
    else:
        execution_symbols = list(data_symbols)
    if live_execution_enabled and len(execution_symbols) < 1:
        logging.warning('LIVE MODE FALLBACK | reason=no_execution_symbols mode=data_only')
        live_execution_enabled = False
        live_session = None
    logging.info('STARTUP SUMMARY | total=%s valid=%s execution_ready=%s', len(symbols_before_sanitize), len(data_symbols), len(execution_symbols))
    configured_account_equity_usd = float(os.getenv('ACCOUNT_EQUITY_USD', '0') or 0.0)
    initial_account_snapshot = fetch_live_account_equity(live_session)
    logging.info(
        'ACCOUNT INIT | equity=%.2f balance=%.2f free_margin=%.2f',
        float(_safe_positive_float(initial_account_snapshot.get('equity')) or 0.0),
        float(_safe_positive_float(initial_account_snapshot.get('balance')) or 0.0),
        float(_safe_positive_float(initial_account_snapshot.get('free_margin')) or 0.0),
    )
    # =========================================
    # FIX 1: STORE LIVE BALANCE FOR POSITION SIZING
    # =========================================
    live_balance = 0.0
    try:
        if DATA_BACKEND == "mt5":
            account_info = _mt5_safe("account_info")
            if account_info is not None:
                live_balance = float(getattr(account_info, "balance", 0.0) or 0.0)
                logging.info(
                    "ACCOUNT LIVE | balance=%.2f",
                    live_balance,
                )
    except Exception as e:
        logging.warning("ACCOUNT FETCH FAILED | %s", str(e))
    hard_min_data_bars = int(float(os.getenv("HARD_MIN_DATA_BARS", "150") or 150))
    disable_fallback_without_data = _env_bool("DISABLE_FALLBACK_WITHOUT_DATA", "true")

    bar_index = 0
    while True:
        bar_index += 1
        logging.warning(
            "🔥 MAIN LOOP STARTED | force_mode=%s | symbols=%s",
            str(effective_force_mode()).lower(),
            ",".join(data_symbols),
        )

        loop_now = datetime.now(timezone.utc)
        heartbeat_broker_count = int(locals().get("broker_open_positions_count", 0) or 0)
        heartbeat_open_positions = min(
            1,
            count_runtime_open_positions(data_symbols, symbol_states, heartbeat_broker_count),
        )
        logging.info(
            "BOT HEARTBEAT | running | open_positions=%d",
            int(heartbeat_open_positions),
        )
        if sweep_stats["trades"] > 0:
            try:
                winrate = sweep_stats["wins"] / sweep_stats["trades"]
                expectancy = sweep_stats["pnl_total"] / sweep_stats["trades"]
                logging.info(
                    "SWEEP STATS | trades=%d winrate=%.2f expectancy=%.5f",
                    sweep_stats["trades"],
                    winrate,
                    expectancy,
                )
            except Exception:
                pass
        global_loop_count = int(getattr(main, "_global_loop_count", 0)) + 1
        setattr(main, "_global_loop_count", global_loop_count)
        if reset_risk_day(risk_state, loop_now):
            for state in symbol_states.values():
                state.adaptive_filters.daily_stop_triggered = False
                state.adaptive_filters.session_peak_pnl_usd = 0.0
                state.adaptive_filters.session_drawdown_usd = 0.0
            logging.info('New UTC day resetting daily risk')
            logging.info('Trading resumed')
        scan_rows: list[dict[str, Any]] = []
        runtime_scan_outputs: dict[str, list[dict[str, Any]]] = {}
        ranked_inputs: list[dict[str, float]] = []
        bars_by_symbol: dict[str, pd.DataFrame] = {}
        eligible_symbols_with_data = 0
        log_legacy_execution_disabled_once()
        if global_loop_count % 200 == 0:
            for symbol, revive_state in symbol_states.items():
                if revive_state.disabled:
                    revive_state.disabled = False
                    logging.info("SYMBOL REVIVED | symbol=%s", symbol)
        for symbol in data_symbols:
            logging.warning("🔥 PROCESSING SYMBOL | %s", symbol)
            state = symbol_states[symbol]
            if state.disabled:
                scan_rows.append({"symbol": symbol, "bars": 0, "volatility": 0.0, "signal_score": 0.0, "eligible": False, "signal_generated": False, "executable": False, "blocked_reason": "symbol_disabled"})
                continue
            state.adaptive_filters.maybe_log_state(loop_now)

            bars = get_market_data_safe(
                symbol=symbol,
                timeframe_seconds=args.bar_seconds,
                required_bars=hard_min_data_bars,
            )

            bars_by_symbol[symbol] = bars
            bar_count = 0 if bars is None else len(bars)
            logging.info(
                "MARKET DATA ACTIVE | symbol=%s bars=%d backend=%s",
                symbol,
                bar_count,
                DATA_BACKEND,
            )
            if bar_count < hard_min_data_bars:
                mark_symbol_ineligible_for_data(
                    symbol=symbol,
                    reason="insufficient_data",
                    bars_count=bar_count,
                    min_required=hard_min_data_bars,
                )
                scan_rows.append({"symbol": symbol, "bars": bar_count, "volatility": 0.0, "signal_score": 0.0, "eligible": False, "signal_generated": False, "executable": False, "blocked_reason": "insufficient_hard_data"})
                continue
            eligible_symbols_with_data += 1
            if len(bars) < MIN_SIGNAL_BARS:
                scan_rows.append({"symbol": symbol, "bars": len(bars), "volatility": 0.0, "signal_score": 0.0, "eligible": False, "signal_generated": False, "executable": False, "blocked_reason": "insufficient_bars"})
                continue

            normalized_setups = scan_symbol_setups_runtime(
                find_setups_fn=find_setups_fn,
                bars_df=bars,
                config=params,
                symbol=symbol,
                tracker=scan_health_tracker,
                bar_index=bar_index,
            )
            runtime_scan_research.record_symbol_cycle(symbol=symbol, had_setup=bool(normalized_setups))
            if normalized_setups:
                runtime_scan_outputs[symbol] = normalized_setups
                health_snapshot = scan_health_tracker.summary(current_bar=bar_index)
                symbol_health_context = {
                    "scan_failures": int(health_snapshot.get("failed", {}).get(symbol, 0)),
                    "scan_attempts": int(health_snapshot.get("attempted", {}).get(symbol, 0)),
                    "scan_consecutive_failures": int(health_snapshot.get("consecutive", {}).get(symbol, 0)),
                }
                strategy_module = str(strategy.get("strategy_module") or strategy.get("module") or "")
                for setup in normalized_setups:
                    runtime_scan_research.capture_setup(
                        setup=setup,
                        bar_index=bar_index,
                        bars=bars,
                        strategy_module=strategy_module or None,
                        scan_health_context=symbol_health_context,
                    )

            closes = pd.Series(bars['close'], dtype=float).to_numpy(dtype=float)
            symbol_vol = compute_symbol_volatility(closes)
            state.recent_symbol_volatility.append(symbol_vol)
            base_threshold = resolve_dynamic_symbol_volatility_threshold(
                symbol=symbol,
                volatility=symbol_vol,
                base_threshold=float(DEFAULT_SYMBOL_VOLATILITY_THRESHOLDS.get(symbol.upper(), 0.0001)),
                history=state.recent_symbol_volatility,
            )
            regime = detect_market_regime(symbol_vol)
            performance_score = float(state.performance.get("avg_pnl", 0.0))
            dynamic_threshold = float(base_threshold)
            logging.info("DYNAMIC THRESHOLD VALUE | value=%.6f", dynamic_threshold)
            adaptive_threshold = compute_adaptive_threshold(
                base=dynamic_threshold,
                volatility=symbol_vol,
                regime=regime,
                performance_score=performance_score,
            )
            adaptive_threshold *= float(state.threshold_bias)
            adaptive_threshold = apply_auto_relax(
                adaptive_threshold,
                state.adaptive_filters.trades_last_5min(loop_now),
                state.loop_count_without_trade,
            )
            adaptive_threshold = float(np.clip(adaptive_threshold, 0.00001, 1.0))
            logging.info("THRESHOLD BIAS | symbol=%s bias=%.4f", symbol, state.threshold_bias)
            logging.info(
                "ADAPTIVE | symbol=%s regime=%s vol=%.6f threshold=%.6f",
                symbol,
                regime,
                float(symbol_vol),
                float(adaptive_threshold),
            )
            eligible = symbol_is_eligible(symbol, symbol_vol, adaptive_threshold)
            signal_score_seed = abs(float(closes[-1] - closes[-2])) / max(abs(float(closes[-2])), 1e-9) if len(closes) > 1 else 0.0
            symbol_regime = detect_market_regime(symbol_vol)
            performance_score = float(state.performance.get("avg_pnl", 0.0))
            adaptive_threshold = compute_adaptive_threshold(
                base=base_threshold,
                volatility=symbol_vol,
                regime=symbol_regime,
                performance_score=performance_score,
            )
            trades_last_5min_symbol = state.adaptive_filters.trades_last_5min(loop_now)
            if trades_last_5min_symbol == 0:
                adaptive_threshold *= 0.7
            if trades_last_5min_symbol == 0 and int(state.loop_count_without_trade) > 20:
                adaptive_threshold *= 0.5
            if AGGRESSION_MODE:
                adaptive_threshold *= 0.8
            adaptive_threshold *= float(state.threshold_bias)
            adaptive_threshold = float(np.clip(adaptive_threshold, 0.00001, 1.0))
            logging.info(
                "ADAPTIVE | symbol=%s regime=%s vol=%.6f threshold=%.6f",
                symbol,
                symbol_regime,
                float(symbol_vol),
                float(adaptive_threshold),
            )
            logging.info("THRESHOLD BIAS | symbol=%s bias=%.4f", symbol, state.threshold_bias)
            eligible = symbol_is_eligible(symbol, symbol_vol, adaptive_threshold)
            signal_strength = abs(float(closes[-1] - closes[-2])) / max(abs(float(closes[-2])), 1e-9) if len(closes) > 1 else 0.0
            signal_strength *= 1000.0
            if signal_strength < 0.05:
                signal_strength *= 1.5
            signal_strength = float(np.clip(signal_strength, 0.0, 1.0))
            signal_score_seed = signal_strength
            if not eligible:
                scan_rows.append({"symbol": symbol, "bars": len(bars), "volatility": symbol_vol, "signal_score": signal_score_seed, "eligible": False, "signal_generated": False, "executable": False, "blocked_reason": "low_volatility"})
                continue
            spread_penalty = 0.0
            ranked_inputs.append({
                "symbol": symbol,
                "volatility": symbol_vol,
                "signal_score": signal_score_seed,
                "eligible": True,
                "regime": regime,
                "signal_strength": signal_score_seed,
                "regime": symbol_regime,
                "regime_bonus": 0.0,
                "spread_penalty": spread_penalty,
                "spread_ratio": spread_penalty,
                "win_rate": float(state.performance.get("win_rate", 0.0)),
                "avg_pnl": float(state.performance.get("avg_pnl", 0.0)),
            })
            scan_rows.append({"symbol": symbol, "bars": len(bars), "volatility": symbol_vol, "signal_score": signal_score_seed, "eligible": True, "signal_generated": signal_score_seed > 0, "executable": True, "blocked_reason": "ok"})

        if find_setups_fn is not None and bar_index % 10 == 0:
            scan_summary = scan_health_tracker.summary(current_bar=bar_index)
            logging.info(
                "SCAN_HEALTH | bar=%s disabled=%s failures=%s setups=%s",
                bar_index,
                scan_summary.get("disabled", []),
                scan_summary.get("failed", {}),
                scan_summary.get("setups", {}),
            )
        if eligible_symbols_with_data == 0:
            logging.warning(
                "MARKET DATA NOT READY | no symbols with >=%d real bars",
                hard_min_data_bars,
            )
            _sleep()
            continue

        logging.info(
            "LOOP DATA SUMMARY | bars_loaded=%d symbols=%s",
            len([sym for sym, bars in bars_by_symbol.items() if bars is not None and len(bars) > 0]),
            [sym for sym, bars in bars_by_symbol.items() if bars is not None and len(bars) > 0],
        )
        runtime_scan_research.evaluate_due_setups(
            bars_by_symbol=bars_by_symbol,
            current_bar_index=bar_index,
        )
        if bar_index % 10 == 0:
            runtime_scan_research.build_summary()

        active_symbol_count = max(1, sum(1 for row in scan_rows if bool(row.get("eligible", False))))
        base_risk_per_trade_usd = float(os.getenv('RISK_PER_TRADE_USD', '0') or 0.0)
        adjusted_risk_per_trade_usd = scale_portfolio_risk(base_risk_per_trade_usd, active_symbol_count) if base_risk_per_trade_usd > 0 else 0.0
        allocation_weights = compute_capital_allocation_weights(symbol_states)
        for row in scan_rows:
            logging.info(
                "SYMBOL STATUS | symbol=%s vol=%.8f signal=%.6f eligible=%s",
                row["symbol"],
                float(row.get("volatility", 0.0)),
                float(row.get("signal_score", 0.0)),
                str(bool(row.get("eligible", False))).lower(),
            )
        readiness_rows = [
            {
                "volatility": float(row.get("volatility", 0.0)),
                "signal": float(row.get("signal_score", 0.0)),
            }
            for row in scan_rows
        ]
        if not is_market_ready(readiness_rows):
            logging.info("MARKET NOT READY | skipping cycle")
            _sleep()
            continue
        logging.info(
            "LOOP PRE-RANK | symbols_ready=%d force_mode=%s",
            len([row for row in scan_rows if bool(row.get("eligible", False))]),
            str(FORCE_MODE).lower(),
        )
        ranked_symbols = rank_symbols(
            ranked_inputs,
            state=state,
            positions=list(getattr(state, "positions", []) or []),
            evo_state=evo_engine_state if _evo2_enabled() else None,
        )
        eligible_symbols = [s for s in ranked_symbols if s.get("eligible")]
        no_eligible_symbols = len(eligible_symbols) == 0
        if no_eligible_symbols:
            logging.warning("LEVEL7 BOOST | no eligible symbols → soft activation")
            for s in ranked_symbols:
                s["score"] = float(s.get("score", 0.0)) * 1.15
        selected_quality = 0.0
        fallback_mode_active = False
        if no_eligible_symbols:
            best_signal = max((float(row.get("signal_score", 0.0)) for row in scan_rows), default=0.0)
            best_quality = max(
                (
                    float(symbol_row.get("quality_score", symbol_row.get("signal_score", 0.0)))
                    for symbol_row in ranked_symbols
                ),
                default=0.0,
            )
            if best_signal < MIN_SIGNAL_FOR_FALLBACK or best_quality < MIN_QUALITY_FOR_FALLBACK:
                logging.info("FALLBACK SKIPPED | no viable signals")
                _sleep()
                continue
            if scan_rows:
                fallback_mode_active = True
                fallback = max(scan_rows, key=lambda x: float(x.get("volatility", 0.0)))
                selected_symbol = str(fallback.get("symbol", ""))
                logging.warning("FALLBACK MODE ACTIVATED | symbol=%s", selected_symbol)
                if not selected_symbol or selected_symbol not in symbol_states or selected_symbol not in bars_by_symbol:
                    _sleep()
                    continue
                selected_score = float(fallback.get("signal_score", 0.0))
                selected_vol = float(fallback.get("volatility", 0.0))
                selected_signal = float(fallback.get("signal_score", 0.0))
                fallback_floor = FALLBACK_ABSOLUTE_SYMBOL_FLOOR_XAUUSD if selected_symbol.upper().startswith("XAU") else FALLBACK_ABSOLUTE_SYMBOL_FLOOR_FX
                fallback_quality = compute_entry_quality_score(
                    signal_score=selected_signal,
                    realized_volatility=selected_vol,
                    spread_ratio=0.0,
                    regime=detect_market_regime(selected_vol),
                    symbol_rank_score=0.2,
                )
                fallback_allowed = (
                    selected_signal >= FALLBACK_MIN_SIGNAL_SCORE
                    and selected_vol >= fallback_floor * 0.8
                    and fallback_quality >= 0.50
                )
                selected_quality = fallback_quality
                logging.info(
                    "LEVEL7 FALLBACK | symbol=%s signal=%.4f quality=%.4f allowed=%s",
                    selected_symbol,
                    float(selected_signal),
                    float(fallback_quality),
                    str(fallback_allowed).lower(),
                )
                if not fallback_allowed:
                    selected_score = 0.0
            else:
                _sleep()
                continue
        else:
            fallback_mode_active = False
            selected_symbol = str(eligible_symbols[0]["symbol"])
            selected_score = float(eligible_symbols[0].get("score", 0.0))
            selected_vol = float(eligible_symbols[0].get("volatility", 0.0))
            selected_signal = float(eligible_symbols[0].get("signal_score", 0.0))
            selected_quality = float(eligible_symbols[0].get("quality_score", selected_signal))
        logging.info(
            "SYMBOL PICKED | symbol=%s score=%.6f vol=%.8f signal=%.6f",
            selected_symbol,
            selected_score,
            selected_vol,
            selected_signal,
        )

        logging.info(
            "SYMBOL PICK DEBUG | selected=%s score=%.4f quality=%.4f fallback=%s",
            selected_symbol,
            float(selected_score),
            float(selected_quality),
            str(fallback_mode_active).lower(),
        )
        if selected_symbol not in bars_by_symbol:
            logging.warning('SYMBOL LOOP SKIP | symbol=%s reason=bars_missing', selected_symbol)
            _sleep()
            continue
        if not is_symbol_allowed(selected_symbol):
            logging.warning(
                "SYMBOL BLOCKED | symbol=%s reason=not_in_whitelist",
                selected_symbol,
            )
            _sleep()
            continue
        if str(selected_symbol).upper() == "XAUUSD":
            logging.warning("XAU HARD BLOCK | skipping symbol")
            _sleep()
            continue
        if selected_score > 0.05 and selected_quality >= FALLBACK_MIN_QUALITY_SCORE:
            logging.info(
                "SYMBOL CANDIDATE READY | symbol=%s score=%.4f quality=%.4f",
                selected_symbol,
                selected_score,
                selected_quality,
            )
        preselected_bars = bars_by_symbol.get(selected_symbol)
        preselected_count = get_bar_count_safe(preselected_bars)
        if preselected_count < hard_min_data_bars:
            logging.warning(
                "PRE-EXEC HARD DATA BLOCK | symbol=%s bars=%d required=%d",
                selected_symbol,
                preselected_count,
                hard_min_data_bars,
            )
            _sleep()
            continue
        # =========================================
        # 🔥 GLOBAL SAFE DEFAULTS (CRASH PROOF)
        # voorkomt UnboundLocalError in ALLE flows
        # =========================================
        signal_score = float(locals().get("signal_score", 0.0))
        try:
            entry_quality_safe = max(float(locals().get("entry_quality", 0.3)), 0.3)
        except Exception:
            entry_quality_safe = 0.3
        realized_volatility = float(locals().get("realized_volatility", 0.0))

        args.symbol = selected_symbol
        FORCE_TOP_SYMBOL = str(os.getenv("FORCE_TOP_SYMBOL", "1")).strip().lower() in {"1", "true", "yes"}
        force_top_symbol = bool(FORCE_TOP_SYMBOL)
        if FORCE_TOP_SYMBOL:
            logging.info(
                "TOP SYMBOL FORCE ENABLED | symbol=%s score=%.4f",
                args.symbol,
                float(selected_signal),
            )
        state = symbol_states[selected_symbol]
        if not hasattr(state, "no_trade_loops"):
            state.no_trade_loops = 0
        _ensure_profit_engine_state(state)
        if not getattr(state, "active_trade", None):
            if getattr(state, "profit_v2", None):
                if (
                    state.profit_v2.get("partial_taken")
                    or state.profit_v2.get("be_armed")
                    or state.profit_v2.get("trail_armed")
                ):
                    _reset_profit_engine_state(state, None)
        update_smart_threshold_relaxation(state, selected_symbol)
        if global_loop_count % 500 == 0:
            logging.info(
                "STATS | trades=%d wins=%d losses=%d pnl=%.2f max_eq=%.2f dd=%.2f",
                int(state.total_trades),
                int(state.wins),
                int(state.losses),
                float(state.total_pnl),
                float(state.max_equity),
                float(state.current_drawdown),
            )
        adaptive_threshold_relax = state.adaptive_threshold_relax
        adaptive_filters = state.adaptive_filters
        bars = bars_by_symbol[selected_symbol]

        if bars is None:
            logging.warning("HARD DATA BLOCK | symbol=%s reason=bars_none", args.symbol)
            _evo2_register_block(evo_engine_state, selected_symbol, "startup_block")
            _sleep()
            continue

        # ===============================
        # HARD DATA MODE (NO SYNTHETIC BARS)
        # ===============================
        current_bar_count = get_bar_count_safe(bars)

        if current_bar_count < hard_min_data_bars:
            logging.warning(
                "HARD DATA BLOCK | symbol=%s bars=%d required=150 (NO SYNTHETIC)",
                args.symbol,
                current_bar_count,
            )
            _evo2_register_block(evo_engine_state, selected_symbol, "insufficient_data")
            _sleep()
            continue

        if disable_fallback_without_data:
            try:
                fallback_mode_active = False
            except Exception:
                pass
            try:
                force_top_symbol = False if current_bar_count < hard_min_data_bars else force_top_symbol
            except Exception:
                pass
        logging.info(
            "BAR COUNT DEBUG | symbol=%s bars=%d",
            args.symbol,
            len(bars),
        )
        active_trade = state.active_trade
        active_position_scale = state.active_position_scale
        active_notional_usd = state.active_notional_usd
        loop_count_without_trade = state.loop_count_without_trade
        last_trade_signature = state.last_trade_signature
        last_execution_index = state.last_execution_index
        no_trade_snapshot_for_active_trade = state.no_trade_snapshot_for_active_trade
        live_symbol_specs = live_symbol_specs_map.get(selected_symbol, live_symbol_specs)
        live_position_qty = 0.0
        live_position_idx = None

        latest_bar = bars.iloc[-1]
        latest_bar_time = pd.Timestamp(latest_bar['timestamp']).to_pydatetime()
        is_same_bar = getattr(state, "last_decision_bar_time", None) == latest_bar_time
        debug_allow_same_bar = _env_bool("DEBUG_ALLOW_SAME_BAR", True)
        if is_same_bar and not debug_allow_same_bar:
            logging.info("SKIP SAME BAR | symbol=%s bar=%s", args.symbol, str(latest_bar_time))
            time.sleep(0.25)
            continue
        if is_same_bar and debug_allow_same_bar:
            logging.warning(
                "DEBUG MODE | allowing same bar execution | symbol=%s",
                args.symbol,
            )
        state.last_decision_bar_time = latest_bar_time
        latest_index = len(bars) - 1
        loop_now = datetime.now(timezone.utc)
        if EXIT_ENGINE_V3 and state.active_trade:
            try:
                logging.info(
                    "EXIT ENGINE V3 | symbol=%s price=%.5f",
                    args.symbol,
                    float(latest_bar["close"]),
                )
                manage_exit_v3(state.active_trade, float(latest_bar["close"]), loop_now.timestamp())
            except Exception as e:
                logging.warning("EXIT ENGINE V3 FAILED | symbol=%s err=%s", args.symbol, str(e))
        broker_flat = False
        if live_session is not None:
            _enforce_mt5_live_session_health(live_session, live_safety_controller, reason_prefix='mt5_heartbeat_failed')
        if live_session is not None and live_symbol_specs is not None:
            sync_result = sync_bot_with_exchange_position(
                session=live_session,
                symbol=args.symbol,
                active_trade=active_trade,
                active_notional_usd=active_notional_usd,
                active_position_scale=active_position_scale,
                risk_state=risk_state,
                safety_controller=live_safety_controller,
                specs=live_symbol_specs,
                leverage=float(os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0),
            )
            active_trade = sync_result.active_trade
            active_notional_usd = sync_result.active_notional_usd
            active_position_scale = sync_result.active_position_scale
            live_position_qty = sync_result.exchange_position.qty
            live_position_idx = sync_result.exchange_position.position_idx
            state.active_trade = active_trade
            state.active_notional_usd = active_notional_usd
            state.active_position_scale = active_position_scale
            broker_flat = not sync_result.exchange_position.is_open
            if broker_flat:
                hard_reset = reset_flat_internal_state(
                    symbol=args.symbol,
                    state=state,
                    risk_state=risk_state,
                    reason='broker_flat_confirmed_post_sync',
                    now=loop_now,
                    force_reentry_ready=True,
                )
                if hard_reset:
                    logging.warning(
                        'FLAT RECOVERY | symbol=%s reason=direct_post_sync_reset broker_flat=true',
                        args.symbol,
                    )
            flat_recovery_triggered = recover_if_stale_flat_state(
                symbol=args.symbol,
                state=state,
                risk_state=risk_state,
                exchange_position=sync_result.exchange_position,
                now=loop_now,
            )
            if flat_recovery_triggered:
                logging.warning(
                    'FLAT RECOVERY DETECTED | symbol=%s -> forcing clean state',
                    args.symbol,
                )
                state.active_trade = None
                state.active_notional_usd = 0.0
                state.active_position_scale = 0.0
                _reset_profit_engine_state(state, None)
                logging.info(
                    "PROFIT ENGINE FLAT RESET | symbol=%s reason=position_closed",
                    args.symbol,
                )
                risk_state.open_positions = 0
            active_trade = state.active_trade
            active_notional_usd = state.active_notional_usd
            active_position_scale = state.active_position_scale
            if sync_result.safe_mode_triggered:
                _sleep()
                continue
            if active_trade is not None and sync_result.exchange_position.is_open:
                ensure_exchange_protection(
                    session=live_session,
                    symbol=args.symbol,
                    trade=active_trade,
                    exchange_position=sync_result.exchange_position,
                    specs=live_symbol_specs,
                    safety_controller=live_safety_controller,
                    trailing_policy=live_trailing_policy,
                )

        if live_session is not None:
            broker_open_positions_count = count_symbol_open_positions(live_session, args.symbol)
            setattr(risk_state, 'broker_open_positions', broker_open_positions_count)
            internal_open_positions = int(getattr(risk_state, 'open_positions', 0))
            exchange_position_for_sync = fetch_open_position(live_session, args.symbol)
            if broker_open_positions_count != risk_state.open_positions:
                logging.warning(
                    'DESYNC DETECTED | forcing resync | symbol=%s',
                    args.symbol,
                )
                perform_hard_position_sync(
                    symbol=args.symbol,
                    state=state,
                    risk_state=risk_state,
                    broker_positions=broker_open_positions_count,
                    exchange_position=exchange_position_for_sync if exchange_position_for_sync.is_open else None,
                    now=loop_now,
                )
            elif broker_open_positions_count == 0:
                perform_hard_position_sync(
                    symbol=args.symbol,
                    state=state,
                    risk_state=risk_state,
                    broker_positions=0,
                    exchange_position=None,
                    now=loop_now,
                )
            logging.info(
                'POSITION SYNC | internal=%d broker=%d',
                risk_state.open_positions,
                broker_open_positions_count,
            )
            active_trade = state.active_trade
            logging.info(
                "OPEN POSITION SYNC FIX | internal=%d broker=%d final=%d",
                internal_open_positions,
                broker_open_positions_count,
                int(getattr(risk_state, 'open_positions', broker_open_positions_count)),
            )
        # =========================================
        # 🔥 CANONICAL BROKER STATE (CLEAN)
        # =========================================
        raw_broker_count = locals().get("broker_open_positions_count")
        if raw_broker_count is None:
            logging.warning("BROKER STATE FALLBACK | assuming flat")
            broker_open_positions_count = 0
        else:
            try:
                broker_open_positions_count = int(raw_broker_count)
            except Exception:
                broker_open_positions_count = 0

        truth: PositionTruth = resolve_position_truth(
            str(args.symbol),
            active_trade=state.active_trade,
            broker_open_positions_count=broker_open_positions_count,
        )
        broker_flat = truth.broker_flat
        logging.info(
            "POSITION TRUTH | symbol=%s evo_open=%s active_trade_open=%s broker_open=%s is_open=%s source=%s",
            truth.symbol,
            str(truth.evo_open).lower(),
            str(truth.active_trade_open).lower(),
            str(truth.broker_open).lower(),
            str(truth.is_open).lower(),
            truth.source,
        )
        logging.info(
            "BROKER STATE | symbol=%s broker_count=%d flat=%s source=%s",
            args.symbol,
            broker_open_positions_count,
            str(broker_flat).lower(),
            truth.source,
        )
        # =========================================
        # 🔥 ACCOUNT STATE (CLEAN)
        # =========================================
        account_snapshot = {}
        with suppress(Exception):
            account_snapshot = fetch_live_account_equity(live_session) or {}

        raw_equity = account_snapshot.get("equity")
        raw_margin = account_snapshot.get("free_margin")

        def _safe_float_or_default(v, default):
            try:
                f = float(v)
                return f if f > 0 else default
            except Exception:
                return default

        equity = _safe_float_or_default(
            raw_equity,
            float(getattr(state, "last_equity", 50.0) or 50.0),
        )
        free_margin = _safe_float_or_default(
            raw_margin,
            float(equity * 0.5),
        )

        state.last_equity = float(equity)
        logging.info(
            "ACCOUNT SYNC SAFE | equity=%.2f free_margin=%.2f",
            float(equity),
            float(free_margin),
        )

        same_bar_entry_allowed_before_loop = bool(risk_state.same_bar_entry_allowed)
        exit_result = manage_active_trade(
            trade=active_trade,
            risk_state=risk_state,
            risk_cfg=risk_cfg,
            latest_price=float(latest_bar['close']),
            latest_bar_time=latest_bar_time,
            loop_now=loop_now,
            active_notional_usd=active_notional_usd,
            active_position_scale=active_position_scale,
            no_trade_snapshot_for_active_trade=no_trade_snapshot_for_active_trade,
            same_bar_entry_allowed_before_loop=same_bar_entry_allowed_before_loop,
            trades_file=trades_file,
            symbol=args.symbol,
            bars=bars,
            adaptive_filters=adaptive_filters,
            live_session=live_session,
            live_position_qty=live_position_qty,
            live_position_idx=live_position_idx,
            live_safety_controller=live_safety_controller if live_session is not None else None,
            live_trailing_policy=live_trailing_policy if live_session is not None else None,
        )
        active_trade = exit_result.trade
        active_position_scale = exit_result.position_scale
        active_notional_usd = exit_result.active_notional_usd
        no_trade_snapshot_for_active_trade = exit_result.no_trade_snapshot
        if active_trade is None and state.reversal_pending_close:
            state.reversal_pending_close = False
        if exit_result.exited:
            state.last_trade_closed_timestamp = time.time()
            state.last_close_timestamp = time.time()
            state.recent_close_timer = 3
            configured_account_equity_usd = float(os.getenv('ACCOUNT_EQUITY_USD', '0') or 0.0)
            account_equity_usd = resolve_effective_account_equity(
                live_session,
                configured_account_equity_usd,
            )
            csv_perf_logging_enabled = str(os.getenv("PERFORMANCE_CSV_LOGGING", "0")).strip().lower() in {"1", "true", "yes", "on"}
            metrics = update_closed_trade_performance(
                state=state,
                symbol=args.symbol,
                realized_pnl=float(exit_result.realized_pnl),
                account_equity_usd=account_equity_usd,
                csv_logging_enabled=csv_perf_logging_enabled,
            )
            trades_count = int(state.total_trades)
            win_rate = float(metrics["winrate"])
            if trades_count >= 3:
                if win_rate > 0.6:
                    state.threshold_bias *= 0.97
                if win_rate < 0.4:
                    state.threshold_bias *= 1.05
                state.threshold_bias = float(_clamp(state.threshold_bias, 0.7, 1.3))
            if trades_count >= 10 and win_rate < 0.35:
                state.disabled = True
                logging.warning("SYMBOL DISABLED | symbol=%s reason=low_performance", args.symbol)
            trade_context = exit_result.closed_trade_context
            trade_regime = str(
                trade_context.get("regime")
                or getattr(exit_result, "regime", "")
                or getattr(latest_filter, "current_market_regime", "NORMAL")
            ).upper()
            record_trade_performance(trade_regime, float(exit_result.realized_pnl))
            logging.info(
                "PERF TRACK | regime=%s pnl=%.4f samples=%d",
                trade_regime,
                float(exit_result.realized_pnl),
                len(performance_tracker.get(trade_regime, [])),
            )
            trade_memory.append({
                "symbol": str(trade_context.get("symbol", args.symbol)),
                "signal_score": float(trade_context.get("signal_score", 0.0)),
                "volatility": float(trade_context.get("volatility", 0.0)),
                "spread": float(trade_context.get("spread", 0.0)),
                "regime": str(trade_context.get("regime", "UNKNOWN")),
                "result": float(exit_result.pnl_ratio),
                "win": bool(float(exit_result.realized_pnl) > 0),
                "hold_seconds": float(exit_result.hold_seconds),
            })
            if float(exit_result.realized_pnl) < 0:
                state.recent_loss_patterns.append((round(float(trade_context.get("volatility", 0.0)), 4), round(float(trade_context.get("signal_score", 0.0)), 2)))
                pattern_counts: dict[tuple[float, float], int] = {}
                for signature in state.recent_loss_patterns:
                    pattern_counts[signature] = pattern_counts.get(signature, 0) + 1
                for signature, count in pattern_counts.items():
                    if count >= 3:
                        if signature not in state.blocked_patterns:
                            state.blocked_patterns.append(signature)
                            state.blocked_patterns_expires_at[signature] = global_loop_count + 200
                            logging.warning("PATTERN BLOCKED | reason=repeated losses")
            if str(os.getenv("FX_EDGE_ANALYSIS_AUTOWRITE", "true")).lower() in {"1", "true", "yes", "on"}:
                with suppress(Exception):
                    summary_payload = write_edge_analysis(config=EDGE_ANALYSIS_CONFIG)
                    logging.info(
                        "FX EDGE SUMMARY WRITTEN | groups=%d top=%d bottom=%d promotable=%d disabled=%d",
                        len(summary_payload.get("groups", [])),
                        len(summary_payload.get("top_edges", [])),
                        len(summary_payload.get("bottom_edges", [])),
                        len(summary_payload.get("promotable_edges", [])),
                        len(summary_payload.get("disabled_edges", [])),
                    )

        loop_pnl_delta = float(risk_state.daily_pnl_usd - previous_daily_pnl_usd)
        previous_daily_pnl_usd = float(risk_state.daily_pnl_usd)
        live_params = adaptive_filters.current_params(params)
        live_params = evolution_engine.get_adjusted_params(live_params, symbol=args.symbol)
        live_params['data_backend'] = data_backend
        arr = {k: bars[k].to_numpy() for k in ['open', 'high', 'low', 'close', 'volume']}
        runtime_context = prepare_runtime_context(arr, live_params)
        runtime_context["recent_signal_scores"] = list(adaptive_filters.recent_signal_scores)
        strategy_type = str(strategy.get("strategy_type") or "default")
        runtime_context["signal_strength_series"] = compute_signal_strength_series(runtime_context, strategy_type)
        enrich_signal_strength_context(runtime_context)
        latest_index = len(arr['close']) - 1
        latest_filter = evaluate_entry_filter_snapshot(runtime_context, latest_index)
        signal, signal_strength = evaluate_runtime_signal(runtime_context, strategy_type, latest_index)
        adaptive_filters.update_from_snapshot(latest_filter)
        realized_volatility = compute_effective_volatility(bars, latest_filter, window=VOLATILITY_LOOKBACK)
        adaptive_filters.rolling_spread_ratios.append(float(latest_filter.spread_ratio))
        rolling_median_spread = median(adaptive_filters.rolling_spread_ratios) if adaptive_filters.rolling_spread_ratios else float(latest_filter.spread_ratio)
        adaptive_spread = max(float(live_params.get('max_spread_ratio', latest_filter.spread_threshold)), rolling_median_spread * float(live_params.get('spread_multiplier', adaptive_filters.spread_multiplier)) * 1.2)
        prev_signal_score = adaptive_filters.recent_signal_scores[-1] if adaptive_filters.recent_signal_scores else None
        logging.warning("🔥 BEFORE SIGNAL CALC | symbol=%s", args.symbol)
        signal_score = compute_signal_score(
            float(latest_filter.signal_strength),
            float(latest_filter.signal_strength_threshold),
            prev_score=float(prev_signal_score) if prev_signal_score is not None else None,
        )
        logging.warning("🔥 SIGNAL SCORE | symbol=%s score=%.4f", args.symbol, float(signal_score))
        log_stats["signals"] += 1
        log_stats["signal_values"].append(float(signal_score))
        log_stats["last_symbols"].append(str(args.symbol))
        latest_filter = evaluate_entry_filter_snapshot(runtime_context, latest_index, signal_score=signal_score)
        adaptive_filters.update_from_snapshot(latest_filter)
        signal_priority = classify_signal_priority(signal_score)
        symbol_rank_score = float(selected_score)
        quality_score = compute_entry_quality_score(
            signal_score=float(signal_score),
            realized_volatility=float(realized_volatility),
            spread_ratio=float(latest_filter.spread_ratio),
            regime=str(latest_filter.current_market_regime),
            symbol_rank_score=symbol_rank_score,
        )
        logging.info(
            "ENTRY QUALITY | symbol=%s signal=%.4f quality=%.4f spread=%.6f vol=%.6f regime=%s",
            args.symbol,
            signal_score,
            quality_score,
            float(latest_filter.spread_ratio),
            float(realized_volatility),
            str(latest_filter.current_market_regime),
        )
        signal_decay_blocked, signal_decay_drop, signal_decay_ratio = evaluate_signal_decay_guard(
            runtime_context.get('signal_strength_series'),
            latest_signal_score=signal_score,
        )
        if signal_decay_blocked:
            logging.warning(
                "SIGNAL DECAY GUARD | symbol=%s blocked=true drop=%.4f ratio=%.4f score=%.4f",
                args.symbol,
                signal_decay_drop,
                signal_decay_ratio,
                signal_score,
            )
        else:
            logging.info(
                "SIGNAL DECAY GUARD | symbol=%s blocked=false drop=%.4f ratio=%.4f score=%.4f",
                args.symbol,
                signal_decay_drop,
                signal_decay_ratio,
                signal_score,
            )
        generated_signals_count = int(sum(adaptive_filters.signals_generated_last_100))
        early_signal_mode_active = (
            len(bars) >= 5
            and generated_signals_count <= 2
            and signal_score >= 0.55
        )
        if early_signal_mode_active:
            logging.info(
                "EARLY SIGNAL MODE | bars=%d score=%.4f",
                len(bars),
                signal_score,
            )
        logging.info(
            'Signal strength check | raw=%.6f raw_mean=%.6f normalized=%.6f normalized_mean=%.6f normalized_std=%.6f threshold=%.6f',
            latest_filter.signal_strength_raw,
            latest_filter.signal_strength_raw_mean,
            latest_filter.signal_strength,
            latest_filter.signal_strength_mean,
            latest_filter.signal_strength_std,
            latest_filter.signal_strength_threshold,
        )
        if should_apply_volume_filter(data_backend=data_backend, symbol=args.symbol):
            logging.info(
                (
                    'Volume filter check | raw_volume=%.6f rolling_volume_mean=%.6f '
                    'normalized_volume=%.6f normalized_volume_mean=%.6f relative_volume=%.6f '
                    'formula_inputs=(normalized_volume=%.6f normalized_volume_mean=%.6f) '
                    'baseline_source=%s fallback_applied=%s threshold=%.4f effective=%.4f override=%s regime=%s'
                ),
                latest_filter.volume,
                latest_filter.raw_volume_mean,
                latest_filter.normalized_volume,
                latest_filter.normalized_volume_mean,
                latest_filter.relative_volume,
                latest_filter.normalized_volume,
                latest_filter.normalized_volume_mean,
                getattr(latest_filter, 'normalized_volume_mean_source', 'unknown'),
                str(bool(getattr(latest_filter, 'normalized_volume_mean_fallback_applied', False))).lower(),
                latest_filter.min_volume_ratio,
                latest_filter.effective_volume_ratio_threshold,
                str(bool(getattr(latest_filter, 'volume_override_active', False))).lower(),
                latest_filter.current_market_regime,
            )
            if latest_filter.volume_filter_active and latest_filter.relative_volume <= 0.0:
                logging.warning('Volume anomaly detected | mean=%.8f current=%.8f', latest_filter.volume_mean, latest_filter.volume)
        logging.info('Adaptive spread | spread=%.6f allowed_spread=%.6f rolling_median=%.6f multiplier=%.4f vol_boost=%.4f fill_risk_score=%.4f blocked_reason=%s', latest_filter.spread_ratio, adaptive_spread, rolling_median_spread, float(live_params.get('spread_multiplier', adaptive_filters.spread_multiplier)), latest_filter.volatility_boost, float(getattr(latest_filter, 'fill_risk_score', 0.0)), adaptive_filters.last_block_reason)
        # =========================
        # HARD FILL RISK BLOCK (CRITICAL FIX)
        # =========================
        try:
            max_fill_risk_score = get_max_fill_risk(args.symbol)
        except Exception:
            max_fill_risk_score = 25000.0

        if float(getattr(latest_filter, 'fill_risk_score', 0.0)) > max_fill_risk_score:
            logging.warning(
                "HARD BLOCK | fill risk too high | symbol=%s score=%.2f max=%.2f spread=%.6f",
                args.symbol,
                float(getattr(latest_filter, 'fill_risk_score', 0.0)),
                float(max_fill_risk_score),
                float(latest_filter.spread_ratio),
            )

            signal_generated = False
            signal_executable = False
            final_execution_allowed = False
            _evo2_register_block(evo_engine_state, args.symbol, "fill_risk")
            _sleep()
            continue
        signal_generated = (
            signal_score >= 0.5
            or signal_priority in {'medium', 'high'}
        )
        if signal_score >= 0.8:
            signal_generated = True
        if signal_generated and signal_score < 0.6 and signal_priority != 'high' and not early_signal_mode_active:
            signal_generated = False
        filter_passed = bool(latest_filter.passed and latest_filter.spread_ratio <= adaptive_spread)
        filter_override = adaptive_filters.maybe_override_filter(latest_filter, signal_priority, signal_score)
        final_filter_passed = filter_passed or filter_override
        if not final_filter_passed and str(getattr(latest_filter, "reason_code", "")) in {"weak_trend", "low_trend_strength", "structure_missing"}:
            _evo2_register_block(evo_engine_state, args.symbol, "no_structure")
        if signal_score >= 0.75:
            final_filter_passed = True
        if early_signal_mode_active:
            final_filter_passed = True
        pre_exec_score_threshold = compute_execution_score_threshold(
            market_regime=str(latest_filter.current_market_regime),
            volatility=realized_volatility,
            signal_strength=float(getattr(latest_filter, 'signal_strength', 0.0)),
            signal_strength_threshold=float(getattr(latest_filter, 'signal_strength_threshold', 1.0)),
            soft_trend_applied=bool(getattr(latest_filter, 'soft_trend_applied', False)),
            consecutive_losses=risk_state.consecutive_losses,
            performance_score=float(state.performance.get("avg_pnl", 0.0)),
            trades_last_5min=adaptive_filters.trades_last_5min(loop_now),
            loops_without_trade=loop_count_without_trade,
        )
        execution_priority_required = pre_exec_score_threshold
        execution_priority_required *= 0.85
        execution_threshold = float(live_params.get('execution_score_threshold', execution_priority_required))
        execution_threshold = max(_safe_float(os.getenv("ABS_MIN_EXEC_THRESHOLD", "0.25"), 0.25), execution_threshold)
        min_trend_strength = float(live_params.get('min_trend_strength', 0.0))
        position_scale = float(live_params.get('position_scale', 1.0))
        if float(risk_state.daily_pnl_usd) < 0.0 and int(risk_state.consecutive_losses) >= 5:
            position_scale *= 0.8
            execution_threshold *= 1.05
            logging.warning("GLOBAL DEFENSE MODE ACTIVATED")
        recent_trades = list(trade_memory)[-20:]
        if recent_trades:
            win_rate_last_20 = float(np.mean([1.0 if bool(t.get("win", False)) else 0.0 for t in recent_trades]))
            if win_rate_last_20 < 0.4:
                execution_threshold *= 1.05
                position_scale *= 0.8
                logging.info("META MODE SWITCH | mode=SAFE")
            elif win_rate_last_20 > 0.6:
                execution_threshold *= 0.95
                position_scale *= 1.1
                logging.info("META MODE SWITCH | mode=AGGRESSIVE")
        perf = state.performance
        if int(perf.get("trades", 0)) >= 5 and float(perf.get("win_rate", 0.0)) > 0.7:
            position_scale *= 1.1
            execution_threshold *= 0.97
            logging.info("HOT HAND MODE | symbol=%s", args.symbol)
        spread_tolerance = float(adaptive_spread)
        if realized_volatility >= max(0.0012, selected_vol * 1.1):
            execution_threshold *= 0.97
            position_scale *= 1.05
            spread_tolerance *= 1.05
            logging.info("AGGRESSION MODE | symbol=%s vol=%.8f", args.symbol, realized_volatility)
        adaptive_spread = spread_tolerance
        execution_score_threshold = max(
            EXECUTION_THRESHOLD_FLOOR,
            execution_threshold * 0.6,
        )
        current_trade_features = {
            "symbol": args.symbol,
            "signal_score": float(signal_score),
            "volatility": float(realized_volatility),
            "spread": float(latest_filter.spread_ratio),
            "regime": str(latest_filter.current_market_regime),
        }
        predicted_win_rate, predicted_pnl, ai_confidence = predict_trade_success(current_trade_features)
        recent_trade_closed = False
        if state.last_trade_closed_timestamp:
            if time.time() - state.last_trade_closed_timestamp < 60:
                recent_trade_closed = True
                state.recent_close_timer = max(int(state.recent_close_timer), 3)
        if state.recent_close_timer > 0:
            state.recent_close_timer -= 1
            recent_trade_closed = True
        else:
            recent_trade_closed = False

        profit_threshold = MIN_PROFIT_THRESHOLD
        if recent_trade_closed:
            profit_threshold *= 0.5
            execution_score_threshold *= 0.9
            logging.info("PROFIT FLOW MODE ACTIVE")

        expected_profit = float(predicted_pnl)
        min_expected_profit = float(profit_threshold)
        base_profit_ok = expected_profit >= min_expected_profit
        allow_reentry = bool(recent_trade_closed)
        top_signal = float(getattr(latest_filter, "signal_strength", signal_score))
        profit_gate_passed = False

        profit_gate_passed = base_profit_ok
        logging.info(
            "FLOW MODE | recent_close=%s profit_threshold=%.6f signal=%.4f",
            str(recent_trade_closed).lower(),
            profit_threshold,
            signal_score,
        )
        ai_confidence_override = False
        execution_score_threshold *= (1.0 - (ai_confidence - 0.5))
        if predicted_pnl < 0:
            position_scale *= 0.7
        elif predicted_pnl > 0:
            position_scale *= 1.1
        volatility = float(getattr(latest_filter, "volatility", 0.0))
        allowed_spread_ratio = max(float(adaptive_spread), 1e-9)
        effective_signal_threshold = max(0.45, LEVEL7_FAST_SIGNAL_MIN - adaptive_threshold_relax)
        effective_quality_threshold = max(0.55, LEVEL7_FAST_QUALITY_MIN - adaptive_threshold_relax)
        fast_lane = is_level7_fast_lane(
            quality_score=quality_score,
            signal_score=signal_score,
            spread_ratio=float(latest_filter.spread_ratio),
            allowed_spread_ratio=allowed_spread_ratio,
            realized_volatility=float(realized_volatility),
        )
        fast_lane = (
            quality_score >= effective_quality_threshold
            and signal_score >= effective_signal_threshold
            and float(latest_filter.spread_ratio) <= float(allowed_spread_ratio) * LEVEL7_MAX_SPREAD_USAGE
            and float(realized_volatility) >= LEVEL7_FAST_VOL_MIN
        )
        ultra_fast_lane = is_level7_ultra_fast_lane(
            quality_score=quality_score,
            signal_score=signal_score,
            spread_ratio=float(latest_filter.spread_ratio),
            allowed_spread_ratio=allowed_spread_ratio,
            realized_volatility=float(realized_volatility),
        )
        logging.info(
            "LEVEL7 FAST LANE | symbol=%s fast=%s ultra=%s quality=%.4f signal=%.4f vol=%.6f spread=%.6f",
            args.symbol,
            str(fast_lane).lower(),
            str(ultra_fast_lane).lower(),
            float(quality_score),
            float(signal_score),
            float(realized_volatility),
            float(latest_filter.spread_ratio),
        )
        logging.info(
            "SMART THRESHOLDS | symbol=%s signal=%.4f quality=%.4f relax=%.4f effective_signal=%.4f effective_quality=%.4f",
            args.symbol,
            float(signal_score),
            float(quality_score),
            adaptive_threshold_relax,
            effective_signal_threshold,
            effective_quality_threshold,
        )
        configured_account_equity_usd = float(os.getenv('ACCOUNT_EQUITY_USD', '0') or 0.0)
        account_resolution = resolve_account_equity(
            live_session,
            configured_account_equity_usd,
        )
        account_equity_usd = max(1.0, float(account_resolution.equity))
        logging.info('POSITION SIZING INPUT | equity=%.2f source=%s', float(account_equity_usd), account_resolution.source)
        csv_perf_logging_enabled = str(os.getenv("PERFORMANCE_CSV_LOGGING", "0")).strip().lower() in {"1", "true", "yes", "on"}
        update_equity_tracking(
            state=state,
            symbol=args.symbol,
            equity=account_equity_usd,
            csv_logging_enabled=csv_perf_logging_enabled,
        )
        if fast_lane and configured_account_equity_usd < 30:
            logging.info("FAST LANE DISABLED | low capital")
            fast_lane = False
            ultra_fast_lane = False
        effective_execution_score_threshold = max(
            EXECUTION_THRESHOLD_FLOOR,
            execution_score_threshold - (adaptive_threshold_relax * 0.35)
        )
        logging.info(
            "SMART EXECUTION THRESHOLD | symbol=%s base=%.4f effective=%.4f relax=%.4f",
            args.symbol,
            execution_score_threshold,
            effective_execution_score_threshold,
            adaptive_threshold_relax,
        )
        logging.info(
            "SIGNAL VS THRESHOLD | symbol=%s signal=%.4f required=%.4f delta=%.4f",
            args.symbol,
            float(signal_score),
            float(effective_execution_score_threshold),
            float(signal_score - effective_execution_score_threshold),
        )
        execution_priority_required = max(
            0.45,
            min(1.5, effective_execution_score_threshold)
        )
        execution_priority_allowed = (
            signal_score >= execution_priority_required
            or
            signal_priority in {'medium', 'high'}
            or signal_score >= SIGNAL_PRIORITY_MIN_EXECUTION_SCORE
        )
        if signal_score < 0.02:
            execution_priority_allowed = False
        entry_allowed = True
        force_entry_threshold = _env_float("FORCE_ENTRY_THRESHOLD", 0.005)

        get_current_price_callable = locals().get("get_current_price", globals().get("get_current_price"))
        def _profit_engine_price(symbol: str) -> float:
            price_source = 'unknown'
            price_value: float | None = None
            if callable(get_current_price_callable):
                try:
                    candidate = float(get_current_price_callable(symbol))
                    if candidate > 0:
                        price_value = candidate
                        price_source = 'live_symbol_getter'
                except Exception:
                    price_value = None
            if price_value is None and live_session is not None:
                try:
                    broker_positions = fetch_open_positions_from_broker(live_session)
                    for bp in broker_positions:
                        if str(getattr(bp, 'symbol', '')).upper() == str(symbol).upper():
                            candidate = float(getattr(bp, 'mark_price', 0.0) or getattr(bp, 'last_price', 0.0) or 0.0)
                            if candidate > 0:
                                price_value = candidate
                                price_source = 'broker_quote'
                                break
                except Exception:
                    price_value = None
            if price_value is None:
                price_value = float(latest_bar['close'])
                price_source = 'latest_bar_close'
            logging.info("PROFIT ENGINE PRICE SOURCE | symbol=%s source=%s price=%.5f", symbol, price_source, price_value)
            return float(price_value)
        close_position_callable = locals().get("close_position", globals().get("close_position"))
        close_partial_position_callable = locals().get("close_partial_position", globals().get("close_partial_position"))

        apply_profit_engine(
            state=risk_state,
            signal_score=signal_score,
            get_current_price=_profit_engine_price,
            close_position=close_position_callable,
            close_partial_position=close_partial_position_callable,
        )
        if signal_score > 0.15:
            profit_gate_passed = True

        if not profit_gate_passed:
            signal_generated = False
            logging.warning(
                "PROFIT GATE BLOCKED | symbol=%s expected=%.5f min=%.5f signal=%.4f",
                args.symbol,
                expected_profit,
                min_expected_profit,
                signal_score,
            )

        # Flow diagnostics only; no execution overrides are allowed.
        flow_override = False
        positions = list(getattr(state, "positions", []) or [])

        if getattr(state, "recent_exit", False):
            logging.info("FLOW STATE | recent_exit=true")

        for p in positions:
            if getattr(p, "runner_active", False):
                entry = float(getattr(p, "entry_price", 0.0))
                sl = float(getattr(p, "stop_loss", 0.0))
                side = str(getattr(p, "side", "")).upper()

                if side in {"LONG", "BUY"} and sl >= entry:
                    logging.info("FLOW STATE | risk_free_runner=long")

                if side in {"SHORT", "SELL"} and sl <= entry:
                    logging.info("FLOW STATE | risk_free_runner=short")

        if any(getattr(p, "closed_partial", False) for p in positions):
            logging.info("FLOW STATE | partial_tp_hit=true")

        if getattr(state, "no_trade_loops", 0) > 8:
            logging.info("FLOW STATE | no_trade_loops=%d", int(getattr(state, "no_trade_loops", 0)))

        executed_trades = sum(adaptive_filters.signals_executed_last_100)
        if generated_signals_count >= 3 and executed_trades == 0:
            execution_priority_required *= 0.95
            min_trend_strength *= 0.9
            live_params['min_trend_strength'] = max(0.0, min_trend_strength)
            logging.info("AUTO BOOST | no executions → relaxing filters")

        smart_timing = compute_smart_entry_timing(
            signal_score=signal_score,
            volatility=realized_volatility,
            regime=str(latest_filter.current_market_regime),
            consecutive_losses=risk_state.consecutive_losses,
            recent_execution_rate=adaptive_filters.execution_rate(),
        )
        min_time_target, cooldown_target = compute_level7_timing(
            base_min_time=int(round(smart_timing["min_time_seconds"])),
            base_cooldown=int(round(smart_timing["cooldown_seconds"])),
            fast_lane=fast_lane,
            ultra_fast_lane=ultra_fast_lane,
            consecutive_losses=risk_state.consecutive_losses,
        )
        adaptive_filters.adaptive_min_time_between_trades_seconds = min_time_target
        adaptive_filters.adaptive_cooldown_seconds = cooldown_target
        logging.info(
            "LEVEL7 SMART TIMING | symbol=%s min_time=%d cooldown=%d",
            args.symbol,
            adaptive_filters.adaptive_min_time_between_trades_seconds,
            adaptive_filters.adaptive_cooldown_seconds,
        )

        logging.info("STRICT EXECUTION MODE ACTIVE")

        pattern_signature = (round(float(realized_volatility), 4), round(float(signal_score), 2))
        expired_patterns = [sig for sig, expiry in state.blocked_patterns_expires_at.items() if global_loop_count >= int(expiry)]
        for sig in expired_patterns:
            state.blocked_patterns_expires_at.pop(sig, None)
            if sig in state.blocked_patterns:
                state.blocked_patterns.remove(sig)
        if pattern_signature in state.blocked_patterns:
            signal_generated = False
            logging.warning("PATTERN BLOCKED | reason=repeated losses")
        if ai_confidence < 0.35:
            ai_penalty = 0.95
            ai_action = "soft_penalty"
        elif ai_confidence > 0.65:
            ai_penalty = 1.05
            ai_action = "boost"
        else:
            ai_penalty = 1.0
            ai_action = "neutral"

        signal_score *= ai_penalty
        logging.info(
            "AI RELAXED | confidence=%.4f adjusted_signal=%.4f action=%s",
            ai_confidence,
            signal_score,
            ai_action,
        )
        if signal_score >= 0.9:
            signal_generated = True
            execution_priority_allowed = True
        signal_executable = (
            signal_generated
            and final_filter_passed
            and execution_priority_allowed
            and entry_allowed
        )
        if force_top_symbol:
            signal_executable = True
        if signal_decay_blocked:
            decay_penalty = max(0.5, min(0.95, float(signal_decay_ratio)))
            signal_score *= decay_penalty
            logging.warning(
                "SOFT BLOCK | converted to warning | symbol=%s reason=signal_decay_guard drop=%.4f ratio=%.4f penalty=%.4f",
                args.symbol,
                signal_decay_drop,
                signal_decay_ratio,
                decay_penalty,
            )
        adaptive_filters.record_signal(signal_generated, signal_executable, False, signal_score=signal_score)
        logging.info(
            'PRE-EXEC PRIORITY CHECK | score=%.3f required=%.3f priority=%s regime=%s volatility=%.6f allowed=%s',
            signal_score,
            execution_priority_required,
            signal_priority,
            latest_filter.current_market_regime,
            float(getattr(latest_filter, 'volatility', 0.0)),
            str(execution_priority_allowed).lower(),
        )
        logging.info(
            'EXECUTION BLOCK DEBUG | score=%.4f threshold=%.4f filter=%s priority=%s',
            signal_score,
            execution_priority_required,
            str(final_filter_passed),
            str(execution_priority_allowed),
        )

        signal_score = _safe_float(locals().get("signal_score", signal_score), 0.0)
        entry_quality_safe = max(_safe_float(locals().get("entry_quality_safe", locals().get("entry_quality", 0.3)), 0.3), 0.3)
        realized_volatility = _safe_float(locals().get("realized_volatility", 0.0))
        try:
            _load_evo_edge_state()
        except Exception:
            pass

        loop_status = LoopExecutionStatus(symbol=str(args.symbol))
        entry_ok = False
        entry_side = "LONG"

        # =========================================
        # EVO PRIMARY EXIT ENGINE (RUN FIRST)
        # =========================================
        try:
            symbol = str(args.symbol)
            price = float(latest_bar["close"])
            truth = resolve_position_truth(
                symbol,
                active_trade=state.active_trade,
                broker_open_positions_count=locals().get("broker_open_positions_count"),
            )
            logging.info(
                "SYSTEM STATE | symbol=%s is_open=%s active_trade=%s source=%s",
                symbol,
                str(truth.is_open).lower(),
                str(truth.active_trade_open).lower(),
                truth.source,
            )
            if truth.is_open:
                update_loop_status(
                    loop_status,
                    state="POSITION_ALREADY_OPEN",
                    reason="already_open",
                    authority="position_truth",
                    generated=False,
                    executable=False,
                    executed=False,
                    blocked=False,
                    setup_source=truth.source,
                )
                closed = manage(symbol, price)
                if closed:
                    active_trade = None
                    state.active_trade = None
                    update_loop_status(
                        loop_status,
                        state="EXIT_MANAGED",
                        reason="skipped_after_execution",
                        authority="evo_exit_manager",
                        generated=False,
                        executable=False,
                        executed=False,
                        blocked=False,
                    )
                    logging.info("EVO SYNC | global forced close")
                    logging.info("LEGACY EXIT SKIPPED | evo handled exit")
                    _sleep()
                    continue
        except Exception as evo_manage_exc:
            logging.exception("EVO PRIMARY EXIT ERROR | %s", evo_manage_exc)

        logging.info(
            "ENTRY INPUT DEBUG | symbol=%s signal=%.4f quality=%.4f",
            args.symbol,
            signal_score,
            entry_quality_safe,
        )

        # FX execution normalization: relax, but do not force
        if is_fx_symbol(args.symbol):
            effective_execution_score_threshold = min(
                _safe_float(locals().get("effective_execution_score_threshold", execution_score_threshold)),
                float(os.getenv("FX_MAX_EXECUTION_THRESHOLD", "0.42") or 0.42),
            )
            execution_score_threshold = float(effective_execution_score_threshold)
            logging.info("FX UNLOCK ACTIVE | threshold=%.4f", float(effective_execution_score_threshold))

        active_genome = _select_active_genome(str(args.symbol))
        genome_mode = str(ACTIVE_GENOME_MODE_CONTEXT.get(_symbol_key(str(args.symbol)), "other"))
        threshold_passed = bool(float(signal_score) >= float(execution_score_threshold))
        pass_stage_reached = "threshold_passed" if threshold_passed else "ranked"
        block_reason_secondary: str | None = None
        block_reason_primary: str | None = None
        setup_notes: str | None = None
        sizing_trace = build_sizing_trace(qty_requested=float(locals().get("live_position_qty", 0.0) or 0.0), qty_final=0.0)

        edge = ict_edge_v3(bars, symbol=str(args.symbol), genome=active_genome)
        if edge.should_trade:
            pass_stage_reached = "ict_passed"
        elif threshold_passed:
            pass_stage_reached = "threshold_passed"
        logging.info(
            "ICT V3 | trade=%s side=%s reason=%s conf=%.2f pd=%.2f pd_state=%s confluence=%.2f genome=%s",
            edge.should_trade,
            edge.side,
            edge.reason,
            edge.confidence,
            float(getattr(edge, "pd", 0.0) or 0.0),
            str(getattr(edge, "pd_state", "unknown")),
            float(getattr(edge, "confluence_score", 0.0) or 0.0),
            getattr(edge, "genome", active_genome.genome_id),
        )
        _log_evo_edge_summary(str(args.symbol))

        trade = None
        if edge.reason == "ict_v3_retrace_confirmed":
            trade = {
                "side": edge.side,
                "entry": edge.entry,
                "sl": edge.stop,
                "tp": edge.tp,
            }

        # HARD BLOCK
        if edge.reason != "ict_v3_retrace_confirmed":
            trade = None
            _register_blocked_genome(str(args.symbol), active_genome.genome_id, str(edge.reason))
            block_reason_primary = normalize_block_reason(str(edge.reason))
            if not threshold_passed:
                block_reason_secondary = "threshold_not_met"
            logging.info(
                "ENTRY V3 BLOCK | symbol=%s side=%s reason=%s pd=%.2f pd_state=%s confluence=%.2f genome=%s",
                args.symbol,
                str(edge.side or "UNKNOWN"),
                str(edge.reason),
                float(getattr(edge, "pd", 0.0) or 0.0),
                str(getattr(edge, "pd_state", "unknown")),
                float(getattr(edge, "confluence_score", 0.0) or 0.0),
                getattr(edge, "genome", active_genome.genome_id),
            )
            SETUP_AUDIT.append(
                SetupAuditRecord(
                    timestamp=utc_now_iso(),
                    symbol=str(args.symbol),
                    timeframe="M1",
                    genome_id=getattr(edge, "genome", active_genome.genome_id),
                    genome_mode=genome_mode if genome_mode in {"weighted", "forced_seed"} else "other",
                    selected_by_ranker=True,
                    rank_score=None,
                    selection_score=float(signal_score),
                    signal_score_raw=float(top_signal),
                    signal_score_adjusted=float(signal_score),
                    threshold_value=float(execution_score_threshold),
                    threshold_passed=threshold_passed,
                    side=str(edge.side or "LONG"),
                    entry_mode=str(getattr(active_genome, "entry_mode", "unknown")),
                    ict_trade_decision=bool(edge.should_trade),
                    final_decision="blocked",
                    block_reason_primary=block_reason_primary,
                    block_reason_secondary=block_reason_secondary,
                    pass_stage_reached=pass_stage_reached,
                    pd_state=str(getattr(edge, "pd_state", None) or None),
                    sweep_detected=False if block_reason_primary == "no_sweep" else None,
                    displacement_detected=False if block_reason_primary == "no_displacement" else None,
                    mss_detected=False if block_reason_primary == "no_mss" else None,
                    retrace_confirmed=False if block_reason_primary == "no_retrace_confirmation" else None,
                    fvg_detected=False if block_reason_primary == "no_fvg" else None,
                    position_already_open=False,
                    account_equity_source=str(account_resolution.source),
                    qty_requested=float(sizing_trace.qty_requested),
                    qty_final=float(sizing_trace.qty_final),
                    qty_fallback_used=bool(sizing_trace.fallback_used),
                    notes=setup_notes,
                    reason_detail=(
                        f"{str(edge.reason)}|pd={float(getattr(edge, 'pd', 0.0) or 0.0):.2f}"
                        f"|pd_state={str(getattr(edge, 'pd_state', 'unknown'))}"
                        f"|confluence={float(getattr(edge, 'confluence_score', 0.0) or 0.0):.2f}"
                        f"|sweep_override={bool(getattr(edge, 'sweep_override_used', False))}"
                        f"|mss_override={bool(getattr(edge, 'mss_override_used', False))}"
                        f"|imbalance_fallback={bool(getattr(edge, 'imbalance_fallback_used', False))}"
                    ),
                    pd_value=float(getattr(edge, "pd", 0.0) or 0.0),
                    confluence_score=float(getattr(edge, "confluence_score", 0.0) or 0.0),
                    sweep_override_used=bool(getattr(edge, "sweep_override_used", False)),
                    mss_override_used=bool(getattr(edge, "mss_override_used", False)),
                    imbalance_fallback_used=bool(getattr(edge, "imbalance_fallback_used", False)),
                )
            )
            if SETUP_AUDIT.setup_attempts_total % max(1, int(os.getenv("SETUP_AUDIT_SUMMARY_EVERY", "25") or 25)) == 0:
                logging.info(SETUP_AUDIT.summary_line())
            try:
                active_genome.trades += 1
                active_genome.weight = max(0.25, float(active_genome.weight) * 0.999)
            except Exception:
                pass
            continue

        entry_ok = True
        entry_side = str(edge.side or "LONG")
        ict_entry_confirmed = True
        signal_generated = True
        executed_entry_price = float(edge.entry)
        _register_entry_genome(str(args.symbol), active_genome.genome_id)
        ACTIVE_GENOME_CONTEXT[_symbol_key(str(args.symbol))] = active_genome.genome_id
        logging.info(
            "ENTRY V3 PASS | symbol=%s side=%s pd=%.2f pd_state=%s confluence=%.2f genome=%s",
            args.symbol,
            entry_side,
            float(getattr(edge, "pd", 0.0) or 0.0),
            str(getattr(edge, "pd_state", "unknown")),
            float(getattr(edge, "confluence_score", 0.0) or 0.0),
            getattr(edge, "genome", active_genome.genome_id),
        )
        try:
            active_genome.trades += 1
            active_genome.weight = min(5.0, float(active_genome.weight) * 1.01)
        except Exception:
            pass
        execution_score = float(signal_score)
        logging.info(
            "ENTRY CHECK | symbol=%s score=%.4f threshold=%.4f passed=%s",
            args.symbol,
            execution_score,
            execution_score_threshold,
            str(execution_score >= execution_score_threshold).lower(),
        )
        logging.info(
            "ENTRY SETUP GATE | delegated_to_ict_edge_v3=true signal=%.4f min_setup_cfg=%.4f",
            execution_score,
            MIN_SETUP_SCORE,
        )
        entry_allowed = True
        # execution assist only when ICT already confirmed the setup
        if ict_entry_confirmed and execution_score >= max(0.40, execution_score_threshold * 0.92):
            logging.warning(
                "ICT EXECUTION ASSIST | symbol=%s score=%.4f threshold=%.4f",
                args.symbol,
                float(execution_score),
                float(execution_score_threshold),
            )
            execution_priority_allowed = True
            signal_executable = True
        elif _env_bool("ALLOW_FORCE_ENTRY", "false") and signal_score > force_entry_threshold:
            logging.warning(
                "FORCE ENTRY ACTIVATED | signal=%.4f > %.4f",
                signal_score,
                force_entry_threshold,
            )
            entry_allowed = True
            execution_priority_allowed = True
            signal_generated = True
            signal_executable = True
        if _env_bool("ALLOW_FORCE_ENTRY", "false") and (not entry_allowed) and signal_score > 0.75:
            logging.warning("SMART FORCE ENTRY | high confidence override | signal=%.4f", signal_score)
            entry_allowed = True

        # =========================================
        # EVO PRIMARY ENTRY EXECUTION
        # =========================================
        try:
            symbol_key = str(args.symbol)
            EVO_ONLY_MODE = True

            truth = resolve_position_truth(
                symbol_key,
                active_trade=state.active_trade,
                broker_open_positions_count=locals().get("broker_open_positions_count"),
            )
            if truth.is_open:
                update_loop_status(
                    loop_status,
                    state="POSITION_ALREADY_OPEN",
                    reason="already_open",
                    authority="position_truth",
                    generated=False,
                    executable=False,
                    executed=False,
                    blocked=False,
                    setup_source=truth.source,
                )
                logging.info("EVO BLOCK | already in position | source=%s", truth.source)
                SETUP_AUDIT.append(
                    SetupAuditRecord(
                        timestamp=utc_now_iso(),
                        symbol=str(args.symbol),
                        timeframe="M1",
                        genome_id=getattr(edge, "genome", active_genome.genome_id),
                        genome_mode=genome_mode if genome_mode in {"weighted", "forced_seed"} else "other",
                        selected_by_ranker=True,
                        rank_score=None,
                        selection_score=float(signal_score),
                        signal_score_raw=float(top_signal),
                        signal_score_adjusted=float(signal_score),
                        threshold_value=float(execution_score_threshold),
                        threshold_passed=threshold_passed,
                        side=str(edge.side or "LONG"),
                        entry_mode=str(getattr(active_genome, "entry_mode", "unknown")),
                        ict_trade_decision=bool(edge.should_trade),
                        final_decision="blocked",
                        block_reason_primary="position_already_open",
                        block_reason_secondary=None,
                        pass_stage_reached="ict_passed",
                        pd_state=str(getattr(edge, "pd_state", None) or None),
                        sweep_detected=None,
                        displacement_detected=None,
                        mss_detected=None,
                        retrace_confirmed=True,
                        fvg_detected=None,
                        position_already_open=True,
                        account_equity_source=str(account_resolution.source),
                        qty_requested=float(sizing_trace.qty_requested),
                        qty_final=float(sizing_trace.qty_final),
                        qty_fallback_used=bool(sizing_trace.fallback_used),
                        notes=f"position_truth_source={truth.source}",
                        reason_detail="already_open",
                        pd_value=float(getattr(edge, "pd", 0.0) or 0.0),
                        confluence_score=float(getattr(edge, "confluence_score", 0.0) or 0.0),
                        sweep_override_used=bool(getattr(edge, "sweep_override_used", False)),
                        mss_override_used=bool(getattr(edge, "mss_override_used", False)),
                        imbalance_fallback_used=bool(getattr(edge, "imbalance_fallback_used", False)),
                    )
                )
                if SETUP_AUDIT.setup_attempts_total % max(1, int(os.getenv("SETUP_AUDIT_SUMMARY_EVERY", "25") or 25)) == 0:
                    logging.info(SETUP_AUDIT.summary_line())
                continue

            requested_qty = float(locals().get("live_position_qty", 0.0) or 0.0)
            executed_by_evo, evo_qty = execution_allowed(
                float(signal_score),
                float(execution_score_threshold),
                requested_qty,
            )
            sizing_trace = build_sizing_trace(
                qty_requested=requested_qty,
                qty_final=float(evo_qty),
                fallback_reason="min_lot_floor" if requested_qty <= 0.0 and float(evo_qty) >= float(EVO_EDGE_CONFIG.min_qty) else None,
            )
            logging.info(
                "SIZING TRACE | symbol=%s requested=%.4f final=%.4f fallback=%s reason=%s",
                args.symbol,
                float(sizing_trace.qty_requested),
                float(sizing_trace.qty_final),
                str(sizing_trace.fallback_used).lower(),
                str(sizing_trace.fallback_reason or "none"),
            )

            if not executed_by_evo:
                update_loop_status(
                    loop_status,
                    state="SETUP_BLOCKED",
                    reason="delegated_execution_failed",
                    authority="ict_v3_evo",
                    generated=True,
                    executable=False,
                    executed=False,
                    blocked=True,
                    block_stage="primary_engine_floor",
                )
                logging.info("EVO ENTRY BLOCK | execution floor")
                SETUP_AUDIT.append(
                    SetupAuditRecord(
                        timestamp=utc_now_iso(),
                        symbol=str(args.symbol),
                        timeframe="M1",
                        genome_id=getattr(edge, "genome", active_genome.genome_id),
                        genome_mode=genome_mode if genome_mode in {"weighted", "forced_seed"} else "other",
                        selected_by_ranker=True,
                        rank_score=None,
                        selection_score=float(signal_score),
                        signal_score_raw=float(top_signal),
                        signal_score_adjusted=float(signal_score),
                        threshold_value=float(execution_score_threshold),
                        threshold_passed=threshold_passed,
                        side=str(edge.side or "LONG"),
                        entry_mode=str(getattr(active_genome, "entry_mode", "unknown")),
                        ict_trade_decision=bool(edge.should_trade),
                        final_decision="blocked",
                        block_reason_primary="execution_block",
                        block_reason_secondary=None,
                        pass_stage_reached="execution_passed" if threshold_passed else "ict_passed",
                        pd_state=str(getattr(edge, "pd_state", None) or None),
                        sweep_detected=None,
                        displacement_detected=None,
                        mss_detected=None,
                        retrace_confirmed=True,
                        fvg_detected=None,
                        position_already_open=False,
                        account_equity_source=str(account_resolution.source),
                        qty_requested=float(sizing_trace.qty_requested),
                        qty_final=float(sizing_trace.qty_final),
                        qty_fallback_used=bool(sizing_trace.fallback_used),
                        notes=None,
                        reason_detail="execution_floor",
                        pd_value=float(getattr(edge, "pd", 0.0) or 0.0),
                        confluence_score=float(getattr(edge, "confluence_score", 0.0) or 0.0),
                        sweep_override_used=bool(getattr(edge, "sweep_override_used", False)),
                        mss_override_used=bool(getattr(edge, "mss_override_used", False)),
                        imbalance_fallback_used=bool(getattr(edge, "imbalance_fallback_used", False)),
                    )
                )
                if SETUP_AUDIT.setup_attempts_total % max(1, int(os.getenv("SETUP_AUDIT_SUMMARY_EVERY", "25") or 25)) == 0:
                    logging.info(SETUP_AUDIT.summary_line())
                continue

            execution_soft_penalties = tuple(
                getattr(locals().get("execution_decision", None), "soft_penalties", tuple()) or tuple()
            )
            force_mode_active = effective_force_mode()
            force_execution_active = effective_force_execution()
            force_any_active = bool(force_mode_active or force_execution_active)
            discovery_gate = evaluate_discovery_gates(
                config=DISCOVERY_MODE_CONFIG,
                ict_confidence=float(getattr(edge, "confidence", 0.0) or 0.0),
                entry_quality=float(locals().get("entry_quality_safe", locals().get("entry_quality", 0.0)) or 0.0),
                signal_score=float(signal_score),
                threshold_required=float(execution_score_threshold),
                force_path_requested=force_any_active,
                override_path_requested=bool("profit_override_applied" in execution_soft_penalties),
            )
            if not discovery_gate.allowed:
                logging.warning(
                    "DISCOVERY STRICT BLOCK | symbol=%s reasons=%s signal=%.4f threshold=%.4f",
                    symbol_key,
                    ",".join(discovery_gate.block_reasons),
                    float(signal_score),
                    float(execution_score_threshold),
                )
                continue

            pos = open_position(
                symbol_key,
                str(edge.side or "LONG"),
                float(edge.entry),
                float(edge.stop),
                float(edge.tp),
                float(evo_qty),
                time.time(),
            )

            entry_price = float(pos.entry_price)
            stop_price = float(pos.initial_stop_price or pos.stop_price)
            risk_dist = max(abs(entry_price - stop_price), 1e-9)
            tp_price = float(pos.runner_tp_price or pos.take_profit_price)

            tp_pct = abs(tp_price - entry_price) / max(entry_price, 1e-9)
            sl_pct = abs(entry_price - stop_price) / max(entry_price, 1e-9)
            trailing_activation_pct = max(
                float(os.getenv("EVO_TRAILING_ACTIVATION_PCT", "0.0010") or 0.0010),
                sl_pct,
            )
            trailing_offset_pct = max(
                float(os.getenv("EVO_TRAILING_OFFSET_PCT", "0.0005") or 0.0005),
                sl_pct * 0.50,
            )
            max_hold_seconds = float(os.getenv("EVO_MAX_HOLD_SECONDS", "180") or 180.0)

            trade = ActiveTrade(
                instrument=symbol_key,
                side=str(pos.side),
                entry_price=float(entry_price),
                entry_time=loop_now,
                entry_index=latest_index,
                signal_score=float(signal_score),
                tp_pct=float(tp_pct),
                sl_pct=float(sl_pct),
                trailing_activation_pct=float(trailing_activation_pct),
                trailing_offset_pct=float(trailing_offset_pct),
                max_hold_seconds=float(max_hold_seconds),
                source="evo_primary",
            )
            trade.stop_price = float(stop_price)
            trade.take_profit_price = float(tp_price)
            trade.sl_price = float(stop_price)
            trade.tp_price = float(tp_price)
            trade.qty = float(pos.qty)
            trade.initial_qty = float(pos.initial_qty)
            trade.remaining_qty = float(pos.qty)
            trade.partial_tp_price = float(pos.partial_tp_price)
            trade.runner_tp_price = float(pos.runner_tp_price)
            trade.initial_risk_per_unit = float(pos.initial_risk_per_unit)
            trade.setup_type = "ict_v3_evo_primary"
            trade.exit_profile = str(os.getenv("FX_EXIT_PROFILE", getattr(trade, "exit_profile", "partial_runner")) or "partial_runner").strip().lower()
            trade.exit_tier = "medium_quality" if float(signal_score) >= 0.9 else "weak_quality"
            trade.position_scale = float(locals().get("position_scale", 1.0) or 1.0)
            trade.context = {
                "symbol": symbol_key,
                "entry_engine": "evo_primary",
                "ict_reason": str(edge.reason),
                "ict_confidence": float(getattr(edge, "confidence", 0.0) or 0.0),
                "ict_genome": str(getattr(edge, "genome", active_genome.genome_id)),
                "ict_side": str(edge.side),
                "rr_mode": "v2_partial_runner",
                "threshold_required": float(execution_score_threshold),
                "signal_delta": float(signal_score - execution_score_threshold),
                "entry_quality": float(locals().get("entry_quality_safe", locals().get("entry_quality", 0.0)) or 0.0),
                "override_used": bool("profit_override_applied" in execution_soft_penalties),
                "force_enabled": force_any_active,
            }

            active_trade = trade
            state.active_trade = trade
            active_trade.source = "evo_primary"
            logging.info("EVO SYNC | state.active_trade set")
            signal_executable = True
            signal_generated = True

            logging.info(
                "TRADE EXECUTED | symbol=%s score=%.4f setup=%s",
                symbol_key,
                float(signal_score),
                "ict_v3_evo_primary",
            )
            logging.info(
                "EXECUTION AUTHORITY | symbol=%s authority=ict_v3_evo",
                symbol_key,
            )
            if force_any_active:
                logging.error(
                    "FORCE AUTHORITY VIOLATION | symbol=%s force_mode=%s force_execution=%s",
                    symbol_key,
                    force_mode_active,
                    force_execution_active,
                )
            update_loop_status(
                loop_status,
                state="EXECUTED",
                reason="executed_by_primary_engine",
                authority="ict_v3_evo",
                generated=True,
                executable=True,
                executed=True,
                blocked=False,
                setup_source="ict_v3_evo_primary",
            )
            logging.info(
                "OPEN POSITION | symbol=%s side=%s entry=%.5f stop=%.5f tp=%.5f",
                symbol_key,
                trade.side,
                float(trade.entry_price),
                float(trade.stop_price),
                float(trade.take_profit_price),
            )
            SETUP_AUDIT.append(
                SetupAuditRecord(
                    timestamp=utc_now_iso(),
                    symbol=str(args.symbol),
                    timeframe="M1",
                    genome_id=getattr(edge, "genome", active_genome.genome_id),
                    genome_mode=genome_mode if genome_mode in {"weighted", "forced_seed"} else "other",
                    selected_by_ranker=True,
                    rank_score=None,
                    selection_score=float(signal_score),
                    signal_score_raw=float(top_signal),
                    signal_score_adjusted=float(signal_score),
                    threshold_value=float(execution_score_threshold),
                    threshold_passed=threshold_passed,
                    side=str(edge.side or "LONG"),
                    entry_mode=str(getattr(active_genome, "entry_mode", "unknown")),
                    ict_trade_decision=bool(edge.should_trade),
                    final_decision="executed",
                    block_reason_primary=None,
                    block_reason_secondary=None,
                    pass_stage_reached="executed",
                    pd_state=str(getattr(edge, "pd_state", None) or None),
                    sweep_detected=True,
                    displacement_detected=True,
                    mss_detected=True,
                    retrace_confirmed=True,
                    fvg_detected=True,
                    position_already_open=False,
                    account_equity_source=str(account_resolution.source),
                    qty_requested=float(sizing_trace.qty_requested),
                    qty_final=float(pos.qty),
                    qty_fallback_used=bool(sizing_trace.fallback_used),
                    notes="executed_by_primary_engine",
                    reason_detail=str(edge.reason),
                    pd_value=float(getattr(edge, "pd", 0.0) or 0.0),
                    confluence_score=float(getattr(edge, "confluence_score", 0.0) or 0.0),
                    sweep_override_used=bool(getattr(edge, "sweep_override_used", False)),
                    mss_override_used=bool(getattr(edge, "mss_override_used", False)),
                    imbalance_fallback_used=bool(getattr(edge, "imbalance_fallback_used", False)),
                )
            )
            if SETUP_AUDIT.setup_attempts_total % max(1, int(os.getenv("SETUP_AUDIT_SUMMARY_EVERY", "25") or 25)) == 0:
                logging.info(SETUP_AUDIT.summary_line())
            if effective_force_any():
                logging.error(
                    "FORCE AUTHORITY VIOLATION | loop reached forbidden force state | symbol=%s",
                    symbol_key,
                )
        except Exception as evo_entry_exc:
            try:
                STATE.clear(str(args.symbol))
            except Exception:
                pass
            active_trade = None
            state.active_trade = None
            logging.exception("EVO PRIMARY ENTRY ERROR | %s", evo_entry_exc)
            continue

        # =========================================
        # HARD DISABLE LEGACY EXIT OWNERSHIP
        # =========================================
        if getattr(active_trade, "source", "") == "evo_primary":
            LEGACY_EXIT_ENGINE_DISABLED = True
            logging.info("LEGACY EXIT SKIPPED | evo owns exit")
        else:
            LEGACY_EXIT_ENGINE_DISABLED = False

        try:
            _save_evo_edge_state()
        except Exception:
            pass
        try:
            _log_evo_edge_summary(str(args.symbol))
        except Exception:
            pass
        if loop_status.executed:
            state.no_trade_loops = 0
            logging.info(
                "POST-ENTRY RECONCILIATION | symbol=%s state=%s reason=%s authority=%s",
                loop_status.symbol,
                loop_status.final_state,
                loop_status.final_reason,
                loop_status.authority,
            )
            _sleep()
            continue
        if execution_score < execution_score_threshold:
            logging.warning(
                "ENTRY BLOCKED | symbol=%s reason=threshold_not_met score=%.4f threshold=%.4f",
                args.symbol,
                execution_score,
                execution_score_threshold,
            )
        regime = str(getattr(latest_filter, "current_market_regime", "NORMAL")).upper()
        is_fx = is_fx_symbol(args.symbol)
        MIN_SIGNAL = 0.08
        if is_fx:
            MIN_SIGNAL = 0.05
        if is_fx:
            logging.info("FX MODE ACTIVE | symbol=%s regime=%s", args.symbol, regime)

        if is_fx and regime == "LOW_VOL":
            signal_score *= 1.3
            logging.info(
                "FX SIGNAL BOOST | symbol=%s boosted=%.4f",
                args.symbol,
                signal_score,
            )

        # =========================================
        # 🔥 FINAL CLEANUP (ANTI GHOST)
        # =========================================
        try:
            symbol_key = str(args.symbol)
            truth = resolve_position_truth(
                symbol_key,
                active_trade=state.active_trade,
                broker_open_positions_count=locals().get("broker_open_positions_count"),
            )
            if not truth.is_open:
                if active_trade is not None:
                    logging.info("EVO SYNC | cleanup orphan trade")
                active_trade = None
                state.active_trade = None
        except Exception:
            pass

        if is_fx:
            if regime == "LOW_VOL":
                execution_score_threshold *= 0.55
            elif regime == "NORMAL":
                execution_score_threshold *= 0.75
            logging.info(
                "FX THRESHOLD ADJUST | symbol=%s new_threshold=%.4f",
                args.symbol,
                execution_score_threshold,
            )

        minimum_signal_floor = max(RANK_MIN_SIGNAL_FOR_EXECUTION, min(execution_score_threshold, execution_priority_required))
        dynamic_signal_floor = get_dynamic_signal_floor(
            runtime_context.get("recent_signal_scores", []),
            fallback_floor=RANK_MIN_SIGNAL_FOR_EXECUTION,
        )
        dynamic_signal_floor = min(dynamic_signal_floor, execution_score_threshold * 0.95)

        if is_fx:
            if regime == "LOW_VOL":
                dynamic_signal_floor = min(dynamic_signal_floor, execution_score_threshold * 0.90)
            else:
                dynamic_signal_floor = min(dynamic_signal_floor, execution_score_threshold * 0.95)

        dynamic_signal_floor = max(
            RANK_MIN_SIGNAL_FOR_EXECUTION,
            float(dynamic_signal_floor),
        )
        logging.info(
            "FX DYNAMIC FLOOR FIX | symbol=%s floor=%.4f threshold=%.4f",
            args.symbol,
            dynamic_signal_floor,
            execution_score_threshold,
        )
        bypass_dynamic_floor = bool(signal_score >= SIGNAL_PRIORITY_BYPASS_THRESHOLD)
        quality_floor = 0.5
        if is_fx:
            if regime == "LOW_VOL":
                quality_floor = 0.35
            elif regime == "NORMAL":
                quality_floor = 0.45
            logging.info(
                "FX QUALITY ADJUST | symbol=%s quality_floor=%.4f",
                args.symbol,
                quality_floor,
            )

        smart_activation_triggered = False

        if is_fx:
            low_signal_band = 0.02 <= signal_score <= 0.20
            prolonged_inactivity = getattr(state, "no_trade_loops", 0) > 40
            acceptable_quality = quality_score >= (quality_floor * 0.85)
            safe_regime = regime in ("LOW_VOL", "NORMAL")

            if low_signal_band and prolonged_inactivity and acceptable_quality and safe_regime:
                old_threshold = execution_score_threshold
                old_quality_floor = quality_floor

                execution_score_threshold *= 0.80
                quality_floor *= 0.90

                smart_activation_triggered = True

                logging.warning(
                    "SMART ACTIVATION V2 | symbol=%s signal=%.4f quality=%.4f threshold=%.4f→%.4f quality_floor=%.4f→%.4f loops=%d",
                    args.symbol,
                    signal_score,
                    quality_score,
                    old_threshold,
                    execution_score_threshold,
                    old_quality_floor,
                    quality_floor,
                    int(getattr(state, "no_trade_loops", 0)),
                )

        if smart_activation_triggered:
            if signal_score >= execution_score_threshold * 0.75:
                bypass_dynamic_floor = True
                logging.warning(
                    "SMART ACTIVATION ENTRY ASSIST | symbol=%s score=%.4f threshold=%.4f",
                    args.symbol,
                    signal_score,
                    execution_score_threshold,
                )

        if smart_activation_triggered and signal_score < 0.02:
            smart_activation_triggered = False
            logging.warning(
                "SMART ACTIVATION CANCELLED | signal too low | symbol=%s score=%.4f",
                args.symbol,
                signal_score,
            )

        if smart_activation_triggered:
            logging.info(
                "SMART ACTIVATION ACTIVE | symbol=%s regime=%s signal=%.4f quality=%.4f",
                args.symbol,
                regime,
                signal_score,
                quality_score,
            )

        minimum_signal_floor = min(minimum_signal_floor, execution_score_threshold * 0.9)
        dynamic_signal_floor = min(dynamic_signal_floor, execution_score_threshold * 1.1)
        if is_fx and signal_score < 0.05:
            signal_score = 0.05
            logging.warning(
                "FX SIGNAL FLOOR CLAMP | symbol=%s adjusted=%.4f",
                args.symbol,
                signal_score,
            )
        activity_mode_active, execution_score_threshold, dynamic_signal_floor, quality_floor, activity_size_multiplier, activity_reason = compute_activity_mode_adjustment(
            no_trade_loops=int(getattr(state, "no_trade_loops", 0)),
            base_execution_threshold=float(execution_score_threshold),
            base_dynamic_floor=float(dynamic_signal_floor),
            base_quality_floor=float(quality_floor),
            regime=regime,
        )
        # === SAFE INIT PATCH (DO NOT MODIFY) ===
        try:
            cooldown_remaining = float(
                adaptive_filters._cooldown_remaining(
                    risk_state,
                    risk_cfg,
                    loop_now,
                    symbol=args.symbol,
                )
            )
        except Exception:
            cooldown_remaining = 0.0

        cooldown_remaining = max(0.0, cooldown_remaining)

        # ensure min_time_remaining always exists
        min_time_remaining = float(locals().get("min_time_remaining", 0.0))
        min_time_remaining = max(0.0, min_time_remaining)

        no_cooldown_or_lock_active = (
            float(cooldown_remaining) <= 0.0
            and not getattr(risk_state, "daily_loss_stop_triggered", False)
            and not getattr(risk_state, "trading_halted", False)
            and not getattr(risk_state, "max_loss_lock_active", False)
        )
        activity_mode_active = bool(
            activity_mode_active
            and broker_flat
            and active_trade is None
            and int(risk_state.open_positions) == 0
            and no_cooldown_or_lock_active
        )
        if activity_mode_active:
            position_scale *= activity_size_multiplier
            logging.warning(
                "ACTIVITY MODE V2 | symbol=%s loops=%d reason=%s threshold=%.4f dyn_floor=%.4f quality_floor=%.4f size=%.2f",
                args.symbol,
                int(getattr(state, "no_trade_loops", 0)),
                activity_reason,
                execution_score_threshold,
                dynamic_signal_floor,
                quality_floor,
                activity_size_multiplier,
            )

        if is_fx and regime == "LOW_VOL":
            if signal_score >= (execution_score_threshold * 0.8):
                bypass_dynamic_floor = True
                logging.warning(
                    "FX ENTRY ASSIST | symbol=%s score=%.4f threshold=%.4f",
                    args.symbol,
                    signal_score,
                    execution_score_threshold,
                )

        if (
            activity_mode_active
            and broker_flat
            and active_trade is None
            and int(risk_state.open_positions) == 0
            and not signal_decay_blocked
            and signal_score >= execution_score_threshold
            and quality_score >= quality_floor
        ):
            bypass_dynamic_floor = True
            logging.warning(
                "ACTIVITY ENTRY ASSIST | symbol=%s score=%.4f quality=%.4f",
                args.symbol,
                signal_score,
                quality_score,
            )
        if activity_mode_active and signal_score >= execution_score_threshold * 0.9:
            bypass_dynamic_floor = True
            logging.warning(
                "ACTIVITY BOOST ENTRY | symbol=%s score=%.4f threshold=%.4f",
                args.symbol,
                signal_score,
                execution_score_threshold,
            )

        final_execution_allowed, final_execution_reason = compute_final_execution_decision(
            signal_score=signal_score,
            signal_executable=signal_executable,
            execution_threshold=execution_score_threshold,
            dynamic_signal_floor=dynamic_signal_floor,
            quality_score=quality_score,
            quality_floor=quality_floor,
            signal_decay_blocked=signal_decay_blocked,
            bypass_dynamic_floor=bypass_dynamic_floor,
        )
        if final_execution_allowed:
            log_stats["trades"] += 1
        else:
            log_stats["blocked"] += 1
        if log_stats["signals"] > 50 and log_stats["trades"] == 0:
            logging.warning("NO TRADES IN LAST WINDOW | possible over-filtering")
        if time.time() - log_stats["last_print"] > 30:
            avg_signal = (
                sum(log_stats["signal_values"]) / len(log_stats["signal_values"])
                if log_stats["signal_values"]
                else 0.0
            )
            top_symbol = None
            if log_stats["last_symbols"]:
                try:
                    top_symbol = max(
                        set(log_stats["last_symbols"]),
                        key=log_stats["last_symbols"].count,
                    )
                except Exception:
                    top_symbol = "unknown"
            logging.info(
                "===== BOT SNAPSHOT ===== signals=%d trades=%d blocked=%d avg_signal=%.4f top_symbol=%s",
                int(log_stats["signals"]),
                int(log_stats["trades"]),
                int(log_stats["blocked"]),
                float(avg_signal),
                str(top_symbol),
            )
            # reset counters (rolling window)
            log_stats["signals"] = 0
            log_stats["trades"] = 0
            log_stats["blocked"] = 0
            log_stats["signal_values"].clear()
            log_stats["last_symbols"].clear()
            log_stats["last_print"] = time.time()
        if activity_mode_active and final_execution_allowed:

            if quality_score < quality_floor:
                final_execution_allowed = False
                final_execution_reason = "activity_quality_fail"

            elif signal_score < MIN_SIGNAL:
                final_execution_allowed = False
                final_execution_reason = "activity_signal_too_low"

            elif predicted_pnl < min_expected_profit and signal_score < 0.20:
                final_execution_allowed = False
                final_execution_reason = "activity_weak_edge"

            elif latest_filter.relative_volume <= 0:
                final_execution_allowed = False
                final_execution_reason = "activity_volume_fail"

            elif latest_filter.spread_ratio > adaptive_spread * 1.25:
                final_execution_allowed = False
                final_execution_reason = "activity_spread_fail"

            if not final_execution_allowed:
                logging.warning(
                    "ACTIVITY FILTER BLOCK | symbol=%s reason=%s",
                    args.symbol,
                    final_execution_reason,
                )

        if final_execution_allowed and not signal_generated:
            signal_generated = True

        if final_execution_allowed and not signal_executable:
            signal_executable = True

        if final_execution_allowed:
            logging.warning(
                "ACTIVITY MODE SYNC | symbol=%s generated=true executable=true allowed=true",
                args.symbol,
            )

        logging.info(
            "POST-FINAL SYNC | symbol=%s generated=%s executable=%s allowed=%s reason=%s",
            args.symbol,
            str(signal_generated).lower(),
            str(signal_executable).lower(),
            str(final_execution_allowed).lower(),
            final_execution_reason,
        )
        logging.info(
            "FINAL EXEC CHECK | symbol=%s score=%.4f allowed=%s reason=%s",
            args.symbol,
            float(signal_score),
            str(bool(final_execution_allowed)).lower(),
            final_execution_reason,
        )
        logging.info(
            "FINAL DECISION | symbol=%s score=%.4f quality=%.4f allowed=%s reason=%s",
            args.symbol,
            float(signal_score),
            float(quality_score),
            str(bool(final_execution_allowed)).lower(),
            final_execution_reason,
        )

        if final_execution_allowed:
            signal_executable = True
        logging.info(
            "CENTRAL EXECUTION | symbol=%s allowed=%s reason=%s signal=%.4f",
            args.symbol,
            str(final_execution_allowed).lower(),
            final_execution_reason,
            signal_score,
        )
        executed = final_execution_allowed
        logging.info(
            "EXEC V2 | score=%.4f dyn_floor=%.4f executed=%s reason=%s",
            signal_score,
            dynamic_signal_floor,
            str(executed).lower(),
            final_execution_reason,
        )
        logging.info(
            "DYNAMIC SIGNAL FLOOR | symbol=%s score=%.4f required=%.4f bypass=%s",
            args.symbol,
            float(signal_score),
            float(dynamic_signal_floor),
            str(bypass_dynamic_floor).lower(),
        )

        # Make current symbol visible to ict_entry_v2 without changing all call sites
        os.environ["CURRENT_SYMBOL"] = str(args.symbol).upper()

        # Optional symbol allowlist
        allow_symbols_raw = str(os.getenv("ALLOW_SYMBOLS", "") or "").strip()
        if allow_symbols_raw:
            allow_symbols = {s.strip().upper() for s in allow_symbols_raw.split(",") if s.strip()}
            if str(args.symbol).upper() not in allow_symbols:
                logging.warning("SYMBOL BLOCKED | symbol=%s reason=not_in_allowlist", args.symbol)
                _sleep()
                continue

        # Strong spread / fill-risk protection
        max_fill_risk_score = get_max_fill_risk(args.symbol)
        max_spread_ratio_xau = float(os.getenv("MAX_SPREAD_RATIO_XAU", "0.000030") or 0.000030)
        current_spread_ratio = 0.0
        with suppress(Exception):
            if float(arr["close"][-1]) > 0:
                current_spread_ratio = float(spread) / float(arr["close"][-1])

        # --- SAFE FILL RISK RESOLVE ---
        fill_risk_score_safe = 0.0

        try:
            fill_risk_score_safe = float(fill_risk_score)
        except Exception:
            try:
                # fallback: calculate basic proxy
                if float(arr["close"][-1]) > 0:
                    spread_ratio = float(spread) / float(arr["close"][-1])
                    fill_risk_score_safe = spread_ratio * 1_000_000
                else:
                    fill_risk_score_safe = 0.0
            except Exception:
                fill_risk_score_safe = 0.0

        logging.info(
            "FILL RISK SAFE | symbol=%s score=%.2f max=%.2f",
            args.symbol,
            fill_risk_score_safe,
            max_fill_risk_score,
        )

        if float(fill_risk_score_safe) > max_fill_risk_score:
            soft_cap = float(max_fill_risk_score) * 1.5
            if float(fill_risk_score_safe) < soft_cap:
                logging.warning(
                    "FILL RISK SOFT PASS | symbol=%s score=%.2f threshold=%.2f soft_cap=%.2f",
                    args.symbol,
                    float(fill_risk_score_safe),
                    float(max_fill_risk_score),
                    float(soft_cap),
                )
            else:
                logging.warning(
                    "HARD ENTRY FILTER | symbol=%s blocked=true reason=fill_risk score=%.2f max=%.2f",
                    args.symbol,
                    float(fill_risk_score_safe),
                    float(max_fill_risk_score),
                )
                signal_executable = False
                final_execution_allowed = False
        logging.info(
            "FILL RISK CHECK | symbol=%s score=%.2f threshold=%.2f",
            args.symbol,
            float(fill_risk_score_safe),
            float(max_fill_risk_score),
        )

        # ===============================
        # SAFE ENTRY QUALITY (CRASH FIX)
        # ===============================
        entry_quality_safe = 0.0
        try:
            entry_quality_safe = float(entry_quality)
        except Exception:
            try:
                entry_quality_safe = float(getattr(latest_filter, "entry_quality", 0.0))
            except Exception:
                entry_quality_safe = 0.0

        if entry_quality_safe <= 0.0:
            try:
                score_component = max(0.0, min(1.5, float(signal_score)))
            except Exception:
                score_component = 0.0

            try:
                spread_component = 1.0
                if float(current_spread_ratio) > 0:
                    spread_component = max(
                        0.0,
                        min(1.0, 1.0 - (float(current_spread_ratio) / max(max_spread_ratio_xau, 1e-12))),
                    )
            except Exception:
                spread_component = 0.5

            entry_quality_safe = (score_component * 0.7) + (spread_component * 0.3)

        logging.info(
            "ENTRY QUALITY SAFE | symbol=%s quality=%.4f",
            args.symbol,
            float(entry_quality_safe),
        )

        # =========================================
        # 🔥 ENTRY CONTEXT FOR ICT MODULE
        # =========================================
        os.environ["CURRENT_SYMBOL"] = str(args.symbol).upper()
        os.environ["CURRENT_SIGNAL_SCORE"] = str(float(signal_score))
        os.environ["CURRENT_ENTRY_QUALITY"] = str(float(entry_quality_safe))
        os.environ["CURRENT_VOLATILITY"] = str(float(realized_volatility if 'realized_volatility' in locals() else 0.0))

        # =========================================
        # 🔥 PROFIT ENGINE (SOFT FILTER)
        # =========================================
        pe = ProfitEngineV2()
        allow_pe, pe_reason = pe.allow_entry(float(signal_score), float(entry_quality_safe))

        logging.info(
            "DEBUG ENTRY CHECK | symbol=%s signal=%.4f quality=%.4f pe_allowed=%s pe_reason=%s",
            args.symbol,
            float(signal_score),
            float(entry_quality_safe),
            str(allow_pe).lower(),
            pe_reason,
        )

        if not allow_pe:
            if FORCE_MODE:
                logging.warning("FORCE MODE → overriding PE filter")
            else:
                logging.warning(
                    "PE SOFT BLOCK | symbol=%s reason=%s signal=%.4f quality=%.4f",
                    args.symbol,
                    pe_reason,
                    float(signal_score),
                    float(entry_quality_safe),
                )
                # 🔥 STRONGER PENALTY (maar niet killen)
                signal_score *= 0.75

        base_risk_per_trade_usd = _safe_float(os.getenv("RISK_PER_TRADE_USD", "0.15"), 0.15)
        base_cooldown_seconds = _safe_float(os.getenv("COOLDOWN_SECONDS", "90"), 90.0)

        if _evo2_enabled():
            old_signal_score = float(signal_score)
            old_entry_quality = float(entry_quality_safe)
            old_execution_threshold = float(execution_score_threshold)
            old_risk = float(base_risk_per_trade_usd)
            old_cooldown = float(base_cooldown_seconds)
            old_priority = float(selected_score)
            evo_adj = apply_evo_adjustments(
                state=evo_engine_state,
                symbol=args.symbol,
                signal_score=float(signal_score),
                entry_quality=float(entry_quality_safe),
                execution_threshold=float(execution_score_threshold),
                risk_per_trade_usd=base_risk_per_trade_usd,
                cooldown_seconds=base_cooldown_seconds,
                priority_score=float(selected_score),
            )
            signal_score = float(evo_adj["signal_score"])
            entry_quality_safe = float(evo_adj["entry_quality"])
            # =========================================
            # 🔥 EVO → PE SYNC
            # =========================================
            try:
                pe.min_signal = min(pe.min_signal, float(signal_score) * 0.95)
            except Exception:
                pass
            execution_score_threshold = float(evo_adj["execution_threshold"])
            base_risk_per_trade_usd = float(evo_adj["risk_per_trade_usd"])
            base_cooldown_seconds = float(evo_adj["cooldown_seconds"])
            selected_score = float(evo_adj["priority_score"])
            log_stats["evo_updates"] = int(log_stats.get("evo_updates", 0)) + 1
            log_stats["evo_last_symbol"] = str(args.symbol).upper()
            _maybe_log_evo2_apply(
                symbol=str(args.symbol).upper(),
                loop_count=int(global_loop_count),
                old_signal=old_signal_score,
                new_signal=float(signal_score),
                old_quality=old_entry_quality,
                new_quality=float(entry_quality_safe),
                old_exec=old_execution_threshold,
                new_exec=float(execution_score_threshold),
                old_risk=old_risk,
                new_risk=float(base_risk_per_trade_usd),
                old_cooldown=old_cooldown,
                new_cooldown=float(base_cooldown_seconds),
                old_priority=old_priority,
                new_priority=float(selected_score),
            )
            evo_symbol = evo_get_symbol_state(evo_engine_state, args.symbol)
            logging.info(
                "EVO2 STATE | symbol=%s trades=%d win_rate=%.3f avg_pnl=%.5f streak_w=%d streak_l=%d",
                str(args.symbol).upper(),
                int(evo_symbol.get("trades", 0)),
                float(evo_symbol.get("win_rate", 0.0)),
                float(evo_symbol.get("avg_pnl", 0.0)),
                int(evo_symbol.get("win_streak", 0)),
                int(evo_symbol.get("loss_streak", 0)),
            )
        else:
            logging.info("EVO2 | disabled")

        # =========================================
        # 🔥 EVO SOFT CLAMP
        # =========================================
        evo_signal_req = min(_safe_float(locals().get("evo_signal_req", 0.5), 0.5), 0.42)
        evo_quality_req = min(_safe_float(locals().get("evo_quality_req", 0.5), 0.5), 0.45)
        logging.info(
            "EVO CLAMP | signal=%.4f quality=%.4f",
            float(evo_signal_req),
            float(evo_quality_req),
        )

        # keep a sane floor/cap, but do not nuke the FX threshold path
        execution_score_threshold = max(
            float(os.getenv("MIN_EXECUTION_THRESHOLD", "0.20") or 0.20),
            min(float(execution_score_threshold), float(os.getenv("MAX_EXECUTION_THRESHOLD", "0.42") or 0.42))
        )
        logging.info(
            "DEBUG THRESHOLD | symbol=%s execution_threshold=%.4f signal=%.4f quality=%.4f",
            args.symbol,
            float(execution_score_threshold),
            float(signal_score),
            float(entry_quality_safe),
        )

        try:
            entry_quality = max(float(entry_quality_safe), 0.01)
        except Exception:
            entry_quality = 0.01
            logging.warning(
                "ENTRY QUALITY FALLBACK | symbol=%s forced=0.01 raw=%s",
                args.symbol,
                str(entry_quality_safe),
            )

        # =========================================
        # 🔥 ENTRY QUALITY TRACE (DEBUG GOLD)
        # =========================================
        logging.info(
            "ENTRY QUALITY FINAL | symbol=%s safe=%.4f final=%.4f",
            args.symbol,
            float(entry_quality_safe),
            float(entry_quality),
        )

        # =========================================
        # 🔥 HARD ENTRY FILTER (SPREAD)
        # =========================================
        if str(args.symbol).upper() == "XAUUSD" and current_spread_ratio > max_spread_ratio_xau:
            logging.warning(
                "HARD ENTRY FILTER | symbol=%s blocked=true reason=spread_ratio ratio=%.8f max=%.8f",
                args.symbol, float(current_spread_ratio), float(max_spread_ratio_xau),
            )
            signal_executable = False
            final_execution_allowed = False
            _evo2_register_block(evo_engine_state, args.symbol, "spread_ratio")

        # =========================================
        # 🔥 ADAPTIVE HARD GATE
        # =========================================
        regime = str(os.getenv("MARKET_REGIME", "NORMAL") or "NORMAL").upper()
        no_trades_mode = _env_bool("NO_TRADES_MODE", "true")

        base_min_signal = float(
            os.getenv("HARD_MIN_SIGNAL", os.getenv("MIN_SIGNAL_HARD", "0.20")) or 0.20
        )
        base_min_quality = float(
            os.getenv("HARD_MIN_QUALITY", os.getenv("MIN_QUALITY_HARD", "0.25")) or 0.25
        )

        # 🔥 HARD FLOOR FIX (anders blijft hij blokkeren)
        base_min_signal = min(base_min_signal, 0.25)
        base_min_quality = min(base_min_quality, 0.30)

        logging.info(
            "DEBUG HARD FILTER | symbol=%s min_signal=%.4f min_quality=%.4f signal=%.4f quality=%.4f",
            args.symbol,
            float(base_min_signal),
            float(base_min_quality),
            float(signal_score),
            float(entry_quality_safe),
        )

        relax_factor = 1.0
        if no_trades_mode:
            relax_factor *= 0.75
        if regime in {"FX", "FOREX"}:
            relax_factor *= 0.85
        if float(entry_quality_safe) > 0.75:
            relax_factor *= 0.85
        if float(signal_score) > 0.6:
            relax_factor *= 0.8

        min_signal_hard = base_min_signal * relax_factor
        min_quality_hard = base_min_quality * relax_factor
        # 🔥 CRITICAL FIX: FX scale
        abs_min_signal_hard = _safe_float(os.getenv("ABS_MIN_SIGNAL_HARD", "0.12"), 0.12)
        abs_min_quality_hard = _safe_float(os.getenv("ABS_MIN_QUALITY_HARD", "0.15"), 0.15)

        min_signal_hard = max(abs_min_signal_hard, float(min_signal_hard))
        min_quality_hard = max(abs_min_quality_hard, float(min_quality_hard))

        logging.info("DEBUG ABS FLOOR | signal_floor=%.4f quality_floor=%.4f", abs_min_signal_hard, abs_min_quality_hard)
        if evo_threshold_enabled and evo_threshold is not None:
            min_signal_hard, min_quality_hard = evo_threshold.adjust_thresholds(
                min_signal_hard,
                min_quality_hard,
            )
        hard_filter_passed = (
            float(signal_score) >= float(min_signal_hard)
            and float(entry_quality_safe) >= float(min_quality_hard)
        )

        # ===============================
        # 🔥 FIX 3 — NO TRADES MODE OVERRIDE
        # ===============================
        no_trades_override = str(os.getenv("NO_TRADES_MODE", "true")).lower() in ("1", "true", "yes")
        if not hard_filter_passed and no_trades_override:
            logging.warning(
                "NO TRADE MODE OVERRIDE | forcing pass | symbol=%s",
                args.symbol,
            )
            hard_filter_passed = True

        logging.warning(
            "HARD FILTER ADAPTIVE | symbol=%s passed=%s signal=%.4f req_signal=%.4f quality=%.4f req_quality=%.4f relax=%.3f regime=%s no_trades=%s",
            args.symbol,
            str(hard_filter_passed).lower(),
            float(signal_score),
            float(min_signal_hard),
            float(entry_quality_safe),
            float(min_quality_hard),
            float(relax_factor),
            regime,
            str(no_trades_mode).lower(),
        )
        # ===============================
        # 🔥 FIX 1 — PREVENT DOUBLE BLOCK
        # ===============================
        if not hard_filter_passed:
            if final_execution_allowed:
                logging.warning(
                    "HARD FILTER BYPASSED | execution already approved | symbol=%s",
                    args.symbol,
                )
            else:
                signal_executable = False
                final_execution_allowed = False
                _evo2_register_block(evo_engine_state, args.symbol, "hard_filter_failed")

        # Symbol-specific hard filters
        symbol_upper = str(args.symbol).upper()
        # 🔥 XAU ADAPTIVE FILTER (EVO-COMPATIBLE)
        if symbol_upper == "XAUUSD":
            base_xau_signal = float(os.getenv("XAU_MIN_SIGNAL", "0.75") or 0.75)
            base_xau_quality = float(os.getenv("XAU_MIN_QUALITY", "0.70") or 0.70)

            # EVO relaxation
            evo_signal_delta = float(evo_adj.get("signal_delta", 0.0))
            evo_quality_delta = float(evo_adj.get("quality_delta", 0.0))

            # No-trade mode → extra relax
            no_trades_mode = _env_bool("NO_TRADES_MODE", "true")
            relax_factor = 1.0

            if no_trades_mode:
                relax_factor *= 0.7

            if float(signal_score) > 0.5:
                relax_factor *= 0.85

            if float(entry_quality_safe) > 0.7:
                relax_factor *= 0.85

            xau_min_signal = max(
                float(os.getenv("ABS_MIN_SIGNAL_HARD", "0.45")),
                (base_xau_signal + evo_signal_delta) * relax_factor,
            )

            xau_min_quality = max(
                float(os.getenv("ABS_MIN_QUALITY_HARD", "0.50")),
                (base_xau_quality + evo_quality_delta) * relax_factor,
            )

            passed = (
                float(signal_score) >= float(xau_min_signal)
                and float(entry_quality_safe) >= float(xau_min_quality)
            )

            logging.warning(
                "XAU ADAPTIVE FILTER | symbol=%s passed=%s signal=%.4f req_signal=%.4f quality=%.4f req_quality=%.4f relax=%.3f",
                args.symbol,
                str(passed).lower(),
                float(signal_score),
                float(xau_min_signal),
                float(entry_quality_safe),
                float(xau_min_quality),
                float(relax_factor),
            )

            if not passed:
                signal_executable = False
                final_execution_allowed = False
                _evo2_register_block(evo_engine_state, args.symbol, "xau_hard_filter")

        if not final_execution_allowed:
            logging.warning("ENTRY BLOCKED | symbol=%s reason=hard_filter_failed", args.symbol)
            if evo_threshold_enabled and evo_threshold is not None:
                evo_threshold.update(None)
            _sleep()
            continue
        if evo_threshold_enabled and evo_threshold is not None:
            evo_threshold.update(0.0)
        is_stacking_entry = int(getattr(risk_state, "open_positions", 0)) > 0
        current_positions = int(getattr(risk_state, "open_positions", 0))

        # =========================
        # 🔥 HARD FIX: BROKER FLAT SYNC
        # =========================
        broker_positions = 0
        broker_open_positions_count = 0
        broker_flat = True
        try:
            if mt5 is not None:
                positions = mt5.positions_get(symbol=args.symbol)
                if positions is not None:
                    broker_positions = len(positions)

            # fallback to internal if MT5 fails
            effective_positions = max(current_positions, broker_positions)
            broker_open_positions_count = int(effective_positions)
            broker_flat = broker_open_positions_count == 0

            logging.info(
                "BROKER SYNC | symbol=%s internal=%d broker=%d effective=%d flat=%s",
                args.symbol,
                current_positions,
                broker_positions,
                effective_positions,
                str(broker_flat).lower(),
            )
        except Exception as e:
            logging.warning(
                "BROKER SYNC FALLBACK | symbol=%s error=%s → using safe defaults",
                args.symbol,
                str(e),
            )
            broker_open_positions_count = 0
            broker_flat = True
            effective_positions = current_positions

        logging.info(
            "BROKER SYNC SAFE | symbol=%s broker_positions=%d flat=%s",
            args.symbol,
            broker_open_positions_count,
            str(broker_flat).lower(),
        )

        # =========================================
        # 🔥 FIX — FORCE PAPER TRADING FLAT MODE
        # =========================================
        paper_mode = str(os.getenv("ENABLE_PAPER_TRADING", "false")).lower() in ("1", "true", "yes")
        live_execution_enabled = str(os.getenv("LIVE_EXECUTION_ENABLED", "true")).lower() in ("1", "true", "yes")

        if paper_mode and (not live_execution_enabled):
            logging.warning(
                "PAPER MODE ACTIVE (SAFE MODE) | symbol=%s",
                args.symbol,
            )
        else:
            logging.info(
                "LIVE MODE ACTIVE | symbol=%s live_execution_enabled=%s",
                args.symbol,
                str(live_execution_enabled).lower(),
            )

        # ==========================================
        # 🔥🔥🔥 FIX 5 — FORCE SYNC (CRITICAL)
        # ==========================================
        force_sync = str(os.getenv("FORCE_STATE_SYNC", "true")).lower() in ("1", "true", "yes")

        if force_sync and broker_flat:
            logging.warning(
                "FORCE SYNC | correcting mismatch | symbol=%s",
                args.symbol,
            )

            # reset internal state
            risk_state.open_positions = 0
            active_trade = None

            try:
                state.active_trade = None
                state.position_size = 0
            except Exception:
                pass

            current_positions = 0
            effective_positions = 0

        # ==========================================
        # 🔥 FIX 6 — ALLOW RE-ENTRY WHEN DESYNC
        # ==========================================
        allow_reentry = str(os.getenv("ALLOW_REENTRY_ON_DESYNC", "true")).lower() in ("1", "true", "yes")

        if not broker_flat and allow_reentry:
            logging.warning(
                "REENTRY OVERRIDE | allowing trade despite non-flat state | symbol=%s",
                args.symbol,
            )
            risk_state.same_bar_entry_allowed = True
            final_execution_allowed = True

        # =========================
        # 🔥 SAFE STACK LOGIC
        # =========================
        allow_stacking = _env_bool("ALLOW_POSITION_STACKING", "false")

        valid_stack = False
        if allow_stacking and effective_positions < int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1")):
            valid_stack = True

        # =========================
        # FINAL ENTRY BLOCK FIX
        # =========================
        if not broker_flat and not valid_stack:
            logging.warning(
                "ENTRY BLOCK SKIPPED (paper mode override) | symbol=%s",
                args.symbol,
            )

        cooldown_active = float(cooldown_remaining) > 0.0
        # =========================
        # COOLDOWN (PRE-EXEC ONLY)
        # =========================
        if cooldown_active and not signal_executable:
            logging.warning(
                "COOLDOWN BLOCK | symbol=%s",
                args.symbol,
            )
            _sleep()
            continue

        # If we are here, final execution path already passed executable checks.
        if cooldown_active and signal_executable:
            logging.warning(
                "COOLDOWN BYPASS | execution already approved | symbol=%s",
                args.symbol,
            )

        if not signal_executable or not final_execution_allowed:
            logging.warning("STRICT BLOCK | reason=%s", "signal_not_executable")
            _sleep()
            continue
        if not fast_lane:
            if len(confidence_memory) > 10:
                recent = confidence_memory[-10:]
                low_conf_losses = [
                    t for t in recent
                    if float(t["score"]) < 0.06 and t["result"] == "LOSS"
                ]
                if len(low_conf_losses) >= 5:
                    logging.warning(
                        "ENTRY BLOCKED | low-confidence trades underperforming"
                    )
                    _sleep()
                    continue
        risk_locked_position_scale = float(position_scale)
        position_scale = min(float(position_scale), risk_locked_position_scale)
        logging.info(
            "LEVEL7 RISK LOCK | symbol=%s position_scale=%.4f",
            args.symbol,
            float(position_scale),
        )

        trade = None
        proposed_side = 'LONG' if int(signal) > 0 else 'SHORT'
        proposed_entry_price = float(arr['close'][-1])

        trade_signature = None if trade is None else (float(trade.entry_price), float(trade.signal_score), float(trade.entry_index))
        fallback_trend_strength = _safe_trend_strength_from_bars(bars, fallback=signal_score)
        filter_trend_strength = float(getattr(latest_filter, 'effective_trend_strength', getattr(latest_filter, 'trend_strength', fallback_trend_strength)))
        if not np.isfinite(filter_trend_strength):
            filter_trend_strength = fallback_trend_strength
        blocked_signal = signal_generated and (not final_filter_passed or not execution_priority_allowed)
        cooldown_remaining = float(adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol))
        logging.info(
            "FINAL EXECUTION DECISION | symbol=%s generated=%s executable=%s forced=%s",
            args.symbol,
            str(signal_generated).lower(),
            str(signal_executable).lower(),
            "false",
        )
        min_time_remaining = 0.0
        if risk_state.last_entry_time is not None:
            min_time_remaining = max(
                0.0,
                float(adaptive_filters.adaptive_min_time_between_trades_seconds) - (loop_now - risk_state.last_entry_time).total_seconds(),
            )
        logging.info(
            'Signal decision | generated=%s executable=%s score=%.4f priority=%s',
            str(signal_generated).lower(),
            str(signal_executable).lower(),
            signal_score,
            signal_priority,
        )
        stack_trigger_profit = float(os.getenv("STACK_TRIGGER_PROFIT", "0.0002") or 0.0002)
        stacking_scale = float(os.getenv("STACKING_SCALE", "0.5") or 0.5)
        max_stack_count = int(float(os.getenv("MAX_STACK_COUNT", "3") or 3))
        stack_signal_ratio = float(os.getenv("STACK_SIGNAL_RATIO", "0.6") or 0.6)
        adaptive_stack_signal = float(execution_threshold) * stack_signal_ratio
        allow_add_position = False
        current_profit_pct = 0.0
        is_stacking_entry = False
        if active_trade is not None:
            current_price_for_stack = float(arr['close'][-1]) if len(arr['close']) > 0 else float(active_trade.entry_price)
            active_side = str(active_trade.side).upper()
            proposed_side = 'LONG' if float(signal) > 0 else 'SHORT'
            broker_position_exists = (not broker_flat) or int(broker_open_positions_count) > 0
            reversal_pending_close = bool(getattr(state, 'reversal_pending_close', False))
            if active_side == 'LONG':
                current_profit_pct = (current_price_for_stack - float(active_trade.entry_price)) / max(float(active_trade.entry_price), 1e-9)
            else:
                current_profit_pct = (float(active_trade.entry_price) - current_price_for_stack) / max(float(active_trade.entry_price), 1e-9)
            allow_add_position = (
                current_profit_pct > stack_trigger_profit
                and float(signal_score) > adaptive_stack_signal
                and bool(signal_executable)
            )
            logging.info(
                "STACK CHECK | profit=%.5f signal=%.5f required=%.5f allow=%s",
                float(current_profit_pct),
                float(signal_score),
                float(adaptive_stack_signal),
                str(allow_add_position).lower()
            )
            stack_open_positions = int(getattr(risk_state, "open_positions", 0))
            is_stacking_entry = stack_open_positions > 0
            logging.info(
                "STACK GUARD | symbol=%s open_positions=%d broker_open_positions=%d max_stack=%d current_profit_pct=%.5f",
                args.symbol,
                stack_open_positions,
                int(broker_open_positions_count),
                int(MAX_STACK_PER_SYMBOL),
                float(current_profit_pct),
            )
            # STACK FIX:
            # no stacking into a losing trade, and no "recovery stacking"
            if is_stacking_entry:
                if current_profit_pct <= 0.0004:
                    logging.warning(
                        "STACK BLOCKED | reason=profit_trigger_not_met symbol=%s profit=%.5f required=0.00040",
                        args.symbol,
                        float(current_profit_pct),
                    )
                    allow_add_position = False
                if current_profit_pct < 0:
                    logging.warning(
                        "STACK BLOCKED | reason=negative_pnl symbol=%s pnl=%.5f",
                        args.symbol,
                        float(current_profit_pct),
                    )
                    allow_add_position = False
            if int(getattr(active_trade, "stack_count", 0)) >= max_stack_count:
                allow_add_position = False
            if is_opposite_side(active_side, proposed_side):
                allow_add_position = False
            stack_guard_passed = True
            if stack_open_positions != int(broker_open_positions_count):
                stack_guard_passed = False
            if stack_open_positions >= int(MAX_STACK_PER_SYMBOL):
                stack_guard_passed = False
            if bool(getattr(risk_state, "has_unresolved_execution_failure", False)):
                stack_guard_passed = False
            if quality_score < 0.6:
                stack_guard_passed = False
            if not allow_add_position:
                stack_guard_passed = False
            if not signal_executable:
                stack_guard_passed = False
            if not broker_position_exists:
                logging.warning("STALE TRADE STATE RESET")
                logging.info('POSITION BLOCK | reason=stale_state_detected')
                reset_flat_internal_state(
                    symbol=args.symbol,
                    state=state,
                    risk_state=risk_state,
                    reason='stale_state_detected',
                    now=loop_now,
                    force_reentry_ready=True,
                )
                active_trade = state.active_trade
                active_notional_usd = state.active_notional_usd
                active_position_scale = state.active_position_scale
                state.reversal_pending_close = False
                evolution_engine.update(
                    signal_generated=signal_generated,
                    signal_executed=False,
                    signal_filtered_reason='stale_state_detected',
                    pnl=loop_pnl_delta,
                    volatility=realized_volatility,
                    signal_score=signal_score,
                    symbol=args.symbol,
                    trend_strength=filter_trend_strength,
                )
                _sleep()
                continue
            if broker_flat:
                cooldown_remaining = adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol)
                logging.warning(
                    'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=%s cooldown_remaining=%.2f reason=%s',
                    args.symbol,
                    'true',
                    int(risk_state.open_positions),
                    str(bool(risk_state.same_bar_entry_allowed)).lower(),
                    float(cooldown_remaining),
                    'broker_position_exists',
                )
            active_entry_time = active_trade.entry_time
            try:
                if isinstance(active_entry_time, datetime) and active_entry_time > loop_now:
                    logging.warning(
                        "ENTRY TIME FIX | correcting future timestamp | old=%s now=%s",
                        active_entry_time,
                        loop_now,
                    )
                    active_entry_time = loop_now
                    setattr(active_trade, "entry_time", active_entry_time)
            except Exception:
                pass
            active_trailing_stop = getattr(active_trade, 'trailing_stop_price', None)
            logging.info('Open trade active | side=%s entry_price=%.6f hold_start=%s trailing_active=%s trailing_stop=%s', active_trade.side, active_trade.entry_price, active_entry_time.isoformat() if isinstance(active_entry_time, datetime) else 'none', str(active_trade.trailing_active).lower(), 'none' if active_trailing_stop is None else f'{active_trailing_stop:.6f}')
            pe_result = manage_profit_engine_v2(
                state=state,
                symbol=args.symbol,
                active_trade=active_trade,
                latest_bar=latest_bar,
                loop_index=latest_index,
                signal_score=float(signal_score),
                entry_quality=float(entry_quality_safe),
            )
            logging.info(
                "PROFIT ENGINE V2 | symbol=%s action=%s reason=%s pnl_pct=%.5f partial=%s be=%s trail=%s",
                args.symbol,
                pe_result["action"],
                pe_result["reason"],
                float(pe_result["pnl_pct"]),
                str(bool(state.profit_v2.get("partial_taken", False))).lower(),
                str(bool(state.profit_v2.get("be_armed", False))).lower(),
                str(bool(state.profit_v2.get("trail_armed", False))).lower(),
            )
            if pe_result["move_stop_to_be"]:
                with suppress(Exception):
                    setattr(active_trade, "stop_loss", _safe_float(getattr(active_trade, "entry_price", 0.0)))
            if pe_result["trail_stop_price"] is not None:
                with suppress(Exception):
                    setattr(active_trade, "stop_loss", float(pe_result["trail_stop_price"]))
            if pe_result["action"] == "partial_close":
                with suppress(Exception):
                    setattr(active_trade, "position_scale", max(0.05, 1.0 - float(pe_result["close_fraction"])))
            if pe_result["action"] == "close_full":
                with suppress(Exception):
                    setattr(active_trade, "force_close_reason", str(pe_result["reason"]))
                try:
                    evo_register_close(
                        evo_engine_state,
                        args.symbol,
                        float(pe_result.get("pnl_pct", 0.0)),
                        reason=str(pe_result.get("reason", "forced_close")),
                    )
                    save_evo_state(_evo2_state_path(), evo_engine_state)
                except Exception as exc:
                    logging.warning(
                        "EVO CLOSE UPDATE FAILED | symbol=%s error=%s",
                        args.symbol, str(exc),
                    )
                final_execution_allowed = False
                signal_generated = False
                signal_executable = False
            reversal_candidate = False
            if is_opposite_side(active_side, proposed_side) and (float(current_profit_pct) < 0.0 or float(signal_score) >= 0.9):
                reversal_candidate = True
                state.reversal_pending_close = True
                active_trade.force_close_reason = 'reversal_requested'
                logging.info('REVERSAL CANDIDATE | old_side=%s new_side=%s score=%.4f', active_side, proposed_side, float(signal_score))
                logging.info('REVERSAL EXIT TRIGGERED')
            if reversal_candidate or reversal_pending_close:
                block_reason = 'awaiting_reversal_close'
                logging.info('POSITION BLOCK | reason=%s', block_reason)
                evolution_engine.update(
                    signal_generated=signal_generated,
                    signal_executed=False,
                    signal_filtered_reason=block_reason,
                    pnl=loop_pnl_delta,
                    volatility=realized_volatility,
                    signal_score=signal_score,
                    symbol=args.symbol,
                    trend_strength=filter_trend_strength,
                )
                logging.info('EXECUTION PATH | status=blocked reason=%s side=%s signal_executable=%s', block_reason, active_trade.side, str(signal_executable).lower())
                _sleep()
                continue
            if not is_opposite_side(active_side, proposed_side):
                max_positions_per_symbol = 2
                get_symbol_position_count = getattr(risk_state, 'get_symbol_position_count', None)
                if callable(get_symbol_position_count):
                    existing_positions = int(get_symbol_position_count(args.symbol))
                else:
                    existing_positions = int(getattr(risk_state, 'open_positions', 0))
                allow_pyramiding = False
                if existing_positions < max_positions_per_symbol and stack_guard_passed:
                    if float(signal_score) > float(execution_score_threshold) * 1.1:
                        if float(current_profit_pct) >= -0.0002:
                            allow_pyramiding = True
                same_side_active = existing_positions >= 1 and not allow_pyramiding
                if same_side_active:
                    if is_stacking_entry:
                        logging.warning(
                            "STACK SAME SIDE OVERRIDE | stacking allowed | symbol=%s",
                            args.symbol,
                        )
                    else:
                        block_reason = 'same_side_active'
                        logging.info(
                            'POSITION BLOCK | reason=same_side_active symbol=%s positions=%d',
                            args.symbol,
                            existing_positions,
                        )
                        signal_executable = False
                        evolution_engine.update(
                            signal_generated=signal_generated,
                            signal_executed=False,
                            signal_filtered_reason=block_reason,
                            pnl=loop_pnl_delta,
                            volatility=realized_volatility,
                            signal_score=signal_score,
                            symbol=args.symbol,
                            trend_strength=filter_trend_strength,
                        )
                        logging.info('EXECUTION PATH | status=blocked reason=%s side=%s signal_executable=%s', block_reason, active_trade.side, str(signal_executable).lower())
                        _sleep()
                        continue
                elif allow_pyramiding:
                    position_scale *= 0.5
                    logging.warning(
                        'PYRAMID ENTRY | symbol=%s positions=%d scale=%.2f',
                        args.symbol,
                        existing_positions,
                        position_scale,
                    )
            if is_opposite_side(active_side, proposed_side):
                block_reason = 'broker_position_exists'
                logging.info('POSITION BLOCK | reason=%s', block_reason)
                evolution_engine.update(
                    signal_generated=signal_generated,
                    signal_executed=False,
                    signal_filtered_reason=block_reason,
                    pnl=loop_pnl_delta,
                    volatility=realized_volatility,
                    signal_score=signal_score,
                    symbol=args.symbol,
                    trend_strength=filter_trend_strength,
                )
                logging.info('EXECUTION PATH | status=blocked reason=%s side=%s signal_executable=%s', block_reason, active_trade.side, str(signal_executable).lower())
                _sleep()
                continue
            if stack_guard_passed:
                logging.info("STACKING ENTRY | adding to winning position")
                is_stacking_entry = True
                logging.info(
                    "STACKING MODE ACTIVE | symbol=%s scale=%.2f",
                    args.symbol,
                    float(position_scale),
                )
            else:
                signal_executable = False

        if not can_open_live_entry(live_safety_controller):
            logging.error(
                'EXECUTION BLOCKED | symbol=%s reason=live_entry_guard_failed halt_reason=%s',
                args.symbol,
                str(getattr(live_safety_controller, 'halt_reason', None) or 'none'),
            )
            logging.warning('LOOP CONTINUE SAFETY | symbol=%s', args.symbol)
            _sleep()
            continue
        max_positions = 4
        open_positions = int(getattr(risk_state, "open_positions", 0))
        max_positions_per_symbol = 2

        if int(getattr(risk_state, "open_positions", 0)) > max_positions_per_symbol:
            logging.warning(
                "MAX POSITION LIMIT | symbol=%s positions=%d",
                args.symbol,
                int(getattr(risk_state, "open_positions", 0)),
            )
            signal_executable = False

        ABSOLUTE_MAX_POSITIONS = 3

        if risk_state.open_positions >= ABSOLUTE_MAX_POSITIONS:
            logging.critical(
                "HARD LIMIT HIT | positions=%d blocking all entries",
                int(risk_state.open_positions),
            )
            signal_executable = False

        if is_stacking_entry:
            broker_aligned_for_stack = int(getattr(risk_state, "open_positions", 0)) == int(broker_open_positions_count)
            if not broker_aligned_for_stack:
                logging.warning("STACK BLOCKED | reason=%s symbol=%s", "broker_sync_unaligned", args.symbol)
                signal_executable = False
            if int(getattr(risk_state, "open_positions", 0)) >= MAX_STACK_PER_SYMBOL:
                logging.warning("STACK BLOCKED | reason=%s symbol=%s", "max_stack_per_symbol", args.symbol)
                signal_executable = False
            if active_trade is not None:
                current_price_for_stack = float(arr['close'][-1]) if len(arr['close']) > 0 else float(active_trade.entry_price)
                if str(active_trade.side).upper() == 'LONG':
                    current_profit_pct = (current_price_for_stack - float(active_trade.entry_price)) / max(float(active_trade.entry_price), 1e-9)
                else:
                    current_profit_pct = (float(active_trade.entry_price) - current_price_for_stack) / max(float(active_trade.entry_price), 1e-9)
                if current_profit_pct <= 0.0004:
                    logging.warning("STACK BLOCKED | reason=%s symbol=%s", "profit_trigger_not_met", args.symbol)
                    signal_executable = False
            if bool(getattr(risk_state, "has_unresolved_execution_failure", False)):
                logging.warning("STACK BLOCKED | reason=%s symbol=%s", "unresolved_execution_failure", args.symbol)
                signal_executable = False
            if quality_score < 0.6:
                logging.warning(
                    "STACK BLOCKED | low quality | symbol=%s quality=%.4f",
                    args.symbol,
                    quality_score,
                )
                signal_executable = False
        high_quality_signal = float(signal_score) >= 0.12
        allow_replacement = False
        if open_positions >= max_positions and high_quality_signal:
            risk_positions = list(getattr(risk_state, "positions", []) or [])
            weakest_pnl = min((float(getattr(p, "unrealized_pnl", 0.0)) for p in risk_positions), default=0.0)
            if weakest_pnl < 0:
                allow_replacement = True
                logging.warning(
                    "SMART REPLACEMENT | closing weakest trade pnl=%.6f",
                    weakest_pnl,
                )
        if allow_replacement:
            close_worst_position_fn = globals().get("close_worst_position")
            if callable(close_worst_position_fn):
                close_worst_position_fn(risk_state)
                risk_state.open_positions = max(0, int(getattr(risk_state, "open_positions", 0)) - 1)
                execution_priority_allowed = True
                logging.warning("REPLACEMENT TRADE ENABLED")
            else:
                logging.warning("SMART REPLACEMENT | skipped reason=close_worst_position_unavailable")

        if risk_state.open_positions >= 4:
            logging.warning("STRICT BLOCK | symbol=%s reason=%s score=%.4f", args.symbol, "open_positions_hard_limit", signal_score)
            execution_priority_allowed = False

        if risk_state.open_positions >= 2 and risk_state.open_positions < 4 and not is_stacking_entry:
            logging.info(
                "ENTRY BLOCKED | too many open positions=%d",
                risk_state.open_positions,
            )
            if broker_flat:
                cooldown_remaining = adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol)
                logging.warning(
                    'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=%s cooldown_remaining=%.2f reason=%s',
                    args.symbol,
                    str(bool(getattr(risk_state, "active_trade", False))).lower(),
                    int(risk_state.open_positions),
                    str(bool(risk_state.same_bar_entry_allowed)).lower(),
                    float(cooldown_remaining),
                    'open_positions_limit',
                )
            _sleep()
            continue
        if broker_flat and active_trade is None and int(risk_state.open_positions) <= 0:
            logging.info(
                'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=false open_positions=0 same_bar_allowed=%s cooldown_remaining=%.2f min_time_remaining=%.2f reason=pre_entry_diagnostics',
                args.symbol,
                str(bool(risk_state.same_bar_entry_allowed)).lower(),
                float(cooldown_remaining),
                float(min_time_remaining),
            )
            if int(loop_count_without_trade) >= 60:
                logging.info(
                    'NO TRADE STATE | loops=%d cooldown=%.2f signal_score=%.4f',
                    int(loop_count_without_trade),
                    float(cooldown_remaining),
                    float(signal_score),
                )

        update_no_entry_watchdog(
            symbol=args.symbol,
            state=state,
            risk_state=risk_state,
            broker_flat=broker_flat,
            signal_generated=signal_generated,
            signal_executable=signal_executable,
            cooldown_remaining=adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol),
            reason=adaptive_filters.last_block_reason or 'no_execution_commit',
        )

        if (
            broker_flat
            and active_trade is None
            and int(risk_state.open_positions) == 0
            and final_execution_allowed
        ):
            logging.warning(
                "FINAL OVERRIDE | bypass min_time_between_trades | symbol=%s",
                args.symbol,
            )
            min_time_remaining = 0.0
            cooldown_remaining = 0.0

        # If ICT confirmed, allow a softer final gate for FX only.
        # Discovery mode uses the test sweep thresholds directly.
        if MODE == "DISCOVERY":
            final_min_signal = float(MIN_SIGNAL)
            final_min_quality = float(MIN_QUALITY)
        elif ict_entry_confirmed and is_fx_symbol(args.symbol):
            final_min_signal = float(os.getenv("FX_FINAL_MIN_SIGNAL", os.getenv("FINAL_MIN_SIGNAL", "0.75")) or 0.75)
            final_min_quality = float(os.getenv("FX_FINAL_MIN_QUALITY", os.getenv("FINAL_MIN_QUALITY", "0.75")) or 0.75)
        else:
            final_min_signal = float(os.getenv("FINAL_MIN_SIGNAL", "0.75") or 0.75)
            final_min_quality = float(os.getenv("FINAL_MIN_QUALITY", "0.75") or 0.75)

        final_filter_passed = (
            float(signal_score) >= final_min_signal
            and float(entry_quality_safe) >= final_min_quality
        )

        # 🔥 SAFETY FALLBACK (if override off but close)
        if not final_filter_passed:
            soft_signal = float(signal_score) >= (final_min_signal * 0.85)
            soft_quality = float(entry_quality_safe) >= (final_min_quality * 0.85)

            if soft_signal and soft_quality:
                logging.warning(
                    "FINAL SOFT PASS | symbol=%s signal=%.4f quality=%.4f",
                    args.symbol,
                    float(signal_score),
                    float(entry_quality_safe),
                )
                final_filter_passed = True

        if not final_filter_passed:
            logging.warning(
                "FINAL BLOCK ACTIVE | symbol=%s signal=%.4f required_signal=%.4f quality=%.4f required_quality=%.4f",
                args.symbol,
                float(signal_score),
                float(final_min_signal),
                float(entry_quality_safe),
                float(final_min_quality),
            )
            if float(signal_score) < final_min_signal:
                _evo2_register_block(evo_engine_state, args.symbol, "signal_too_low")
            if float(entry_quality_safe) < final_min_quality:
                _evo2_register_block(evo_engine_state, args.symbol, "quality_too_low")
            _sleep()
            continue

        # Final sanity filter right before execution.
        # This prevents "looks okay" setups from passing just because relax/cooldown logic opened the gate.
        pre_exec_min_signal = float(os.getenv("PRE_EXEC_MIN_SIGNAL", "0.55") or 0.55)
        pre_exec_min_quality = float(os.getenv("PRE_EXEC_MIN_QUALITY", "0.60") or 0.60)

        pre_exec_passed = (
            float(signal_score) >= pre_exec_min_signal
            and float(entry_quality_safe) >= pre_exec_min_quality
        )

        # 🔥 SOFT FALLBACK (buffer zone)
        if not pre_exec_passed:
            soft_signal = float(signal_score) >= (pre_exec_min_signal * 0.85)
            soft_quality = float(entry_quality_safe) >= (pre_exec_min_quality * 0.85)

            if soft_signal and soft_quality:
                logging.warning(
                    "PRE-EXEC SOFT PASS | symbol=%s signal=%.4f quality=%.4f",
                    args.symbol,
                    float(signal_score),
                    float(entry_quality_safe),
                )
                pre_exec_passed = True

        if not pre_exec_passed:
            logging.warning(
                "PRE-EXEC BLOCK ACTIVE | symbol=%s signal=%.4f required=%.4f quality=%.4f required=%.4f",
                args.symbol,
                float(signal_score),
                float(pre_exec_min_signal),
                float(entry_quality_safe),
                float(pre_exec_min_quality),
            )
            _evo2_register_block(evo_engine_state, args.symbol, "pre_exec_block")
            _sleep()
            continue

        # 🔥 REMOVED — execution controlled centrally

        # never fake executability after central gating
        # signal_executable must remain the real downstream state
        if not signal_executable:
            logging.info("PRE-EXEC BLOCK RESPECTED | symbol=%s", args.symbol)
            continue


        if not final_execution_allowed:
            no_trade_reasons: list[str] = []
            if not signal_generated:
                no_trade_reasons.append("no_signal")
            if not signal_executable:
                no_trade_reasons.append("not_executable")
            if not execution_priority_allowed:
                no_trade_reasons.append("below_execution_threshold")
            if open_positions == 0:
                cooldown_remaining = 0.0
            if cooldown_remaining > 0 and open_positions > 0:
                no_trade_reasons.append(f"cooldown_active:{cooldown_remaining:.2f}s")
            if int(risk_state.open_positions) > 0:
                no_trade_reasons.append("open_positions_limit")
            if not broker_flat:
                no_trade_reasons.append("broker_not_flat")
            if active_trade is not None:
                no_trade_reasons.append("active_trade_exists")
            if (
                min_time_remaining > 0
                and not (
                    broker_flat
                    and active_trade is None
                    and int(risk_state.open_positions) == 0
                    and final_execution_allowed
                )
            ):
                no_trade_reasons.append(f"min_time_between_trades:{min_time_remaining:.2f}s")
            logging.info(
                "NO TRADE DECISION | symbol=%s reasons=%s signal=%.4f threshold=%.4f cooldown=%.2f",
                args.symbol,
                ",".join(no_trade_reasons) if no_trade_reasons else "unknown",
                float(signal_score),
                float(effective_execution_score_threshold),
                float(cooldown_remaining),
            )
            if blocked_signal:
                block_reason = 'signal_score_below_execution_threshold' if not execution_priority_allowed else latest_filter.reason_code
                adaptive_filters.record_block_reason(block_reason)
                adaptive_filters.log_pre_exec_block(
                    reason=block_reason,
                    side='LONG' if signal > 0 else 'SHORT',
                    score=signal_score,
                    volume_ratio=float(latest_filter.relative_volume),
                    spread=float(latest_filter.spread_ratio),
                    regime=str(latest_filter.current_market_regime),
                )
                logging.info('Signal decision | score=%.4f cooldown=%d spread=%.6f volume=%.4f action=filtered priority=%s blocked_reason=%s', signal_score, adaptive_filters.adaptive_cooldown_seconds, latest_filter.spread_ratio, latest_filter.relative_volume, signal_priority, adaptive_filters.last_block_reason)
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=%s score=%.4f positions=%d",
                    args.symbol,
                    str(block_reason),
                    float(signal_score),
                    int(getattr(risk_state, "open_positions", 0)),
                )
                logging.info('EXECUTION PATH | status=dropped reason=%s signal_generated=%s signal_executable=%s', block_reason, str(signal_generated).lower(), str(signal_executable).lower())
                if broker_flat:
                    cooldown_remaining = adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol)
                    logging.warning(
                        'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=%s cooldown_remaining=%.2f reason=%s',
                        args.symbol,
                        str(bool(getattr(risk_state, "active_trade", False))).lower(),
                        int(risk_state.open_positions),
                        str(bool(risk_state.same_bar_entry_allowed)).lower(),
                        float(cooldown_remaining),
                        block_reason,
                    )
                evolution_engine.update(
                    signal_generated=signal_generated,
                    signal_executed=False,
                    signal_filtered_reason=block_reason,
                    pnl=loop_pnl_delta,
                    volatility=realized_volatility,
                    signal_score=signal_score,
                    symbol=args.symbol,
                    trend_strength=filter_trend_strength,
                )
            else:
                evolution_engine.update(
                    signal_generated=signal_generated,
                    signal_executed=False,
                    signal_filtered_reason='no_signal',
                    pnl=loop_pnl_delta,
                    volatility=realized_volatility,
                    signal_score=signal_score,
                    symbol=args.symbol,
                    trend_strength=filter_trend_strength,
                )
            loop_count_without_trade += 1
            state.loop_count_without_trade = loop_count_without_trade
            recovery_snapshot = latest_filter if blocked_signal else SimpleNamespace(**{**latest_filter.__dict__, 'reason_code': 'no_signal'})
            adaptive_filters.on_loop(loop_count_without_trade, loop_now, latest_filter=recovery_snapshot)
            if loop_count_without_trade % 50 == 0:
                logging.warning('No signals for %d loops (no-trade timeout diagnostic)', loop_count_without_trade)
            _sleep()
            continue

        no_trade_snapshot = loop_count_without_trade
        loop_count_without_trade += 1
        state.loop_count_without_trade = loop_count_without_trade
        now_ts = time.time()

        # =========================================
        # GLOBAL LOOP STATE RESET
        # =========================================
        edge_blocked = False
        final_execution_reason = "not_set"
        just_committed_trade = False

        # =========================================
        # FIX: BROKER STATE (paper mode safe)
        # =========================================
        if str(os.getenv("TRADING_MODE", "TEST")).upper() == "TEST":
            broker_flat = int(getattr(risk_state, "open_positions", 0)) == 0

        if getattr(state, "reversal_pending_close", False):
            state.last_entry_commit_timestamp = 0.0
        last_entry_ts = float(getattr(state, "last_entry_commit_timestamp", 0.0) or 0.0)
        last_close_ts = float(getattr(state, "last_close_timestamp", 0.0) or 0.0)
        reversal_pending = bool(getattr(state, "reversal_pending_close", False))

        # =========================================
        # ONLY RESET TIMERS ON TRUE DESYNC / CLOSED TRADE
        # NOT just because broker_flat is true in paper mode
        # =========================================
        if broker_flat and bool(getattr(risk_state, "active_trade", False)):
            logging.warning(
                "COOLDOWN RESET | broker/state mismatch -> clearing timers | symbol=%s",
                args.symbol,
            )
            state.last_entry_commit_timestamp = 0.0
            state.last_close_timestamp = 0.0
            last_trade_timestamp = 0.0
            last_closed_trade_timestamp = 0.0
            cooldown_remaining = 0.0
            last_entry_ts = 0.0
            last_close_ts = 0.0
            risk_state.active_trade = False

        # =========================================
        # FIXED FORCE SYNC (ONLY on mismatch)
        # =========================================
        if broker_flat and bool(getattr(risk_state, "active_trade", False)):
            logging.warning(
                "FORCE SYNC | correcting mismatch | symbol=%s",
                args.symbol,
            )
            risk_state.active_trade = False
            state.last_entry_commit_timestamp = 0.0
            state.last_close_timestamp = 0.0
            last_entry_ts = 0.0
            last_close_ts = 0.0

        seconds_since_entry = now_ts - last_entry_ts if last_entry_ts > 0 else 9999.0
        seconds_since_close = now_ts - last_close_ts if last_close_ts > 0 else 9999.0

        logging.info(
            "OVERTRADE CHECK | symbol=%s seconds_since_entry=%.2f seconds_since_close=%.2f reversal_pending=%s",
            args.symbol,
            float(seconds_since_entry),
            float(seconds_since_close),
            str(reversal_pending).lower(),
        )
        logging.info(
            "OVERTRADE STATE | symbol=%s last_entry_ts=%.2f last_close_ts=%.2f active_trade=%s open_positions=%d broker_flat=%s",
            args.symbol,
            float(last_entry_ts),
            float(last_close_ts),
            str(bool(getattr(risk_state, "active_trade", False))).lower(),
            int(getattr(risk_state, "open_positions", 0)),
            str(broker_flat).lower(),
        )

        OVERTRADE_MIN_SECONDS = float(os.getenv("OVERTRADE_MIN_SECONDS", "20") or 20)

        # =========================================
        # GLOBAL LOOP STATE (RESET)
        # =========================================
        signal_generated = False
        signal_executable = False
        final_execution_allowed = False

        disable_overtrade = str(os.getenv("DISABLE_OVERTRADE", "true")).lower() in ("1", "true", "yes")
        overtrade_block = (
            seconds_since_entry < OVERTRADE_MIN_SECONDS
            and not reversal_pending
            and seconds_since_close > 2.0
        )
        if is_stacking_entry:
            overtrade_block = False
            logging.info(
                "OVERTRADE BYPASS | stacking entry | symbol=%s",
                args.symbol,
            )

        if overtrade_block and not disable_overtrade:
            logging.warning(
                "STRICT BLOCK | symbol=%s reason=overtrade score=%.4f",
                args.symbol,
                float(signal_score),
            )
            _sleep()
            continue
        if overtrade_block and disable_overtrade:
            logging.warning(
                "OVERTRADE BYPASSED | symbol=%s score=%.4f",
                args.symbol,
                float(signal_score),
            )
        loss_streak_limit = 3
        if int(consecutive_losses) >= loss_streak_limit:
            logging.warning(
                "LOSS STREAK DETECTED (SOFT) | symbol=%s losses=%d",
                args.symbol,
                int(consecutive_losses),
            )
            # safer risk reduction
            try:
                if 'risk_pct' in locals():
                    risk_pct = float(risk_pct) * 0.5
                    logging.info(
                        "RISK REDUCED | symbol=%s new_risk=%.5f",
                        args.symbol,
                        risk_pct,
                    )
            except Exception as e:
                logging.warning("RISK REDUCTION FAILED | %s", str(e))
            # NO HARD BLOCK — allow trading
        if (
            last_symbol is not None
            and str(args.symbol).upper() == str(last_symbol).upper()
            and (now_ts - float(last_closed_trade_timestamp)) < 60.0
        ):
            if not disable_overtrade:
                logging.warning(
                    "STRICT BLOCK | symbol=%s reason=overtrade_recent score=%.4f",
                    args.symbol,
                    float(signal_score),
                )
                _sleep()
                continue
            else:
                logging.warning(
                    "OVERTRADE RECENT BYPASSED | symbol=%s score=%.4f",
                    args.symbol,
                    float(signal_score),
                )
        if is_stacking_entry:
            min_time_remaining = 0.0
            logging.info(
                "MIN TIME BYPASS | stacking entry | symbol=%s",
                args.symbol,
            )
            risk_state.same_bar_entry_allowed = True

        # ALLOW FAST RE-ENTRY only when explicitly configured
        risk_state.same_bar_entry_allowed = bool(DISABLE_REENTRY_BLOCK)

        adaptive_filters.update_profit_protection(risk_state, risk_cfg)
        latest_bar_id = None if bars.empty else str(bars['timestamp'].iloc[-1])
        context = {
            "symbol": args.symbol,
            "price": float(proposed_entry_price),
            "side": proposed_side,
            "signal_score": float(signal_score),
            "quality": float(quality_score),
            "volatility": float(realized_volatility),
            "spread": float(latest_filter.spread_ratio),
            "impulse_body_ratio": float(getattr(latest_filter, "impulse_body_ratio", 0.0) or 0.0),
            "followthrough": float(getattr(latest_filter, "followthrough_progress", 0.0) or 0.0),
            "session_ok": True,
            "execution_allowed": True,
            "candles": bars.tail(250).to_dict("records") if hasattr(bars, "tail") else [],
        }
        precheck = decide_trade(context)
        current_setup_type = "unknown"

        if precheck.get("status") != "READY":
            precheck_reason = precheck.get("reason") or ",".join(precheck.get("reasons", [])) or "filtered"
            if should_skip_blocking_stage(loop_status, stage="precheck"):
                _sleep()
                continue
            if MODE == "DISCOVERY":
                logging.warning(
                    "DISCOVERY OVERRIDE | bypassing precheck | symbol=%s reason=%s",
                    args.symbol,
                    precheck_reason,
                )
            else:
                logging.info("⛔ HARD BLOCK FROM PRECHECK")
                execution_decision = SimpleNamespace(
                    should_execute=False,
                    reason=precheck_reason,
                    adjusted_score=float(signal_score),
                    position_scale=0.0,
                    dynamic_profit_floor=1.0,
                    size_tier="filtered",
                )
                logging.info(
                    "PRECHECK BLOCK | symbol=%s reason=%s",
                    args.symbol,
                    execution_decision.reason,
                )
                update_loop_status(
                    loop_status,
                    state="SETUP_BLOCKED",
                    reason=execution_decision.reason,
                    authority="central_precheck",
                    generated=True,
                    executable=False,
                    executed=False,
                    blocked=True,
                    setup_source=current_setup_type,
                    block_stage="precheck",
                )
                logging.info(
                    "PRECHECK DIAGNOSTICS | symbol=%s diagnostics=%s",
                    args.symbol,
                    precheck.get("diagnostics", {}),
                )

                final_execution_allowed = False
                final_execution_reason = execution_decision.reason

                logging.info(
                    "EXEC FINAL | score=%.3f side=%s position_scale=%.4f dynamic_floor=%.4f execution_reason=%s size_tier=%s risk_reason=%s",
                    float(signal_score),
                    str(proposed_side),
                    float(getattr(execution_decision, "position_scale", 0.0)),
                    float(getattr(execution_decision, "dynamic_profit_floor", 1.0)),
                    str(execution_decision.reason),
                    str(getattr(execution_decision, "size_tier", "filtered")),
                    "precheck_blocked",
                )

                _sleep()
                continue
        else:
            logging.info(
                "PRECHECK READY | symbol=%s diagnostics=%s",
                args.symbol,
                precheck.get("diagnostics", {}),
            )
        current_setup_type = str(
            precheck.get("diagnostics", {}).get("setup_type", "unknown")
        )

        # =========================================
        # PRECHECK
        # =========================================
        diagnostics = precheck.get("diagnostics", {})
        current_setup_type = str(
            diagnostics.get("setup_type", "unknown")
        ).lower().strip()
        sweep_info = diagnostics.get("sweep_info", {})
        displacement_info = diagnostics.get("displacement_info", {})

        # =========================================
        # 🔥 NORMALIZE SETUP TYPE (SAFETY)
        # =========================================
        if not current_setup_type:
            current_setup_type = "unknown"

        # reset each loop explicitly
        edge_blocked = False
        final_execution_reason = "ok"
        discovery_filter_blocked = False

        # =========================================
        # 🔥 SETUP CLASSIFICATION
        # =========================================
        sweep_like_setups = {"sweep_only", "sweep_displacement"}
        displacement_like_setups = {"displacement_only", "sweep_displacement"}

        is_sweep_setup = current_setup_type in sweep_like_setups
        is_displacement_setup = current_setup_type in displacement_like_setups

        # =========================================
        # 🔥 EDGE FILTER (MASTER GATE)
        # =========================================
        if MODE == "DISCOVERY":
            if current_setup_type not in ALLOWED_SETUPS:
                edge_blocked = True
                discovery_filter_blocked = True
                final_execution_reason = "not_in_test_set"
                logging.info(
                    "DISCOVERY FILTER | blocked | setup=%s allowed=%s",
                    current_setup_type,
                    ALLOWED_SETUPS,
                )
            elif is_sweep_setup and not sweep_info.get("detected", False):
                edge_blocked = True
                final_execution_reason = "edge_no_sweep"
            elif is_displacement_setup and not displacement_info.get("detected", False):
                edge_blocked = True
                final_execution_reason = "edge_no_displacement"
        else:
            if not is_sweep_setup:
                edge_blocked = True
                final_execution_reason = "edge_not_sweep"
            elif not sweep_info.get("detected", False):
                edge_blocked = True
                final_execution_reason = "edge_no_sweep"
            elif is_displacement_setup and not displacement_info.get("detected", False):
                edge_blocked = True
                final_execution_reason = "edge_no_displacement"

        if edge_blocked:
            if should_skip_blocking_stage(loop_status, stage="edge_filter"):
                _sleep()
                continue
            update_loop_status(
                loop_status,
                state="SETUP_BLOCKED",
                reason=final_execution_reason,
                authority="legacy_guard",
                generated=True,
                executable=False,
                executed=False,
                blocked=True,
                setup_source=current_setup_type,
                block_stage="edge_filter",
            )
            logging.warning(
                "HARD EDGE BLOCK | symbol=%s reason=%s mode=%s",
                args.symbol,
                final_execution_reason,
                MODE,
            )
            _sleep()
            continue

        # =========================================
        # 🔒 HARD SETUP FILTER (MASTER LOCK)
        # =========================================
        if current_setup_type not in ALLOWED_SETUPS:
            if should_skip_blocking_stage(loop_status, stage="setup_filter"):
                _sleep()
                continue
            update_loop_status(
                loop_status,
                state="SETUP_INVALID",
                reason="invalid_setup",
                authority="legacy_guard",
                generated=True,
                executable=False,
                executed=False,
                blocked=True,
                setup_source=current_setup_type,
                block_stage="setup_filter",
            )
            logging.warning(
                "SETUP HARD BLOCK | symbol=%s setup=%s allowed=%s",
                args.symbol,
                current_setup_type,
                ALLOWED_SETUPS,
            )
            _sleep()
            continue

        logging.info("EDGE FILTER | PASSED | setup=%s", current_setup_type)

        mode_name = (
            "DISCOVERY" if str(os.getenv("MODE", "")).upper() == "DISCOVERY" else "TEST"
        )
        from core.execution.engine import decide_execution_v2

        execution_decision = decide_execution_v2(
            symbol=args.symbol,
            side=proposed_side,
            signal_score=float(signal_score),
            entry_quality=float(entry_quality),
            setup_type=current_setup_type,
            session_windows_ok=True,
            risk_ok=True,
            broker_flat=True,
            mode=mode_name,
        )
        if MODE == "DISCOVERY":
            execution_decision.allowed = True
            execution_decision.reason = "discovery_override"

        # =========================================
        # 🔒 FINAL EXECUTION LOCK (ABSOLUTE AUTHORITY)
        # =========================================
        final_execution_allowed = bool(getattr(execution_decision, "allowed", False))
        final_reason = str(getattr(execution_decision, "reason", "blocked"))
        logging.info(
            "FINAL LOCK CHECK | symbol=%s allowed=%s reason=%s",
            args.symbol,
            str(final_execution_allowed),
            final_reason,
        )
        if not final_execution_allowed:
            if should_skip_blocking_stage(loop_status, stage="final_gate"):
                _sleep()
                continue
            if MODE == "DISCOVERY":
                logging.warning(
                    "DISCOVERY FINAL OVERRIDE | forcing execution | symbol=%s",
                    args.symbol,
                )
                final_execution_allowed = True
            else:
                update_loop_status(
                    loop_status,
                    state="SETUP_BLOCKED",
                    reason=final_reason,
                    authority="legacy_guard",
                    generated=True,
                    executable=False,
                    executed=False,
                    blocked=True,
                    setup_source=current_setup_type,
                    block_stage="final_gate",
                )
                logging.warning(
                    "FINAL BLOCK | symbol=%s reason=%s",
                    args.symbol,
                    final_reason,
                )
                _sleep()
                continue
        # =========================================
        # 🔥 BACKWARD COMPAT FIX (FULL SAFE)
        # =========================================
        if not hasattr(execution_decision, "should_execute"):
            execution_decision.should_execute = execution_decision.allowed
        if not hasattr(execution_decision, "adjusted_score"):
            execution_decision.adjusted_score = float(
                getattr(execution_decision, "signal_score", 0.0)
            )
        if not hasattr(execution_decision, "position_scale"):
            execution_decision.position_scale = (
                1.0 if execution_decision.allowed else 0.0
            )
        if not hasattr(execution_decision, "dynamic_profit_floor"):
            execution_decision.dynamic_profit_floor = 0.0
        if not hasattr(execution_decision, "size_tier"):
            execution_decision.size_tier = (
                "full_size" if execution_decision.allowed else "blocked"
            )
        if not hasattr(execution_decision, "probability"):
            execution_decision.probability = float(
                getattr(execution_decision, "signal_score", 0.0)
            )
        # =========================================
        # SINGLE SOURCE OF TRUTH (OVERRIDE OLD ENGINE)
        # =========================================
        ok = execution_decision.allowed
        reason = execution_decision.reason
        effective_cooldown = 0

        # =========================================
        # KEEP LEGACY VARIABLES IN SYNC (NO DRIFT)
        # =========================================
        final_execution_allowed = execution_decision.allowed
        final_execution_reason = execution_decision.reason
        # =========================================
        # SINGLE DECISION ENGINE ONLY
        # =========================================
        try:
            side_value = proposed_side if "proposed_side" in locals() else None
            signal_value = float(signal_score) if "signal_score" in locals() else 0.0
            quality_value = float(entry_quality) if "entry_quality" in locals() else 0.0
            setup_value = current_setup_type if "current_setup_type" in locals() else None

            if execution_decision.allowed:
                signal_generated = True
                signal_executable = True
                allowed = True
                logging.info(
                    "FINAL DECISION | ALLOWED | symbol=%s score=%.4f quality=%.4f setup=%s",
                    args.symbol,
                    signal_value,
                    quality_value,
                    setup_value,
                )
                # =========================================
                # SWEEP TRACKING
                # =========================================
                if current_setup_type == "sweep_only":
                    sweep_stats["trades"] += 1
                    logging.info(
                        "SWEEP TRADE COUNT | total=%d",
                        sweep_stats["trades"],
                    )
            else:
                signal_generated = False
                signal_executable = False
                allowed = False
                logging.warning(
                    "FINAL DECISION | BLOCKED | symbol=%s reason=%s",
                    args.symbol,
                    execution_decision.reason,
                )
                _sleep()
                continue
        except Exception as e:
            logging.exception("DECISION ENGINE FAILURE | symbol=%s err=%s", args.symbol, e)
            continue

        # =========================================
        # 🔒 FINAL ENTRY ALIGNMENT (NO DRIFT)
        # =========================================
        if current_setup_type not in ALLOWED_SETUPS:
            logging.warning(
                "FINAL ENTRY BLOCK | setup_not_allowed | symbol=%s setup=%s",
                args.symbol,
                current_setup_type,
            )
            _sleep()
            continue

        # =========================================================
        # TEST MODE = observe, not force
        # =========================================================
        if ENABLE_TEST_MODE:
            logging.warning("TEST MODE ACTIVE | no force execution")
            if TEST_FORCE_ENTRY:
                logging.warning("TEST_FORCE_ENTRY requested but ignored by clean observe-mode patch")
            if not execution_decision.allowed:
                logging.info("TEST MODE | trade skipped (valid behavior)")
        activity_hard_guards = {
            "daily_loss_stop",
            "max_daily_loss_triggered",
            "adaptive_daily_stop",
            "max_trades_per_day_reached",
            "adaptive_trade_limit_reached",
            "spread_too_high",
            "signal_decay_blocked",
            "desync_guard",
            "live_safety_controller_halted",
            "force_execution_blocked",
        }
        execution_reason = str(getattr(execution_decision, "reason", "") or "")
        if execution_reason in activity_hard_guards and not ENABLE_TEST_MODE:
            final_execution_allowed = False
            final_execution_reason = execution_reason
            logging.warning("ACTIVITY MODE DENIED | hard_guard=%s", execution_reason)
        logging.info(
            "THRESHOLD TUNED | signal=%.4f quality=%.4f fast_signal_min=%.2f fast_quality_min=%.2f",
            float(signal_score),
            float(quality_score),
            LEVEL7_FAST_SIGNAL_MIN,
            LEVEL7_FAST_QUALITY_MIN,
        )
        final_execution_allowed = execution_decision.allowed
        final_execution_reason = execution_decision.reason

        soft_block_reasons = {"min_time_between_trades_active", "ai_decision_neutral", "signal_decay_blocked"}
        hard_block_reasons = {"daily_loss_stop", "max_trades_per_day_reached", "max_daily_loss_triggered", "adaptive_daily_stop", "adaptive_trade_limit_reached"}
        if not execution_decision.allowed and not ENABLE_TEST_MODE and str(getattr(execution_decision, "reason", "")) not in soft_block_reasons:
            logging.warning("STRICT BLOCK | reason=%s", execution_decision.reason)
            logging.warning(
                "STRICT BLOCK | symbol=%s reason=%s score=%.4f",
                args.symbol,
                "execution_decision_false",
                signal_score,
            )
            logging.info(
                'EXECUTION SKIPPED | symbol=%s reason=should_execute_false signal=%.4f',
                args.symbol,
                signal_score,
            )
            adaptive_filters.log_final_block(
                reason=execution_decision.reason,
                side=proposed_side,
                score=execution_decision.adjusted_score,
                cooldown_remaining=adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol),
                position_state=adaptive_filters._position_state(risk_state),
            )
            logging.warning('Signal skipped due to execution engine: %s', execution_decision.reason)
            logging.info('Signal decision | score=%.4f cooldown=%d spread=%.6f volume=%.4f action=blocked reason=%s allowed_spread=%.6f fill_risk_score=%.4f', signal_score, adaptive_filters.adaptive_cooldown_seconds, latest_filter.spread_ratio, latest_filter.relative_volume, execution_decision.reason, adaptive_spread, float(getattr(latest_filter, 'fill_risk_score', 0.0)))
            if execution_decision.reason == 'same_bar_entry_blocked':
                logging.info('EXECUTION PATH | status=blocked reason=same_bar_entry_allowed_false latest_bar_id=%s last_entry_bar_id=%s', latest_bar_id, adaptive_filters.last_entry_bar_id)
            if broker_flat:
                cooldown_remaining = adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol)
                logging.warning(
                    'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=%s cooldown_remaining=%.2f reason=%s',
                    args.symbol,
                    str(bool(getattr(risk_state, "active_trade", False))).lower(),
                    int(risk_state.open_positions),
                    str(bool(risk_state.same_bar_entry_allowed)).lower(),
                    float(cooldown_remaining),
                    execution_decision.reason,
                )
            logging.info('EXECUTION PATH | status=blocked reason=%s signal_generated=%s signal_executable=%s', execution_decision.reason, str(signal_generated).lower(), str(signal_executable).lower())
            evolution_engine.update(
                signal_generated=signal_generated,
                signal_executed=False,
                signal_filtered_reason=execution_decision.reason,
                pnl=loop_pnl_delta,
                volatility=realized_volatility,
                signal_score=signal_score,
                symbol=args.symbol,
                trend_strength=filter_trend_strength,
            )
            _sleep()
            continue
        if not execution_decision.allowed and not ENABLE_TEST_MODE and str(getattr(execution_decision, "reason", "")) in soft_block_reasons:
            logging.warning("SOFT BLOCK | converted to warning")

        adaptive_filters.record_profit_gate_passed()
        # OLD ENGINE DISABLED — USING DECISION ENGINE ONLY
        if reason == 'daily_reset':
            logging.info('New UTC day resetting daily risk')
            logging.info('Trading resumed')
        if not ok and reason not in soft_block_reasons:
            logging.warning("STRICT BLOCK | reason=%s", reason)
            if reason in {'max_daily_loss_triggered', 'adaptive_daily_stop'}:
                logging.error('Daily loss hit trading paused')
            elif reason in {'max_trades_per_day_reached', 'adaptive_trade_limit_reached'}:
                logging.warning('Adaptive trade capacity reached')
            else:
                logging.warning('Signal skipped due to risk rule: %s', reason)
            logging.info('Signal decision | score=%.4f cooldown=%d spread=%.6f volume=%.4f action=blocked reason=%s allowed_spread=%.6f fill_risk_score=%.4f', signal_score, effective_cooldown, latest_filter.spread_ratio, latest_filter.relative_volume, reason, adaptive_spread, float(getattr(latest_filter, 'fill_risk_score', 0.0)))
            if reason == 'cooldown_active':
                logging.info('EXECUTION PATH | status=blocked reason=cooldown_active effective_cooldown=%d', effective_cooldown)
            if broker_flat:
                cooldown_remaining = adaptive_filters._cooldown_remaining(risk_state, risk_cfg, loop_now, symbol=args.symbol)
                min_time_remaining = 0.0
                if risk_state.last_entry_time is not None:
                    min_time_remaining = max(
                        0.0,
                        float(adaptive_filters.adaptive_min_time_between_trades_seconds) - (loop_now - risk_state.last_entry_time).total_seconds(),
                    )
                logging.warning(
                    'FLAT BLOCK DIAG | symbol=%s broker_flat=true active_trade=%s open_positions=%d same_bar_allowed=%s cooldown_remaining=%.2f min_time_remaining=%.2f reason=%s',
                    args.symbol,
                    str(bool(getattr(risk_state, "active_trade", False))).lower(),
                    int(risk_state.open_positions),
                    str(bool(risk_state.same_bar_entry_allowed)).lower(),
                    float(cooldown_remaining),
                    float(min_time_remaining),
                    reason,
                )
            logging.info('EXECUTION PATH | status=blocked reason=%s risk_open_positions=%d trading_paused_until=%s', reason, risk_state.open_positions, risk_state.trading_paused_until.isoformat() if risk_state.trading_paused_until else 'none')
            evolution_engine.update(
                signal_generated=signal_generated,
                signal_executed=False,
                signal_filtered_reason=reason,
                pnl=loop_pnl_delta,
                volatility=realized_volatility,
                signal_score=signal_score,
                symbol=args.symbol,
                trend_strength=filter_trend_strength,
            )
            _sleep()
            continue
        if not ok:
            if reason in hard_block_reasons:
                _sleep()
                continue
            logging.warning("SOFT BLOCK | converted to warning")

        spread = float(latest_filter.spread_ratio)
        open_positions = 1 if active_trade is not None else 0

        # =========================================
        # 🔥 COOLdown FIX (CRITICAL)
        # =========================================
        if open_positions == 0:
            cooldown_remaining = 0.0
            min_time_remaining = 0.0

        # =========================================
        # 🔥 FORCE EXECUTION SYNC (VERY EARLY)
        # =========================================
        if signal_generated:
            signal_executable = True

        # =========================================
        # 🔥 ENTRY PIPELINE (PRIMARY AUTHORITY)
        # =========================================
        evo_allowed = True
        if evo_threshold is not None:
            try:
                evo_allowed = float(signal_score) >= float(evo_threshold.get_threshold())
            except Exception:
                evo_allowed = True

        # 🔒 PIPELINE DISABLED — execution_decision is ONLY authority
        allowed = final_execution_allowed
        reasons = ["controlled_by_execution_engine"]

        # 🔒 TEST FORCE DISABLED

        # 🔒 FORCE MODE DISABLED — NO OVERRIDES ALLOWED

        # =========================================
        # 🔒 FINAL SAFETY — NO DRIFT
        # =========================================
        if not final_execution_allowed:
            logging.error(
                "SAFETY BLOCK | execution drift prevented | symbol=%s",
                args.symbol,
            )
            _sleep()
            continue

        # =========================================
        # 🔒 EXECUTION GUARD (ABSOLUTE FINAL)
        # =========================================
        if current_setup_type not in ALLOWED_SETUPS:
            logging.error(
                "EXECUTION BLOCKED FINAL | symbol=%s setup=%s",
                args.symbol,
                current_setup_type,
            )
            _sleep()
            continue

        # =========================================
        # 🔥 ANTI-OVERTRADING (CRITICAL EDGE FIX)
        # =========================================
        now_ts = time.time()
        min_trade_spacing = float(os.getenv("MIN_TRADE_SPACING_SEC", "30"))

        if float(last_closed_trade_timestamp) > 0:
            seconds_since_close = now_ts - float(last_closed_trade_timestamp)
            if seconds_since_close < min_trade_spacing:
                logging.warning(
                    "ANTI OVERTRADE BLOCK | symbol=%s wait=%.2fs required=%.2fs",
                    args.symbol,
                    seconds_since_close,
                    min_trade_spacing,
                )
                _sleep()
                continue

        confirm_price = float(bars.iloc[-1]["close"])
        _sleep()
        latest_price = float(bars.iloc[-1]["close"])
        if ENABLE_TEST_MODE:
            latest_price = _simulate_price(confirm_price)
        price_move = abs(latest_price - confirm_price)
        # =========================================
        # 🔥 FIX — REENTRY BLOCK DISABLED
        # =========================================
        disable_reentry_block = str(os.getenv("DISABLE_REENTRY_BLOCK", "true")).lower() in ("1", "true", "yes")
        reentry_block = price_move <= confirm_price * 0.0005
        if reentry_block and not disable_reentry_block and not ENABLE_TEST_MODE:
            logging.info(
                "ENTRY SKIPPED | reason=reentry_price_not_displaced symbol=%s move=%.6f",
                args.symbol,
                price_move,
            )
            _sleep()
            continue
        elif reentry_block and (disable_reentry_block or ENABLE_TEST_MODE):
            logging.warning(
                "REENTRY BLOCK DISABLED - forcing execution | symbol=%s",
                args.symbol,
            )
        # =========================================
        # 🔥 PROFIT ENGINE EXIT CONTROL
        # =========================================
        try:
            latest_px_for_virtual = float(bars.iloc[-1]["close"])
            if ENABLE_TEST_MODE:
                latest_px_for_virtual = latest_price
            update_virtual_trades(
                symbol=args.symbol,
                current_price=latest_px_for_virtual,
                now_ts=time.time(),
            )
        except Exception as e:
            logging.error("VIRTUAL TRADE UPDATE ERROR | symbol=%s err=%s", args.symbol, str(e))

        if active_trade is not None:
            try:
                ict_mgmt = _maybe_manage_ict_trade(
                    active_trade=active_trade,
                    current_price=float(latest_price),
                    latest_filter=latest_filter,
                    spread=float(spread),
                    now_ts=time.time(),
                )
                if ict_mgmt is not None:
                    if ict_mgmt["action"] == "partial_close":
                        setattr(active_trade, "partial_close", True)
                        setattr(active_trade, "partial_close_fraction", float(ict_mgmt.get("fraction", 0.5)))
                        logging.info(
                            "ICT MGMT ACTION | symbol=%s action=partial_close fraction=%.2f reason=%s",
                            args.symbol,
                            float(ict_mgmt.get("fraction", 0.5)),
                            str(ict_mgmt.get("reason", "partial_tp_hit")),
                        )
                    elif ict_mgmt["action"] == "close":
                        setattr(active_trade, "force_close_reason", str(ict_mgmt.get("reason", "ict_exit")))
                        logging.info(
                            "ICT MGMT ACTION | symbol=%s action=close reason=%s",
                            args.symbol,
                            str(ict_mgmt.get("reason", "ict_exit")),
                        )
                        continue

                pe_action, pe_reason = pe.manage_trade(active_trade, latest_price)

                # 🔥 EXTRA DEBUG
                logging.info(
                    "PE DEBUG | symbol=%s action=%s reason=%s latest_price=%.5f",
                    args.symbol,
                    pe_action,
                    pe_reason,
                    float(latest_price),
                )

                # =========================================
                # 🔥 PROFIT LOCK / TRAILING MANAGEMENT
                # =========================================
                pl_action, pl_reason = profit_lock_engine.manage_trade(active_trade, float(latest_price), bars)
                logging.info(
                    "PROFIT LOCK DECISION | symbol=%s action=%s reason=%s",
                    args.symbol,
                    pl_action,
                    pl_reason,
                )

                if pl_action == "close":
                    logging.warning(
                        "PROFIT LOCK CLOSE | symbol=%s reason=%s",
                        args.symbol,
                        pl_reason,
                    )
                    try:
                        entry = float(getattr(active_trade, "entry_price", 0.0) or 0.0)
                        exit_price = float(latest_price)
                        side = str(getattr(active_trade, "side", "")).upper()

                        if side == "LONG":
                            pnl_pct = (exit_price - entry) / max(entry, 1e-9)
                        else:
                            pnl_pct = (entry - exit_price) / max(entry, 1e-9)

                        equity = float(account_equity_usd)
                        risk_engine.update_after_trade(pnl_pct, equity)
                    except Exception as e:
                        logging.error("RISK UPDATE FAILED | %s", str(e))
                    setattr(active_trade, "force_close_reason", pl_reason)
                    continue

                if pe_action == "close":
                    logging.warning("PE CLOSE | symbol=%s reason=%s", args.symbol, pe_reason)
                    setattr(active_trade, "force_close_reason", pe_reason)
                    continue
                elif pe_action == "partial_close":
                    logging.info("PE PARTIAL CLOSE | symbol=%s reason=%s", args.symbol, pe_reason)
                    setattr(active_trade, "partial_close", True)
                elif pe_action == "hold":
                    pass
            except Exception as e:
                logging.error("PE ERROR | %s", str(e))

        blocked_reasons = {
            "daily_loss_stop",
            "max_trades_per_day_reached",
            "spread_too_high",
            "desync_guard",
        }

        # 🔥 FINAL EXECUTION BLOCK (ANTI-XAU FAIL SAFE)
        if str(args.symbol).upper() == "XAUUSD":
            logging.error(
                "EXECUTION HARD BLOCK | symbol=XAUUSD reason=disabled_for_low_balance"
            )
            signal_generated = False
            signal_executable = False
            final_execution_allowed = False

        if str(final_execution_reason) in blocked_reasons:
            final_execution_allowed = False
            logging.warning(
                "ACTIVITY MODE DENIED | hard_guard=%s",
                final_execution_reason,
            )

        # =========================================
        # 🔥 FINAL EXECUTION AUTHORITY FIX
        # =========================================
        if ENABLE_TEST_MODE:
            final_execution_allowed = True

        # also block retry spam after execution failure
        if execution_blocked(args.symbol):
            logging.warning(
                "EXECUTION BLOCKED (COOLDOWN) | symbol=%s",
                args.symbol,
            )
            if not ENABLE_TEST_MODE:
                _sleep()
            continue

        allow_trade = True
        block_reason = None

        # 🔒 HARD BLOCKS (single authority)
        if float(cooldown_remaining) > 0.0:
            allow_trade = False
            block_reason = "cooldown_active"

        if float(min_time_remaining) > 0.0:
            allow_trade = False
            block_reason = "min_time_between_trades_active"

        logging.info(
            "EXECUTION OVERRIDE CHECK | force_top_symbol=%s signal_executable=%s",
            str(force_top_symbol).lower(),
            str(signal_executable).lower(),
        )

        if not signal_executable and not force_top_symbol:
            allow_trade = False
            block_reason = "signal_not_executable"

        if not final_execution_allowed:
            allow_trade = False
            block_reason = "execution_not_allowed"

        if ENABLE_TEST_MODE:
            allow_trade = True
            block_reason = None

        # =========================================
        # 🔥 FIX 2 — HARD FORCE EXECUTION
        # =========================================
        if not allow_trade:
            if FORCE_MODE:
                logging.warning(
                    "FORCE MODE → bypass trade block | reason=%s",
                    str(block_reason),
                )
                allow_trade = True
            else:
                logging.warning(
                    "FINAL EXEC BLOCK | symbol=%s reason=%s",
                    args.symbol,
                    str(block_reason),
                )
                _sleep()
                continue

        # =========================================
        # 🔥 FORCE FINAL EXECUTION GATE (CRITICAL)
        # =========================================
        if not final_execution_allowed:
            if FORCE_MODE:
                logging.warning(
                    "FORCE MODE → bypass final_execution_gate | symbol=%s",
                    args.symbol,
                )
                final_execution_allowed = True
            else:
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=final_execution_gate",
                    args.symbol,
                )
                _sleep()
                continue

        # =========================================
        # 🔥 DEBUG FLOW (ALTIJD ZICHTBAAR)
        # =========================================
        logging.info(
            "DEBUG FLOW | symbol=%s signal=%.4f quality=%.4f allow_trade=%s final_allowed=%s",
            args.symbol,
            float(signal_score),
            float(entry_quality_safe),
            str(allow_trade).lower(),
            str(final_execution_allowed).lower(),
        )
        if int(getattr(risk_state, "open_positions", 0)) >= 1:
            logging.info("ENTRY SKIPPED | already in position")
            continue

        if not signal_generated:
            signal_generated = True
            logging.warning(
                "ENTRY SYNC | symbol=%s generated repaired before build_entry_candidate",
                args.symbol,
            )

        if not signal_executable:
            signal_executable = True
            logging.warning(
                "ENTRY SYNC | symbol=%s executable repaired before build_entry_candidate",
                args.symbol,
            )

        trade = build_entry_candidate(
            1 if str(entry_side).upper() == "LONG" else -1,
            float(arr['close'][-1]),
            latest_index,
            signal_score,
            realized_volatility,
            pd.Timestamp(bars['timestamp'].iloc[-1]).to_pydatetime(),
            'signal_execution',
            churn_pressure=adaptive_filters.profit_churn_pressure(),
        )
        if trade is None:
            logging.error("CRITICAL | TRADE BUILD FAILED | forcing skip with trace")
            logging.warning(
                "ENTRY BUILD FAILED | symbol=%s allowed=%s score=%.4f reason=build_entry_candidate_returned_none",
                args.symbol,
                str(final_execution_allowed).lower(),
                float(signal_score),
            )
            logging.warning("STRICT BLOCK | reason=%s", "trade_build_failed")
            _sleep()
            continue
        trade.entry_volatility = float(realized_volatility)
        trade.entry_spread = float(latest_filter.spread_ratio)
        trade.entry_regime = str(latest_filter.current_market_regime)
        trade.context = {
            "symbol": args.symbol,
            "source": "execution_engine",
            "genome_id": active_genome.genome_id,
            "edge_reason": edge.reason,
            "edge_source": "ict_v3_evo",
        }
        with suppress(Exception):
            trade.source = f"ict_v3:{getattr(edge, 'genome', active_genome.genome_id)}"
        with suppress(Exception):
            trade.edge_genome = str(getattr(edge, "genome", active_genome.genome_id))
        # =========================================
        # ATTACH DATA TO TRADE (EDGE DATASET)
        # =========================================
        try:
            trade.setup_type = current_setup_type
            trade.signal_score = float(signal_score)
            trade.entry_quality = float(entry_quality)
        except Exception:
            pass
        logging.info(
            "TRADE READY | symbol=%s side=%s score=%.4f source=%s",
            _trade_symbol(trade),
            getattr(trade, "side", "UNKNOWN"),
            float(getattr(trade, "signal_score", 0.0)),
            getattr(trade, "source", "unknown"),
        )
        gate_allowed, gate_reason, gate_diagnostics = entry_filter_gate(
            symbol=args.symbol,
            side=str(getattr(trade, "side", "LONG")),
            signal_score=float(signal_score),
            entry_quality=float(entry_quality_safe),
            realized_volatility=float(realized_volatility),
            spread_ratio=float(getattr(latest_filter, "spread_ratio", 0.0)),
            regime=str(getattr(latest_filter, "current_market_regime", "UNKNOWN")),
            recent_bars=bars.tail(30) if isinstance(bars, pd.DataFrame) else None,
            timestamp=loop_now,
        )
        logging.info(
            "ENTRY FILTER GATE | symbol=%s allowed=%s reason=%s diagnostics=%s",
            args.symbol,
            str(gate_allowed).lower(),
            gate_reason,
            gate_diagnostics,
        )
        # =========================================
        # 🔥 GATE NEUTRALIZED (LOG ONLY)
        # =========================================
        gate_allowed = True
        if not gate_allowed:
            if ENABLE_TEST_MODE and TEST_MODE_BYPASS_FILTER_GATE:
                logging.warning(
                    "ENTRY FILTER GATE BYPASS | symbol=%s reason=%s test_mode_bypass=true",
                    args.symbol,
                    gate_reason,
                )
            else:
                _evo2_register_block(evo_engine_state, args.symbol, gate_reason)
                _sleep()
                continue

        trade_executed_this_loop = False
        logging.info(
            'EXECUTION TRIGGERED | symbol=%s signal=%.4f',
            args.symbol,
            signal_score,
        )
        logging.info(
            "ENTRY HANDOFF | symbol=%s generated=%s executable=%s allowed=%s side=%s score=%.4f",
            args.symbol,
            str(signal_generated).lower(),
            str(signal_executable).lower(),
            str(final_execution_allowed).lower(),
            str(getattr(trade, "side", "UNKNOWN")),
            float(signal_score),
        )

        # =========================================
        # live/paper symbol handling
        # =========================================
        trade_symbol = str(args.symbol).upper()
        if not trade_symbol or trade_symbol == "UNKNOWN":
            trade_symbol = str(selected_symbol or args.symbol or "UNKNOWN").upper()

        logging.info(
            "TRADE READY | symbol=%s side=%s score=%.4f source=signal_execution",
            trade_symbol,
            str(proposed_side),
            float(signal_score),
        )

        # =========================================
        # 🔒 LAST DEFENSE (SANITY CHECK)
        # =========================================
        if current_setup_type not in ALLOWED_SETUPS:
            logging.critical(
                "SANITY BLOCK | trade stopped | symbol=%s setup=%s",
                trade_symbol,
                current_setup_type,
            )
            _sleep()
            continue

        # ------------------------------------------
        # LIVE EXECUTION READINESS
        # ------------------------------------------
        enable_trading = ENABLE_TRADING
        live_session_present = live_session is not None
        live_symbol_specs_present = live_symbol_specs is not None
        allow_legacy = _env_bool("ALLOW_LEGACY_MT5_FLOW", "true")
        legacy_mt5_ready = False
        if enable_trading and not live_session_present and allow_legacy:
            legacy_mt5_ready = _legacy_mt5_ready(args.symbol)
        live_execution_enabled = bool(
            LIVE_EXECUTION_ENABLED
            and (
                (live_session_present and live_symbol_specs_present)
                or legacy_mt5_ready
            )
        )
        can_send_live_order = live_execution_enabled

        if IS_PAPER_MODE:
            can_send_live_order = False
            live_session_present = False
            live_symbol_specs_present = False

        logging.info(
            "LIVE READINESS | enable=%s session=%s specs=%s legacy_allowed=%s legacy_ready=%s live_execution_enabled=%s",
            str(can_send_live_order).lower(),
            str(live_session_present).lower(),
            str(live_symbol_specs_present).lower(),
            str(bool(final_execution_allowed)).lower(),
            str(bool(can_send_live_order and final_execution_allowed)).lower(),
            str(bool(LIVE_EXECUTION_ENABLED)).lower(),
        )

        try:
            balance = float(getattr(state, "balance", 10000.0))
        except Exception:
            balance = 10000.0
        entry_price = float(getattr(trade, "entry_price", 0.0))
        trade_side = str(getattr(trade, "side", "")).upper()
        ict_exit_plan = _compute_ict_exit_plan(
            entry_price=float(entry_price),
            side=trade_side,
            latest_filter=latest_filter,
        )
        if ict_exit_plan is None:
            logging.warning(
                "ENTRY BLOCKED | ICT exit plan invalid | symbol=%s side=%s entry=%.5f",
                args.symbol,
                trade_side,
                float(entry_price),
            )
            _sleep()
            continue
        tp_price = float(ict_exit_plan["tp_price"])
        sl_price = float(ict_exit_plan["sl_price"])
        stop_loss_price = float(sl_price)
        logging.info(
            "EXIT PLAN CREATED | symbol=%s side=%s tp=%.5f sl=%.5f rr:%.2f partial_tp=%.5f min_hold=%ds",
            args.symbol,
            trade_side,
            tp_price,
            sl_price,
            float(ict_exit_plan["rr"]),
            float(ict_exit_plan["partial_tp_price"]),
            int(ict_exit_plan["min_hold_seconds"]),
        )
        # =========================================
        # 🔥 BALANCE FIX (REAL SOURCE)
        # =========================================
        try:
            account_info = _mt5_safe("account_info")
            mt5_balance = _safe_float(getattr(account_info, "balance", 0.0))
        except Exception:
            mt5_balance = 0.0
        env_balance = _safe_float(os.getenv("ACCOUNT_EQUITY_USD", "0"), 0.0)
        effective_balance = mt5_balance if mt5_balance > 0 else env_balance
        if effective_balance <= 0:
            effective_balance = float(account_equity_usd)
        account_equity_usd = max(1.0, float(effective_balance))
        logging.info(
            "BALANCE | mt5=%.2f env=%.2f used=%.2f",
            float(mt5_balance),
            float(env_balance),
            float(effective_balance),
        )

        # 🔥 dynamic risk inject
        os.environ["RISK_PER_TRADE"] = str(risk_engine.get_risk())
        position_size = calculate_position_size(
            balance=effective_balance,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            signal_score=float(signal_score),
        )
        if position_size <= 0:
            logging.info("ENTRY BLOCKED | invalid position size")
            _sleep()
            continue
        logging.info(
            "EXECUTION SIZE | symbol=%s size=%.4f",
            trade_symbol,
            float(position_size),
        )

        # =========================================================
        # 🔥 FX MARGIN SAFE PRECHECK (CRITICAL FIX)
        # =========================================================
        try:
            contract_size = float(os.getenv("FX_CONTRACT_SIZE", "100000"))
            leverage = float(os.getenv("DEFAULT_LEVERAGE", "100"))

            notional = float(position_size) * contract_size * float(entry_price)
            required_margin = notional / max(leverage, 1.0)

            if required_margin > float(account_equity_usd) and not ENABLE_TEST_MODE:
                logging.warning(
                    "ENTRY BLOCKED | insufficient_margin_precheck symbol=%s required=%.2f equity=%.2f",
                    args.symbol,
                    required_margin,
                    float(account_equity_usd),
                )

                try:
                    create_virtual_trade(
                        symbol=args.symbol,
                        side=str(entry_side).upper(),
                        entry_price=float(entry_price),
                        sl_price=float(stop_loss_price),
                        tp_price=float(tp_price),
                        signal_score=float(signal_score),
                        entry_quality=float(entry_quality_safe),
                        latest_bar_id=latest_bar_id,
                        reason="insufficient_margin_precheck",
                        setup_type=current_setup_type,
                        now_ts=time.time(),
                        max_hold_seconds=min(
                            float(
                                ict_exit_plan.get(
                                    "min_hold_seconds",
                                    float(os.getenv("VIRTUAL_TIMEOUT_MIN", "0.2")) * 60.0,
                                )
                            )
                            * float(os.getenv("VIRTUAL_HOLD_MULTIPLIER", "10.0")),
                            20.0,
                        ),
                    )
                except Exception as ve:
                    logging.error("VIRTUAL TRADE CREATE ERROR | %s", str(ve))

                register_execution_failure(args.symbol, "insufficient_margin_precheck")
                _sleep()
                continue
        except Exception as e:
            logging.error("MARGIN PRECHECK ERROR | %s", str(e))

        # =========================================================
        # 🔥 SINGLE SOURCE OF TRUTH (SIDE FIX)
        # =========================================================
        final_entry_side = str(entry_side).upper()

        # TEST MODE → skip broker / margin execution constraints
        if ENABLE_TEST_MODE:
            broker_flat = True
            broker_open_positions_count = 0

            logging.warning("TEST MODE EXECUTION | simulated trade opened | symbol=%s side=%s", args.symbol, final_entry_side)
            try:
                create_virtual_trade(
                    symbol=args.symbol,
                    side=final_entry_side,
                    entry_price=float(entry_price),
                    sl_price=float(sl_price),
                    tp_price=float(tp_price),
                    signal_score=float(signal_score),
                    entry_quality=float(entry_quality_safe),
                    latest_bar_id=latest_bar_id,
                    reason="test_mode",
                    setup_type=current_setup_type,
                    now_ts=time.time(),
                    max_hold_seconds=min(
                        float(
                            ict_exit_plan.get(
                                "min_hold_seconds",
                                float(os.getenv("VIRTUAL_TIMEOUT_MIN", "0.2")) * 60.0,
                            )
                        )
                        * float(os.getenv("VIRTUAL_HOLD_MULTIPLIER", "1.0")),
                        20.0,
                    ),
                )
            except Exception as e:
                logging.error("TEST MODE TRADE CREATE FAILED | %s", str(e))
            _sleep()
            continue

        # Do not allow re-entry override when broker already has a live position
        if (int(broker_open_positions_count) > 0 or not broker_flat) and not ENABLE_TEST_MODE:
            logging.warning(
                "REENTRY BLOCKED | symbol=%s broker_flat=%s broker_open_positions=%d",
                args.symbol,
                str(broker_flat).lower(),
                int(broker_open_positions_count),
            )
            _sleep()
            continue
        resolved_symbol = args.symbol
        maybe_resolved = _ensure_mt5_symbol(args.symbol)
        if maybe_resolved:
            resolved_symbol = maybe_resolved
        if _is_fx_symbol(args.symbol):
            max_live_fx_lot = _safe_float(os.getenv("MAX_LIVE_FX_LOT", "0.20"), 0.20)
            if position_size > max_live_fx_lot:
                logging.warning(
                    "FX SIZE CAP | symbol=%s old=%.4f capped=%.4f",
                    args.symbol,
                    float(position_size),
                    float(max_live_fx_lot),
                )
                position_size = float(max_live_fx_lot)

        side = final_entry_side
        confidence = float(signal_score)
        risk_pct = float(os.getenv('RISK_PCT', '0') or 0.0)
        effective_risk_usd = _compute_effective_risk_usd(float(account_equity_usd))
        if float(adjusted_risk_per_trade_usd or 0.0) > 0:
            effective_risk_usd = float(adjusted_risk_per_trade_usd)
        logging.info(
            'POSITION SIZING INPUT | symbol=%s equity=%.2f effective_risk_usd=%.4f',
            trade_symbol,
            float(account_equity_usd),
            float(effective_risk_usd),
        )
        if confidence > 1.5:
            position_scale = 1.25
        elif confidence > 1.0:
            position_scale = 1.0
        elif confidence > 0.8:
            position_scale = 0.75
        else:
            position_scale = 0.5
        symbol_weight = float(allocation_weights.get(args.symbol, 1.0 / max(1, len(allocation_weights) or 1)))
        position_value_usd = float(args.notional_usd) * symbol_weight
        max_allowed_notional = min(
            float(account_equity_usd) * 0.5,
            float(risk_cfg.notional_usd)
        )
        safe_scale = min(
            position_scale,
            max_allowed_notional / max(position_value_usd, 1e-6)
        )
        position_scale = safe_scale
        if is_stacking_entry:
            dynamic_scale = max(0.3, min(0.7, current_profit_pct * 1000))
            position_scale *= dynamic_scale
            logging.info(
                "STACK EXECUTION | count=%d profit=%.5f scale=%.2f",
                int(getattr(active_trade, "stack_count", 0)),
                float(current_profit_pct),
                float(position_scale),
            )
        effective_notional_usd = position_value_usd * position_scale
        logging.info("CAPITAL ALLOCATION | symbol=%s weight=%.4f risk=%.6f", args.symbol, symbol_weight, effective_notional_usd)
        logging.info(
            "EXECUTION PATH | status=attempting symbol=%s score=%.4f signal=%.4f",
            trade_symbol,
            signal_score,
            float(getattr(latest_filter, 'signal_strength', signal_score)),
        )
        logging.info(
            "EXECUTION TRACE PRE-LIVE | symbol=%s side=%s score=%.4f quality=%.4f position_scale=%.4f",
            trade_symbol,
            str(entry_side).upper(),
            float(signal_score),
            float(entry_quality_safe),
            float(position_scale if 'position_scale' in locals() else 1.0),
        )

        logging.info(
            'EXECUTION PATH | status=attempting side=%s signal_score=%.4f signal_priority=%s effective_notional_usd=%.6f position_value_usd=%.6f position_scale=%.4f latest_bar_id=%s live_execution_enabled=%s live_session_present=%s live_symbol_specs_present=%s risk_open_positions=%d can_open_new_position=%s can_open_reason=%s cooldown_seconds=%d pause_active=%s final_filter_passed=%s signal_generated=%s signal_executable=%s',
            side,
            signal_score,
            signal_priority,
            effective_notional_usd,
            effective_notional_usd,
            position_scale,
            latest_bar_id or 'none',
            str(live_execution_enabled).lower(),
            str(live_session_present).lower(),
            str(live_symbol_specs_present).lower(),
            risk_state.open_positions,
            str(ok).lower(),
            reason,
            effective_cooldown,
            str(risk_state.trading_paused_until is not None and loop_now < risk_state.trading_paused_until).lower(),
            str(final_filter_passed).lower(),
            str(signal_generated).lower(),
            str(signal_executable).lower(),
        )
        executed_entry_price = float(trade.entry_price)
        exchange_order_result: LiveOrderResult | None = None
        # Extra guard against low-edge entries in live mode.
        live_min_signal = float(os.getenv("LIVE_MIN_SIGNAL", "0.85") or 0.85)
        live_min_quality = float(os.getenv("LIVE_MIN_QUALITY", "0.88") or 0.88)

        # =========================================================
        # Harmonized final gate
        # =========================================================
        if IS_LIVE:
            final_required_signal = float(os.getenv("FINAL_MIN_SIGNAL", "0.90") or 0.90)
            final_required_quality = float(os.getenv("FINAL_MIN_QUALITY", "0.90") or 0.90)
        elif DISCOVERY_MODE:
            final_required_signal = float(os.getenv("DISCOVERY_FINAL_MIN_SIGNAL", "0.45") or 0.45)
            final_required_quality = float(os.getenv("DISCOVERY_FINAL_MIN_QUALITY", "0.60") or 0.60)
        else:
            final_required_signal = float(os.getenv("TEST_FINAL_MIN_SIGNAL", "0.50") or 0.50)
            final_required_quality = float(os.getenv("TEST_FINAL_MIN_QUALITY", "0.65") or 0.65)

        if float(signal_score) < final_required_signal or float(entry_quality_safe) < final_required_quality:
            logging.warning(
                "FINAL BLOCK ACTIVE | symbol=%s signal=%.4f required_signal=%.4f quality=%.4f required_quality=%.4f",
                args.symbol,
                float(signal_score),
                float(final_required_signal),
                float(entry_quality_safe),
                float(final_required_quality),
            )
            _sleep()
            continue

        logging.info(
            "FINAL GATE PASS | symbol=%s signal=%.4f quality=%.4f required_signal=%.4f required_quality=%.4f",
            args.symbol,
            float(signal_score),
            float(entry_quality_safe),
            float(final_required_signal),
            float(final_required_quality),
        )

        # =========================================
        # LIVE BLOCKS
        # =========================================

        disable_live_block = str(os.getenv("DISABLE_LIVE_ENTRY_BLOCK", "false")).lower() in ("1", "true", "yes")
        disable_xau_override = str(os.getenv("DISABLE_XAU_OVERRIDE", "true")).lower() in ("1", "true", "yes")

        if IS_LIVE:
            logging.info(
                "LIVE FILTER CHECK | symbol=%s signal=%.4f/%.2f quality=%.4f/%.2f",
                args.symbol,
                float(signal_score),
                float(live_min_signal),
                float(entry_quality_safe),
                float(live_min_quality),
            )
            if float(signal_score) < live_min_signal:
                logging.warning("LIVE HARD BLOCK | symbol=%s reason=signal_too_low", args.symbol)
                continue
            if float(entry_quality_safe) < 0.75:
                logging.warning("LIVE HARD BLOCK | symbol=%s reason=quality_too_low", args.symbol)
                continue
            try:
                trade.position_scale = min(float(getattr(trade, "position_scale", 1.0)), MAX_LIVE_RISK)
            except Exception:
                trade.position_scale = MAX_LIVE_RISK

        if final_execution_allowed:
            # ✅ STATE LOCK (prevent reset loop)
            risk_state.active_trade = True
            state.last_entry_commit_timestamp = now_ts
            state.last_close_timestamp = 0.0
            last_entry_ts = now_ts
            last_close_ts = 0.0
            just_committed_trade = True

            logging.info(
                "TRADE EXECUTED | symbol=%s score=%.4f setup=%s",
                trade_symbol,
                float(signal_score),
                current_setup_type,
            )

        if can_send_live_order and not disable_live_block:
            # Optional XAU override (disabled by default)
            if str(args.symbol).upper() == "XAUUSD" and not disable_xau_override:
                live_min_signal = max(live_min_signal, float(os.getenv("XAU_MIN_SIGNAL", "0.70") or 0.70))
                live_min_quality = max(live_min_quality, float(os.getenv("XAU_MIN_QUALITY", "0.70") or 0.70))

            block_live = False
            block_reason = None

            if float(signal_score) < live_min_signal:
                block_live = True
                block_reason = "signal_too_low"

            if float(entry_quality_safe) < live_min_quality:
                block_live = True
                block_reason = "quality_too_low"

            if block_live:
                logging.warning("LIVE ENTRY BLOCK ACTIVE | symbol=%s", args.symbol)
                _evo2_register_block(evo_engine_state, args.symbol, block_reason)
                _sleep()
                continue
        elif disable_live_block:
            logging.warning("LIVE ENTRY BLOCK DISABLED -> pass | symbol=%s", args.symbol)
        if live_session_present:
            if int(getattr(risk_state, "open_positions", 0)) > 0:
                logging.error(
                    "LIVE ENTRY BLOCK | symbol=%s reason=internal_position_exists open_positions=%d",
                    args.symbol,
                    int(getattr(risk_state, "open_positions", 0)),
                )
                _sleep()
                continue

            if _live_broker_position_exists(live_session, args.symbol):
                logging.error(
                    "LIVE ENTRY BLOCK | symbol=%s reason=broker_position_exists",
                    args.symbol,
                )
                _sleep()
                continue

            broker_positions = count_symbol_open_positions(live_session, args.symbol)
            if int(broker_positions) != int(getattr(risk_state, 'open_positions', 0)):
                logging.warning("DESYNC DETECTED | forcing resync | symbol=%s", args.symbol)
                exchange_position_for_sync = fetch_open_position(live_session, args.symbol)
                perform_hard_position_sync(
                    symbol=args.symbol,
                    state=state,
                    risk_state=risk_state,
                    broker_positions=int(broker_positions),
                    exchange_position=exchange_position_for_sync if exchange_position_for_sync.is_open else None,
                    now=loop_now,
                )
                active_trade = state.active_trade
            if live_symbol_specs is None:
                live_safety_controller.activate_kill_switch('missing_symbol_specs')
                logging.info('EXECUTION PATH | status=blocked reason=symbol_specs_unavailable live_session_present=true')
                _sleep()
                continue
            max_position_value_usd = float(live_safety_controller.max_position_value_usd)
            if effective_notional_usd > max_position_value_usd:
                logging.warning(
                    "SOFT LIMIT | reducing position size | requested=%.2f max=%.2f",
                    effective_notional_usd,
                    max_position_value_usd,
                )
                scale_factor = max_position_value_usd / max(effective_notional_usd, 1e-9)
                position_scale *= scale_factor
                effective_notional_usd = max_position_value_usd
            if position_scale < 0.05:
                logging.warning("MIN SCALE FLOOR ACTIVATED")
                position_scale = 0.05
            if signal_executable:
                assert position_scale > 0, "Position scale must not be zero"
            logging.info('EXECUTION TRACE PRE-LIVE | signal_score=%.4f signal_priority=%s side=%s effective_notional_usd=%.6f position_value_usd=%.6f position_scale=%.4f latest_bar_id=%s live_execution_enabled=%s live_session_present=%s live_symbol_specs_present=%s risk_open_positions=%d can_open_new_position=%s can_open_reason=%s cooldown_seconds=%d pause_active=%s final_filter_passed=%s signal_generated=%s signal_executable=%s', signal_score, signal_priority, side, effective_notional_usd, effective_notional_usd, position_scale, latest_bar_id or 'none', str(live_execution_enabled).lower(), 'true', str(live_symbol_specs is not None).lower(), risk_state.open_positions, str(ok).lower(), reason, effective_cooldown, str(risk_state.trading_paused_until is not None and loop_now < risk_state.trading_paused_until).lower(), str(final_filter_passed).lower(), str(signal_generated).lower(), str(signal_executable).lower())
            exchange_order_result, post_entry_position, safe_qty = execute_live_entry_flow(
                session=live_session,
                symbol=args.symbol,
                trade=trade,
                position_value_usd=effective_notional_usd,
                risk_per_trade_usd=adjusted_risk_per_trade_usd or None,
                risk_pct=risk_pct or None,
                account_equity_usd=account_equity_usd or None,
                position_scale=position_scale,
                specs=live_symbol_specs,
                risk_state=risk_state,
                safety_controller=live_safety_controller,
                trailing_policy=live_trailing_policy,
                latest_bar_id=latest_bar_id,
            )
            if exchange_order_result is None:
                logging.error("CRITICAL | LIVE EXECUTION FAILED | result=None")
                try:
                    create_virtual_trade(
                        symbol=args.symbol,
                        side=str(side).upper(),
                        entry_price=float(entry_price),
                        sl_price=float(stop_loss_price),
                        tp_price=float(tp_price),
                        signal_score=float(signal_score),
                        entry_quality=float(entry_quality_safe),
                        latest_bar_id=latest_bar_id,
                        reason="exchange_result_none",
                        setup_type=current_setup_type,
                        now_ts=time.time(),
                        max_hold_seconds=min(
                            float(
                                ict_exit_plan.get(
                                    "min_hold_seconds",
                                    float(os.getenv("VIRTUAL_TIMEOUT_MIN", "0.2")) * 60.0,
                                )
                            )
                            * float(os.getenv("VIRTUAL_HOLD_MULTIPLIER", "10.0")),
                            20.0,
                        ),
                    )
                except Exception as ve:
                    logging.error(
                        "VIRTUAL TRADE CREATE ERROR | symbol=%s err=%s",
                        args.symbol,
                        str(ve),
                    )
                # 🔥 register failure → cooldown
                register_execution_failure(args.symbol, "exchange_result_none")
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=%s score=%.4f positions=%d",
                    args.symbol,
                    "exchange_order_result_is_none",
                    float(signal_score),
                    int(getattr(risk_state, "open_positions", 0)),
                )
                logging.info('EXECUTION PATH | status=dropped reason=exchange_order_result_is_none')
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=%s score=%.4f positions=%d",
                    args.symbol,
                    "execute_live_entry_flow_returned_none",
                    float(signal_score),
                    int(getattr(risk_state, "open_positions", 0)),
                )
                logging.info('EXECUTION PATH | status=dropped reason=execute_live_entry_flow_returned_none')
                _sleep()
                continue
            logging.info(
                "EXECUTION RESULT | success=%s order_id=%s reason=%s",
                str(bool(getattr(exchange_order_result, 'success', False))).lower(),
                str(getattr(exchange_order_result, 'order_id', 'none')),
                str(getattr(exchange_order_result, 'reason', 'none')),
            )

            if post_entry_position is None:
                logging.error("CRITICAL | POSITION NOT CREATED AFTER EXECUTION")
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=%s score=%.4f positions=%d",
                    args.symbol,
                    "post_entry_position_is_none",
                    float(signal_score),
                    int(getattr(risk_state, "open_positions", 0)),
                )
                logging.info('EXECUTION PATH | status=dropped reason=post_entry_position_is_none')
                _sleep()
                continue
            if safe_qty is None:
                logging.error(
                    "EXECUTION DROP TRACE | symbol=%s reason=%s score=%.4f positions=%d",
                    args.symbol,
                    "safe_qty_is_none",
                    float(signal_score),
                    int(getattr(risk_state, "open_positions", 0)),
                )
                logging.info('EXECUTION PATH | status=dropped reason=safe_qty_is_none')
                _sleep()
                continue
            if live_safety_controller.live_trading_halted:
                logging.info('EXECUTION PATH | status=blocked reason=live_trading_halted_became_true halt_reason=%s', live_safety_controller.halt_reason)
                _sleep()
                continue
            logging.info('EXECUTION PATH | status=submitted order_id=%s qty=%.12f', exchange_order_result.order_id or 'none', exchange_order_result.qty)
            executed_entry_price = float(trade.entry_price)
            live_position_qty = post_entry_position.qty
            live_position_idx = post_entry_position.position_idx
            logging.warning(
                "REAL TRADE OPENED | symbol=%s side=%s qty=%.4f price=%.5f",
                args.symbol,
                side,
                float(safe_qty),
                float(executed_entry_price),
            )
        elif enable_trading and not live_session_present and allow_legacy:
            try:
                if not mt5.initialize():
                    logging.error("MT5 REINIT FAILED")
                    _sleep()
                    continue

                resolved_symbol = _ensure_mt5_symbol(args.symbol)
                if not resolved_symbol:
                    logging.error("LEGACY LIVE BLOCK | symbol not ready | %s", args.symbol)
                    _sleep()
                    continue

                order_side = "LONG" if str(side).upper() == "LONG" else "SHORT"

                tick = mt5.symbol_info_tick(resolved_symbol)
                if tick is None:
                    logging.error("LEGACY LIVE BLOCK | no tick | %s", resolved_symbol)
                    _sleep()
                    continue

                live_price = float(tick.ask) if order_side == "LONG" else float(tick.bid)
                if live_price <= 0:
                    logging.error("LEGACY LIVE BLOCK | invalid live price | %s", resolved_symbol)
                    _sleep()
                    continue

                sl_price = float(stop_loss_price)
                tp_price = float(ict_exit_plan["tp_price"])

                logging.info(
                    "LEGACY ORDER ROUTE | requested=%s resolved=%s side=%s volume=%.4f price=%.5f",
                    args.symbol,
                    resolved_symbol,
                    order_side,
                    float(position_size),
                    float(live_price),
                )

                broker_order_result = broker_adapter.place_market_order(
                    symbol=resolved_symbol,
                    side=order_side,
                    qty=float(position_size),
                    price=float(live_price),
                    sl=float(sl_price),
                    tp=float(tp_price),
                    deviation=int(_safe_int(os.getenv("MT5_DEVIATION", "20"), 20)),
                    comment=os.getenv("MT5_ORDER_COMMENT", "LIVE BOT"),
                )

                if not bool(getattr(broker_order_result, "success", False)):
                    logging.error(
                        "LEGACY ORDER FAILED | symbol=%s side=%s reason=%s raw=%s",
                        resolved_symbol,
                        order_side,
                        str(getattr(broker_order_result, "reason", "unknown")),
                        str(getattr(broker_order_result, "raw", {})),
                    )
                    _sleep()
                    continue

                executed_entry_price = float(
                    getattr(broker_order_result, "avg_price", 0.0) or live_price
                )
                trade_executed_this_loop = True
                logging.warning(
                    "REAL TRADE OPENED | symbol=%s side=%s qty=%.4f price=%.5f order_id=%s",
                    resolved_symbol,
                    order_side,
                    float(getattr(broker_order_result, "qty", position_size)),
                    float(executed_entry_price),
                    str(getattr(broker_order_result, "order_id", "none")),
                )
            except Exception as e:
                logging.error("LEGACY ORDER ERROR | symbol=%s err=%s", args.symbol, str(e))
                _sleep()
                continue
        elif enable_trading:
            logging.error('LIVE EXECUTION REQUESTED | session_unavailable=true no_state_commit')
            logging.info('EXECUTION PATH | status=blocked reason=live_session_unavailable live_execution_enabled=true')
            _sleep()
            continue

        execution_happened = False
        try:
            execution_happened = bool(executed)
        except Exception:
            execution_happened = bool(signal_executable)

        if exchange_order_result is not None:
            if bool(getattr(exchange_order_result, "success", False)):
                trade_executed_this_loop = True

        if trade_executed_this_loop:
            state.no_trade_loops = 0
        else:
            state.no_trade_loops += 1

        state.no_trade_loops = min(state.no_trade_loops, 50)
        logging.info(
            "NO TRADE TRACK | loops=%d executed=%s",
            state.no_trade_loops,
            str(bool(execution_decision.allowed)).lower(),
        )

        adaptive_filters.commit_execution(execution_decision)
        adaptive_filters.record_signal(signal_generated, signal_executable, True, signal_score=signal_score)
        register_entry(risk_state, when=loop_now, symbol=args.symbol)
        state.no_trade_loops = 0

        # =========================================
        # 🔥 FIX 3 — FINAL SAFETY CHECK
        # =========================================
        if not allow_trade:
            logging.warning(
                "EXECUTION CANCELLED | symbol=%s reason=final_block",
                args.symbol,
            )
            continue
        logging.info(
            "EXECUTION COMMITTED | symbol=%s setup=%s score=%.4f quality=%.4f",
            args.symbol,
            current_setup_type,
            float(signal_score),
            float(entry_quality),
        )
        last_trade_timestamp = time.time()
        state.last_entry_commit_timestamp = time.time()
        selected_symbol_memory.append(args.symbol.upper())
        logging.info(
            "ROTATION MEMORY | selected=%s size=%d recent=%s",
            args.symbol,
            len(selected_symbol_memory),
            list(selected_symbol_memory)[-10:],
        )
        logging.info('EXECUTION PATH | status=committed side=%s signal_score=%.4f position_scale=%.4f', side, signal_score, position_scale)
        risk_state.same_bar_entry_allowed = False
        adaptive_filters.last_entry_side = side
        adaptive_filters.last_entry_price = float(executed_entry_price)
        adaptive_filters.last_entry_at = loop_now
        adaptive_filters.last_entry_bar_id = latest_bar_id
        if is_stacking_entry and active_trade is not None:
            trade.stack_count = int(getattr(active_trade, "stack_count", 0)) + 1
        else:
            trade.stack_count = int(getattr(active_trade, "stack_count", 0)) if active_trade else 0
        active_trade = trade

        active_position_scale = execution_decision.position_scale

        # =========================================
        # 🔥 INIT PROFIT LOCK STATE ON NEW TRADE
        # =========================================
        try:
            active_trade.symbol = str(args.symbol).upper()
            active_trade.side = str(getattr(active_trade, "side", trade_side)).upper()
            active_trade.entry_price = float(getattr(active_trade, "entry_price", entry_price) or entry_price)
            active_trade.tp_price = float(tp_price)
            active_trade.sl_price = float(sl_price)
            active_trade.partial_tp_price = float(ict_exit_plan["partial_tp_price"])
            active_trade.partial_fraction = float(ict_exit_plan["partial_fraction"])
            active_trade.trailing_activation_price = float(ict_exit_plan["trailing_activation_price"])
            active_trade.min_hold_seconds = int(ict_exit_plan["min_hold_seconds"])
            active_trade.noise_spread_mult = float(ict_exit_plan["noise_spread_mult"])
            active_trade.use_structure_trailing = bool(ict_exit_plan["use_structure_trailing"])
            active_trade.partial_taken = False
            active_trade.runner = False
            if getattr(active_trade, "entry_time", None) is None:
                active_trade.entry_time = _safe_float(state.last_entry_commit_timestamp, 0.0)
            logging.info(
                "ENTRY TIME SET | symbol=%s entry_time=%.6f",
                args.symbol,
                float(active_trade.entry_time),
            )
            active_trade.peak_profit_pct = 0.0
            active_trade.break_even_armed = False
            active_trade.profit_lock_armed = False
            active_trade.trailing_armed = False
            active_trade.profit_floor_pct = 0.0
            active_trade.trailing_stop_pct = None
            logging.info(
                "ICT TRADE ARMED | symbol=%s side=%s entry=%.5f tp=%.5f sl=%.5f partial_tp=%.5f",
                args.symbol,
                str(active_trade.side).upper(),
                float(active_trade.entry_price),
                float(active_trade.tp_price),
                float(active_trade.sl_price),
                float(active_trade.partial_tp_price),
            )
            logging.info("PROFIT LOCK INIT | symbol=%s side=%s", active_trade.symbol, active_trade.side)
        except Exception as e:
            logging.error("PROFIT LOCK INIT ERROR | %s", str(e))

        if live_session is not None and post_entry_position is not None and live_symbol_specs is not None:
            leverage_for_commit = float(os.getenv("ACCOUNT_LEVERAGE", "100") or 100.0)
            commit_metrics = compute_live_position_metrics(
                symbol=args.symbol,
                qty=float(post_entry_position.qty),
                entry_price=float(post_entry_position.entry_price),
                specs=live_symbol_specs,
                leverage=leverage_for_commit,
            )
            active_notional_usd = float(commit_metrics.notional_value_usd)
            logging.info(
                "COMMIT METRICS | symbol=%s qty=%.4f notional=%.2f",
                args.symbol,
                float(commit_metrics.qty),
                float(commit_metrics.notional_value_usd),
            )
        else:
            active_notional_usd = effective_notional_usd
        no_trade_snapshot_for_active_trade = no_trade_snapshot
        logging.info(
            'LIVE ENTRY | side=%s qty=%.12f price=%.6f reason=entry_committed signal_score=%.4f exit_tier=%s tp_pct=%.5f sl_pct=%.5f trailing_activation_pct=%.5f trailing_offset_pct=%.5f max_hold_seconds=%.1f volatility_factor=%.3f executed_scale=%.4f',
            side,
            live_position_qty if live_session is not None else 0.0,
            executed_entry_price,
            signal_score,
            trade.exit_tier,
            trade.tp_pct,
            trade.sl_pct,
            trade.trailing_activation_pct,
            trade.trailing_offset_pct,
            trade.max_hold_seconds,
            trade.volatility_factor,
            position_scale,
        )
        logging.info(
            'Execution committed | executed=true source=%s reason=%s position_scale=%.4f live=%s exchange_qty=%s',
            trade.source,
            execution_decision.reason,
            execution_decision.position_scale,
            str(live_session is not None).lower(),
            'none' if exchange_order_result is None else f'{exchange_order_result.qty:.12f}',
        )
        logging.info('Signal decision | score=%.4f cooldown=%d spread=%.6f volume=%.4f action=executed priority=%s source=%s', signal_score, effective_cooldown, latest_filter.spread_ratio, latest_filter.relative_volume, signal_priority, trade.source)
        evolution_engine.update(
            signal_generated=signal_generated,
            signal_executed=True,
            signal_filtered_reason='executed',
            pnl=loop_pnl_delta,
            volatility=realized_volatility,
            signal_score=signal_score,
            symbol=args.symbol,
            trend_strength=filter_trend_strength,
        )
        loop_count_without_trade = 0
        adaptive_threshold_relax = 0.0
        state.adaptive_threshold_relax = adaptive_threshold_relax
        logging.info(
            "SMART RELAX RESET | symbol=%s",
            args.symbol,
        )
        last_execution_index = latest_index
        last_trade_signature = trade_signature
        state.active_trade = active_trade
        state.active_position_scale = active_position_scale
        state.active_notional_usd = active_notional_usd
        state.loop_count_without_trade = loop_count_without_trade
        state.adaptive_threshold_relax = adaptive_threshold_relax
        state.last_trade_signature = last_trade_signature
        state.last_execution_index = last_execution_index
        state.no_trade_snapshot_for_active_trade = no_trade_snapshot_for_active_trade
        state.reversal_pending_close = False
        try:
            evo_register_entry(evo_engine_state, args.symbol, meta={"reason": "entry"})
            save_evo_state(_evo2_state_path(), evo_engine_state)
        except Exception as exc:
            logging.warning(
                "EVO ENTRY UPDATE FAILED | symbol=%s error=%s",
                args.symbol,
                str(exc),
            )

        _reset_profit_engine_state(state, latest_index)
        logging.info(
            "PROFIT ENGINE RESET | symbol=%s entry_loop=%d",
            args.symbol,
            int(latest_index),
        )
        _sleep()


if __name__ == '__main__':
    main()
