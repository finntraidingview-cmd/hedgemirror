import requests
from flask import Flask, request, jsonify, Response, send_from_directory
import os

app = Flask(__name__)
BASE = "https://api.topstepx.com"

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/<path:path>", methods=["GET","POST","OPTIONS"])
def proxy(path):
    if request.method == "OPTIONS":
        return "", 200
    h = {"Content-Type": "application/json"}
    t = request.headers.get("Authorization", "")
    if t:
        h["Authorization"] = t
    try:
        r = requests.request(
            request.method,
            f"{BASE}/api/{path}",
            json=request.get_json(silent=True),
            headers=h,
            timeout=10
        )
        print(f"→ {path} | {r.status_code} | {r.text[:200]}")
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({"error": str(e), "success": False, "errorMessage": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
