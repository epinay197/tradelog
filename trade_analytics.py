"""
trade_analytics.py — Trade Journal Analytics Engine
Fetches trades.json, computes performance metrics, generates analytics dashboard.
Runs every 15 min during ET market hours. Portable: Python 3.9+, stdlib only.

Usage:
    python trade_analytics.py              # Scheduled run (market-hours gated)
    python trade_analytics.py --force      # Run anytime
    python trade_analytics.py --weekly     # Weekly performance review
    python trade_analytics.py --local FILE # Use local trades.json
    python trade_analytics.py --json       # Print JSON to stdout, no push
"""
import json, os, sys, argparse, datetime, base64, hashlib, re
import urllib.request, urllib.error
from collections import defaultdict
from zoneinfo import ZoneInfo

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE    = os.path.join(SCRIPT_DIR, "analytics.log")
ET          = ZoneInfo("America/New_York")

# ── Logging ────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def notify(title, msg):
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, msg, duration=6, threaded=True)
    except Exception:
        pass

# ── Market-hours gate ──────────────────────────────────────────────────────
def in_market_window():
    now_et = datetime.datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    return 8 <= now_et.hour < 16

# ── Config ─────────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

# ── Data acquisition ───────────────────────────────────────────────────────
def gh_request(method, path, cfg, body=None):
    url = f"https://api.github.com/repos/{cfg['gh_owner']}/{cfg['gh_repo']}/contents/{path}"
    headers = {
        "Authorization": f"token {cfg['gh_token']}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    data_enc = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data_enc, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def fetch_trades_github(cfg):
    resp, status = gh_request("GET", "data/trades.json", cfg)
    if status == 200:
        content = base64.b64decode(resp["content"]).decode()
        return json.loads(content)
    return []

def fetch_trades_local(filepath):
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)

# ── Time helpers ───────────────────────────────────────────────────────────
TIME_BLOCKS = [
    ("09:30-10:30", (9, 30), (10, 30)),
    ("10:30-11:30", (10, 30), (11, 30)),
    ("11:30-12:30", (11, 30), (12, 30)),
    ("12:30-13:30", (12, 30), (13, 30)),
    ("13:30-14:30", (13, 30), (14, 30)),
    ("14:30-15:30", (14, 30), (15, 30)),
    ("15:30-16:00", (15, 30), (16, 0)),
]
DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

def time_to_minutes(t_str):
    try:
        h, m = map(int, t_str.split(":"))
        return h * 60 + m
    except Exception:
        return -1

def get_time_block(t_str):
    mins = time_to_minutes(t_str)
    if mins < 0:
        return "Unknown"
    for label, (sh, sm), (eh, em) in TIME_BLOCKS:
        if sh * 60 + sm <= mins < eh * 60 + em:
            return label
    if mins < 9 * 60 + 30:
        return "Pre-market"
    return "After-hours"

def get_dow(date_str):
    try:
        d = datetime.date.fromisoformat(date_str)
        wd = d.weekday()
        return DOW_NAMES[wd] if wd < 5 else date_str
    except Exception:
        return "Unknown"

# ── Core analytics ─────────────────────────────────────────────────────────
def _group_metrics(trades):
    """Compute standard metrics for a list of trades."""
    if not trades:
        return {"trades": 0, "winners": 0, "losers": 0, "breakeven": 0,
                "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
                "avg_win": 0, "avg_loss": 0, "largest_win": 0,
                "largest_loss": 0, "expectancy": 0, "profit_factor": 0}

    pnls = [t["netPnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p < 0]
    be      = [p for p in pnls if p == 0]

    n = len(pnls)
    w = len(winners)
    l = len(losers)
    wr = (w / n * 100) if n else 0
    total = sum(pnls)
    avg = total / n if n else 0
    avg_w = sum(winners) / w if w else 0
    avg_l = sum(losers) / l if l else 0
    gross_w = sum(winners)
    gross_l = abs(sum(losers))
    pf = (gross_w / gross_l) if gross_l else float("inf") if gross_w else 0
    lr = l / n if n else 0
    expectancy = (wr / 100 * avg_w) + (lr * avg_l)

    return {
        "trades": n, "winners": w, "losers": l, "breakeven": len(be),
        "win_rate": round(wr, 1),
        "total_pnl": round(total, 2), "avg_pnl": round(avg, 2),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "largest_win": round(max(pnls), 2) if pnls else 0,
        "largest_loss": round(min(pnls), 2) if pnls else 0,
        "expectancy": round(expectancy, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else 999.99,
    }

def compute_overall(trades):
    m = _group_metrics(trades)
    # Streaks
    max_w_streak = max_l_streak = cur_w = cur_l = 0
    cur_type = None
    cur_count = 0
    for t in sorted(trades, key=lambda x: (x["date"], x["time"])):
        if t["netPnl"] > 0:
            cur_w += 1; cur_l = 0
            cur_type = "W"; cur_count = cur_w
        elif t["netPnl"] < 0:
            cur_l += 1; cur_w = 0
            cur_type = "L"; cur_count = cur_l
        else:
            cur_w = cur_l = 0
            cur_type = "BE"; cur_count = 0
        max_w_streak = max(max_w_streak, cur_w)
        max_l_streak = max(max_l_streak, cur_l)
    m["max_win_streak"] = max_w_streak
    m["max_loss_streak"] = max_l_streak
    m["current_streak"] = f"{cur_count}{cur_type}" if cur_type else "—"
    return m

def compute_by_setup(trades):
    groups = defaultdict(list)
    for t in trades:
        setup = t.get("setup", "").strip() or "Unclassified"
        groups[setup].append(t)
    result = []
    for setup, tl in sorted(groups.items(), key=lambda x: -sum(t["netPnl"] for t in x[1])):
        m = _group_metrics(tl)
        m["setup"] = setup
        result.append(m)
    return result

def compute_by_time(trades):
    groups = defaultdict(list)
    for t in trades:
        block = get_time_block(t.get("time", ""))
        groups[block].append(t)
    result = []
    for label, _, _ in TIME_BLOCKS:
        m = _group_metrics(groups.get(label, []))
        m["block"] = label
        result.append(m)
    # Add pre/after if any
    for extra in ["Pre-market", "After-hours"]:
        if extra in groups:
            m = _group_metrics(groups[extra])
            m["block"] = extra
            result.append(m)
    return result

def compute_by_dow(trades):
    groups = defaultdict(list)
    for t in trades:
        dow = get_dow(t.get("date", ""))
        groups[dow].append(t)
    result = []
    for day in DOW_NAMES:
        m = _group_metrics(groups.get(day, []))
        m["day"] = day
        result.append(m)
    return result

def compute_by_symbol(trades):
    groups = defaultdict(list)
    for t in trades:
        sym = re.sub(r'[FGHJKMNQUVXZ]\d{2}$', '', t.get("symbol", "Unknown"))
        groups[sym].append(t)
    result = []
    for sym, tl in sorted(groups.items(), key=lambda x: -sum(t["netPnl"] for t in x[1])):
        m = _group_metrics(tl)
        m["symbol"] = sym
        result.append(m)
    return result

def compute_by_side(trades):
    longs  = [t for t in trades if t.get("side") == "Long"]
    shorts = [t for t in trades if t.get("side") == "Short"]
    return {"Long": _group_metrics(longs), "Short": _group_metrics(shorts)}

# ── Pattern detection ──────────────────────────────────────────────────────
def detect_patterns(trades, by_time, by_setup, by_dow):
    MIN_TRADES = 2  # minimum trades to flag a pattern

    def best_worst(items, key, name_key, min_n=MIN_TRADES):
        filtered = [i for i in items if i["trades"] >= min_n]
        if not filtered:
            return None, None
        best = max(filtered, key=lambda x: x[key])
        worst = min(filtered, key=lambda x: x[key])
        return (best[name_key], best[key]), (worst[name_key], worst[key])

    bh, wh = best_worst(by_time, "avg_pnl", "block")
    bs, ws = best_worst(by_setup, "avg_pnl", "setup")
    bd, wd = best_worst(by_dow, "avg_pnl", "day")

    # Recent trend (last 10 trades)
    recent = sorted(trades, key=lambda x: (x["date"], x["time"]))[-10:]
    recent_m = _group_metrics(recent)

    # Losing time blocks
    losing_blocks = [b for b in by_time
                     if b["trades"] >= MIN_TRADES and b["total_pnl"] < 0]

    return {
        "best_hour": bh, "worst_hour": wh,
        "best_setup": bs, "worst_setup": ws,
        "best_day": bd, "worst_day": wd,
        "recent_10": recent_m,
        "losing_blocks": [(b["block"], b["total_pnl"], b["trades"]) for b in losing_blocks],
    }

# ── Weekly review ──────────────────────────────────────────────────────────
def compute_weekly_review(trades):
    now_et = datetime.datetime.now(ET)
    today = now_et.date()
    # Current ISO week
    iso_year, iso_week, _ = today.isocalendar()
    mon = today - datetime.timedelta(days=today.weekday())
    fri = mon + datetime.timedelta(days=4)

    def week_of(d_str):
        try:
            return datetime.date.fromisoformat(d_str).isocalendar()[1]
        except:
            return -1

    this_week  = [t for t in trades if week_of(t["date"]) == iso_week
                  and datetime.date.fromisoformat(t["date"]).isocalendar()[0] == iso_year]
    prior_week = [t for t in trades if week_of(t["date"]) == iso_week - 1
                  and datetime.date.fromisoformat(t["date"]).isocalendar()[0] == iso_year]

    # Last 4 weeks
    four_wk = [t for t in trades
               if iso_week - 4 < week_of(t["date"]) <= iso_week
               and datetime.date.fromisoformat(t["date"]).isocalendar()[0] == iso_year]

    cw = _group_metrics(this_week)
    pw = _group_metrics(prior_week)
    fw = _group_metrics(four_wk)

    # Daily breakdown for current week
    daily = {}
    for t in this_week:
        d = t["date"]
        daily.setdefault(d, []).append(t)
    daily_summary = []
    for d in sorted(daily.keys()):
        dm = _group_metrics(daily[d])
        dm["date"] = d
        dm["day"] = get_dow(d)
        daily_summary.append(dm)

    return {
        "week_number": iso_week,
        "date_range": f"{mon.isoformat()} to {fri.isoformat()}",
        "current_week": cw,
        "prior_week": pw,
        "four_week_avg": fw,
        "daily_breakdown": daily_summary,
        "delta_vs_prior": {
            "win_rate": round(cw["win_rate"] - pw["win_rate"], 1),
            "avg_pnl": round(cw["avg_pnl"] - pw["avg_pnl"], 2),
            "total_pnl": round(cw["total_pnl"] - pw["total_pnl"], 2),
        } if pw["trades"] > 0 else None,
    }

# ── Summary + recommendations ─────────────────────────────────────────────
def generate_summary(overall, patterns):
    lines = []
    n = overall["trades"]
    lines.append(f"Across {n} trade{'s' if n != 1 else ''}, "
                 f"win rate is {overall['win_rate']}% with "
                 f"an expectancy of ${overall['expectancy']:+.2f} per trade.")
    lines.append(f"Total net P&L: ${overall['total_pnl']:+,.2f}. "
                 f"Profit factor: {overall['profit_factor']:.2f}.")

    if patterns["best_hour"]:
        bh_name, bh_val = patterns["best_hour"]
        lines.append(f"Best performing hour: {bh_name} (avg ${bh_val:+.2f}).")
    if patterns["worst_hour"]:
        wh_name, wh_val = patterns["worst_hour"]
        lines.append(f"Worst performing hour: {wh_name} (avg ${wh_val:+.2f}).")

    r10 = patterns["recent_10"]
    if r10["trades"] >= 5:
        trend = "above" if r10["win_rate"] > overall["win_rate"] else "below"
        lines.append(f"Last 10 trades: {r10['win_rate']}% win rate ({trend} baseline).")

    if overall["current_streak"]:
        lines.append(f"Current streak: {overall['current_streak']}.")

    return " ".join(lines)

def generate_recommendations(overall, patterns, by_side):
    recs = []
    # Losing time blocks
    for block, pnl, count in patterns.get("losing_blocks", []):
        recs.append(f"Consistently losing in {block} ({count} trades, ${pnl:+,.2f}). "
                    f"Consider reducing size or avoiding this window.")

    # Best setup focus
    if patterns["best_setup"]:
        bs_name, bs_val = patterns["best_setup"]
        if bs_name != "Unclassified" and bs_val > overall["avg_pnl"] * 1.5:
            recs.append(f"'{bs_name}' setup outperforms (${bs_val:+.2f} avg). Double down on these entries.")

    # Side bias
    long_wr = by_side["Long"]["win_rate"]
    short_wr = by_side["Short"]["win_rate"]
    if by_side["Long"]["trades"] >= 3 and by_side["Short"]["trades"] >= 3:
        if long_wr > short_wr + 15:
            recs.append(f"Strong long bias: {long_wr}% vs {short_wr}% short win rate. Be selective on shorts.")
        elif short_wr > long_wr + 15:
            recs.append(f"Strong short bias: {short_wr}% vs {long_wr}% long win rate. Be selective on longs.")

    # Losing streak
    if overall["max_loss_streak"] >= 3 and overall["current_streak"].startswith(str(overall["max_loss_streak"])):
        recs.append("Currently at max losing streak. Consider reducing position size until streak breaks.")

    # Recent trend
    r10 = patterns["recent_10"]
    if r10["trades"] >= 5 and r10["win_rate"] < 40:
        recs.append(f"Recent 10-trade win rate is {r10['win_rate']}%. Review entry criteria before next trade.")

    # Best/worst day
    if patterns["best_day"]:
        bd_name, bd_val = patterns["best_day"]
        recs.append(f"Best day: {bd_name} (${bd_val:+.2f} avg). Lean into this session.")
    if patterns["worst_day"]:
        wd_name, wd_val = patterns["worst_day"]
        if wd_val < 0:
            recs.append(f"Worst day: {wd_name} (${wd_val:+.2f} avg). Consider sitting out or reducing size.")

    # Setup tagging
    has_setups = any(t.get("setup", "").strip() for t in [])  # placeholder
    by_setup_unclassified = overall["trades"]  # will be overridden
    if not recs or len(recs) < 2:
        recs.append("Tag your trades with setup types to unlock per-strategy analytics.")

    return recs

# ── HTML generation ────────────────────────────────────────────────────────
def _pnl_color(val):
    if val > 0: return "color:var(--green)"
    if val < 0: return "color:var(--red)"
    return "color:var(--muted)"

def _pnl_cell(val):
    return f'<td style="{_pnl_color(val)}">${val:+,.2f}</td>'

def _pct_cell(val):
    color = "var(--green)" if val >= 50 else "var(--red)"
    return f'<td style="color:{color}">{val:.1f}%</td>'

def generate_html(analytics, weekly=None):
    o = analytics["overall"]
    now_str = analytics["generated_at"]

    # Build metric cards
    cards = f"""
    <div class="cards">
      <div class="card"><div class="card-label">Total Trades</div><div class="card-val">{o['trades']}</div></div>
      <div class="card"><div class="card-label">Win Rate</div><div class="card-val" style="{_pnl_color(o['win_rate']-50)}">{o['win_rate']}%</div></div>
      <div class="card"><div class="card-label">Expectancy</div><div class="card-val" style="{_pnl_color(o['expectancy'])}">${o['expectancy']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Profit Factor</div><div class="card-val" style="{_pnl_color(o['profit_factor']-1)}">{o['profit_factor']:.2f}</div></div>
      <div class="card"><div class="card-label">Total P&amp;L</div><div class="card-val" style="{_pnl_color(o['total_pnl'])}">${o['total_pnl']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Avg P&amp;L</div><div class="card-val" style="{_pnl_color(o['avg_pnl'])}">${o['avg_pnl']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Largest Win</div><div class="card-val" style="color:var(--green)">${o['largest_win']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Largest Loss</div><div class="card-val" style="color:var(--red)">${o['largest_loss']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Avg Win</div><div class="card-val" style="color:var(--green)">${o['avg_win']:+,.2f}</div></div>
      <div class="card"><div class="card-label">Avg Loss</div><div class="card-val" style="color:var(--red)">${o['avg_loss']:+,.2f}</div></div>
      <div class="card"><div class="card-label">W / L</div><div class="card-val">{o['winners']} / {o['losers']}</div></div>
      <div class="card"><div class="card-label">Streak</div><div class="card-val">{o['current_streak']}</div></div>
    </div>"""

    # Summary + Recommendations
    summary_html = f'<div class="summary-box"><p>{analytics["summary"]}</p></div>'
    recs_items = "".join(f"<li>{r}</li>" for r in analytics["recommendations"])
    recs_html = f'<div class="recs-box"><h3>Recommendations</h3><ul>{recs_items}</ul></div>'

    # Overall stats table
    overall_tbl = f"""
    <table>
      <caption>Overall Statistics</caption>
      <tr><th>Metric</th><th>Value</th></tr>
      <tr><td>Total Trades</td><td>{o['trades']}</td></tr>
      <tr><td>Winners / Losers / Breakeven</td><td>{o['winners']} / {o['losers']} / {o['breakeven']}</td></tr>
      <tr><td>Win Rate</td>{_pct_cell(o['win_rate'])}</tr>
      <tr><td>Total P&amp;L</td>{_pnl_cell(o['total_pnl'])}</tr>
      <tr><td>Average P&amp;L per Trade</td>{_pnl_cell(o['avg_pnl'])}</tr>
      <tr><td>Average Win</td>{_pnl_cell(o['avg_win'])}</tr>
      <tr><td>Average Loss</td>{_pnl_cell(o['avg_loss'])}</tr>
      <tr><td>Largest Win</td>{_pnl_cell(o['largest_win'])}</tr>
      <tr><td>Largest Loss</td>{_pnl_cell(o['largest_loss'])}</tr>
      <tr><td>Expectancy</td>{_pnl_cell(o['expectancy'])}</tr>
      <tr><td>Profit Factor</td><td>{o['profit_factor']:.2f}</td></tr>
      <tr><td>Max Win Streak</td><td>{o['max_win_streak']}</td></tr>
      <tr><td>Max Loss Streak</td><td>{o['max_loss_streak']}</td></tr>
      <tr><td>Current Streak</td><td>{o['current_streak']}</td></tr>
    </table>"""

    # Time of day table
    time_rows = ""
    for b in analytics["by_time_of_day"]:
        if b["trades"] == 0:
            time_rows += f'<tr><td>{b["block"]}</td><td class="muted">0</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td></tr>'
        else:
            time_rows += f'<tr><td>{b["block"]}</td><td>{b["trades"]}</td>{_pct_cell(b["win_rate"])}{_pnl_cell(b["avg_win"])}{_pnl_cell(b["avg_loss"])}{_pnl_cell(b["total_pnl"])}</tr>'
    time_tbl = f"""
    <table>
      <caption>Performance by Time of Day</caption>
      <tr><th>Time Block</th><th>Trades</th><th>Win Rate</th><th>Avg Win</th><th>Avg Loss</th><th>Total P&amp;L</th></tr>
      {time_rows}
    </table>"""

    # Day of week table
    dow_rows = ""
    for d in analytics["by_day_of_week"]:
        if d["trades"] == 0:
            dow_rows += f'<tr><td>{d["day"]}</td><td class="muted">0</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td></tr>'
        else:
            dow_rows += f'<tr><td>{d["day"]}</td><td>{d["trades"]}</td>{_pct_cell(d["win_rate"])}{_pnl_cell(d["avg_win"])}{_pnl_cell(d["avg_loss"])}{_pnl_cell(d["total_pnl"])}</tr>'
    dow_tbl = f"""
    <table>
      <caption>Performance by Day of Week</caption>
      <tr><th>Day</th><th>Trades</th><th>Win Rate</th><th>Avg Win</th><th>Avg Loss</th><th>Total P&amp;L</th></tr>
      {dow_rows}
    </table>"""

    # Setup table
    setup_rows = ""
    for s in analytics["by_setup"]:
        setup_rows += f'<tr><td>{s["setup"]}</td><td>{s["trades"]}</td>{_pct_cell(s["win_rate"])}{_pnl_cell(s["avg_win"])}{_pnl_cell(s["avg_loss"])}{_pnl_cell(s["total_pnl"])}{_pnl_cell(s["expectancy"])}</tr>'
    setup_tbl = f"""
    <table>
      <caption>Performance by Setup Type</caption>
      <tr><th>Setup</th><th>Trades</th><th>Win Rate</th><th>Avg Win</th><th>Avg Loss</th><th>Total P&amp;L</th><th>Expectancy</th></tr>
      {setup_rows}
    </table>"""

    # Symbol table
    sym_rows = ""
    for s in analytics["by_symbol"]:
        sym_rows += f'<tr><td>{s["symbol"]}</td><td>{s["trades"]}</td>{_pct_cell(s["win_rate"])}{_pnl_cell(s["avg_pnl"])}{_pnl_cell(s["total_pnl"])}</tr>'
    sym_tbl = f"""
    <table>
      <caption>Performance by Symbol</caption>
      <tr><th>Symbol</th><th>Trades</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Total P&amp;L</th></tr>
      {sym_rows}
    </table>"""

    # Side table
    side_data = analytics["by_side"]
    side_tbl = f"""
    <table>
      <caption>Long vs Short</caption>
      <tr><th>Side</th><th>Trades</th><th>Win Rate</th><th>Avg P&amp;L</th><th>Total P&amp;L</th></tr>
      <tr><td>Long</td><td>{side_data['Long']['trades']}</td>{_pct_cell(side_data['Long']['win_rate'])}{_pnl_cell(side_data['Long']['avg_pnl'])}{_pnl_cell(side_data['Long']['total_pnl'])}</tr>
      <tr><td>Short</td><td>{side_data['Short']['trades']}</td>{_pct_cell(side_data['Short']['win_rate'])}{_pnl_cell(side_data['Short']['avg_pnl'])}{_pnl_cell(side_data['Short']['total_pnl'])}</tr>
    </table>"""

    # Pattern detection
    p = analytics["patterns"]
    pat_items = []
    if p["best_hour"]:  pat_items.append(f'<li class="g">Best hour: <strong>{p["best_hour"][0]}</strong> — avg ${p["best_hour"][1]:+,.2f}</li>')
    if p["worst_hour"]: pat_items.append(f'<li class="r">Worst hour: <strong>{p["worst_hour"][0]}</strong> — avg ${p["worst_hour"][1]:+,.2f}</li>')
    if p["best_setup"]: pat_items.append(f'<li class="g">Best setup: <strong>{p["best_setup"][0]}</strong> — avg ${p["best_setup"][1]:+,.2f}</li>')
    if p["worst_setup"]:pat_items.append(f'<li class="r">Worst setup: <strong>{p["worst_setup"][0]}</strong> — avg ${p["worst_setup"][1]:+,.2f}</li>')
    if p["best_day"]:   pat_items.append(f'<li class="g">Best day: <strong>{p["best_day"][0]}</strong> — avg ${p["best_day"][1]:+,.2f}</li>')
    if p["worst_day"]:  pat_items.append(f'<li class="r">Worst day: <strong>{p["worst_day"][0]}</strong> — avg ${p["worst_day"][1]:+,.2f}</li>')
    for blk, pnl, cnt in p.get("losing_blocks", []):
        pat_items.append(f'<li class="r">Losing block: <strong>{blk}</strong> — {cnt} trades, ${pnl:+,.2f}</li>')
    pat_list = "".join(pat_items) if pat_items else '<li class="m">Not enough data for pattern detection yet.</li>'
    pat_html = f'<div class="pat-box"><h3>Pattern Detection</h3><ul>{pat_list}</ul></div>'

    # Weekly review section
    weekly_html = ""
    if weekly:
        wk = weekly
        cw = wk["current_week"]
        weekly_html = f"""
    <div class="section">
      <h2>Weekly Review — Week {wk['week_number']} ({wk['date_range']})</h2>
      <div class="cards">
        <div class="card"><div class="card-label">Week Trades</div><div class="card-val">{cw['trades']}</div></div>
        <div class="card"><div class="card-label">Week Win Rate</div><div class="card-val" style="{_pnl_color(cw['win_rate']-50)}">{cw['win_rate']}%</div></div>
        <div class="card"><div class="card-label">Week P&amp;L</div><div class="card-val" style="{_pnl_color(cw['total_pnl'])}">${cw['total_pnl']:+,.2f}</div></div>
        <div class="card"><div class="card-label">Week Expectancy</div><div class="card-val" style="{_pnl_color(cw['expectancy'])}">${cw['expectancy']:+,.2f}</div></div>
      </div>"""

        if wk.get("delta_vs_prior"):
            d = wk["delta_vs_prior"]
            weekly_html += f"""
      <table>
        <caption>Week-over-Week Comparison</caption>
        <tr><th>Metric</th><th>This Week</th><th>Prior Week</th><th>Delta</th></tr>
        <tr><td>Win Rate</td>{_pct_cell(cw['win_rate'])}{_pct_cell(wk['prior_week']['win_rate'])}{_pnl_cell(d['win_rate'])}</tr>
        <tr><td>Avg P&amp;L</td>{_pnl_cell(cw['avg_pnl'])}{_pnl_cell(wk['prior_week']['avg_pnl'])}{_pnl_cell(d['avg_pnl'])}</tr>
        <tr><td>Total P&amp;L</td>{_pnl_cell(cw['total_pnl'])}{_pnl_cell(wk['prior_week']['total_pnl'])}{_pnl_cell(d['total_pnl'])}</tr>
      </table>"""

        if wk.get("daily_breakdown"):
            day_rows = ""
            for db in wk["daily_breakdown"]:
                day_rows += f'<tr><td>{db["day"]}</td><td>{db["date"]}</td><td>{db["trades"]}</td>{_pct_cell(db["win_rate"])}{_pnl_cell(db["total_pnl"])}</tr>'
            weekly_html += f"""
      <table>
        <caption>Daily Breakdown</caption>
        <tr><th>Day</th><th>Date</th><th>Trades</th><th>Win Rate</th><th>P&amp;L</th></tr>
        {day_rows}
      </table>"""

        weekly_html += "\n    </div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeLog Analytics</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Syne:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{
  --bg:#07070f;--s1:#0d0d1a;--s2:#12121f;--s3:#181828;--s4:#1f1f32;
  --border:#252538;--border2:#2e2e48;
  --text:#d0d0e8;--muted:#6060a0;
  --green:#00e87a;--red:#ff4d6a;--blue:#4d9dff;--yellow:#ffd060;--purple:#a060ff;
  --grbg:rgba(0,232,122,0.08);--rdbg:rgba(255,77,106,0.08);--blbg:rgba(77,157,255,0.1);
  --ui:'Syne',sans-serif;--mono:'DM Mono',monospace;
  --r:6px;--r2:10px;--r3:14px;
}}
body{{background:var(--bg);color:var(--text);font-family:var(--ui);font-size:14px;padding:24px;max-width:1200px;margin:0 auto}}
a{{color:var(--blue);text-decoration:none}}
a:hover{{text-decoration:underline}}
h1{{font-size:24px;font-weight:700;margin-bottom:4px}}
h2{{font-size:18px;font-weight:600;margin:32px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
h3{{font-size:15px;font-weight:600;margin-bottom:8px}}
.meta{{color:var(--muted);font-family:var(--mono);font-size:12px;margin-bottom:24px}}
.nav{{margin-bottom:24px;display:flex;gap:12px;font-family:var(--mono);font-size:13px}}

/* Cards */
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:24px}}
.card{{background:var(--s2);border:1px solid var(--border);border-radius:var(--r2);padding:14px 16px}}
.card-label{{font-size:11px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}}
.card-val{{font-size:20px;font-weight:700;font-family:var(--mono)}}

/* Tables */
table{{width:100%;border-collapse:collapse;margin:16px 0 28px;background:var(--s1);border-radius:var(--r2);overflow:hidden;border:1px solid var(--border)}}
caption{{padding:12px 16px;text-align:left;font-weight:600;font-size:14px;background:var(--s2);border-bottom:1px solid var(--border)}}
th{{padding:10px 14px;text-align:left;font-size:12px;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:0.5px;background:var(--s2);border-bottom:1px solid var(--border)}}
td{{padding:10px 14px;font-family:var(--mono);font-size:13px;border-bottom:1px solid var(--border)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(77,157,255,0.03)}}
td.muted{{color:var(--muted)}}

/* Summary & Recs */
.summary-box{{background:var(--s2);border:1px solid var(--border);border-radius:var(--r2);padding:18px 20px;margin-bottom:16px;line-height:1.7;font-size:14px}}
.recs-box,.pat-box{{background:var(--s2);border:1px solid var(--border);border-radius:var(--r2);padding:18px 20px;margin-bottom:24px}}
.recs-box ul,.pat-box ul{{list-style:none;padding:0}}
.recs-box li,.pat-box li{{padding:8px 12px;margin:6px 0;border-radius:var(--r);background:var(--s3);border-left:3px solid var(--yellow);font-size:13px;line-height:1.6}}
.pat-box li.g{{border-left-color:var(--green)}}
.pat-box li.r{{border-left-color:var(--red)}}
.pat-box li.m{{border-left-color:var(--muted)}}

.section{{margin-bottom:32px}}

/* Footer */
.footer{{text-align:center;color:var(--muted);font-family:var(--mono);font-size:11px;padding:32px 0;border-top:1px solid var(--border);margin-top:40px}}
</style>
</head>
<body>

<h1>TradeLog Analytics</h1>
<div class="meta">Generated: {now_str} &nbsp;|&nbsp; {o['trades']} trades analyzed</div>
<div class="nav"><a href="index.html">&larr; Trade Journal</a></div>

{cards}
{summary_html}
{recs_html}

<h2>1. Overall Statistics</h2>
{overall_tbl}

<h2>2. Performance by Setup Type</h2>
<!-- Tag trades with setup names in your journal for per-strategy breakdown -->
{setup_tbl}

<h2>3. Performance by Time of Day</h2>
<!-- Hourly blocks aligned to US market hours -->
{time_tbl}

<h2>4. Performance by Day of Week</h2>
{dow_tbl}

<h2>5. Performance by Symbol</h2>
{sym_tbl}

<h2>6. Long vs Short</h2>
{side_tbl}

<h2>7. Pattern Detection</h2>
<!-- Automatically identifies best/worst hours, setups, and days -->
{pat_html}

{weekly_html}

<div class="footer">TradeLog Analytics Engine &mdash; auto-generated every 15 min during market hours</div>
</body>
</html>"""

# ── GitHub push ────────────────────────────────────────────────────────────
def push_file(cfg, path, content_str, message):
    resp, status = gh_request("GET", path, cfg)
    sha = resp.get("sha") if status == 200 else None
    b64 = base64.b64encode(content_str.encode("utf-8")).decode()
    body = {
        "message": message,
        "content": b64,
        "committer": {"name": "Analytics Engine", "email": "analytics@tradelog.local"},
    }
    if sha:
        body["sha"] = sha
    resp, status = gh_request("PUT", path, cfg, body)
    return status in (200, 201)

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="TradeLog Analytics Engine")
    parser.add_argument("--force", action="store_true", help="Run regardless of time/day")
    parser.add_argument("--weekly", action="store_true", help="Include weekly review")
    parser.add_argument("--local", type=str, default=None, help="Path to local trades.json")
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout, no push")
    args = parser.parse_args()

    if not args.force and not sys.stdin.isatty():
        if not in_market_window():
            sys.exit(0)

    log("=== TradeLog Analytics Engine ===")

    # Load trades
    if args.local:
        trades = fetch_trades_local(args.local)
        log(f"Loaded {len(trades)} trades from {args.local}")
    else:
        cfg = load_config()
        trades = fetch_trades_github(cfg)
        log(f"Fetched {len(trades)} trades from GitHub")

    if not trades:
        log("No trades found. Nothing to analyze.")
        return

    # Compute all analytics
    overall   = compute_overall(trades)
    by_setup  = compute_by_setup(trades)
    by_time   = compute_by_time(trades)
    by_dow    = compute_by_dow(trades)
    by_symbol = compute_by_symbol(trades)
    by_side   = compute_by_side(trades)
    patterns  = detect_patterns(trades, by_time, by_setup, by_dow)

    analytics = {
        "generated_at": datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "trade_count": len(trades),
        "overall": overall,
        "by_setup": by_setup,
        "by_time_of_day": by_time,
        "by_day_of_week": by_dow,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "patterns": patterns,
        "summary": generate_summary(overall, patterns),
        "recommendations": generate_recommendations(overall, patterns, by_side),
    }

    weekly = None
    if args.weekly:
        weekly = compute_weekly_review(trades)
        analytics["weekly_review"] = weekly

    if args.json:
        print(json.dumps(analytics, indent=2, default=str))
        return

    # Generate HTML
    html = generate_html(analytics, weekly)
    log(f"Analytics computed: {overall['trades']} trades, {overall['win_rate']}% WR, ${overall['total_pnl']:+,.2f}")

    # Push to GitHub
    if args.local:
        # Local mode: write files locally
        out_dir = os.path.dirname(os.path.abspath(args.local))
        with open(os.path.join(out_dir, "analytics.json"), "w") as f:
            json.dump(analytics, f, indent=2, default=str)
        with open(os.path.join(out_dir, "analytics.html"), "w") as f:
            f.write(html)
        log("Written analytics.json + analytics.html locally")
    else:
        ts = datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M")
        msg = f"Analytics update {ts}"
        ok1 = push_file(cfg, "data/analytics.json", json.dumps(analytics, indent=2, default=str), msg)
        ok2 = push_file(cfg, "analytics.html", html, msg)
        if ok1 and ok2:
            log("SUCCESS: pushed analytics.json + analytics.html to GitHub")
            notify("TradeLog Analytics", f"{overall['trades']} trades | {overall['win_rate']}% WR | ${overall['total_pnl']:+,.2f}")
        else:
            log(f"ERROR: push failed (json={'OK' if ok1 else 'FAIL'}, html={'OK' if ok2 else 'FAIL'})")
            notify("TradeLog Analytics ERROR", "Push failed — check analytics.log")

if __name__ == "__main__":
    main()
