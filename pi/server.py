import json, time, threading
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import yfinance as yf

BASE = Path(__file__).parent
TICKERS = BASE / "tickers.json"
app = Flask(__name__)
lock = threading.Lock()
quotes = {}          # symbole -> {"s", "p", "c", "cur", "prev", "live"}
state = {"ts": 0, "ws": None}
CUR = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CHF": "CHF "}

def load():
    return json.loads(TICKERS.read_text())

# --- Base : clôture de la veille + devise, toutes les 60 s (et prix si le direct est muet) ---
def baseline():
    for s in load():
        try:
            fi = yf.Ticker(s).fast_info
            last, prev = fi.last_price, fi.previous_close
            with lock:
                q = quotes.setdefault(s, {"s": s, "live": 0})
                q["prev"], q["cur"] = prev, CUR.get(fi.currency or "", "")
                if time.time() - q["live"] > 120:      # pas de direct récent : on prend ce prix
                    q["p"] = round(last, 2)
                    q["c"] = round((last - prev) / prev * 100, 2)
                state["ts"] = time.time()
        except Exception:
            with lock:
                quotes.setdefault(s, {"s": s, "p": None, "c": None, "cur": "", "live": 0})

def baseline_loop():
    while True:
        baseline()
        time.sleep(60)

# --- Direct : flux temps réel Yahoo, chaque transaction arrive en ~1 s ---
def on_tick(m):
    s, p = m.get("id"), m.get("price")
    if not s or p is None:
        return
    with lock:
        q = quotes.setdefault(s, {"s": s, "cur": "", "live": 0})
        prev = q.get("prev") or m.get("previous_close")
        q["p"] = round(p, 2)
        q["c"] = round((p - prev) / prev * 100, 2) if prev else round(m.get("change_percent", 0), 2)
        q["live"] = state["ts"] = time.time()

def stream_loop():
    while True:
        ws = yf.WebSocket(verbose=False)
        state["ws"] = ws
        try:
            ws.subscribe(load())
            ws.listen(on_tick)          # rend la main si la connexion tombe
        except Exception:
            pass
        finally:
            try: ws.close()
            except Exception: pass
        time.sleep(2)                   # puis on se reconnecte

@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.get("/edit")
def edit():
    return send_from_directory(BASE, "edit.html")

@app.get("/api/quotes")
def get_quotes():
    with lock:
        data = [{k: quotes.get(s, {}).get(k) for k in ("p", "c", "cur")} | {"s": s} for s in load()]
        return jsonify({"data": data, "ts": state["ts"]})

@app.route("/api/tickers", methods=["GET", "POST"])
def tickers():
    if request.method == "POST":
        syms = []
        for s in request.json:
            s = str(s).strip().upper()
            if s and s not in syms:
                syms.append(s)
        TICKERS.write_text(json.dumps(syms))
        threading.Thread(target=baseline, daemon=True).start()
        if state["ws"]:
            try: state["ws"].close()    # force la reconnexion avec la nouvelle liste
            except Exception: pass
    return jsonify(load())

if __name__ == "__main__":
    threading.Thread(target=baseline_loop, daemon=True).start()
    threading.Thread(target=stream_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8050)
