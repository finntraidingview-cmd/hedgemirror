import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from flask import Flask, request, jsonify, Response, send_from_directory
import os

app = Flask(__name__)
TSX_BASE = "https://api.topstepx.com"
MA_BASE  = "https://mt-client-api-v1.london.agiliumtrade.ai"

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ma-token, ma-account"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/<path:path>", methods=["GET","POST","OPTIONS"])
def tsx_proxy(path):
    if request.method == "OPTIONS": return "", 200
    h = {"Content-Type": "application/json"}
    t = request.headers.get("Authorization", "")
    if t: h["Authorization"] = t
    try:
        r = requests.request(request.method, f"{TSX_BASE}/api/{path}",
            json=request.get_json(silent=True), headers=h, timeout=10)
        print(f"TSX → {path} | {r.status_code}")
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route("/ma/<path:path>", methods=["GET","POST","OPTIONS"])
def ma_proxy(path):
    if request.method == "OPTIONS": return "", 200
    token      = request.headers.get("ma-token", "")
    account_id = request.headers.get("ma-account", "")
    h = {"Content-Type": "application/json", "auth-token": token}
    url = f"{MA_BASE}/users/current/accounts/{account_id}/account-information" if account_id and path == "account" else (f"{MA_BASE}/users/current/accounts/{account_id}/{path}" if account_id else f"{MA_BASE}/users/current/{path}")
    try:
        r = requests.request(request.method, url,
            json=request.get_json(silent=True), headers=h, timeout=15, verify=False)
        print(f"MA → {path} | {r.status_code} | {r.text[:100]}")
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
