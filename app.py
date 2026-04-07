import requests
import urllib3
import threading
import json
import time
import uuid
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from flask import Flask, request, jsonify, Response, send_from_directory
import os

app = Flask(__name__)
TSX_BASE = "https://api.topstepx.com"
MA_BASE  = "https://mt-client-api-v1.london.agiliumtrade.ai"

# Active mirror sessions: pair_id -> session data
mirror_sessions = {}

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ma-token, ma-account"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

# ── TopstepX Proxy ──
@app.route("/api/<path:path>", methods=["GET","POST","OPTIONS"])
def tsx_proxy(path):
    if request.method == "OPTIONS": return "", 200
    h = {"Content-Type": "application/json"}
    t = request.headers.get("Authorization", "")
    if t: h["Authorization"] = t
    try:
        r = requests.request(request.method, f"{TSX_BASE}/api/{path}",
            json=request.get_json(silent=True), headers=h, timeout=10)
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

# ── MetaApi Proxy ──
@app.route("/ma/<path:path>", methods=["GET","POST","OPTIONS"])
def ma_proxy(path):
    if request.method == "OPTIONS": return "", 200
    token      = request.headers.get("ma-token", "")
    account_id = request.headers.get("ma-account", "")
    h = {"Content-Type": "application/json", "auth-token": token}
    if account_id and path == "account":
        url = f"{MA_BASE}/users/current/accounts/{account_id}/account-information"
    elif account_id:
        url = f"{MA_BASE}/users/current/accounts/{account_id}/{path}"
    else:
        url = f"{MA_BASE}/users/current/{path}"
    try:
        r = requests.request(request.method, url,
            json=request.get_json(silent=True), headers=h, timeout=15, verify=False)
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Mirror Control ──
@app.route("/mirror/start", methods=["POST","OPTIONS"])
def mirror_start():
    if request.method == "OPTIONS": return "", 200
    data = request.get_json()
    pair_id     = data.get("pairId")
    tsx_token   = data.get("tsxToken")
    tsx_acc_id  = data.get("tsxAccountId")
    ma_token    = data.get("maToken")
    ma_acc_id   = data.get("maAccountId")
    multiplier  = float(data.get("multiplier", 0.5))
    symbol_map  = data.get("symbolMap", {"MNQ": "NAS100", "NQ": "NAS100", "ES": "US500", "MES": "US500"})

    if pair_id in mirror_sessions:
        return jsonify({"ok": True, "msg": "Already running"})

    session = {
        "pairId": pair_id,
        "tsxToken": tsx_token,
        "tsxAccountId": tsx_acc_id,
        "maToken": ma_token,
        "maAccountId": ma_acc_id,
        "multiplier": multiplier,
        "targetRiskEur": float(data.get("targetRiskEur", 0)),
        "symbolMap": symbol_map,
        "active": True,
        "positions": {},
        "log": []
    }
    mirror_sessions[pair_id] = session

    thread = threading.Thread(target=run_mirror, args=(pair_id,), daemon=True)
    thread.start()

    return jsonify({"ok": True})

@app.route("/mirror/stop", methods=["POST","OPTIONS"])
def mirror_stop():
    if request.method == "OPTIONS": return "", 200
    data = request.get_json()
    pair_id = data.get("pairId")
    if pair_id in mirror_sessions:
        mirror_sessions[pair_id]["active"] = False
        del mirror_sessions[pair_id]
    return jsonify({"ok": True})

@app.route("/mirror/status", methods=["GET"])
def mirror_status():
    result = {}
    for pid, s in mirror_sessions.items():
        result[pid] = {"active": s["active"], "log": s["log"][-20:], "positions": s["positions"]}
    return jsonify(result)

# ── Mirror Logic (Polling) ──
def run_mirror(pair_id):
    s = mirror_sessions.get(pair_id)
    if not s: return

    log_msg(pair_id, "Mirror gestartet (Polling 0.5s → 1s)")

    known_positions = {}
    start_time = time.time()

    while mirror_sessions.get(pair_id, {}).get("active"):
        try:
            r = requests.post(f"{TSX_BASE}/api/Position/searchOpen",
                headers={"Authorization": f"Bearer {s['tsxToken']}", "Content-Type": "application/json"},
                json={"accountId": int(s["tsxAccountId"])},
                timeout=10)

            if not r.ok:
                log_msg(pair_id, f"Poll error: {r.status_code}")
                time.sleep(5)
                continue

            d = r.json()
            positions = d.get("positions", d.get("data", []))
            current = {str(p.get("id", p.get("positionId", ""))): p for p in positions}

            # Neue Positionen → Hedge öffnen
            for pid, pos in current.items():
                if pid not in known_positions:
                    # Side fix: TopstepX gibt 0=Buy, 1=Sell oder "Buy"/"Sell"
                    raw_side = pos.get("side", pos.get("action", ""))
                    if raw_side in (0, "0", "Buy", "buy", "BUY", "Long", "long"):
                        side = "Buy"
                    else:
                        side = "Sell"
                    contract = pos.get("contractId", "")
                    qty = int(pos.get("size", pos.get("quantity", 1)))
                    tsx_risk = float(pos.get("initialRisk", pos.get("risk", 0)) or 0)
                    log_msg(pair_id, f"🆕 Neue Position: {side} {qty}x {contract} | risk=${tsx_risk}")
                    open_hedge(pair_id, pid, side, contract, qty, tsx_risk)

            # Geschlossene Positionen → Hedge schließen
            for pid in list(known_positions.keys()):
                if pid not in current:
                    log_msg(pair_id, f"❌ Position geschlossen: {pid}")
                    close_hedge(pair_id, pid)

            known_positions = current

        except Exception as e:
            log_msg(pair_id, f"Poll exception: {e}")

        # 0.5s die ersten 2 Minuten, dann 1s
        elapsed = time.time() - start_time
        time.sleep(0.5 if elapsed < 120 else 1.0)

    log_msg(pair_id, "Mirror gestoppt")

def open_hedge(pair_id, order_id, side, contract, qty, tsx_risk_usd=0):
    s = mirror_sessions.get(pair_id)
    if not s: return

    # Symbol mapping
    parts = contract.split(".")
    base = parts[3] if len(parts) > 3 else (parts[2] if len(parts) > 2 else contract[:3])
    mt_symbol = s["symbolMap"].get(base, "NAS100")

    # Lot Berechnung
    target_eur = float(s.get("targetRiskEur", 0))
    multiplier = float(s.get("multiplier", 1.0))

    if target_eur > 0 and tsx_risk_usd > 0:
        # Formel: Lots = (€ Ziel / $ Risiko) × MNQ Contracts × 2.33
        lots = round((target_eur / tsx_risk_usd) * qty * 2.33, 2)
        log_msg(pair_id, f"Lot Berechnung: ({target_eur}€ / ${tsx_risk_usd}) × {qty} × 2.33 = {lots}")
    else:
        # Fallback: fester Multiplikator
        lots = round(qty * multiplier, 2)

    lots = max(0.01, lots)

    # Invertierte Richtung: TSX Buy → MT5 Sell, TSX Sell → MT5 Buy
    mt_side = "ORDER_TYPE_SELL" if side == "Buy" else "ORDER_TYPE_BUY"

    body = {"symbol": mt_symbol, "volume": lots, "actionType": mt_side, "comment": f"HM-{str(order_id)[:8]}"}
    try:
        r = requests.post(
            f"{MA_BASE}/users/current/accounts/{s['maAccountId']}/trade",
            headers={"auth-token": s["maToken"], "Content-Type": "application/json"},
            json=body, timeout=15, verify=False)
        d = r.json()
        if r.ok:
            pos_id = str(d.get("positionId") or d.get("orderId", ""))
            s["positions"][order_id] = pos_id
            log_msg(pair_id, f"✅ Hedge OPEN: {mt_side.split('_')[-1]} {lots}x {mt_symbol} | pos={pos_id}")
        else:
            log_msg(pair_id, f"❌ Open failed: {r.status_code} {r.text[:150]}")
    except Exception as e:
        log_msg(pair_id, f"❌ Open error: {e}")

def close_hedge(pair_id, ref_id):
    s = mirror_sessions.get(pair_id)
    if not s: return

    pos_id = s["positions"].get(ref_id)
    if not pos_id:
        log_msg(pair_id, f"No position found for {ref_id}")
        return

    body = {"actionType": "POSITION_CLOSE_ID", "positionId": pos_id}
    try:
        r = requests.post(
            f"{MA_BASE}/users/current/accounts/{s['maAccountId']}/trade",
            headers={"auth-token": s["maToken"], "Content-Type": "application/json"},
            json=body, timeout=15, verify=False)
        if r.ok:
            log_msg(pair_id, f"✅ Hedge CLOSED: pos={pos_id}")
            del s["positions"][ref_id]
        else:
            log_msg(pair_id, f"❌ Close failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log_msg(pair_id, f"❌ Close error: {e}")

def log_msg(pair_id, msg):
    print(f"[{pair_id}] {msg}")
    if pair_id in mirror_sessions:
        mirror_sessions[pair_id]["log"].append({"ts": time.strftime("%H:%M:%S"), "msg": msg})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

@app.route("/debug/account", methods=["POST","OPTIONS"])
def debug_account():
    if request.method == "OPTIONS": return "", 200
    token = request.headers.get("Authorization","").replace("Bearer ","")
    r = requests.post(f"{TSX_BASE}/api/Account/search",
        json={"onlyActive": True},
        headers={"Authorization": f"Bearer {token}","Content-Type":"application/json"},
        timeout=10)
    return Response(r.content, status=r.status_code, content_type="application/json")
