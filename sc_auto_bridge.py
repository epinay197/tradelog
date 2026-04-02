"""
sc_auto_bridge.py v3 — Sierra Chart TradeActivityLogs + V7 Predator Log → GitHub
Parses SC binary TradeActivityLog_*.data files, pairs FIFO round-trips,
enriches with V7 Predator Elite log metadata, pushes trades.json
"""
import json, os, struct, glob, re, base64, hashlib, datetime, sys, argparse
from zoneinfo import ZoneInfo
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

# ── Market-hours gate ──────────────────────────────────────
def in_market_window():
    """True if current ET time is 04:15-16:15 on a weekday."""
    now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    t = now_et.hour * 60 + now_et.minute
    return 4 * 60 + 15 <= t < 16 * 60 + 15

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
    "RTY": {"denom": 100, "pv": 100.0},
    "M2K": {"denom": 100, "pv": 10.0},
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

# ── V7 Predator Log Parser ─────────────────────────────────────────────────
_V7_LINE_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+(V7_\w+)[:\s]+(.*)')

_V7_FILL_RE = re.compile(
    r'(BUY|SELL)\s+@\s+([\d.]+)\s+Z=([\d.+-]+)\s+ATR=([\d.]+)\s+#(\d+)\s+\[(\w+)\]')

_V7_WIN_RE = re.compile(
    r'#(\d+)\s+PnL=([+\-\d.]+)\s+Day=\$([+\-\d.]+)\s+WR=(\d+)%\s+Z=([\d.+-]+)\s+\[(\w+)\]')

_V7_LOSS_RE = re.compile(
    r'#(\d+)\s+PnL=([+\-\d.]+)\s+Day=\$([+\-\d.]+)\s+Consec=(\d+)\s+WR=(\d+)%\s+\[(\w+)\]')

_V7_TRAIL_RE = re.compile(
    r'peak=([+\-\d.]+)\s+dd=([\d.]+)\s+\[(\w+)\]')

_V7_HARD_RE = re.compile(
    r'loss=([+\-\d.]+)\s+limit=([+\-\d.]+)\s+\[(\w+)\]')

_V7_BE_RE = re.compile(
    r'peak=([+\-\d.]+)\s+now=([+\-\d.]+)\s+\[(\w+)\]')

_V7_SHADOW_RE = re.compile(
    r'^\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\]\s+BLOCKED\s+(\w+):\s+.*\[(\w+)\]')


def parse_v7_log(log_path):
    """
    Parse V7_Predator_Log.txt into structured events.
    Returns dict keyed by (date, base_symbol) → list of event dicts.
    Each event: {type, time, side, price, z, atr, trade_num, pnl, exit_type, peak, dd, ...}
    """
    events = defaultdict(list)
    if not os.path.isfile(log_path):
        return events

    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for raw_line in f:
            line = raw_line.strip()
            m = _V7_LINE_RE.match(line)
            if not m:
                continue
            date_str, time_str, tag, payload = m.groups()

            if tag == 'V7_FILL':
                fm = _V7_FILL_RE.search(payload)
                if fm:
                    sym_raw = fm.group(6)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'FILL',
                        'time':      time_str,
                        'side':      fm.group(1),
                        'price':     float(fm.group(2)),
                        'z':         float(fm.group(3)),
                        'atr':       float(fm.group(4)),
                        'trade_num': int(fm.group(5)),
                        'sym_raw':   sym_raw,
                    })

            elif tag == 'V7_WIN':
                wm = _V7_WIN_RE.search(payload)
                if wm:
                    sym_raw = wm.group(6)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'WIN',
                        'time':      time_str,
                        'trade_num': int(wm.group(1)),
                        'pnl':       float(wm.group(2)),
                        'day_pnl':   float(wm.group(3)),
                        'win_rate':  int(wm.group(4)),
                        'z':         float(wm.group(5)),
                        'sym_raw':   sym_raw,
                    })

            elif tag == 'V7_LOSS':
                lm = _V7_LOSS_RE.search(payload)
                if lm:
                    sym_raw = lm.group(6)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'LOSS',
                        'time':      time_str,
                        'trade_num': int(lm.group(1)),
                        'pnl':       float(lm.group(2)),
                        'day_pnl':   float(lm.group(3)),
                        'consec':    int(lm.group(4)),
                        'win_rate':  int(lm.group(5)),
                        'sym_raw':   sym_raw,
                    })

            elif tag == 'V7_TRAIL_STOP':
                tm = _V7_TRAIL_RE.search(payload)
                if tm:
                    sym_raw = tm.group(3)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'TRAIL_STOP',
                        'time':      time_str,
                        'peak':      float(tm.group(1)),
                        'drawdown':  float(tm.group(2)),
                        'sym_raw':   sym_raw,
                    })

            elif tag == 'V7_HARD_STOP':
                hm = _V7_HARD_RE.search(payload)
                if hm:
                    sym_raw = hm.group(3)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'HARD_STOP',
                        'time':      time_str,
                        'loss':      float(hm.group(1)),
                        'limit':     float(hm.group(2)),
                        'sym_raw':   sym_raw,
                    })

            elif tag == 'V7_BE_GUARD':
                bm = _V7_BE_RE.search(payload)
                if bm:
                    sym_raw = bm.group(3)
                    base = get_base(sym_raw)
                    events[(date_str, base)].append({
                        'type':      'BE_GUARD',
                        'time':      time_str,
                        'peak':      float(bm.group(1)),
                        'now':       float(bm.group(2)),
                        'sym_raw':   sym_raw,
                    })

    return events


def parse_v7_shadow(shadow_path):
    """
    Parse V7_Shadow_Log.txt — count blocked signals per (date, base_symbol, reason).
    Returns dict keyed by (date, base) → {reason: count, ...}
    """
    counts = defaultdict(lambda: defaultdict(int))
    if not os.path.isfile(shadow_path):
        return counts

    with open(shadow_path, 'r', encoding='utf-8', errors='replace') as f:
        for raw_line in f:
            m = _V7_SHADOW_RE.match(raw_line.strip())
            if m:
                date_str, _, reason, sym_raw = m.groups()
                base = get_base(sym_raw)
                counts[(date_str, base)][reason] += 1

    return counts


def _time_to_minutes(t_str):
    """'HH:MM:SS' or 'HH:MM' → minutes since midnight."""
    parts = t_str.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def enrich_with_v7(trades, v7_events, v7_shadow):
    """
    Match V7 log events to paired SC trades by (date, base_symbol).
    Enriches each trade dict in-place with V7 metadata fields.
    """
    for trade in trades:
        date = trade['date']
        # Derive base from the trade symbol (e.g. NQM26 → NQ)
        base = get_base(trade.get('symbol', ''))
        key = (date, base)

        evts = v7_events.get(key, [])
        shadow = v7_shadow.get(key, {})

        # Find FILL events that could match this trade's entry time window
        entry_mins = _time_to_minutes(trade.get('time', '00:00'))
        exit_mins = _time_to_minutes(trade.get('exitTime', '00:00'))

        # Collect V7 fills within ±3 min of entry time
        matched_fills = []
        for e in evts:
            if e['type'] == 'FILL':
                fill_mins = _time_to_minutes(e['time'])
                if abs(fill_mins - entry_mins) <= 3:
                    matched_fills.append(e)

        # Collect exit events (WIN/LOSS/TRAIL/HARD/BE) within ±3 min of exit time
        matched_exits = []
        for e in evts:
            if e['type'] in ('WIN', 'LOSS', 'TRAIL_STOP', 'HARD_STOP', 'BE_GUARD'):
                evt_mins = _time_to_minutes(e['time'])
                if abs(evt_mins - exit_mins) <= 3:
                    matched_exits.append(e)

        # Pick best fill match (closest to entry time)
        best_fill = None
        if matched_fills:
            best_fill = min(matched_fills,
                            key=lambda e: abs(_time_to_minutes(e['time']) - entry_mins))

        # Determine exit type from matched exit events
        exit_type = ""
        v7_peak = None
        v7_exit_z = None
        for e in matched_exits:
            if e['type'] == 'TRAIL_STOP':
                exit_type = "trail_stop"
                v7_peak = e.get('peak')
            elif e['type'] == 'HARD_STOP':
                exit_type = "hard_stop"
            elif e['type'] == 'BE_GUARD':
                exit_type = "breakeven"
                v7_peak = e.get('peak')
            elif e['type'] == 'WIN':
                if not exit_type:
                    exit_type = "win"
                v7_exit_z = e.get('z')
                v7_peak = None  # WIN may not have peak
            elif e['type'] == 'LOSS':
                if not exit_type:
                    exit_type = "loss"

        # Also scan for peak from trail/BE events anywhere in the trade window
        for e in evts:
            if e['type'] in ('TRAIL_STOP', 'BE_GUARD'):
                evt_mins = _time_to_minutes(e['time'])
                if entry_mins <= evt_mins <= exit_mins + 1:
                    if v7_peak is None or e.get('peak', 0) > v7_peak:
                        v7_peak = e.get('peak')

        # Enrich trade with V7 fields
        v7_data = {}
        if best_fill:
            v7_data['v7_z_entry'] = best_fill.get('z')
            v7_data['v7_atr'] = best_fill.get('atr')
            v7_data['v7_entry_side'] = best_fill.get('side')
            v7_data['v7_trade_num'] = best_fill.get('trade_num')
        if exit_type:
            v7_data['v7_exit_type'] = exit_type
        if v7_exit_z is not None:
            v7_data['v7_exit_z'] = v7_exit_z
        if v7_peak is not None:
            v7_data['v7_peak_unreal'] = v7_peak
        if shadow:
            v7_data['v7_signals_blocked'] = dict(shadow)

        # Auto-tag setup from V7 if not already tagged
        if not trade.get('setup') and best_fill:
            z_val = best_fill.get('z', 0)
            if abs(z_val) > 3.0:
                trade['setup'] = 'V7-Momentum'
            elif abs(z_val) > 2.0:
                trade['setup'] = 'V7-Signal'

        # Merge v7 into trade
        if v7_data:
            trade['v7'] = v7_data
            trade['source'] = 'auto+v7'

    return trades


def build_v7_trades(v7_events, v7_shadow, comm_per_contract=4.0):
    """
    Build complete trade records purely from V7 log data.
    Used when SC binary TradeActivityLogs have no fills (V7 is the order source).
    Pairs V7_FILL entries with subsequent WIN/LOSS/exit events by trade_num + symbol.
    """
    trades = []
    for (date_str, base), evts in v7_events.items():
        fills = [e for e in evts if e['type'] == 'FILL']
        exits = [e for e in evts if e['type'] in ('WIN', 'LOSS')]
        exit_events = [e for e in evts if e['type'] in
                       ('TRAIL_STOP', 'HARD_STOP', 'BE_GUARD')]

        # Group by trade_num
        for fill in fills:
            tnum = fill.get('trade_num', 0)
            sym_raw = fill.get('sym_raw', '')

            # Find matching exit (WIN or LOSS with same trade_num)
            matched_exit = None
            for ex in exits:
                if ex.get('trade_num') == tnum:
                    matched_exit = ex
                    break

            # Find exit mechanism (TRAIL/HARD/BE closest after fill time)
            fill_mins = _time_to_minutes(fill['time'])
            exit_mechanism = ""
            peak_unreal = None
            exit_time_str = fill['time']  # fallback

            if matched_exit:
                exit_time_str = matched_exit['time']
                exit_mins = _time_to_minutes(exit_time_str)
            else:
                exit_mins = fill_mins + 60  # assume 1hr max if no exit found

            for ee in exit_events:
                ee_mins = _time_to_minutes(ee['time'])
                if fill_mins <= ee_mins <= exit_mins + 1:
                    if ee['type'] == 'TRAIL_STOP':
                        exit_mechanism = 'trail_stop'
                        peak_unreal = ee.get('peak')
                    elif ee['type'] == 'HARD_STOP':
                        exit_mechanism = 'hard_stop'
                    elif ee['type'] == 'BE_GUARD':
                        exit_mechanism = 'breakeven'
                        peak_unreal = ee.get('peak')

            # Determine side and compute exit price from PnL
            side_str = fill['side']  # BUY or SELL from V7
            entry_price = fill['price']
            m = meta(base)
            pv = m['pv']

            if matched_exit:
                pnl = matched_exit.get('pnl', 0)
                # Back-compute exit price: pnl = (exit - entry) * pv * direction
                direction = -1 if side_str == 'SELL' else 1
                if pv > 0:
                    exit_price = entry_price + (pnl / (pv * 1))  # qty=1 for V7
                else:
                    exit_price = entry_price
                is_win = matched_exit['type'] == 'WIN'
                if not exit_mechanism:
                    exit_mechanism = 'win' if is_win else 'loss'
            else:
                exit_price = entry_price
                pnl = 0
                is_win = False

            gross_pnl = pnl
            commission = comm_per_contract * 1  # V7 trades 1 contract
            net_pnl = gross_pnl - commission

            clean_sym = re.sub(r'_FUT_\w+$', '', sym_raw)
            trade_side = "Short" if side_str == "SELL" else "Long"

            # Entry/exit times — V7 log times are in local/CEST, convert display
            entry_hm = fill['time'][:5]  # HH:MM
            exit_hm = exit_time_str[:5] if matched_exit else entry_hm

            duration = max(0, _time_to_minutes(exit_hm) - _time_to_minutes(entry_hm))

            trade_id = hashlib.md5(
                f"{sym_raw}{date_str}{entry_price:.4f}{tnum}v7".encode()
            ).hexdigest()[:12]

            # Z-score setup tag
            z_val = fill.get('z', 0)
            setup = 'V7-Momentum' if abs(z_val) > 3.0 else 'V7-Signal'

            shadow = v7_shadow.get((date_str, base), {})

            v7_data = {
                'v7_z_entry': fill.get('z'),
                'v7_atr': fill.get('atr'),
                'v7_entry_side': side_str,
                'v7_trade_num': tnum,
                'v7_exit_type': exit_mechanism,
            }
            if matched_exit and matched_exit['type'] == 'WIN':
                v7_data['v7_exit_z'] = matched_exit.get('z')
            if peak_unreal is not None:
                v7_data['v7_peak_unreal'] = peak_unreal
            if shadow:
                v7_data['v7_signals_blocked'] = dict(shadow)

            trades.append({
                "id":          trade_id,
                "date":        date_str,
                "time":        entry_hm,
                "symbol":      clean_sym,
                "side":        trade_side,
                "qty":         1,
                "entryPrice":  round(entry_price, 4),
                "exitPrice":   round(exit_price, 4),
                "grossPnl":    round(gross_pnl, 2),
                "commission":  round(commission, 2),
                "netPnl":      round(net_pnl, 2),
                "exitTime":    exit_hm,
                "duration":    duration,
                "rMultiple":   0,
                "grade":       "",
                "setup":       setup,
                "notes":       "",
                "source":      "v7",
                "v7":          v7_data,
            })

    return sorted(trades, key=lambda t: (t['date'], t['time']))


# ── SC timestamp decoder ───────────────────────────────────────────────────
# SC stores datetimes as microseconds since December 30, 1899 UTC
_SC_EPOCH_OFFSET_US = (25567 + 2) * 86400 * 1_000_000  # Dec-30-1899 to Jan-1-1970 in us

def sc_ts_to_et_str(ts_us):
    """Return HH:MM in ET (DST-aware)."""
    try:
        unix_us = ts_us - _SC_EPOCH_OFFSET_US
        dt_utc = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(microseconds=unix_us)
        dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
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

        # Extract trade notes from SC (tag 112 = order text notes)
        trade_note = get_str(r, 112)

        fills.append({
            'date':     date_str,
            'sym_raw':  sym_raw,
            'base':     base,
            'side':     side,
            'qty':      fill_qty,
            'price':    actual_price,
            'order_id': order_id,
            'time':     fill_time,
            'ts_us':    ts_us,
            'pv':       m['pv'],
            'note':     trade_note,
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

        # Compute duration in minutes from entry to exit
        def _time_mins(t):
            try:
                h, m = map(int, t.split(":"))
                return h * 60 + m
            except Exception:
                return 0
        duration = max(0, _time_mins(exit_time) - _time_mins(entry_time))

        # Extract notes and auto-detect setup from SC trade notes
        all_notes = [f.get('note', '') for f in entry_fills + exit_fills]
        combined_notes = " ".join(n for n in all_notes if n).strip()
        setup = ""
        # Auto-tag: look for setup keywords in notes
        setup_keywords = {
            "breakout": "Breakout", "BO": "Breakout",
            "pullback": "Pullback", "PB": "Pullback",
            "reversal": "Reversal", "REV": "Reversal",
            "trend": "Trend", "momentum": "Momentum", "MOM": "Momentum",
            "range": "Range", "fade": "Fade",
            "vwap": "VWAP", "orb": "ORB",
            "gap": "Gap Fill", "scalp": "Scalp",
        }
        for kw, label in setup_keywords.items():
            if re.search(rf'\b{kw}\b', combined_notes, re.IGNORECASE):
                setup = label
                break

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
            "duration":    duration,
            "rMultiple":   0,
            "grade":       "",
            "setup":       setup,
            "notes":       combined_notes,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run regardless of time/day")
    args = parser.parse_args()

    if not args.force:
        if not in_market_window():
            sys.exit(0)

    log("=== SC Bridge v3 (binary TradeActivityLogs + V7 Predator) ===")
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

    # ── V7 Predator log enrichment ──────────────────────────────
    v7_log_path = cfg.get("v7_log", os.path.join(
        sc_parent, "V7_Predator_Log.txt"))
    v7_shadow_path = cfg.get("v7_shadow_log", os.path.join(
        sc_parent, "V7_Shadow_Log.txt"))

    v7_events = parse_v7_log(v7_log_path)
    v7_shadow = parse_v7_shadow(v7_shadow_path)

    if v7_events:
        log(f"V7 log: {sum(len(v) for v in v7_events.values())} events across "
            f"{len(v7_events)} symbol-days")
    else:
        log("V7 log: no events found (file missing or empty)")
    if v7_shadow:
        total_blocked = sum(sum(r.values()) for r in v7_shadow.values())
        log(f"V7 shadow: {total_blocked} blocked signals across "
            f"{len(v7_shadow)} symbol-days")

    new_trades = enrich_with_v7(new_trades, v7_events, v7_shadow)
    v7_enriched = sum(1 for t in new_trades if 'v7' in t)
    log(f"V7 enrichment: {v7_enriched}/{len(new_trades)} SC trades matched")

    # Build standalone V7 trades for dates/symbols with no SC binary fills
    if v7_events:
        v7_only_trades = build_v7_trades(v7_events, v7_shadow, comm)
        # Deduplicate: skip V7 trades that overlap with SC-paired trades (same date+base+time±3min)
        sc_keys = set()
        for t in new_trades:
            base = get_base(t.get('symbol', ''))
            sc_keys.add((t['date'], base, _time_to_minutes(t.get('time', '00:00'))))
        added_v7 = []
        for vt in v7_only_trades:
            base = get_base(vt.get('symbol', ''))
            vt_mins = _time_to_minutes(vt.get('time', '00:00'))
            # Check if any SC trade is within ±3 min
            overlap = any(abs(vt_mins - m) <= 3
                          for (d, b, m) in sc_keys
                          if d == vt['date'] and b == base)
            if not overlap:
                added_v7.append(vt)
        if added_v7:
            new_trades.extend(added_v7)
            new_trades.sort(key=lambda t: (t['date'], t['time']))
            log(f"V7 standalone trades: {len(added_v7)} (no SC binary match)")
        else:
            log("V7 standalone trades: 0 (all covered by SC fills)")

    existing, sha = get_existing_trades(cfg)
    existing_ids = {t["id"] for t in existing if "id" in t}
    added = [t for t in new_trades if t["id"] not in existing_ids]

    # Re-enrich existing trades that don't yet have V7 data
    if v7_events:
        stale = [t for t in existing if 'v7' not in t]
        if stale:
            enrich_with_v7(stale, v7_events, v7_shadow)
            re_enriched = sum(1 for t in stale if 'v7' in t)
            if re_enriched:
                log(f"V7 re-enriched {re_enriched} existing trades")

    if not added:
        # Even with no new trades, push if we re-enriched existing ones
        re_enriched_any = v7_events and any('v7' in t for t in existing
                                            if t.get('source') != 'auto+v7')
        if re_enriched_any:
            if put_trades(cfg, existing, sha):
                log(f"Pushed V7 enrichment for existing trades ({len(existing)} total)")
                notify("TradeLog", "V7 enrichment synced.")
            return
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
