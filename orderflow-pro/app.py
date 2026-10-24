import asyncio
import hmac
import json
import logging
import os
import threading
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

import pyotp
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("orderflow")
ROOT = Path(__file__).resolve().parent
COOKIE = "orderflow_session"
COOKIE_AGE = 43200
MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
FUTURES = {
    "NIFTY 50": ("NIFTY", "NFO", 2), "BANK NIFTY": ("BANKNIFTY", "NFO", 2),
    "FIN NIFTY": ("FINNIFTY", "NFO", 2), "MIDCAP NIFTY": ("MIDCPNIFTY", "NFO", 2),
    "SENSEX": ("SENSEX", "BFO", 4), "BANKEX": ("BANKEX", "BFO", 4),
    "CRUDE OIL": ("CRUDEOIL", "MCX", 5), "NATURAL GAS": ("NATURALGAS", "MCX", 5),
    "GOLD": ("GOLD", "MCX", 5), "SILVER": ("SILVER", "MCX", 5),
    "COPPER": ("COPPER", "MCX", 5), "ZINC": ("ZINC", "MCX", 5),
}
SPOT = {
    "NIFTY 50": ("NIFTY", "NSE", 1), "BANK NIFTY": ("BANKNIFTY", "NSE", 1),
    "FIN NIFTY": ("FINNIFTY", "NSE", 1), "MIDCAP NIFTY": ("MIDCPNIFTY", "NSE", 1),
    "SENSEX": ("SENSEX", "BSE", 3), "BANKEX": ("BANKEX", "BSE", 3),
}
state = {"login_ok": False, "feed_connected": False, "last_tick_at": None,
         "last_error": None, "subscribed": [], "clients": set(), "feed": None,
         "loop": None, "reconnect_scheduled": False}


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError("Missing environment variable: " + name)
    return value


def serializer():
    return URLSafeTimedSerializer(required_env("APP_SECRET_KEY"), salt="orderflow-session-v1")


def valid_cookie(raw):
    if not raw:
        return False
    try:
        serializer().loads(raw, max_age=COOKIE_AGE)
        return True
    except (BadSignature, SignatureExpired, RuntimeError):
        return False


def load_master():
    request = urllib.request.Request(MASTER_URL, headers={"User-Agent": "OrderFlowPro/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        rows = json.load(response)
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Angel One scrip-master format")
    return rows


def nearest_future(master, name, segment):
    options = []
    for row in master:
        if row.get("name") != name or row.get("exch_seg") != segment:
            continue
        if not str(row.get("instrumenttype", "")).startswith("FUT"):
            continue
        try:
            expiry = datetime.strptime(row["expiry"], "%d%b%Y").date()
        except (KeyError, ValueError, TypeError):
            continue
        if expiry >= date.today():
            options.append((expiry, row))
    return min(options, key=lambda item: item[0])[1] if options else None


async def broadcast(message):
    payload = json.dumps(message, separators=(",", ":"))
    for client in list(state["clients"]):
        try:
            await client.send_text(payload)
        except Exception:
            state["clients"].discard(client)


def push(message):
    loop = state.get("loop")
    if loop and not loop.is_closed():
        asyncio.run_coroutine_threadsafe(broadcast(message), loop)


def start_feed():
    try:
        api_key = required_env("ANGEL_API_KEY")
        client_code = required_env("ANGEL_CLIENT_CODE")
        pin = required_env("ANGEL_PIN")
        totp_seed = required_env("ANGEL_TOTP_SECRET")
        smart = SmartConnect(api_key=api_key)
        login = smart.generateSession(client_code, pin, pyotp.TOTP(totp_seed).now())
        if not login or not login.get("status"):
            raise RuntimeError("Angel One login rejected; check API access, PIN and TOTP seed")
        jwt = login["data"]["jwtToken"]
        feed_token = smart.getfeedToken()
        master = load_master()
        futures, spots, groups = {}, {}, {}
        for label, (name, segment, exchange_type) in FUTURES.items():
            row = nearest_future(master, name, segment)
            if not row:
                log.warning("No current futures contract for %s", label)
                continue
            token = str(row["token"])
            futures[token] = {"name": label, "lot": max(1, int(float(row.get("lotsize", 1))))}
            groups.setdefault(exchange_type, []).append(token)
        for label, (name, segment, exchange_type) in SPOT.items():
            row = next((x for x in master if x.get("name") == name and
                        x.get("exch_seg") == segment and x.get("instrumenttype") == "AMXIDX"), None)
            if row:
                token = str(row["token"])
                spots[token] = label
                groups.setdefault(exchange_type, []).append(token)
        if not futures:
            raise RuntimeError("No supported futures found in Angel One scrip master")
        previous = {}
        feed = SmartWebSocketV2(jwt, api_key, client_code, feed_token)
        state["feed"] = feed
        state["subscribed"] = sorted({item["name"] for item in futures.values()})

        def on_open(_):
            try:
                subs = [{"exchangeType": ex, "tokens": list(dict.fromkeys(tokens))}
                        for ex, tokens in groups.items() if tokens]
                feed.subscribe("orderflow-pro-live", 2, subs)
                state.update(feed_connected=True, last_error=None)
                log.info("Angel One websocket connected; exchange groups=%d", len(subs))
                push({"type": "status", "broker_login": True, "feed_connected": True})
            except Exception as exc:
                state.update(feed_connected=False, last_error="Subscription failed: " + type(exc).__name__)
                log.exception("SmartAPI subscription failed")

        def on_data(_, message):
            try:
                token = str(message.get("token", ""))
                if "last_traded_price" not in message:
                    return
                price = float(message["last_traded_price"]) / 100.0
                state["last_tick_at"] = datetime.now().astimezone().isoformat()
                if token in spots:
                    push({"s": spots[token], "spot": price})
                    return
                contract = futures.get(token)
                if not contract:
                    return
                volume = int(message.get("volume_trade_for_the_day") or 0)
                prev = previous.get(token)
                if prev is None:
                    previous[token] = {"price": price, "volume": volume, "side": 1}
                    return
                if volume < prev["volume"]:
                    prev.update(price=price, volume=volume)
                    return
                delta = volume - prev["volume"]
                side = 1 if price > prev["price"] else (0 if price < prev["price"] else prev["side"])
                prev.update(price=price, volume=volume, side=side)
                if delta:
                    push({"s": contract["name"], "p": price,
                          "q": max(1, round(delta / contract["lot"])), "b": side})
            except Exception as exc:
                state["last_error"] = "Tick processing error: " + type(exc).__name__
                log.exception("Tick processing failed")

        def on_error(*args):
            state.update(feed_connected=False, last_error="Angel One websocket error; inspect Render logs")
            log.error("SmartAPI websocket error: %s", args)

        def on_close(*args):
            state["feed_connected"] = False
            log.warning("SmartAPI websocket closed: %s", args)
            if not state["reconnect_scheduled"]:
                state["reconnect_scheduled"] = True
                def reconnect():
                    import time
                    time.sleep(5)
                    state["reconnect_scheduled"] = False
                    start_feed()
                threading.Thread(target=reconnect, daemon=True, name="angel-reconnect").start()

        feed.on_open, feed.on_data = on_open, on_data
        feed.on_error, feed.on_close = on_error, on_close
        state.update(login_ok=True, last_error=None)
        feed.connect()
    except Exception as exc:
        state.update(login_ok=False, feed_connected=False,
                     last_error=type(exc).__name__ + ": " + str(exc))
        log.exception("Angel One feed startup failed")


@asynccontextmanager
async def lifespan(_app):
    state["loop"] = asyncio.get_running_loop()
    required = ["APP_SECRET_KEY", "ORDERFLOW_ACCESS_PASSWORD", "ANGEL_API_KEY",
                "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        state["last_error"] = "Missing environment variables: " + ", ".join(missing)
        log.warning(state["last_error"])
    else:
        threading.Thread(target=start_feed, daemon=True, name="angel-smartapi-feed").start()
    yield
    try:
        if state.get("feed"):
            state["feed"].close_connection()
    except Exception:
        log.exception("SmartAPI feed shutdown error")


app = FastAPI(title="OrderFlow Pro", lifespan=lifespan)
LOGIN_HTML = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OrderFlow Pro Login</title>
<style>body{background:#0d1117;color:#dbe5f1;font:16px system-ui;display:grid;min-height:100vh;place-items:center;margin:0}
.box{width:min(360px,88vw);background:#151b23;padding:28px;border:1px solid #26303d;border-radius:12px}
input,button{box-sizing:border-box;width:100%;padding:12px;margin-top:12px;background:#0d1117;color:#dbe5f1;border:1px solid #26303d;border-radius:6px}
button{background:#2563eb;cursor:pointer}</style>
<form class="box" method="post" action="/api/login"><h2>OrderFlow Pro</h2>
<p>Private live-market footprint dashboard.</p><input name="password" type="password" required
autocomplete="current-password" placeholder="Access password"><button type="submit">Sign in</button></form></html>"""


@app.get("/health")
def health():
    return {"status": "ok", "broker_login": state["login_ok"],
            "feed_connected": state["feed_connected"], "last_tick_at": state["last_tick_at"],
            "subscribed": state["subscribed"], "last_error": state["last_error"]}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/api/login")
async def login(request: Request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("password", "")), required_env("ORDERFLOW_ACCESS_PASSWORD")):
        raise HTTPException(status_code=401, detail="Invalid password")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, serializer().dumps({"authenticated": True}), max_age=COOKIE_AGE,
                        httponly=True, secure=True, samesite="strict", path="/")
    return response


@app.post("/api/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/")
def index(request: Request):
    if not valid_cookie(request.cookies.get(COOKIE)):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(ROOT / "index.html", media_type="text/html")


@app.websocket("/ws")
async def websocket_feed(ws: WebSocket):
    if not valid_cookie(ws.cookies.get(COOKIE)):
        await ws.close(code=1008, reason="Login required")
        return
    await ws.accept()
    state["clients"].add(ws)
    try:
        await ws.send_text(json.dumps({"type": "status", "broker_login": state["login_ok"],
            "feed_connected": state["feed_connected"], "last_tick_at": state["last_tick_at"],
            "last_error": state["last_error"]}))
        while True:
            await asyncio.sleep(20)
            await ws.send_text(json.dumps({"type": "heartbeat", "feed_connected": state["feed_connected"],
                "last_tick_at": state["last_tick_at"], "last_error": state["last_error"]}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        state["clients"].discard(ws)
