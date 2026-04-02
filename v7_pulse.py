#!/usr/bin/env python3
"""
V7 Pulse — Automated Trading Monitor & Alert System
=====================================================
Runs every 15 min during market hours. Reads V7 logs, detects issues,
writes structured reports, sends email alerts for critical events.

Modes:
    python v7_pulse.py              — Standard 15-min check (scheduled)
    python v7_pulse.py --eod        — End-of-day summary report
    python v7_pulse.py --status     — Quick status to stdout
"""

import json, os, re, sys, datetime, hashlib, smtplib, argparse
from collections import Counter, defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# -- Config ----------------------------------------------------
V7_LOG       = r"D:\Sierra Chart\V7_Predator_Log.txt"
V7_SHADOW    = r"D:\Sierra Chart\V7_Shadow_Log.txt"
REPORT_DIR   = r"C:\Users\Anwender\Code\tradelog\reports"
STATE_FILE   = r"C:\Users\Anwender\Code\tradelog\.pulse_state.json"
PULSE_LOG    = r"C:\Users\Anwender\Code\tradelog\pulse.log"
GITHUB_DATA  = r"C:\Users\Anwender\Code\tradelog\data"

MARKET_OPEN  = (4, 15)   # 04:15 ET
MARKET_CLOSE = (16, 0)   # 16:00 ET
ALERT_EMAIL  = "kottowc@gmail.com"

SYMBOL_META = {
    "NQ":  {"tick": 0.25, "pv": 20.0,  "name": "Nasdaq"},
    "MNQ": {"tick": 0.25, "pv": 2.0,   "name": "Micro Nasdaq"},
    "ES":  {"tick": 0.25, "pv": 50.0,  "name": "S&P 500"},
    "MES": {"tick": 0.25, "pv": 5.0,   "name": "Micro S&P"},
    "YM":  {"tick": 1.0,  "pv": 5.0,   "name": "Dow"},
    "MYM": {"tick": 1.0,  "pv": 0.5,   "name": "Micro Dow"},
    "RTY": {"tick": 0.10, "pv": 50.0,  "name": "Russell"},
    "M2K": {"tick": 0.10, "pv": 5.0,   "name": "Micro Russell"},
}

# -- Logging ---------------------------------------------------
def plog(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(PULSE_LOG), exist_ok=True)
    with open(PULSE_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# -- State persistence -----------------------------------------
def load_state():
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_line": 0, "last_fill_count": 0, "alerts_sent": [],
            "last_date": "", "fills_today": [], "exits_today": [],
            "daily_pnl": 0.0, "errors": []}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

# -- Symbol helpers --------------------------------------------
def get_base(sym_raw):
    s = re.sub(r'(_FUT_CME|_FUT_CBOT|_FUT_NYMEX|_FUT_COMEX)$', '', sym_raw.upper())
    s = re.sub(r'[FGHJKMNQUVXZ]\d{2}$', '', s)
    return s

# -- Log parsers -----------------------------------------------
LINE_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+(V7_\w+)[:\s]+(.*)')

FILL_RE = re.compile(
    r'(BUY|SELL)\s+@\s+([\d.]+)\s+Z=([\d.+-]+)\s+ATR=([\d.]+)\s+#(\d+)\s+\[(\w+)\]')
WIN_RE = re.compile(
    r'#(\d+)\s+PnL=([+\-\d.]+)\s+Day=\$([+\-\d.]+)\s+WR=(\d+)%')
LOSS_RE = re.compile(
    r'#(\d+)\s+PnL=([+\-\d.]+)\s+Day=\$([+\-\d.]+)\s+Consec=(\d+)\s+WR=(\d+)%')
TRAIL_RE = re.compile(r'peak=([+\-\d.]+)\s+dd=([\d.]+)\s+\[(\w+)\]')
HARD_RE = re.compile(r'loss=([+\-\d.]+)\s+limit=([+\-\d.]+)\s+\[(\w+)\]')
BE_RE = re.compile(r'peak=([+\-\d.]+)\s+now=([+\-\d.]+)\s+\[(\w+)\]')
KILL_RE = re.compile(r'Session end')
START_RE = re.compile(r'(\w+_FUT_\w+)\s+Slot=(\d+).*ET=(\d+)\s+Offset=(\d+)')
ERR_RE = re.compile(r'V7_ERR')

SHADOW_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\]\s+BLOCKED\s+(\w+):.*\[(\w+)\]')


def parse_new_lines(log_path, start_line):
    """Parse V7 log from start_line onward. Returns (events, new_line_count)."""
    events = []
    if not os.path.isfile(log_path):
        return events, start_line

    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    for i in range(start_line, len(lines)):
        raw = lines[i].strip()
        m = LINE_RE.match(raw)
        if not m:
            continue
        date_str, time_str, tag, payload = m.groups()
        evt = {"date": date_str, "time": time_str, "tag": tag,
               "payload": payload, "line": i}

        if tag == "V7_FILL":
            fm = FILL_RE.search(payload)
            if fm:
                evt.update({"side": fm.group(1), "price": float(fm.group(2)),
                            "z": float(fm.group(3)), "atr": float(fm.group(4)),
                            "trade_num": int(fm.group(5)),
                            "symbol": get_base(fm.group(6)),
                            "sym_raw": fm.group(6)})

        elif tag == "V7_WIN":
            wm = WIN_RE.search(payload)
            if wm:
                evt.update({"trade_num": int(wm.group(1)),
                            "pnl": float(wm.group(2)),
                            "day_pnl": float(wm.group(3)),
                            "win_rate": int(wm.group(4)),
                            "exit_type": "win"})

        elif tag == "V7_LOSS":
            lm = LOSS_RE.search(payload)
            if lm:
                evt.update({"trade_num": int(lm.group(1)),
                            "pnl": float(lm.group(2)),
                            "day_pnl": float(lm.group(3)),
                            "consec": int(lm.group(4)),
                            "win_rate": int(lm.group(5)),
                            "exit_type": "loss"})

        elif tag == "V7_TRAIL_STOP":
            tm = TRAIL_RE.search(payload)
            if tm:
                evt.update({"peak": float(tm.group(1)),
                            "dd": float(tm.group(2)),
                            "symbol": get_base(tm.group(3)),
                            "exit_type": "trail_stop"})

        elif tag == "V7_HARD_STOP":
            hm = HARD_RE.search(payload)
            if hm:
                evt.update({"loss": float(hm.group(1)),
                            "limit": float(hm.group(2)),
                            "symbol": get_base(hm.group(3)),
                            "exit_type": "hard_stop"})

        elif tag == "V7_BE_GUARD":
            bm = BE_RE.search(payload)
            if bm:
                evt.update({"peak": float(bm.group(1)),
                            "now": float(bm.group(2)),
                            "symbol": get_base(bm.group(3)),
                            "exit_type": "breakeven"})

        elif tag == "V7_KILL_SWITCH":
            evt["is_kill"] = True

        elif tag == "V7_START":
            sm = START_RE.search(payload)
            if sm:
                evt.update({"sym_raw": sm.group(1), "slot": int(sm.group(2)),
                            "et_time": int(sm.group(3)),
                            "offset": int(sm.group(4))})

        elif "ERR" in tag:
            evt["is_error"] = True

        events.append(evt)

    return events, len(lines)


def count_shadow_today(shadow_path, today_str):
    """Count blocked signals for today by reason."""
    counts = Counter()
    if not os.path.isfile(shadow_path):
        return counts
    with open(shadow_path, 'r', encoding='utf-8', errors='replace') as f:
        for raw in f:
            m = SHADOW_RE.match(raw.strip())
            if m and m.group(1) == today_str:
                counts[m.group(2)] += 1
    return counts


# -- Analysis --------------------------------------------------

def analyze(events, state, today_str):
    """Analyze new events, return (report_lines, alerts, updated_state)."""
    report = []
    alerts = []  # critical issues needing email

    # Reset state on new day
    if state.get("last_date") != today_str:
        state["last_date"] = today_str
        state["fills_today"] = []
        state["exits_today"] = []
        state["daily_pnl"] = 0.0
        state["errors"] = []
        state["alerts_sent"] = []

    new_fills = []
    new_exits = []
    new_errors = []
    new_starts = []
    kill_count = 0

    for evt in events:
        if evt["date"] != today_str:
            continue

        if evt["tag"] == "V7_FILL":
            new_fills.append(evt)
            state["fills_today"].append({
                "time": evt["time"], "symbol": evt.get("symbol", "?"),
                "side": evt.get("side", "?"), "price": evt.get("price", 0),
                "z": evt.get("z", 0), "atr": evt.get("atr", 0),
                "trade_num": evt.get("trade_num", 0)
            })

        elif evt["tag"] in ("V7_WIN", "V7_LOSS"):
            new_exits.append(evt)
            pnl = evt.get("pnl", 0)
            state["daily_pnl"] = evt.get("day_pnl", state["daily_pnl"])
            state["exits_today"].append({
                "time": evt["time"], "type": evt.get("exit_type", "?"),
                "pnl": pnl, "day_pnl": evt.get("day_pnl", 0),
                "win_rate": evt.get("win_rate", 0)
            })

        elif evt["tag"] in ("V7_TRAIL_STOP", "V7_HARD_STOP", "V7_BE_GUARD"):
            new_exits.append(evt)

        elif evt["tag"] == "V7_KILL_SWITCH":
            kill_count += 1

        elif evt["tag"] == "V7_START":
            new_starts.append(evt)

        elif evt.get("is_error"):
            new_errors.append(evt)
            state["errors"].append({"time": evt["time"],
                                     "msg": evt["payload"]})

    # -- Build report --
    now_et_str = _now_et().strftime("%H:%M ET")
    total_fills = len(state["fills_today"])
    total_exits = len(state["exits_today"])
    pnl = state["daily_pnl"]
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

    report.append(f"V7 PULSE [{now_et_str}]  Trades: {total_fills}  P&L: {pnl_str}")
    report.append("-" * 50)

    # New activity since last check
    if new_fills:
        for f in new_fills:
            report.append(f"  NEW  {f['time']} {f.get('symbol','?'):>4} {f.get('side','?'):>4} @ {f.get('price',0):.2f}  Z={f.get('z',0):.0f}  ATR={f.get('atr',0):.2f}")
    if new_exits:
        for e in new_exits:
            etype = e.get("exit_type", e["tag"])
            if e["tag"] in ("V7_WIN", "V7_LOSS"):
                report.append(f"  EXIT {e['time']} {etype:>12}  PnL={e.get('pnl',0):+.2f}  Day={e.get('day_pnl',0):+.2f}  WR={e.get('win_rate',0)}%")
            else:
                sym = e.get("symbol", "?")
                report.append(f"  EXIT {e['time']} {sym:>4} {etype}")

    if not new_fills and not new_exits:
        report.append("  No new activity since last check")

    # Starts (recompiles)
    if new_starts:
        for s in new_starts:
            report.append(f"  LOAD {s['time']} {s.get('sym_raw','?')} Slot={s.get('slot','?')} ET={s.get('et_time','?')}")

    # Errors
    if new_errors:
        report.append("  (!) ERRORS:")
        for e in new_errors:
            report.append(f"    {e['time']} {e['payload'][:80]}")

    # Kill switches (unexpected)
    if kill_count > 0 and new_starts:
        # Kills after starts during session = possible ET offset bug
        report.append(f"  (!) {kill_count} kill switch(es) fired")

    report.append("")

    # -- Daily scoreboard --
    report.append("SCOREBOARD")
    report.append("-" * 50)
    fills_by_sym = defaultdict(list)
    for f in state["fills_today"]:
        fills_by_sym[f["symbol"]].append(f)

    for sym in sorted(fills_by_sym.keys()):
        fills = fills_by_sym[sym]
        report.append(f"  {sym:>4}: {len(fills)} trade(s)")

    wins = sum(1 for e in state["exits_today"] if e.get("type") == "win"
               or (e.get("pnl", 0) > 0))
    losses = sum(1 for e in state["exits_today"] if e.get("pnl", 0) < 0)
    wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    report.append(f"  W/L: {wins}/{losses}  WR: {wr:.0f}%  Net: {pnl_str}")
    report.append("")

    # -- Anomaly detection (triggers alerts) --
    now_et = _now_et()
    minutes_since_open = (now_et.hour * 60 + now_et.minute) - (MARKET_OPEN[0] * 60 + MARKET_OPEN[1])

    # Alert: No trades after 90 min in session
    if minutes_since_open > 90 and total_fills == 0:
        alert_key = f"no_trades_{today_str}"
        if alert_key not in state["alerts_sent"]:
            msg = f"NO TRADES after {minutes_since_open} min in session. Check script is running."
            alerts.append({"severity": "CRITICAL", "msg": msg})
            state["alerts_sent"].append(alert_key)
            report.append(f"  [!!] ALERT: {msg}")

    # Alert: 3+ consecutive losses
    recent_exits = state["exits_today"][-5:]
    consec_loss = 0
    for e in reversed(recent_exits):
        if e.get("pnl", 0) < 0:
            consec_loss += 1
        else:
            break
    if consec_loss >= 3:
        alert_key = f"consec_loss_{today_str}_{consec_loss}"
        if alert_key not in state["alerts_sent"]:
            msg = f"{consec_loss} consecutive losses. Daily P&L: {pnl_str}"
            alerts.append({"severity": "WARNING", "msg": msg})
            state["alerts_sent"].append(alert_key)
            report.append(f"  [!] ALERT: {msg}")

    # Alert: Daily loss exceeds threshold
    if pnl < -150:
        alert_key = f"max_loss_{today_str}"
        if alert_key not in state["alerts_sent"]:
            msg = f"Daily loss {pnl_str} approaching max ($200). Risk limit near."
            alerts.append({"severity": "CRITICAL", "msg": msg})
            state["alerts_sent"].append(alert_key)
            report.append(f"  [!!] ALERT: {msg}")

    # Alert: Errors detected
    if new_errors:
        alert_key = f"errors_{today_str}_{len(state['errors'])}"
        if alert_key not in state["alerts_sent"]:
            msg = f"{len(new_errors)} V7 error(s): {new_errors[0]['payload'][:60]}"
            alerts.append({"severity": "CRITICAL", "msg": msg})
            state["alerts_sent"].append(alert_key)
            report.append(f"  [!!] ALERT: {msg}")

    # Alert: Kill switch during session hours (not at end of day)
    if kill_count > 0 and minutes_since_open > 30 and now_et.hour < 15:
        alert_key = f"kill_mid_{today_str}_{now_et.hour}"
        if alert_key not in state["alerts_sent"]:
            msg = f"Kill switch fired mid-session at {now_et_str}. Script may think it's outside session."
            alerts.append({"severity": "CRITICAL", "msg": msg})
            state["alerts_sent"].append(alert_key)
            report.append(f"  [!!] ALERT: {msg}")

    return report, alerts, state


def build_eod_report(state, shadow_counts):
    """Build end-of-day summary."""
    today_str = state.get("last_date", "?")
    pnl = state.get("daily_pnl", 0)
    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    fills = state.get("fills_today", [])
    exits = state.get("exits_today", [])
    errors = state.get("errors", [])

    wins = sum(1 for e in exits if e.get("pnl", 0) > 0)
    losses = sum(1 for e in exits if e.get("pnl", 0) < 0)
    wr = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    total_blocked = sum(shadow_counts.values())

    r = []
    r.append("=" * 55)
    r.append(f"  V7 PREDATOR - END OF DAY REPORT  {today_str}")
    r.append("=" * 55)
    r.append("")
    r.append(f"  Net P&L:     {pnl_str}")
    r.append(f"  Trades:      {len(fills)}")
    r.append(f"  Win/Loss:    {wins}W / {losses}L  ({wr:.0f}%)")
    r.append(f"  Signals:     {total_blocked} blocked")
    r.append("")

    # Per-symbol breakdown
    r.append("  SYMBOL BREAKDOWN")
    r.append("  " + "-" * 45)
    by_sym = defaultdict(lambda: {"fills": 0, "pnl": 0.0})
    for f in fills:
        by_sym[f["symbol"]]["fills"] += 1
    # PnL by symbol - approximate from fills and exits order
    for sym, data in sorted(by_sym.items()):
        r.append(f"    {sym:>4}: {data['fills']} trade(s)")

    r.append("")

    # Trade log
    r.append("  TRADE LOG")
    r.append("  " + "-" * 45)
    for f in fills:
        r.append(f"    {f['time']} {f['symbol']:>4} {f['side']:>4} @ {f['price']:.2f}  Z={f['z']:.0f}")
    for e in exits:
        pnl_e = e.get("pnl", 0)
        tag = "WIN" if pnl_e > 0 else "LOSS"
        r.append(f"    {e['time']} {tag:>4} {e.get('type','?'):>12}  PnL={pnl_e:+.2f}")

    r.append("")

    # Shadow analysis
    if shadow_counts:
        r.append("  BLOCKED SIGNALS")
        r.append("  " + "-" * 45)
        for reason, cnt in shadow_counts.most_common():
            pct = cnt / total_blocked * 100 if total_blocked > 0 else 0
            bar = "#" * int(pct / 3)
            r.append(f"    {reason:<12} {cnt:>6}  ({pct:4.1f}%) {bar}")

    r.append("")

    # Errors
    if errors:
        r.append("  (!) ERRORS")
        r.append("  " + "-" * 45)
        for e in errors:
            r.append(f"    {e['time']} {e['msg'][:60]}")
    else:
        r.append("  [OK] No errors")

    r.append("")
    r.append("=" * 55)

    return r


# -- Email -----------------------------------------------------
def build_email_body(report_lines, alerts):
    """Build HTML email from report lines."""
    lines_html = "\n".join(f"<pre>{line}</pre>" for line in report_lines)

    alert_html = ""
    if alerts:
        alert_html = "<h2 style='color:red'>(!) ALERTS</h2><ul>"
        for a in alerts:
            color = "red" if a["severity"] == "CRITICAL" else "orange"
            alert_html += f"<li style='color:{color}'><b>[{a['severity']}]</b> {a['msg']}</li>"
        alert_html += "</ul><hr>"

    return f"""
    <html><body style="font-family: Consolas, monospace; font-size: 13px; background: #1a1a2e; color: #e0e0e0; padding: 20px;">
    {alert_html}
    <div style="background: #16213e; padding: 15px; border-radius: 8px; border-left: 4px solid {'#e74c3c' if alerts else '#2ecc71'};">
    {lines_html}
    </div>
    <p style="color: #666; font-size: 11px; margin-top: 20px;">
    V7 Pulse — Automated monitor |
    <a href="https://epinay197.github.io/tradelog/">Dashboard</a>
    </p>
    </body></html>
    """


# -- Report file -----------------------------------------------
def save_report(report_lines, filename):
    """Save report to file."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    return path


def save_pulse_json(state, shadow_counts, today_str):
    """Save machine-readable pulse data for GitHub Pages dashboard."""
    os.makedirs(GITHUB_DATA, exist_ok=True)
    pulse_data = {
        "date": today_str,
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "daily_pnl": state.get("daily_pnl", 0),
        "fills": state.get("fills_today", []),
        "exits": state.get("exits_today", []),
        "errors": state.get("errors", []),
        "shadow_blocks": dict(shadow_counts),
        "status": "active" if state.get("fills_today") else "waiting"
    }
    path = os.path.join(GITHUB_DATA, "pulse.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pulse_data, f, indent=2)
    return path


# -- Time helpers ----------------------------------------------
def _now_et():
    """Current time in ET."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: Luxembourg CEST = ET + 6
        return datetime.datetime.utcnow() - datetime.timedelta(hours=4)

def _in_market():
    now = _now_et()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return MARKET_OPEN[0]*60+MARKET_OPEN[1] <= t < MARKET_CLOSE[0]*60+MARKET_CLOSE[1]


# -- Main ------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="V7 Pulse Monitor")
    parser.add_argument("--eod", action="store_true", help="End-of-day report")
    parser.add_argument("--status", action="store_true", help="Quick status")
    parser.add_argument("--force", action="store_true", help="Run outside market hours")
    parser.add_argument("--email", action="store_true", help="Send report via email draft")
    args = parser.parse_args()

    now_et = _now_et()
    today_str = now_et.strftime("%Y-%m-%d")

    if not args.force and not args.status and not _in_market() and not args.eod:
        plog("Outside market hours, skipping.")
        return

    state = load_state()

    # Parse new log lines since last check
    start_line = state.get("last_line", 0)
    # On new day, re-scan full log
    if state.get("last_date") != today_str:
        start_line = 0

    events, new_line = parse_new_lines(V7_LOG, start_line)
    state["last_line"] = new_line

    if args.eod:
        # End-of-day mode: full analysis + shadow counts
        shadow_counts = count_shadow_today(V7_SHADOW, today_str)
        # Make sure state is up to date
        _, _, state = analyze(events, state, today_str)
        report = build_eod_report(state, shadow_counts)
        save_report(report, f"eod_{today_str}.txt")
        save_pulse_json(state, shadow_counts, today_str)
        save_state(state)

        print("\n".join(report))
        plog(f"EOD report saved. P&L: {state.get('daily_pnl', 0):.2f}")

        if args.email:
            return {"report": report, "alerts": [], "email": True, "subject": f"V7 EOD {today_str} — {'+' if state.get('daily_pnl',0)>=0 else ''}{state.get('daily_pnl',0):.2f}"}
        return

    # Standard pulse check
    report, alerts, state = analyze(events, state, today_str)

    # Save state
    save_state(state)

    # Save periodic report
    ts = now_et.strftime("%H%M")
    save_report(report, f"pulse_{today_str}_{ts}.txt")

    # Save pulse JSON for dashboard
    shadow_counts = count_shadow_today(V7_SHADOW, today_str)
    save_pulse_json(state, shadow_counts, today_str)

    # Print
    if args.status:
        for line in report:
            print(line)
        return

    for line in report:
        print(line)

    plog(f"Pulse check done. Fills={len(state.get('fills_today',[]))} PnL={state.get('daily_pnl',0):.2f} Alerts={len(alerts)}")

    if args.email or alerts:
        return {"report": report, "alerts": alerts, "email": True,
                "subject": f"V7 {'[!!]' if alerts else '[OK]'} {today_str} {ts}ET — {len(state.get('fills_today',[]))} trades {'+' if state.get('daily_pnl',0)>=0 else ''}{state.get('daily_pnl',0):.2f}"}

    return {"report": report, "alerts": alerts, "email": False}


if __name__ == "__main__":
    result = main()
