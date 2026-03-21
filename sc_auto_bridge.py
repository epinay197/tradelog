"""
sc_auto_bridge.py — Sierra Chart → GitHub Auto-Sync
Reads SC fills CSV, pairs FIFO round-trips, pushes to data/trades.json via GitHub API
"""
import json, os, csv, glob, base64, hashlib, datetime, sys, time, io
import urllib.request, urllib.error

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "sc_bridge.log")

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def notify(title, msg):
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, msg, duration=6, threaded=True)
    except Exception:
        pass

def load_config():
    if not os.path.exists(CONFIG_FILE):
        log("ERROR: config.json not found. Run setup_windows.bat first.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)

def gh_request(method, path, cfg, body=None):
    url = f"https://api.github.com/repos/{cfg['gh_owner']}/{cfg['gh_repo']}/contents/{path}"
    headers = {
        "Authorization": f"token {cfg['gh_token']}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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
        "message": f"Auto-sync trades {datetime.date.today()}",
        "content": content_b64,
        "committer": {"name": "SC Bridge", "email": "bridge@tradelog.local"}
    }
    if sha:
        body["sha"] = sha
    resp, status = gh_request("PUT", "data/trades.json", cfg, body)
    return status in (200, 201)

TICK_MAP = {
    "NQ":5, "MNQ":0.5, "ES":12.5, "MES":1.25,
    "YM":5, "MYM":0.5, "RTY":10, "GC":10,
    "CL":10, "ZB":31.25, "ZN":31.25
}

def get_tick_value(sym, cfg):
    base = sym.rstrip("0123456789").upper()
    for k, v in TICK_MAP.items():
        if base.startswith(k):
            return v
    return float(cfg.get("default_tick", 5))

def find_sc_files(sc_dir):
    patterns = ["AccountTrade*.csv","TradeAccount*.csv","Fills*.csv","*Fill*.csv","*Trade*.csv"]
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(sc_dir, p)))
    return list(set(files))

def parse_fills(filepath):
    fills = []
    with open(filepath, newline="", errors="ignore") as f:
        sample = f.read(2048); f.seek(0)
        dialect = "excel-tab" if sample.count("\t") > sample.count(",") else "excel"
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            try:
                sym = (row.get("Symbol") or row.get("symbol","")).strip()
                side = (row.get("Buy/Sell") or row.get("Side","")).strip().upper()
                qty = abs(float(row.get("Quantity") or row.get("Qty",0)))
                price = float(row.get("Price") or row.get("Fill Price",0))
                date_str = (row.get("Date") or row.get("Trade Date","")).strip()
                time_str = (row.get("Time") or row.get("Trade Time","00:00")).strip()
                if not sym or not side or qty == 0 or price == 0:
                    continue
                # Try various date formats
                dt = None
                for fmt in ("%Y-%m-%d","%m/%d/%Y","%d/%m/%Y","%Y%m%d"):
                    try: dt = datetime.datetime.strptime(date_str, fmt); break
                    except: pass
                if not dt:
                    continue
                fills.append({"sym":sym,"side":side,"qty":qty,"price":price,
                              "date":dt.strftime("%Y-%m-%d"),"time":time_str})
            except Exception:
                continue
    return fills

def pair_fills(fills, cfg):
    from collections import defaultdict
    by_sym_date = defaultdict(list)
    for f in fills:
        by_sym_date[(f["sym"], f["date"])].append(f)
    
    trades = []
    for (sym, date), group in by_sym_date.items():
        buys  = [f for f in group if "BUY" in f["side"]]
        sells = [f for f in group if "SELL" in f["side"]]
        tick  = get_tick_value(sym, cfg)
        comm  = float(cfg.get("default_comm", 4.0))
        
        buy_q  = sum(f["qty"] for f in buys)
        sell_q = sum(f["qty"] for f in sells)
        qty    = min(buy_q, sell_q)
        if qty == 0:
            continue
        
        avg_buy  = sum(f["price"]*f["qty"] for f in buys)  / buy_q  if buy_q  else 0
        avg_sell = sum(f["price"]*f["qty"] for f in sells) / sell_q if sell_q else 0
        
        ticks   = (avg_sell - avg_buy) * qty
        gross   = ticks * tick
        net_pnl = gross - comm * qty
        
        entry_time = sorted([f["time"] for f in buys])[0]  if buys  else "09:30"
        exit_time  = sorted([f["time"] for f in sells])[-1] if sells else "16:00"
        
        trade_id = hashlib.md5(f"{sym}{date}{avg_buy:.4f}{avg_sell:.4f}{qty}".encode()).hexdigest()[:12]
        trades.append({
            "id": trade_id, "date": date, "time": entry_time,
            "symbol": sym, "side": "Long" if buys else "Short",
            "qty": int(qty), "entry": round(avg_buy,4), "exit": round(avg_sell,4),
            "grossPnl": round(gross,2), "commission": round(comm*qty,2),
            "netPnl": round(net_pnl,2), "exitTime": exit_time,
            "duration": 0, "rMultiple": 0, "grade":"", "setup":"", "notes":"",
            "source": "auto"
        })
    return trades

def main():
    log("=== SC Auto-Bridge starting ===")
    cfg = load_config()
    sc_dir = cfg.get("sc_dir","")
    if not sc_dir or not os.path.isdir(sc_dir):
        log(f"ERROR: SC directory not found: {sc_dir}")
        sys.exit(1)
    
    files = find_sc_files(sc_dir)
    if not files:
        log("No Sierra Chart fills files found.")
        notify("TradeLog", "No SC fills files found.")
        return
    log(f"Found {len(files)} fills file(s)")
    
    all_fills = []
    for fp in files:
        f = parse_fills(fp)
        log(f"  {os.path.basename(fp)}: {len(f)} fills")
        all_fills.extend(f)
    
    new_trades = pair_fills(all_fills, cfg)
    log(f"Paired into {len(new_trades)} trades")
    
    existing, sha = get_existing_trades(cfg)
    existing_ids = {t["id"] for t in existing if "id" in t}
    added = [t for t in new_trades if t["id"] not in existing_ids]
    
    if not added:
        log("No new trades to sync.")
        notify("TradeLog", "Already up to date — no new trades.")
        return
    
    merged = existing + added
    if put_trades(cfg, merged, sha):
        log(f"SUCCESS: pushed {len(added)} new trade(s).")
        notify("TradeLog Synced ✓", f"{len(added)} new trade(s) pushed to GitHub.")
    else:
        log("ERROR: failed to push trades.")
        notify("TradeLog ERROR", "Push to GitHub failed — check sc_bridge.log.")

if __name__ == "__main__":
    main()
