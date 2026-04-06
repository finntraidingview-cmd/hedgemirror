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

# ── Mirror Logic ──
def run_mirror(pair_id):
    try:
        from signalrcore.hub_connection_builder import HubConnectionBuilder
    except ImportError:
        log_msg(pair_id, "ERROR: signalrcore nicht installiert")
        return

    s = mirror_sessions.get(pair_id)
    if not s: return

    # Get fresh TSX token
    try:
        r = requests.post(f"{TSX_BASE}/api/Auth/validate",
            headers={"Authorization": f"Bearer {s['tsxToken']}"}, timeout=10)
        if not r.ok:
            log_msg(pair_id, f"TSX Token invalid: {r.status_code}")
            return
    except Exception as e:
        log_msg(pair_id, f"TSX Auth error: {e}")
        return

    token = s["tsxToken"]
    hub_url = f"https://rtc.topstepx.com/hubs/user?access_token={token}"

    conn = (HubConnectionBuilder()
        .with_url(hub_url, options={
            "skip_negotiation": True,
        })
        .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 10, "reconnect_interval": 5})
        .build())

    def on_order(data):
        try:
            e = data[0] if isinstance(data, list) else data
            log_msg(pair_id, f"Order: {str(e)[:150]}")
            status   = e.get("status", "")
            side     = e.get("side", e.get("action", ""))
            contract = e.get("contractId", "")
            qty      = int(e.get("size", e.get("quantity", 1)))
            order_id = str(e.get("orderId", e.get("id", "")))
            if status in ("Filled", "PartiallyFilled", 2, "2") and side in ("Buy", "Sell", 0, 1):
                open_hedge(pair_id, order_id, side, contract, qty)
        except Exception as ex:
            log_msg(pair_id, f"Order event error: {ex}")

    def on_position(data):
        try:
            e = data[0] if isinstance(data, list) else data
            log_msg(pair_id, f"Position: {str(e)[:150]}")
            status = e.get("status", "")
            pos_id = str(e.get("positionId", e.get("id", "")))
            if status in ("Closed", "Liquidated", "Cancelled", 0):
                close_hedge(pair_id, pos_id)
        except Exception as ex:
            log_msg(pair_id, f"Position event error: {ex}")

    # Correct event names from ProjectX docs
    conn.on("GatewayUserOrder", on_order)
    conn.on("GatewayUserPosition", on_position)
    conn.on("GatewayUserTrade", lambda d: log_msg(pair_id, f"Trade: {str(d)[:150]}"))
    conn.on("GatewayUserAccount", lambda d: log_msg(pair_id, f"Account: {str(d)[:150]}"))
    conn.on_open(lambda: [
        log_msg(pair_id, "✅ WebSocket verbunden"),
        conn.send("SubscribeAccounts", []),
    ])
    conn.on_close(lambda: log_msg(pair_id, "WebSocket getrennt"))
    conn.on_error(lambda e: log_msg(pair_id, f"WS Error: {e}"))

    conn.start()
    log_msg(pair_id, "Mirror gestartet")

    while mirror_sessions.get(pair_id, {}).get("active"):
        time.sleep(1)

    conn.stop()
    log_msg(pair_id, "Mirror gestoppt")

def open_hedge(pair_id, order_id, side, contract, qty):
    s = mirror_sessions.get(pair_id)
    if not s: return

    # Map symbol
    base = contract.split(".")[2] if "." in contract else contract[:3]
    mt_symbol = s["symbolMap"].get(base, "NAS100")

    # Inverted side
    mt_side = "ORDER_TYPE_BUY" if side == "Sell" else "ORDER_TYPE_SELL"
    lots = round(qty * s["multiplier"], 2)

    body = {"symbol": mt_symbol, "volume": lots, "actionType": mt_side, "comment": f"HM-{order_id[:8]}"}
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
            log_msg(pair_id, f"❌ Open failed: {r.status_code} {r.text[:100]}")
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
