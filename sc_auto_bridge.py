"""
sc_auto_bridge.py v2 — Sierra Chart TradeActivityLogs → GitHub
Parses SC binary TradeActivityLog_*.data files, pairs FIFO round-trips, pushes trades.json
"""
import json, os, struct, glob, re, base64, hashlib, datetime, sys
import urllib.request, urllib.error
from collections import defaultdict

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "sc_bridge.log")

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

# ── Config ─────────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

# ── Symbol metadata ────────────────────────────────────────────────────────
# price_denom: divide stored double by this to get actual price
# point_val:   $/point per contract
SYMBOL_META = {
    "NQ":  {"denom": 100, "pv": 20.0},
    "MNQ": {"denom": 100, "pv":  2.0},
    "ES":  {"denom": 100, "pv": 50.0},
    "MES": {"denom": 100, "pv":  5.0},
    "YM":  {"denom":   1, "pv": 10.0},
    "MYM": {"denom":   1, "pv":  0.5},
    "RTY": {"denom":  10, "pv": 100.0},
    "M2K": {"denom":  10, "pv": 10.0},
    "CL":  {"denom": 100, "pv": 1000.0},
    "GC":  {"denom":  10, "pv": 100.0},
    "ZB":  {"denom": 100, "pv": 31.25},
    "ZN":  {"denom": 100, "pv": 31.25},
    "SI":  {"denom": 100, "pv": 50.0},
}

def get_base(sym_raw):
    """MNQH26_FUT_CME -> MNQ"""
    s = re.sub(r'(_FUT_CME|_FUT_CBOT|_FUT_NYMEX|_FUT_COMEX)$', '', sym_raw.upper())
    s = re.sub(r'[FGHJKMNQUVXZ]\d{2}$', '', s)
    return s

def meta(base):
    for k, v in SYMBOL_META.items():
        if base.startswith(k):
            return v
    return {"denom": 100, "pv": 5.0}

# ── SC timestamp decoder ───────────────────────────────────────────────────
# SC stores datetimes as microseconds since December 30, 1899 UTC
_SC_EPOCH_OFFSET_US = (25567 + 2) * 86400 * 1_000_000  # Dec-30-1899 to Jan-1-1970 in us

def sc_ts_to_et_str(ts_us):
    """Return HH:MM in ET (UTC-5 simplified)."""
    try:
        unix_us = ts_us - _SC_EPOCH_OFFSET_US
        dt_utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(microseconds=unix_us)
        dt_et = dt_utc - datetime.timedelta(hours=5)
        return dt_et.strftime("%H:%M")
    except Exception:
        return "00:00"

# ── Binary TLV parser ──────────────────────────────────────────────────────
def parse_tlv_records(data):
    """
    Parse SC binary TLV format.
    Each field: [tag uint32 LE][length uint32 LE][value bytes]
    Records terminated by tag=199 len=0.
    """
    pos = 0
    cur = {}
    recs = []
    while pos + 8 <= len(data):
        tag = struct.unpack_from('<I', data, pos)[0]
        ln  = struct.unpack_from('<I', data, pos + 4)[0]
        if tag > 1000 or ln > 1_000_000:
            pos += 1
            continue
        if pos + 8 + ln > len(data):
            break
        val = data[pos + 8: pos + 8 + ln]
        pos += 8 + ln
        if tag == 199 and ln == 0:
            if cur:
                recs.append(cur)
            cur = {}
        else:
            cur[tag] = val
    if cur:
        recs.append(cur)
    return recs

# ── Fill extraction ─────────────────────────────────────────────────────────
def get_i64(r, tag):
    v = r.get(tag)
    return struct.unpack('<q', v)[0] if v and len(v) == 8 else 0

def get_f64(r, tag):
    v = r.get(tag)
    return struct.unpack('<d', v)[0] if v and len(v) == 8 else 0.0

def get_i32(r, tag):
    v = r.get(tag)
    return struct.unpack('<i', v)[0] if v and len(v) == 4 else 0

def get_str(r, tag):
    v = r.get(tag)
    return v.decode('latin-1').rstrip('\x00') if v else ''

def extract_fills(fp, date_str):
    """Extract fill events from one .data file."""
    try:
        data = open(fp, 'rb').read()
    except Exception:
        return []
    recs = parse_tlv_records(data)
    fills = []
    for r in recs:
        desc = get_str(r, 104)
        if 'Filled' not in desc and 'Fill)' not in desc:
            continue
        sym_raw = get_str(r, 103)
        if not sym_raw:
            continue
        stored_price = get_f64(r, 113)
        if stored_price <= 0:
            continue
        # Fill qty: tag 126 > abs(tag 125) > tag 108
        if 126 in r:
            fill_qty = abs(get_i32(r, 126))
        elif 125 in r:
            fill_qty = abs(int(get_f64(r, 125)))
        else:
            fill_qty = abs(int(get_f64(r, 108)))
        if fill_qty <= 0:
            continue

        side_byte = r[109][0] if (109 in r and r[109]) else 0
        side = 'BUY' if side_byte == 1 else 'SELL'

        order_id = get_i64(r, 105)
        ts_us = get_i64(r, 102) or get_i64(r, 160)
        fill_time = sc_ts_to_et_str(ts_us) if ts_us > 0 else '00:00'

        base = get_base(sym_raw)
        m = meta(base)
        actual_price = round(stored_price / m['denom'], 4)

        fills.append({
            'date':     date_str,
            'sym_raw':  sym_raw,
            'base':     base,
            'side':     side,
            'qty':      fill_qty,
            'price':    actual_price,
            'order_id': order_id,
            'time':     fill_time,
            'pv':       m['pv'],
        })
    return fills

# ── FIFO trade pairing ──────────────────────────────────────────────────────
def pair_fills(all_fills, comm_per_contract):
    by_key = defaultdict(lambda: {'BUY': [], 'SELL': []})
    for f in all_fills:
        by_key[(f['date'], f['base'])][f['side']].append(f)

    trades = []
    for (date, base), sides in by_key.items():
        buys  = sides['BUY']
        sells = sides['SELL']
        if not buys or not sells:
            continue

        buy_qty  = sum(f['qty'] for f in buys)
        sell_qty = sum(f['qty'] for f in sells)
        qty = min(buy_qty, sell_qty)
        if qty == 0:
            continue

        avg_buy  = sum(f['price'] * f['qty'] for f in buys)  / buy_qty
        avg_sell = sum(f['price'] * f['qty'] for f in sells) / sell_qty

        first_buy_id  = min(f['order_id'] for f in buys)
        first_sell_id = min(f['order_id'] for f in sells)
        is_long = first_buy_id < first_sell_id

        pv = buys[0]['pv'] if buys else sells[0]['pv']
        gross = (avg_sell - avg_buy) * qty * pv
        commission = comm_per_contract * qty
        net_pnl = gross - commission

        sym_raw = buys[0]['sym_raw'] if buys else sells[0]['sym_raw']
        clean_sym = re.sub(r'_FUT_\w+$', '', sym_raw)

        entry_fills = buys if is_long else sells
        exit_fills  = sells if is_long else buys
        entry_time = min(f['time'] for f in entry_fills)
        exit_time  = max(f['time'] for f in exit_fills)

        trade_id = hashlib.md5(
            f"{sym_raw}{date}{avg_buy:.4f}{avg_sell:.4f}{qty}".encode()
        ).hexdigest()[:12]

        trades.append({
            "id":          trade_id,
            "date":        date,
            "time":        entry_time,
            "symbol":      clean_sym,
            "side":        "Long" if is_long else "Short",
            "qty":         int(qty),
            "entryPrice":  round(avg_buy, 4),
            "exitPrice":   round(avg_sell, 4),
            "grossPnl":    round(gross, 2),
            "commission":  round(commission, 2),
            "netPnl":      round(net_pnl, 2),
            "exitTime":    exit_time,
            "duration":    0,
            "rMultiple":   0,
            "grade":       "",
            "setup":       "",
            "notes":       "",
            "source":      "auto",
        })

    return sorted(trades, key=lambda t: (t['date'], t['time']))

# ── GitHub API ─────────────────────────────────────────────────────────────
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

def get_existing_trades(cfg):
    resp, status = gh_request("GET", "data/trades.json", cfg)
    if status == 200:
        content = base64.b64decode(resp["content"]).decode()
        return json.loads(content), resp.get("sha")
    return [], None

def put_trades(cfg, trades, sha):
    content_b64 = base64.b64encode(json.dumps(trades, indent=2).encode()).decode()
    body = {
        "message":   f"Auto-sync trades {datetime.date.today()}",
        "content":   content_b64,
        "committer": {"name": "SC Bridge", "email": "bridge@tradelog.local"},
    }
    if sha:
        body["sha"] = sha
    resp, status = gh_request("PUT", "data/trades.json", cfg, body)
    return status in (200, 201)

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    log("=== SC Bridge v2 (binary TradeActivityLogs) ===")
    cfg = load_config()

    sc_dir   = cfg.get("sc_dir", "")
    # Derive logs dir: D:\Sierra Chart\Data -> D:\Sierra Chart\TradeActivityLogs
    sc_parent = os.path.dirname(sc_dir.rstrip("/\\"))
    logs_dir  = os.path.join(sc_parent, "TradeActivityLogs")
    if not os.path.isdir(logs_dir):
        for base in [r"D:\Sierra Chart", r"C:\Sierra Chart", r"D:\SierraChart", r"C:\SierraChart"]:
            candidate = os.path.join(base, "TradeActivityLogs")
            if os.path.isdir(candidate):
                logs_dir = candidate
                break

    if not os.path.isdir(logs_dir):
        log(f"ERROR: TradeActivityLogs not found (sc_dir={sc_dir})")
        sys.exit(1)
    log(f"Logs dir: {logs_dir}")

    # Auto-detect account ID (highest file count, non-simulated)
    from collections import Counter
    pat = re.compile(r'TradeActivityLog_(\d{4}-\d{2}-\d{2})_UTC\.(\w+)\.data')
    counts = Counter()
    all_entries = []
    for fn in os.listdir(logs_dir):
        m = pat.match(fn)
        if m:
            date_str, acct = m.group(1), m.group(2)
            if acct != 'None' and 'simulated' not in fn.lower():
                counts[acct] += 1
                all_entries.append((date_str, acct, os.path.join(logs_dir, fn)))

    if not counts:
        log("ERROR: No account log files found")
        sys.exit(1)

    account_id = counts.most_common(1)[0][0]
    log(f"Account: {account_id}")

    files = sorted((d, fp) for d, a, fp in all_entries if a == account_id)
    log(f"Processing {len(files)} log files...")

    all_fills = []
    for date_str, fp in files:
        fills = extract_fills(fp, date_str)
        if fills:
            all_fills.extend(fills)

    log(f"Total fills extracted: {len(all_fills)}")

    comm = float(cfg.get("default_comm", 4.0))
    new_trades = pair_fills(all_fills, comm)
    log(f"Paired into {len(new_trades)} round-trip trades")

    existing, sha = get_existing_trades(cfg)
    existing_ids = {t["id"] for t in existing if "id" in t}
    added = [t for t in new_trades if t["id"] not in existing_ids]

    if not added:
        log("No new trades to sync.")
        notify("TradeLog", "Up to date.")
        return

    merged = existing + added
    if put_trades(cfg, merged, sha):
        total_pnl = sum(t["netPnl"] for t in added)
        pnl_sign = "+" if total_pnl >= 0 else ""
        log(f"SUCCESS: pushed {len(added)} new trade(s) ({len(merged)} total) — net P&L: {pnl_sign}{total_pnl:.2f}")
        notify("TradeLog Synced", f"{len(added)} new trade(s) — P&L: {pnl_sign}${total_pnl:.2f}")
    else:
        log("ERROR: push to GitHub failed")
        notify("TradeLog ERROR", "Push failed — check sc_bridge.log")

if __name__ == "__main__":
    main()
