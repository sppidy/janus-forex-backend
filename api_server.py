"""Forex Trading API — Multi-user with PostgreSQL, Dockerized."""

import asyncio
import json
import os
import sys
import random
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Agent module loading ──
AGENT_DIR = os.environ.get("AGENT_DIR", str(Path(__file__).resolve().parent.parent / "agent"))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

import config as fx_config
from data_fetcher import get_watchlist_prices, get_historical_data, get_market_regime, get_live_price
from market_calendar import is_market_open, get_active_sessions, now_et, time_to_market_open
from strategy import get_scored_signal

from users import (
    get_user, create_user, list_users, deactivate_user, regenerate_key,
    get_user_by_username, User,
    get_portfolio_summary, get_positions_detail, get_user_trades,
    execute_buy, execute_sell, reset_portfolio,
)

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forex_api")


# ── Auth ──

def _require_user(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> User:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")
    user = get_user(x_api_key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


def _require_admin(user: User = Depends(_require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── App ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Forex API starting up (PostgreSQL backend)")
    yield
    logger.info("Forex API shutting down")


app = FastAPI(title="Forex Trading API", lifespan=lifespan)

# CORS — restrict to trusted origins. Set TRUSTED_ORIGINS env var to a comma-
# separated list of full origins (scheme + host + optional port). Falls back
# to localhost only so a misconfigured deploy doesn't quietly accept all
# origins (which would let any website call this API with the user's key).
TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "TRUSTED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=TRUSTED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ── Admin endpoints ──

class CreateUserRequest(BaseModel):
    username: str
    display_name: str = ""


@app.post("/api/admin/users")
async def admin_create_user(req: CreateUserRequest, admin: User = Depends(_require_admin)):
    existing = get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{req.username}' already exists")
    user_info, api_key = create_user(req.username, req.display_name)
    logger.info(f"[ADMIN] Created user: {req.username}")
    return {"username": user_info["username"], "display_name": user_info["display_name"], "api_key": api_key}


@app.get("/api/admin/users")
async def admin_list_users(admin: User = Depends(_require_admin)):
    return {"users": list_users()}


@app.post("/api/admin/users/{username}/deactivate")
async def admin_deactivate_user(username: str, admin: User = Depends(_require_admin)):
    if deactivate_user(username):
        return {"status": "deactivated", "username": username}
    raise HTTPException(status_code=404, detail="User not found")


@app.post("/api/admin/users/{username}/regenerate-key")
async def admin_regenerate_key(username: str, admin: User = Depends(_require_admin)):
    new_key = regenerate_key(username)
    if new_key:
        return {"username": username, "api_key": new_key}
    raise HTTPException(status_code=404, detail="User not found")


# ── Autopilot helpers ──

import subprocess

def _get_autopilot_status() -> dict:
    """Check if forex-autopilot container is running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=forex-autopilot", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        status_text = result.stdout.strip()
        running = "Up" in status_text

        cycle = 0
        cycle_file = os.path.join(AGENT_DIR, "logs", "cycle_count.txt")
        try:
            with open(cycle_file, "r") as f:
                cycle = int(f.read().strip())
        except Exception:
            pass

        return {
            "running": running,
            "cycle": cycle,
            "started_at": status_text if running else None,
            "interval": int(os.environ.get("AUTOPILOT_INTERVAL", "15")),
            "pid": 0,
        }
    except Exception:
        return {"running": False, "cycle": 0, "started_at": None, "interval": 15, "pid": 0}


# ── Portfolio & Status ──

@app.get("/api/status")
async def get_status(user: User = Depends(_require_user)):
    prices = {}
    try:
        prices = get_watchlist_prices()
    except Exception:
        pass

    summary = get_portfolio_summary(user, prices)
    positions = get_positions_detail(user, prices)
    trades = get_user_trades(user, limit=20)
    sessions = get_active_sessions()

    # Convert positions dict to list (Android model expects array)
    positions_list = list(positions.values()) if isinstance(positions, dict) else positions

    return {
        "status": "ok",
        "summary": summary,
        "positions": positions_list,
        "recent_trades": trades,
        "market": {
            "open": is_market_open(),
            "sessions": sessions,
            "time_et": now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
        },
        "autopilot": _get_autopilot_status(),
        "currency": "$",
        "user": user.username,
        "watchlist": fx_config.WATCHLIST,
    }


@app.get("/api/prices")
async def get_prices(user: User = Depends(_require_user)):
    prices = get_watchlist_prices()
    return {"prices": prices, "currency": "$"}


@app.get("/api/market-regime")
async def market_regime(user: User = Depends(_require_user)):
    regime = get_market_regime()
    return {"status": "ok", "regime": regime, "index": fx_config.MARKET_INDEX}


def _normalize_forex_symbol(symbol: str) -> str:
    """Ensure forex symbol has correct yfinance suffix."""
    cleaned = (symbol or "").strip().upper()
    if not cleaned:
        raise HTTPException(status_code=400, detail="symbol is required")
    # Known non-forex symbols (indices)
    if cleaned.startswith("^") or cleaned.startswith("DX-"):
        return cleaned
    # Futures symbols (GC=F, SI=F, etc.) — leave as-is
    if cleaned.endswith("=F"):
        return cleaned
    # Add =X if missing (forex pairs)
    if not cleaned.endswith("=X"):
        cleaned = f"{cleaned}=X"
    return cleaned


@app.get("/api/candles")
async def get_candles(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 300,
    user: User = Depends(_require_user),
):
    yf_symbol = _normalize_forex_symbol(symbol)
    period_map = {"1m": "1d", "5m": "5d", "15m": "30d", "1h": "60d", "4h": "60d", "1d": "1y"}
    period = period_map.get(timeframe, "60d")
    df = get_historical_data(yf_symbol, period=period, interval=timeframe)
    if df.empty:
        return {"status": "ok", "candles": [], "symbol": symbol, "timeframe": timeframe}
    df = df.tail(limit)
    candles = []
    for idx, row in df.iterrows():
        candles.append({
            "t": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "o": round(float(row["Open"]), 5),
            "h": round(float(row["High"]), 5),
            "l": round(float(row["Low"]), 5),
            "c": round(float(row["Close"]), 5),
            "v": int(row.get("Volume", 0)),
        })
    return {"status": "ok", "candles": candles, "symbol": symbol, "timeframe": timeframe}


# ── Trading ──

class TradeRequest(BaseModel):
    symbol: str
    action: str  # "buy" or "sell"
    quantity: int | None = None


@app.post("/api/trade")
async def execute_trade(req: TradeRequest, user: User = Depends(_require_user)):
    if not is_market_open():
        raise HTTPException(status_code=400, detail="Forex market is closed")

    yf_symbol = _normalize_forex_symbol(req.symbol)
    price = get_live_price(yf_symbol)
    if price is None:
        raise HTTPException(status_code=404, detail=f"Could not fetch price for {yf_symbol}")

    if req.action.upper() == "BUY":
        slippage = fx_config.SLIPPAGE_PCT * random.uniform(0.5, 2.0)
        fill_price = price * (1 + slippage)

        # Auto-calculate quantity if not specified
        if req.quantity is None:
            summary = get_portfolio_summary(user, {})
            max_spend = summary["cash"] * fx_config.MAX_POSITION_SIZE_PCT
            req.quantity = int(max_spend / fill_price)

        if req.quantity <= 0:
            raise HTTPException(status_code=400, detail="Insufficient funds")

        total_cost = req.quantity * fill_price
        result = execute_buy(
            user, yf_symbol, price, req.quantity, fill_price, slippage, total_cost,
            dynamic_sl=fx_config.STOP_LOSS_PCT, dynamic_tp=fx_config.TAKE_PROFIT_PCT,
        )
        if result is None:
            raise HTTPException(status_code=400, detail="Order failed — insufficient funds")
        return {"status": "filled", **result}

    elif req.action.upper() == "SELL":
        result = execute_sell(user, yf_symbol, price, quantity=req.quantity)
        if result is None:
            raise HTTPException(status_code=400, detail="Order failed — no position or insufficient quantity")
        return {"status": "filled", **result}

    raise HTTPException(status_code=400, detail="action must be 'buy' or 'sell'")


# ── Apply AI signals (from scan) ──

class ApplySignalPayload(BaseModel):
    symbol: str
    signal: str
    price: float | None = None
    confidence: float | None = None
    reason: str | None = None
    stop_loss: float | None = None
    target: float | None = None
    position_size_pct: float | None = None


class ApplySignalsRequest(BaseModel):
    signals: list[ApplySignalPayload]
    min_confidence: float = 0.0


@app.post("/api/ai-signals/apply")
async def apply_ai_signals(req: ApplySignalsRequest, user: User = Depends(_require_user)):
    """Apply one or more signals from a scan as trades."""
    results = []
    for sig in req.signals:
        if sig.confidence is not None and sig.confidence < req.min_confidence:
            continue

        yf_symbol = _normalize_forex_symbol(sig.symbol)
        price = sig.price
        if not price or price <= 0:
            try:
                price = get_live_price(yf_symbol)
            except Exception:
                price = None
        if not price or price <= 0:
            continue

        action = sig.signal.upper()
        if action == "BUY":
            slippage = fx_config.SLIPPAGE_PCT * random.uniform(0.5, 2.0)
            fill_price = price * (1 + slippage)
            summary = get_portfolio_summary(user, {})
            size_pct = sig.position_size_pct or fx_config.MAX_POSITION_SIZE_PCT
            max_spend = summary["cash"] * min(size_pct, fx_config.MAX_POSITION_SIZE_PCT)
            quantity = int(max_spend / fill_price)
            if quantity <= 0:
                continue
            total_cost = quantity * fill_price
            result = execute_buy(
                user, yf_symbol, price, quantity, fill_price, slippage, total_cost,
                confidence=sig.confidence or 0,
                ai_signal={"signal": sig.signal, "reason": sig.reason, "confidence": sig.confidence},
                dynamic_sl=fx_config.STOP_LOSS_PCT, dynamic_tp=fx_config.TAKE_PROFIT_PCT,
            )
            if result:
                results.append({"action": "BUY", "symbol": yf_symbol, "price": fill_price})

        elif action == "SELL":
            result = execute_sell(user, yf_symbol, price)
            if result:
                results.append({"action": "SELL", "symbol": yf_symbol, "price": result.get("fill_price", price)})

    return {"status": "ok", "trades": results, "message": f"Applied {len(results)} trade(s)"}


# ── Autopilot control ──

class AutopilotRequest(BaseModel):
    interval: int = 15
    use_ai: bool = True
    force: bool = False


@app.post("/api/autopilot/start")
async def start_autopilot(req: AutopilotRequest = AutopilotRequest(), user: User = Depends(_require_user)):
    status = _get_autopilot_status()
    if status["running"]:
        return {"status": "ok", "message": "Autopilot already running", "autopilot": status}
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", "/app/docker-compose.yml", "up", "-d", "forex-autopilot"],
            capture_output=True, text=True, timeout=30,
            cwd="/app",
        )
        # Fallback: try host path
        if result.returncode != 0:
            result = subprocess.run(
                ["docker", "start", "forex-trading-agent-forex-autopilot-1"],
                capture_output=True, text=True, timeout=15,
            )
        import asyncio
        await asyncio.sleep(2)
        status = _get_autopilot_status()
        return {"status": "ok", "message": "Autopilot started", "autopilot": status}
    except Exception as e:
        return {"status": "error", "message": f"Failed to start autopilot: {e}"}


@app.post("/api/autopilot/stop")
async def stop_autopilot(user: User = Depends(_require_user)):
    try:
        subprocess.run(
            ["docker", "stop", "forex-trading-agent-forex-autopilot-1"],
            capture_output=True, text=True, timeout=15,
        )
        return {"status": "ok", "message": "Autopilot stopped"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to stop autopilot: {e}"}


# ── Scanning (Strategy Engine: per-pair strategies + time windows) ──

@app.get("/api/scan")
async def run_scan(user: User = Depends(_require_user)):
    """Run per-pair strategies with time window enforcement."""
    from strategy_engine import scan_all_pairs
    signals = scan_all_pairs(get_historical_data)
    active = [s for s in signals if s.get("in_window")]
    return {"status": "ok", "signals": signals, "type": "strategy_engine",
            "count": len(signals), "active": len(active)}


@app.get("/api/ai-scan")
async def run_ai_scan(user: User = Depends(_require_user)):
    """Same as scan (no AI for forex — uses strategy engine)."""
    from strategy_engine import scan_all_pairs
    try:
        signals = scan_all_pairs(get_historical_data)
        active = [s for s in signals if s.get("in_window")]
        return {"status": "ok", "signals": signals, "type": "strategy_engine",
                "count": len(signals), "active": len(active)}
    except Exception as e:
        logger.error(f"Strategy scan failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")


@app.get("/api/strategies")
async def get_strategies(user: User = Depends(_require_user)):
    """Get current strategy configuration for all pairs."""
    from strategy_engine import get_all_configs
    return {"status": "ok", "strategies": get_all_configs()}


# ── Journal ──

@app.get("/api/journal")
async def get_journal(user: User = Depends(_require_user)):
    trades = get_user_trades(user, limit=100)
    return {"trades": trades, "count": len(trades)}


# ── Portfolio reset ──

@app.post("/api/portfolio/reset")
async def api_reset_portfolio(user: User = Depends(_require_user)):
    reset_portfolio(user, fx_config.INITIAL_CAPITAL)
    return {"status": "reset", "capital": fx_config.INITIAL_CAPITAL, "currency": "$"}


# ── Chat ──

class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest, user: User = Depends(_require_user)):
    prices = {}
    try:
        prices = get_watchlist_prices()
    except Exception:
        pass

    summary = get_portfolio_summary(user, prices)
    sessions = get_active_sessions()

    prompt = f"""You are a forex trading assistant. User: {user.display_name}.
Market: {'OPEN' if is_market_open() else 'CLOSED'}. Active sessions: {', '.join(sessions) or 'None'}.
Portfolio: ${summary['total_value']:,.2f} (${summary['cash']:,.2f} cash, {summary['open_positions']} positions, {summary['total_return_pct']:+.2f}% return).
Watchlist: {', '.join(s.replace('=X','') for s in fx_config.WATCHLIST)}.

User message: {req.message}

Respond helpfully and concisely:"""

    try:
        from ai_strategy import _call_ai_async
        reply = await asyncio.wait_for(_call_ai_async(prompt), timeout=30.0)
        if not isinstance(reply, str):
            reply = str(reply)
    except Exception as e:
        reply = f"AI is temporarily unavailable. Try again in a moment."

    return {"reply": reply, "status": "ok"}


# ── Logs ──

import glob

@app.get("/api/logs/dates")
async def get_log_dates(user: User = Depends(_require_user)):
    log_dir = os.path.join(AGENT_DIR, "logs")
    dates = []
    if os.path.exists(log_dir):
        for f in sorted(glob.glob(os.path.join(log_dir, "*.log*")), reverse=True):
            name = os.path.basename(f)
            # trading_agent.log.2026-04-15 or autopilot.log
            if "." in name:
                parts = name.split(".")
                for p in parts:
                    if len(p) == 10 and p.count("-") == 2:
                        dates.append(p)
        if not dates:
            from datetime import date
            dates.append(date.today().isoformat())
    return {"status": "ok", "dates": sorted(set(dates), reverse=True)[:30]}


@app.get("/api/logs/recent")
async def get_recent_logs(
    lines: int = 500,
    date: str | None = None,
    user: User = Depends(_require_user),
):
    log_dir = os.path.join(AGENT_DIR, "logs")
    # Try autopilot log first, then trading_agent log
    candidates = []
    if date:
        candidates.append(os.path.join(log_dir, f"autopilot.log.{date}"))
        candidates.append(os.path.join(log_dir, f"trading_agent.log.{date}"))
    candidates.append(os.path.join(log_dir, "autopilot.log"))
    candidates.append(os.path.join(log_dir, "trading_agent.log"))

    for log_file in candidates:
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    return {"status": "ok", "logs": [l.rstrip() for l in tail], "source": os.path.basename(log_file)}
            except Exception:
                continue

    return {"status": "ok", "logs": ["No log files found"], "source": "none"}


# ── WebSocket Log Streaming ──

class LogBroadcaster:
    """Watches a log file and broadcasts new lines to connected WebSocket clients."""

    def __init__(self):
        self.clients: list[WebSocket] = []
        self._task: asyncio.Task | None = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._tail_loop())

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def _tail_loop(self):
        """Continuously tail the autopilot log file and broadcast new lines."""
        log_candidates = [
            os.path.join(AGENT_DIR, "logs", "autopilot.log"),
            os.path.join(AGENT_DIR, "logs", "trading_agent.log"),
        ]
        log_file = None
        for c in log_candidates:
            if os.path.exists(c):
                log_file = c
                break

        while self.clients:
            if log_file is None or not os.path.exists(log_file):
                # Check again
                for c in log_candidates:
                    if os.path.exists(c):
                        log_file = c
                        break
                if log_file is None:
                    await asyncio.sleep(5)
                    continue

            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    # Seek to end
                    f.seek(0, 2)
                    try:
                        current_ino = os.fstat(f.fileno()).st_ino
                    except OSError:
                        current_ino = None
                    while self.clients:
                        line = f.readline()
                        if line:
                            line = line.rstrip()
                            if line:
                                msg = json.dumps({"type": "log", "line": line})
                                dead = []
                                for ws in self.clients:
                                    try:
                                        await ws.send_text(msg)
                                    except Exception:
                                        dead.append(ws)
                                for ws in dead:
                                    self.disconnect(ws)
                        else:
                            await asyncio.sleep(0.5)
                            # Detect log rotation: inode changed or file was truncated.
                            try:
                                st = os.stat(log_file)
                                if current_ino is not None and st.st_ino != current_ino:
                                    logger.info("Log file rotated (inode changed); reopening.")
                                    break  # exit inner loop -> outer loop reopens
                                if st.st_size < f.tell():
                                    logger.info("Log file truncated; reopening.")
                                    break
                            except OSError:
                                break
            except Exception as e:
                logger.warning(f"Log tail error: {e}")
                await asyncio.sleep(2)


log_broadcaster = LogBroadcaster()


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """Stream log file updates in real-time.

    Auth precedence:
      1. ``X-API-Key`` request header — preferred for native clients.
      2. First message ``{"type":"auth","key":"..."}`` after accept — for
         browsers (which can't set custom WS headers).
      3. ``?key=`` query string — DEPRECATED. Tokens in URLs leak via proxy
         logs / tracing. Still accepted for back-compat; will be removed.
    """
    AUTH_TIMEOUT_S = 5.0

    header_key = websocket.headers.get("x-api-key")
    query_key = websocket.query_params.get("key")
    if header_key or query_key:
        api_key = header_key or query_key
        if not api_key or not get_user(api_key):
            await websocket.close(code=1008); return
        if query_key and not header_key:
            logger.warning("WS auth via ?key= is deprecated; clients should send X-API-Key header or first-message auth.")
        await websocket.accept()
    else:
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_S)
            payload = json.loads(raw)
            if payload.get("type") != "auth" or not get_user(payload.get("key", "")):
                await websocket.close(code=1008); return
        except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            try: await websocket.close(code=1008)
            except Exception: pass
            return

    log_broadcaster.clients.append(websocket)
    if log_broadcaster._task is None or log_broadcaster._task.done():
        log_broadcaster._task = asyncio.create_task(log_broadcaster._tail_loop())
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        log_broadcaster.disconnect(websocket)


# ── Health ──

@app.get("/api/health")
async def health():
    return {"status": "ok", "market_open": is_market_open(), "sessions": get_active_sessions(), "service": "forex"}


# ── Entry point ──

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8445"))
    uvicorn.run(app, host="0.0.0.0", port=port)
