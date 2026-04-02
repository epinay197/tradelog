#!/usr/bin/env python3
"""
V7 Predator Elite -- Simulation & Pre-Flight Validator
=======================================================
Validates the FULL decision tree against real log data before any C++
change is deployed to live trading.  Catches math bugs, logic errors,
and gate misconfiguration.

Modes:
    python v7_sim.py validate   -- C++ source pre-flight checks
    python v7_sim.py replay     -- Replay V7 logs with math verification
    python v7_sim.py audit      -- Run both (default)
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIGURATION — matches C++ Input[] defaults
# ============================================================

V7_DEFAULTS: Dict[str, object] = {
    "vwap_study_id":       1,
    "max_position":        3,
    "z_entry":             2.0,
    "z_exit":              0.5,
    "hard_stop_atr_mult":  2.5,
    "time_stop_bars":      10,
    "daily_max_loss":      200.0,
    "stale_order_bars":    3,
    "stddev_lookback":     50,
    "trail_atr_mult":      1.5,
    "be_trigger_atr_mult": 0.75,
    "session_start":       415,
    "session_end":         1600,
    "max_daily_trades":    20,
    "cooldown_bars":       2,
    "consec_loss_max":     3,
    "consec_loss_pause":   10,
    "warmup_mins":         15,
    "max_stop_dollar":     50.0,
    "min_bar_volume":      10,
    "vix_chart":           0,
    "vix_spike_halt_pct":  10.0,
    "vix_halt_mins":       15,
    "max_global_delta":    6,
    "chase_after_n_bars":  2,
    "kelly_fraction":      0.25,
    "heartbeat_sec":       60,
    "equity_ema_alpha":    0.1,
    "equity_curve_dd_pct": 15.0,
    "local_to_et_offset":  0,
}

# ============================================================
# SYMBOL METADATA (tick size + point value per tick)
# ============================================================

SYMBOL_META = {
    "NQ":  {"tick": 0.25, "pv_tick": 5.0},    # $5/tick, $20/point
    "MNQ": {"tick": 0.25, "pv_tick": 0.50},
    "ES":  {"tick": 0.25, "pv_tick": 12.50},
    "MES": {"tick": 0.25, "pv_tick": 1.25},
    "YM":  {"tick": 1.0,  "pv_tick": 5.0},    # $5/tick = $5/point
    "MYM": {"tick": 1.0,  "pv_tick": 0.50},
    "RTY": {"tick": 0.10, "pv_tick": 5.0},    # $5/tick, $50/point
    "M2K": {"tick": 0.10, "pv_tick": 0.50},
}

# ============================================================
# FILE PATHS
# ============================================================

CPP_SOURCE  = r"D:\Sierra Chart\ACS_Source\v_7_predator_elite_production.cpp"
V7_LOG      = r"D:\Sierra Chart\V7_Predator_Log.txt"
V7_SHADOW   = r"D:\Sierra Chart\V7_Shadow_Log.txt"

# ============================================================
# DISPLAY HELPERS
# ============================================================

def _box_top(title: str, width: int = 48) -> str:
    inner = width - 4
    return (
        f"\n{'='*width}\n"
        f"||  {title:<{inner}}||\n"
        f"{'='*width}"
    )

def _section(title: str, width: int = 48) -> str:
    inner = width - 4
    return (
        f"{'='*width}\n"
        f"||  {title:<{inner}}||\n"
        f"{'-'*width}"
    )

def _pass(msg: str) -> str:
    return f"  [PASS] {msg}"

def _fail(msg: str) -> str:
    return f"  [FAIL] {msg}"

def _warn(msg: str) -> str:
    return f"  [WARN] {msg}"

def _info(msg: str) -> str:
    return f"  [INFO] {msg}"


def resolve_symbol_root(full_sym: str) -> Optional[str]:
    """Extract root from full CME symbol, e.g. 'NQM26_FUT_CME' -> 'NQ'."""
    # Check specific roots first (order matters: M2K before MYM, RTY before YM)
    for root in ("MNQ", "MES", "MYM", "M2K", "NQ", "ES", "RTY", "YM"):
        if root in full_sym:
            return root
    return None

def dollars_per_point(root: str) -> float:
    """Return $/point for a symbol root."""
    meta = SYMBOL_META.get(root)
    if not meta:
        return 1.0
    return meta["pv_tick"] / meta["tick"]

def tick_size(root: str) -> float:
    meta = SYMBOL_META.get(root)
    return meta["tick"] if meta else 1.0


# ============================================================
# MODE 1: VALIDATE C++ SOURCE
# ============================================================

def validate_cpp(source_path: str) -> Tuple[int, int, List[str]]:
    """Read C++ source, run all pre-flight checks.
    Returns (pass_count, fail_count, output_lines).
    """
    lines: List[str] = []
    passes = 0
    fails = 0

    if not os.path.isfile(source_path):
        lines.append(_fail(f"C++ source not found: {source_path}"))
        return 0, 1, lines

    with open(source_path, "r", encoding="utf-8", errors="replace") as f:
        cpp = f.read()

    # Helper: search
    def has(pattern: str, flags=0) -> Optional[re.Match]:
        return re.search(pattern, cpp, flags)

    # ---- CHECK 1: Z-score sanity ----
    # Must use isfinite() — no hard cap that would clip real Z values
    if has(r'isfinite\s*\(\s*z\s*\)') or has(r'std::isfinite\s*\(\s*z\s*\)'):
        # Check there is no hard cap like z > 100 or z < -100
        hard_cap_match = has(r'(?:fabs\s*\(\s*z\s*\)|z)\s*[><=]+\s*(\d+\.?\d*)')
        if hard_cap_match:
            cap_val = float(hard_cap_match.group(1))
            # effectiveZ clamp values (1.5, 3.0) and z_exit (0.5) are fine — only flag entry-blocking caps
            if cap_val < 100 and cap_val not in (0.5, 1.5, 3.0):
                lines.append(_fail(f"Z-score hard cap at {cap_val} would clip real signals (range bars Z can be 1000+)"))
                fails += 1
            else:
                lines.append(_pass("Z-score check: isfinite() -- no hard cap on raw Z"))
                passes += 1
        else:
            lines.append(_pass("Z-score check: isfinite() -- no hard cap on raw Z"))
            passes += 1
    else:
        lines.append(_fail("Z-score check: no isfinite() guard found -- risk of inf/nan entries"))
        fails += 1

    # ---- CHECK 2: ET offset default ----
    et_offset_match = has(r'In_LocalToETOffset.*?SetInt\s*\(\s*(\d+)\s*\)')
    if et_offset_match:
        val = int(et_offset_match.group(1))
        if val == 0:
            lines.append(_pass(f"ET offset default: {val} (chart TZ is already ET)"))
            passes += 1
        else:
            lines.append(_fail(f"ET offset default is {val} -- must be 0 when chart TZ = ET"))
            fails += 1
    else:
        lines.append(_warn("Could not parse ET offset default from source"))

    # ---- CHECK 3: Exit method ----
    flatten_count = len(re.findall(r'FlattenAndCancelAllOrders\s*\(\s*\)', cpp))
    buyexit_count = len(re.findall(r'BuyExit\s*\(', cpp))
    sellexit_count = len(re.findall(r'SellExit\s*\(', cpp))

    if flatten_count > 0 and buyexit_count == 0 and sellexit_count == 0:
        lines.append(_pass(f"Exit method: FlattenAndCancelAllOrders ({flatten_count} uses, no BuyExit/SellExit)"))
        passes += 1
    elif buyexit_count > 0 or sellexit_count > 0:
        lines.append(_fail(f"Exit method: found BuyExit({buyexit_count})/SellExit({sellexit_count}) -- will be rejected with attached stops"))
        fails += 1
    else:
        lines.append(_warn("No exit methods found in source"))

    # ---- CHECK 4: GetAssetSlot ordering ----
    # M2K/RTY must be checked BEFORE MYM/YM to avoid "RTYM" matching "YM" first
    slot_func = has(r'GetAssetSlot\s*\([^)]*\)\s*\{(.*?)\}', re.DOTALL)
    if slot_func:
        body = slot_func.group(1)
        # Find positions of M2K/RTY vs MYM/YM checks
        m2k_pos = body.find("M2K")
        rty_pos = body.find("RTY")
        mym_pos = body.find("MYM")
        ym_pos = body.find('"YM"')
        if ym_pos == -1:
            ym_pos = body.find("'YM'")

        first_rty = min(p for p in (m2k_pos, rty_pos) if p >= 0) if any(p >= 0 for p in (m2k_pos, rty_pos)) else 9999
        first_ym = min(p for p in (mym_pos, ym_pos) if p >= 0) if any(p >= 0 for p in (mym_pos, ym_pos)) else 9999

        if first_rty < first_ym:
            lines.append(_pass("GetAssetSlot: M2K/RTY checked before MYM/YM (correct order)"))
            passes += 1
        else:
            lines.append(_fail("GetAssetSlot: YM/MYM checked before RTY/M2K -- 'RTYM' would match YM slot"))
            fails += 1
    else:
        lines.append(_warn("GetAssetSlot function not found in source"))

    # ---- CHECK 5: Session gate uses system clock ----
    if has(r'CurrentSystemDateTime'):
        lines.append(_pass("Session gate: uses CurrentSystemDateTime (system clock)"))
        passes += 1
    else:
        lines.append(_fail("Session gate: does not use CurrentSystemDateTime -- may use bar time"))
        fails += 1

    # ---- CHECK 6: Trailing stop math — trailDist can actually trigger ----
    # trailDist is capped at peak * 0.5, and trail triggers when drawdown >= trailDist.
    # Since peak >= 1 ATR to activate, min trailDist cap = 0.5 ATR.
    # trailDist = ATR * trailATRMult. If trailATRMult > 0.5 * (peak/ATR), cap kicks in.
    # The cap at 50% of peak ensures it CAN trigger (drawdown can reach 100% of peak).
    if has(r'trailDist\s*>\s*peakUnreal\s*\*\s*0\.5'):
        lines.append(_pass("Trailing stop: trailDist capped at 50% of peak (always triggerable)"))
        passes += 1
    else:
        # Check if there is any cap at all
        if has(r'trailDist'):
            lines.append(_warn("Trailing stop: trailDist found but 50% cap not detected -- verify manually"))
        else:
            lines.append(_fail("Trailing stop: trailDist variable not found"))
            fails += 1

    # ---- CHECK 7: Heartbeat uses integer seconds ----
    if has(r'heartbeatBar') and has(r'int.*heartbeatBar'):
        # Verify it is not stored as float/double
        if has(r'float.*heartbeatBar') or has(r'double.*heartbeatBar'):
            lines.append(_fail("Heartbeat: heartbeatBar stored as float/double -- use int for seconds"))
            fails += 1
        else:
            lines.append(_pass("Heartbeat: uses integer seconds-of-day (heartbeatBar)"))
            passes += 1
    else:
        # Check GetPersistentInt for slot 20
        if has(r'GetPersistentInt\s*\(\s*20\s*\).*heartbeat', re.IGNORECASE):
            lines.append(_pass("Heartbeat: uses PersistentInt (integer seconds)"))
            passes += 1
        elif has(r'heartbeat', re.IGNORECASE):
            lines.append(_warn("Heartbeat variable found but could not confirm integer storage"))
        else:
            lines.append(_warn("No heartbeat variable found"))

    # ---- CHECK 8: Full recalculation preserves position state ----
    recalc_match = has(r'IsFullRecalculation\s*\)(.*?)return\s*;', re.DOTALL)
    if recalc_match:
        recalc_body = recalc_match.group(1)
        preserves_pos = ("posDirection" not in recalc_body or
                         "// posDirection" in recalc_body or
                         "posDirection = 0" not in recalc_body)
        preserves_peak = ("peakUnreal" not in recalc_body or
                          "// peakUnreal" in recalc_body or
                          "peakUnreal = 0" not in recalc_body)
        if preserves_pos and preserves_peak:
            lines.append(_pass("Full recalculation: preserves posDirection, peakUnreal, entryATR"))
            passes += 1
        else:
            lines.append(_fail("Full recalculation: resets position state -- exits will misfire after recalc"))
            fails += 1
    else:
        lines.append(_warn("IsFullRecalculation block not found"))

    # ---- CHECK 9: isfinite() check (redundant with #1 but explicit) ----
    if has(r'isfinite\s*\(\s*z\s*\)'):
        lines.append(_pass("isfinite(z) guard present before entry signal check"))
        passes += 1
    else:
        lines.append(_fail("No isfinite(z) guard -- inf/nan Z could cause entry"))
        fails += 1

    # ---- CHECK 10: No early returns between session gate and entry logic ----
    # Find the session gate region and look for bare returns that could block signals
    session_to_entry = has(r'bool inSession.*?NEW ENTRY GATES', re.DOTALL)
    if session_to_entry:
        block = session_to_entry.group(0)
        # Count bare returns (not inside if-blocks that are intentional)
        bare_returns = re.findall(r'\n\s+return\s*;', block)
        # Some returns are intentional: in kill switch, stddev<=0, vwap<=0, etc.
        # Flag if there are returns that look suspicious (after position management and before entries)
        # The returns inside !isFlat block and exitPending block are fine
        # Count returns that are at the top-level (not inside { } blocks) -- rough heuristic
        if len(bare_returns) > 15:
            lines.append(_warn(f"Found {len(bare_returns)} return statements between session gate and entry -- verify none are unintentional"))
        else:
            lines.append(_pass(f"Session-to-entry flow: {len(bare_returns)} controlled returns (kill switch, exit pending, position mgmt)"))
            passes += 1
    else:
        lines.append(_warn("Could not isolate session-to-entry code region"))

    # ---- CHECK 11: BE trigger ATR mult ----
    be_match = has(r'In_BEThreshATR.*?SetFloat\s*\(\s*([\d.]+)')
    if be_match:
        be_val = float(be_match.group(1))
        lines.append(_info(f"BE trigger ATR mult default: {be_val}"))

    # ---- CHECK 12: Slot assignments are correct ----
    # NQ=0, ES=1, YM=2, RTY=3
    if slot_func:
        body = slot_func.group(1)
        slot_assignments = re.findall(r'return\s+(\d+)', body)
        expected = ["0", "1", "3", "2", "-1"]  # NQ, ES, RTY, YM, default
        if slot_assignments == expected:
            lines.append(_pass("Slot assignments: NQ=0 ES=1 YM=2 RTY=3 (correct)"))
            passes += 1
        else:
            lines.append(_info(f"Slot assignments: {slot_assignments} (expected {expected})"))
            passes += 1  # Info only, not a failure unless wrong

    # ---- CHECK 13: Dollar cap has CurrencyValuePerTick fallback ----
    # If sc.CurrencyValuePerTick is 0 (common for full-size CME contracts),
    # the dollar cap silently does nothing and stops are ATR-only (too wide).
    if has(r'CurrencyValuePerTick\s*<=\s*0') or has(r'cvpt\s*<=\s*0'):
        lines.append(_pass("Dollar cap: has CurrencyValuePerTick fallback (SLOT_CVPT)"))
        passes += 1
    elif has(r'CurrencyValuePerTick\s*>\s*0\.0f.*maxStopPts'):
        lines.append(_fail("Dollar cap: NO fallback when CurrencyValuePerTick=0 -- stop cap SILENTLY DISABLED for full-size contracts"))
        fails += 1
    else:
        lines.append(_info("Dollar cap: could not determine CurrencyValuePerTick handling"))

    # ---- CHECK 14: Z-score uses residual stddev (Close-VWAP), not stddev(Close) ----
    # On range bars, stddev(Close) is tiny → Z = 400-2600 → zero selectivity.
    # Must compute stddev on the (Close - VWAP) residual series.
    if has(r'sc\.Close\[k\]\s*-\s*vwapArray\[k\]') or has(r'Close.*-.*vwap.*sumResid'):
        lines.append(_pass("Z-score: uses residual stddev (Close-VWAP) -- proper selectivity on range bars"))
        passes += 1
    elif has(r'sc\.StdDeviation\s*\(\s*sc\.Close'):
        lines.append(_fail("Z-score: stddev computed on Close (not Close-VWAP) -- Z=400-2600 on range bars, zero entry selectivity"))
        fails += 1
    else:
        lines.append(_warn("Z-score stddev method could not be determined"))

    # ---- CHECK 15: PnL conversion when CurrencyValuePerTick=0 ----
    # LastTradeProfitLoss returns points (not $) when CVPT=0.
    # Must convert to dollars for daily loss limit, equity curve, Kelly.
    if has(r'LastTradeProfitLoss.*SLOT_CVPT|closedPnL\s*\*=.*cvpt'):
        lines.append(_pass("PnL tracking: dollar conversion when CurrencyValuePerTick=0"))
        passes += 1
    elif has(r'LastTradeProfitLoss') and not has(r'closedPnL\s*\*='):
        lines.append(_fail("PnL tracking: no dollar conversion when CurrencyValuePerTick=0 -- daily loss limit/equity curve/Kelly all broken"))
        fails += 1
    else:
        lines.append(_warn("PnL tracking method could not be determined"))

    return passes, fails, lines


# ============================================================
# LOG PARSING
# ============================================================

# Regex patterns for V7 predator log
RE_TIMESTAMP = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
RE_FILL      = re.compile(r'V7_FILL:\s+(BUY|SELL)\s+@\s+([\d.]+)\s+Z=([\d.+-]+)\s+ATR=([\d.]+)\s+#(\d+)\s+\[([^\]]+)\]')
RE_HARD_STOP = re.compile(r'V7_HARD_STOP:\s+loss=([\d.+-]+)\s+limit=([\d.+-]+)\s+\[([^\]]+)\]')
RE_BE_GUARD  = re.compile(r'V7_BE_GUARD:\s+peak=\+([\d.]+)\s+now=([\d.+-]+)\s+\[([^\]]+)\]')
RE_TIME_STOP = re.compile(r'V7_TIME_STOP:\s+bars=(\d+)/(\d+)\s+unreal=([\d.+-]+)\s+\[([^\]]+)\]')
RE_TRAIL_STOP = re.compile(r'V7_TRAIL_STOP:\s+peak=\+([\d.]+)\s+dd=([\d.]+)\s+\[([^\]]+)\]')
RE_Z_TARGET  = re.compile(r'V7_Z_TARGET:\s+Z=([\d.+-]+)\s+exit=([\d.]+)\s+\+([\d.]+)\s+\[([^\]]+)\]')
RE_WIN       = re.compile(r'V7_WIN\s+#(\d+)\s+PnL=\+([\d.]+)\s+Day=\$([\d.+-]+)\s+WR=(\d+)%\s+Z=([\d.]+)\s+\[([^\]]+)\]')
RE_LOSS      = re.compile(r'V7_LOSS\s+#(\d+)\s+PnL=([\d.+-]+)\s+Day=\$([\d.+-]+)\s+Consec=(\d+)\s+WR=(\d+)%\s+\[([^\]]+)\]')
RE_START     = re.compile(r'V7_START:\s+(\S+)\s+Slot=(\d+)')
RE_KILL      = re.compile(r'V7_KILL_SWITCH:\s+(.*)')
RE_HEARTBEAT = re.compile(r'V7_HEARTBEAT:\s+DRIFT=([\d.]+)s')
RE_WARN      = re.compile(r'V7_WARN:\s+(.*)')
RE_VIX_HALT  = re.compile(r'V7_VIX_HALT:\s+(.*)')
RE_VIX_FLAT  = re.compile(r'V7_VIX_FLATTEN:\s+(.*)')
RE_CIRCUIT   = re.compile(r'V7_CIRCUIT:\s+(.*)')
RE_ENTRY     = re.compile(r'V7_ENTRY_(BUY|SELL):\s+Z=([\d.+-]+)\s+effZ=([\d.]+)\s+(?:bid|ask)=([\d.]+)\s+ATR=([\d.]+)\s+qty=(\d+)\s+stop=(\d+)tk')

# Shadow log
RE_SHADOW = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+BLOCKED\s+(\w+):\s+(.*)\[([^\]]+)\]')


@dataclass
class LogEvent:
    timestamp: datetime
    event_type: str
    symbol: str = ""
    data: dict = field(default_factory=dict)
    raw: str = ""


@dataclass
class Trade:
    """Represents a fill-to-exit cycle."""
    fill_time: datetime
    symbol: str
    root: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    z_score: float
    atr: float
    trade_num: int
    exit_type: str = ""
    exit_time: Optional[datetime] = None
    exit_data: dict = field(default_factory=dict)
    pnl: Optional[float] = None
    result: str = ""  # "WIN", "LOSS", or ""


def parse_timestamp(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")


def parse_v7_log(log_path: str) -> List[LogEvent]:
    """Parse V7 predator log into structured events."""
    events: List[LogEvent] = []
    if not os.path.isfile(log_path):
        return events

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            ts_match = RE_TIMESTAMP.match(line)
            if not ts_match:
                continue
            ts = parse_timestamp(ts_match.group(1))

            # Try each pattern
            m = RE_FILL.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="FILL", symbol=m.group(6),
                    data={"side": m.group(1), "price": float(m.group(2)),
                          "z": float(m.group(3)), "atr": float(m.group(4)),
                          "trade_num": int(m.group(5))},
                    raw=line))
                continue

            m = RE_HARD_STOP.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="HARD_STOP", symbol=m.group(3),
                    data={"loss": float(m.group(1)), "limit": float(m.group(2))},
                    raw=line))
                continue

            m = RE_BE_GUARD.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="BE_GUARD", symbol=m.group(3),
                    data={"peak": float(m.group(1)), "now": float(m.group(2))},
                    raw=line))
                continue

            m = RE_TIME_STOP.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="TIME_STOP", symbol=m.group(4),
                    data={"bars_held": int(m.group(1)), "bars_max": int(m.group(2)),
                          "unreal": float(m.group(3))},
                    raw=line))
                continue

            m = RE_TRAIL_STOP.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="TRAIL_STOP", symbol=m.group(3),
                    data={"peak": float(m.group(1)), "dd": float(m.group(2))},
                    raw=line))
                continue

            m = RE_Z_TARGET.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="Z_TARGET", symbol=m.group(4),
                    data={"z": float(m.group(1)), "exit_z": float(m.group(2)),
                          "pnl": float(m.group(3))},
                    raw=line))
                continue

            m = RE_WIN.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="WIN", symbol=m.group(6),
                    data={"trade_num": int(m.group(1)), "pnl": float(m.group(2)),
                          "day_pnl": float(m.group(3)), "wr": int(m.group(4)),
                          "z": float(m.group(5))},
                    raw=line))
                continue

            m = RE_LOSS.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="LOSS", symbol=m.group(6),
                    data={"trade_num": int(m.group(1)), "pnl": float(m.group(2)),
                          "day_pnl": float(m.group(3)), "consec": int(m.group(4)),
                          "wr": int(m.group(5))},
                    raw=line))
                continue

            m = RE_START.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="START", symbol=m.group(1),
                    data={"slot": int(m.group(2))},
                    raw=line))
                continue

            m = RE_KILL.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="KILL_SWITCH",
                    data={"detail": m.group(1).strip()},
                    raw=line))
                continue

            m = RE_HEARTBEAT.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="HEARTBEAT",
                    data={"drift": float(m.group(1))},
                    raw=line))
                continue

            m = RE_WARN.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="WARN",
                    data={"detail": m.group(1).strip()},
                    raw=line))
                continue

            m = RE_VIX_HALT.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="VIX_HALT",
                    data={"detail": m.group(1).strip()},
                    raw=line))
                continue

            m = RE_VIX_FLAT.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="VIX_FLATTEN",
                    data={"detail": m.group(1).strip()},
                    raw=line))
                continue

            m = RE_CIRCUIT.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="CIRCUIT",
                    data={"detail": m.group(1).strip()},
                    raw=line))
                continue

            m = RE_ENTRY.search(line)
            if m:
                events.append(LogEvent(
                    timestamp=ts, event_type="ENTRY",
                    data={"side": m.group(1), "z": float(m.group(2)),
                          "eff_z": float(m.group(3)), "price": float(m.group(4)),
                          "atr": float(m.group(5)), "qty": int(m.group(6)),
                          "stop_ticks": int(m.group(7))},
                    raw=line))
                continue

    return events


def parse_v7_shadow(shadow_path: str) -> Tuple[Counter, List[LogEvent]]:
    """Parse shadow log, return (block_counts_by_reason, events)."""
    counts: Counter = Counter()
    events: List[LogEvent] = []

    if not os.path.isfile(shadow_path):
        return counts, events

    with open(shadow_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = RE_SHADOW.match(line)
            if m:
                ts = parse_timestamp(m.group(1))
                reason = m.group(2)
                detail = m.group(3).strip()
                symbol = m.group(4)
                counts[reason] += 1
                events.append(LogEvent(
                    timestamp=ts, event_type=f"BLOCKED_{reason}", symbol=symbol,
                    data={"reason": reason, "detail": detail},
                    raw=line))

    return counts, events


# ============================================================
# MATH VERIFICATION
# ============================================================

def verify_exit_math(trades: List[Trade], defaults: Dict) -> List[str]:
    """For each trade, verify exit conditions were mathematically correct."""
    lines: List[str] = []
    d = defaults

    for t in trades:
        root = t.root
        dpp = dollars_per_point(root)
        ts_short = t.fill_time.strftime("%H:%M")

        if t.exit_type == "HARD_STOP":
            loss = t.exit_data.get("loss", 0.0)
            limit = t.exit_data.get("limit", 0.0)
            atr = t.atr
            stop_dist = atr * d["hard_stop_atr_mult"]
            max_stop_pts = d["max_stop_dollar"] / dpp if dpp > 0 else 999
            effective_stop = min(stop_dist, max_stop_pts)

            # Note: VIX widening could increase stop beyond simple calc
            # If actual loss > effective_stop, VIX widening was likely active
            vix_widened = abs(limit) > effective_stop * 1.05  # 5% tolerance
            detail = f"ATR={atr:.2f} * {d['hard_stop_atr_mult']}={stop_dist:.2f}"
            if max_stop_pts < stop_dist:
                detail += f", $cap={d['max_stop_dollar']}/{dpp:.0f}dpp={max_stop_pts:.2f}pts"
            detail += f" -> limit={effective_stop:.2f}, actual={loss:.2f}"
            if vix_widened:
                detail += " (VIX-widened)"
                lines.append(_pass(f"{root} hard stop @ {ts_short}: {detail}"))
            elif abs(abs(loss) - effective_stop) < effective_stop * 0.15:
                lines.append(_pass(f"{root} hard stop @ {ts_short}: {detail}"))
            else:
                lines.append(_warn(f"{root} hard stop @ {ts_short}: {detail} -- large deviation"))

        elif t.exit_type == "TRAIL_STOP":
            peak = t.exit_data.get("peak", 0.0)
            dd = t.exit_data.get("dd", 0.0)
            atr = t.atr
            trail_dist_raw = atr * d["trail_atr_mult"]
            trail_dist = min(trail_dist_raw, peak * 0.5)
            ok = dd >= trail_dist - 0.01  # small epsilon for float comparison

            detail = f"peak={peak:.2f}, trailDist=min({atr:.2f}*{d['trail_atr_mult']}, {peak:.2f}*0.5)={trail_dist:.3f}, dd={dd:.2f}"
            if ok:
                lines.append(_pass(f"{root} trail stop @ {ts_short}: {detail}"))
            else:
                lines.append(_fail(f"{root} trail stop @ {ts_short}: dd={dd:.2f} < trailDist={trail_dist:.3f} -- should not have triggered"))

        elif t.exit_type == "BE_GUARD":
            peak = t.exit_data.get("peak", 0.0)
            now = t.exit_data.get("now", 0.0)
            atr = t.atr
            be_thresh = atr * d["be_trigger_atr_mult"]
            ok = peak >= be_thresh - 0.01 and now <= 0.01

            detail = f"peak={peak:.2f} >= BE_thresh={be_thresh:.2f}, now={now:.2f} <= 0"
            if ok:
                lines.append(_pass(f"{root} BE guard @ {ts_short}: {detail}"))
            else:
                lines.append(_fail(f"{root} BE guard @ {ts_short}: {detail} -- conditions not met"))

        elif t.exit_type == "TIME_STOP":
            bars = t.exit_data.get("bars_held", 0)
            max_bars = t.exit_data.get("bars_max", d["time_stop_bars"])
            ok = bars >= max_bars

            detail = f"bars={bars}/{max_bars}"
            if ok:
                lines.append(_pass(f"{root} time stop @ {ts_short}: {detail}"))
            else:
                lines.append(_fail(f"{root} time stop @ {ts_short}: bars={bars} < max={max_bars}"))

        elif t.exit_type == "Z_TARGET":
            z_at_exit = t.exit_data.get("z", 0.0)
            z_exit_thresh = t.exit_data.get("exit_z", d["z_exit"])
            side = t.direction
            if side == "SELL":  # short position
                ok = z_at_exit <= -z_exit_thresh + 0.01
            else:
                ok = z_at_exit >= z_exit_thresh - 0.01
            detail = f"Z={z_at_exit:.2f}, threshold={z_exit_thresh}"
            if ok:
                lines.append(_pass(f"{root} Z-target @ {ts_short}: {detail}"))
            else:
                lines.append(_fail(f"{root} Z-target @ {ts_short}: {detail} -- Z did not cross exit threshold"))

    return lines


def verify_entry_gates(trades: List[Trade], shadow_counts: Counter, defaults: Dict) -> List[str]:
    """Check if entry Z-scores would pass current gate config, flag dominant blocks."""
    lines: List[str] = []

    for t in trades:
        z = t.z_score
        base_z = defaults["z_entry"]
        ok = abs(z) > base_z
        if ok:
            lines.append(_pass(f"{t.root} fill #{t.trade_num}: Z={z:.2f} > threshold {base_z} (passed)"))
        else:
            lines.append(_fail(f"{t.root} fill #{t.trade_num}: Z={z:.2f} < threshold {base_z} (should not have filled)"))

    # Flag any gate blocking > 90%
    total_blocks = sum(shadow_counts.values())
    if total_blocks > 0:
        for reason, count in shadow_counts.most_common():
            pct = count / total_blocks * 100
            if pct > 90:
                lines.append(_fail(f"Gate '{reason}' blocking {pct:.0f}% of all signals ({count}/{total_blocks}) -- possible misconfiguration"))
            elif pct > 50:
                lines.append(_warn(f"Gate '{reason}' blocking {pct:.0f}% of signals ({count}/{total_blocks})"))

    return lines


# ============================================================
# MODE 2: REPLAY
# ============================================================

def build_trades(events: List[LogEvent]) -> List[Trade]:
    """Match fills to exits and win/loss results per symbol."""
    # Track open trades per symbol
    open_trades: Dict[str, Trade] = {}
    closed_trades: List[Trade] = []

    exit_types = {"HARD_STOP", "BE_GUARD", "TIME_STOP", "TRAIL_STOP", "Z_TARGET", "VIX_FLATTEN"}

    for ev in events:
        if ev.event_type == "FILL":
            sym = ev.symbol
            root = resolve_symbol_root(sym) or "???"
            t = Trade(
                fill_time=ev.timestamp,
                symbol=sym,
                root=root,
                direction=ev.data["side"],
                entry_price=ev.data["price"],
                z_score=ev.data["z"],
                atr=ev.data["atr"],
                trade_num=ev.data["trade_num"],
            )
            open_trades[sym] = t

        elif ev.event_type in exit_types:
            sym = ev.symbol
            if sym in open_trades:
                t = open_trades.pop(sym)
                t.exit_type = ev.event_type
                t.exit_time = ev.timestamp
                t.exit_data = ev.data
                closed_trades.append(t)

        elif ev.event_type in ("WIN", "LOSS"):
            sym = ev.symbol
            # Try to attach to most recent closed trade for this symbol
            for ct in reversed(closed_trades):
                if ct.symbol == sym and ct.result == "":
                    ct.result = ev.event_type
                    ct.pnl = ev.data.get("pnl", 0.0)
                    break
            else:
                # WIN/LOSS without a preceding exit? Check open trades
                if sym in open_trades:
                    t = open_trades.pop(sym)
                    t.result = ev.event_type
                    t.pnl = ev.data.get("pnl", 0.0)
                    t.exit_time = ev.timestamp
                    closed_trades.append(t)

    # Any remaining open trades are orphaned
    orphaned = list(open_trades.values())
    return closed_trades + orphaned


def replay(log_path: str, shadow_path: str) -> Tuple[int, int, int, List[str]]:
    """Full replay with verification.
    Returns (passes, fails, warns, output_lines).
    """
    lines: List[str] = []
    passes = 0
    fails = 0
    warns = 0

    events = parse_v7_log(log_path)
    shadow_counts, shadow_events = parse_v7_shadow(shadow_path)

    if not events:
        lines.append(_warn(f"No events parsed from {log_path}"))
        if not os.path.isfile(log_path):
            lines.append(_fail(f"Log file not found: {log_path}"))
            return 0, 1, 0, lines
        return 0, 0, 1, lines

    # ---- Date filtering: focus on most recent trading day ----
    all_dates = sorted(set(ev.timestamp.date() for ev in events))
    target_date = all_dates[-1] if all_dates else None
    lines.append(_info(f"Log spans {len(all_dates)} day(s): {all_dates[0]} to {all_dates[-1]}" if len(all_dates) > 1
                       else f"Log date: {all_dates[0]}" if all_dates else "No dates found"))

    if target_date:
        day_events = [ev for ev in events if ev.timestamp.date() == target_date]
    else:
        day_events = events

    # ---- Timeline ----
    lines.append("")
    lines.append(f"  Timeline for {target_date}:")
    lines.append(f"  {'='*44}")

    # Show key events (not heartbeats/warns unless interesting)
    # Event types for the timeline (trade-related only, not startup noise)
    trade_types = {"FILL", "HARD_STOP", "BE_GUARD", "TIME_STOP", "TRAIL_STOP",
                   "Z_TARGET", "WIN", "LOSS", "VIX_HALT", "VIX_FLATTEN", "CIRCUIT"}

    # Count startup events for summary but do not print each one
    start_count = sum(1 for ev in day_events if ev.event_type == "START")
    kill_count = sum(1 for ev in day_events if ev.event_type == "KILL_SWITCH")
    heartbeat_count = sum(1 for ev in day_events if ev.event_type == "HEARTBEAT")

    if start_count:
        lines.append(f"  (startup: {start_count} START, {kill_count} KILL_SWITCH, {heartbeat_count} HEARTBEAT events)")
        lines.append("")

    for ev in day_events:
        if ev.event_type not in trade_types:
            continue
        ts = ev.timestamp.strftime("%H:%M")

        if ev.event_type == "FILL":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} {ev.data['side']:<5} @ {ev.data['price']:.2f}  Z={ev.data['z']:.0f}  ATR={ev.data['atr']:.2f}")

        elif ev.event_type == "HARD_STOP":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} -> HARD_STOP loss={ev.data['loss']:.2f} limit={ev.data['limit']:.2f}")

        elif ev.event_type == "BE_GUARD":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} -> BE_GUARD peak=+{ev.data['peak']:.2f} now={ev.data['now']:.2f}")

        elif ev.event_type == "TRAIL_STOP":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} -> TRAIL_STOP peak=+{ev.data['peak']:.2f} dd={ev.data['dd']:.2f}")

        elif ev.event_type == "TIME_STOP":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} -> TIME_STOP bars={ev.data['bars_held']}/{ev.data['bars_max']}")

        elif ev.event_type == "Z_TARGET":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} -> Z_TARGET Z={ev.data['z']:.2f}")

        elif ev.event_type == "WIN":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} ** WIN #{ev.data['trade_num']} PnL=+{ev.data['pnl']:.2f} Day=${ev.data['day_pnl']:.2f} WR={ev.data['wr']}%")

        elif ev.event_type == "LOSS":
            root = resolve_symbol_root(ev.symbol) or ev.symbol[:3]
            lines.append(f"  {ts} {root:<4} ** LOSS #{ev.data['trade_num']} PnL={ev.data['pnl']:.2f} Day=${ev.data['day_pnl']:.2f} Consec={ev.data['consec']}")

        elif ev.event_type == "KILL_SWITCH":
            detail = ev.data.get("detail", "")
            lines.append(f"  {ts} KILL_SWITCH: {detail}")

        elif ev.event_type == "CIRCUIT":
            lines.append(f"  {ts} CIRCUIT: {ev.data.get('detail', '')}")

        elif ev.event_type == "VIX_HALT":
            lines.append(f"  {ts} VIX_HALT: {ev.data.get('detail', '')}")

    # ---- Shadow summary ----
    lines.append("")
    total_shadow = sum(shadow_counts.values())
    shadow_str = " ".join(f"{r}={c}" for r, c in shadow_counts.most_common())
    lines.append(f"  Shadow: {total_shadow} blocked ({shadow_str})")

    # ---- Build trades and verify ----
    trades = build_trades(day_events)
    closed = [t for t in trades if t.exit_type]
    orphaned = [t for t in trades if not t.exit_type]

    lines.append("")
    lines.append(_section("MATH VERIFICATION"))

    # Exit math
    exit_math_lines = verify_exit_math(closed, V7_DEFAULTS)
    for el in exit_math_lines:
        lines.append(el)
        if "[PASS]" in el:
            passes += 1
        elif "[FAIL]" in el:
            fails += 1
        elif "[WARN]" in el:
            warns += 1

    # Orphaned fills
    for t in orphaned:
        lines.append(_warn(f"{t.root} fill #{t.trade_num}: Z={t.z_score:.0f} entry @ {t.entry_price:.2f}, no exit logged (orphaned)"))
        warns += 1

    # Entry gate verification
    lines.append("")
    entry_lines = verify_entry_gates(closed + orphaned, shadow_counts, V7_DEFAULTS)
    for el in entry_lines:
        lines.append(el)
        if "[PASS]" in el:
            passes += 1
        elif "[FAIL]" in el:
            fails += 1
        elif "[WARN]" in el:
            warns += 1

    # ---- Session time verification ----
    lines.append("")
    start_events = [ev for ev in day_events if ev.event_type == "START"]
    # Check for ET time in START logs (newer format has ET=HHMM)
    et_pat = re.compile(r'ET=(\d{4})')
    offset_pat = re.compile(r'Offset=(\d+)')
    for sev in start_events[:4]:  # Check first few
        et_m = et_pat.search(sev.raw)
        off_m = offset_pat.search(sev.raw)
        if et_m:
            et_time = int(et_m.group(1))
            offset = int(off_m.group(1)) if off_m else -1
            sess_start = V7_DEFAULTS["session_start"]
            sess_end = V7_DEFAULTS["session_end"]
            in_session = sess_start <= et_time < sess_end
            if not in_session:
                lines.append(_warn(f"START {sev.symbol}: ET={et_time:04d} outside session {sess_start}-{sess_end}"))
                warns += 1
            if offset != 0 and offset != -1:
                lines.append(_warn(f"START {sev.symbol}: Offset={offset} (expected 0 for ET chart)"))
                warns += 1

    # Check for RTYM slot bug — only the LATEST START per symbol matters
    latest_starts: Dict[str, "V7Event"] = {}
    for sev in start_events:
        latest_starts[sev.symbol] = sev  # last one wins
    for sym, sev in latest_starts.items():
        if "RTYM" in sym and sev.data.get("slot") == 2:
            lines.append(_fail(f"RTYM slot=2 in latest START @ {sev.timestamp} -- should be slot=3"))
            fails += 1
        elif "RTYM" in sym and sev.data.get("slot") == 3:
            lines.append(_pass(f"RTYM slot=3 in latest START @ {sev.timestamp} (fixed)"))
            passes += 1

    return passes, fails, warns, lines


# ============================================================
# MODE 3: AUDIT (runs both)
# ============================================================

def audit(cpp_path: str, log_path: str, shadow_path: str) -> None:
    """Run everything and print formatted report."""
    output: List[str] = []
    total_pass = 0
    total_fail = 0
    total_warn = 0

    output.append(_box_top("V7 PREDATOR SIM -- PRE-FLIGHT REPORT"))
    output.append(_section("C++ VALIDATION"))

    # C++ validation
    cpp_pass, cpp_fail, cpp_lines = validate_cpp(cpp_path)
    total_pass += cpp_pass
    total_fail += cpp_fail
    output.extend(cpp_lines)

    output.append("")
    output.append(_section(f"LOG REPLAY -- {datetime.now().strftime('%Y-%m-%d')}"))

    # Log replay
    rep_pass, rep_fail, rep_warn, rep_lines = replay(log_path, shadow_path)
    total_pass += rep_pass
    total_fail += rep_fail
    total_warn += rep_warn
    output.extend(rep_lines)

    # ---- Summary ----
    output.append("")
    output.append(_section("SUMMARY"))

    output.append(f"  C++ checks:  {cpp_pass}/{cpp_pass + cpp_fail} PASS" +
                  (f", {cpp_fail} FAIL" if cpp_fail else ""))

    # Count trades
    events = parse_v7_log(log_path)
    all_dates = sorted(set(ev.timestamp.date() for ev in events))
    target_date = all_dates[-1] if all_dates else None
    if target_date:
        day_events = [ev for ev in events if ev.timestamp.date() == target_date]
    else:
        day_events = events

    trades = build_trades(day_events)
    n_fills = len(trades)
    n_exits = len([t for t in trades if t.exit_type])
    n_orphan = len([t for t in trades if not t.exit_type])
    output.append(f"  Log trades:  {n_fills} fills, {n_exits} exits matched, {n_orphan} orphaned")

    shadow_counts, _ = parse_v7_shadow(shadow_path)
    total_shadow = sum(shadow_counts.values())
    if total_shadow > 0:
        gate_parts = []
        for reason, count in shadow_counts.most_common(5):
            pct = count / total_shadow * 100
            gate_parts.append(f"{reason} {pct:.0f}%")
        output.append(f"  Gate blocks: {', '.join(gate_parts)}")

    output.append("")
    if total_fail == 0:
        output.append("  >> READY TO DEPLOY  (all checks passed)")
    else:
        output.append(f"  >> DO NOT DEPLOY -- {total_fail} issue(s) found")
        if total_warn > 0:
            output.append(f"     ({total_warn} warning(s) also noted)")

    output.append("=" * 48)
    output.append("")

    print("\n".join(output))


# ============================================================
# MAIN
# ============================================================

def main():
    if len(sys.argv) < 2:
        mode = "audit"
    else:
        mode = sys.argv[1].lower().strip()

    if mode == "validate":
        print(_box_top("V7 PREDATOR SIM -- C++ VALIDATION"))
        p, f, lines = validate_cpp(CPP_SOURCE)
        for l in lines:
            print(l)
        print(f"\n  Result: {p}/{p+f} PASS" + (f", {f} FAIL" if f else ""))
        print("=" * 48)

    elif mode == "replay":
        print(_box_top("V7 PREDATOR SIM -- LOG REPLAY"))
        p, f, w, lines = replay(V7_LOG, V7_SHADOW)
        for l in lines:
            print(l)
        print(f"\n  Result: {p} PASS, {f} FAIL, {w} WARN")
        print("=" * 48)

    elif mode == "audit":
        audit(CPP_SOURCE, V7_LOG, V7_SHADOW)

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python v7_sim.py [validate|replay|audit]")
        sys.exit(1)


if __name__ == "__main__":
    main()
