import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.exists(os.path.join(BASE_DIR, "data")) else BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "stock_alert.db")

# Determine Frontend Directory
if os.path.exists(os.path.join(BASE_DIR, "frontend")):
    FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
else:
    FRONTEND_DIR = BASE_DIR

# --- Database & Auth Helpers ---
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return f"{salt}:{key}"

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, key = stored_hash.split(":")
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
        return secrets.compare_digest(key, new_key)
    except Exception:
        return False

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS watchlists (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        name TEXT,
        target_price REAL NOT NULL,
        alert_threshold_pct REAL DEFAULT 1.0,
        direction TEXT DEFAULT 'both',
        notes TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        last_alert_at TEXT,
        alert_acknowledged INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        watchlist_id TEXT,
        symbol TEXT NOT NULL,
        name TEXT,
        current_price REAL NOT NULL,
        target_price REAL NOT NULL,
        currency TEXT DEFAULT 'THB',
        diff_percent REAL NOT NULL,
        abs_diff_percent REAL NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        refresh_interval_sec INTEGER DEFAULT 15,
        default_threshold_pct REAL DEFAULT 1.0,
        sound_enabled INTEGER DEFAULT 1,
        browser_notifications INTEGER DEFAULT 1,
        desktop_toast INTEGER DEFAULT 1,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Create a default demo user if no users exist
    cursor.execute("SELECT COUNT(*) as count FROM users")
    if cursor.fetchone()["count"] == 0:
        demo_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (demo_id, "demo", hash_password("123456"), "Demo User", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        cursor.execute(
            "INSERT INTO user_settings (user_id) VALUES (?)",
            (demo_id,)
        )
        # Add starter stocks to demo user
        starters = [
            ("PTT.BK", "PTT Public Company Limited", 42.50, 1.0, "both", "แนวต้านสำคัญ รอดูลุ้น Breakout"),
            ("DELTA.BK", "Delta Electronics (Thailand)", 235.00, 1.0, "both", "ทดสอบระดับเป้าหมาย 235 บาท"),
            ("NVDA", "NVIDIA Corporation", 218.00, 1.0, "both", "จับตาดูบริเวณแนวรับ $218")
        ]
        for s, n, tp, at, d, notes in starters:
            cursor.execute(
                """INSERT INTO watchlists (id, user_id, symbol, name, target_price, alert_threshold_pct, direction, notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), demo_id, s, n, tp, at, d, notes, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
    conn.commit()
    conn.close()

# Initialize DB
init_db()

# --- Auth Helpers ---
def get_user_from_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.username, u.display_name, u.created_at
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ?
    """, (token,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def authenticate_request(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization Scheme")
    token = parts[1]
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please login again.")
    return user

# --- Stock Market Service ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

class StockService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.executor = ThreadPoolExecutor(max_workers=10)

    def search(self, query: str) -> List[Dict[str, Any]]:
        query = query.strip()
        if not query:
            return []

        results = []
        seen = set()
        queries_to_try = [query]
        clean_upper = query.upper().replace(".BK", "")
        if clean_upper.isalpha() and len(clean_upper) <= 10:
            queries_to_try.append(f"{clean_upper}.BK")

        for q in queries_to_try:
            url = f"https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=6&newsCount=0"
            try:
                resp = self.session.get(url, timeout=4)
                if resp.status_code == 200:
                    data = resp.json()
                    quotes = data.get("quotes", [])
                    for item in quotes:
                        sym = item.get("symbol")
                        if not sym or sym in seen:
                            continue
                        seen.add(sym)
                        results.append({
                            "symbol": sym,
                            "name": item.get("shortname") or item.get("longname") or sym,
                            "exchange": item.get("exchDisp") or item.get("exchange", ""),
                            "type": item.get("quoteType", ""),
                            "is_thai": sym.endswith(".BK") or item.get("exchange") == "SET"
                        })
            except Exception as e:
                logger.warning(f"Search error for {q}: {e}")

        return results[:10]

    def fetch_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        symbol = symbol.strip().upper()
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=15m&range=5d"
        try:
            resp = self.session.get(url, timeout=5)
            if resp.status_code != 200:
                url_fallback = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo"
                resp = self.session.get(url_fallback, timeout=5)
                if resp.status_code != 200:
                    return None

            data = resp.json()
            chart = data.get("chart", {})
            result_list = chart.get("result")
            if not result_list:
                return None

            result = result_list[0]
            meta = result.get("meta", {})

            price = meta.get("regularMarketPrice")
            if price is None:
                indicators = result.get("indicators", {}).get("quote", [{}])[0]
                closes = indicators.get("close", [])
                valid_closes = [c for c in closes if c is not None]
                if valid_closes:
                    price = round(valid_closes[-1], 4)
                else:
                    return None

            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
            change = meta.get("fulldayChange")
            change_percent = meta.get("regularMarketChangePercent") or meta.get("fulldayChangePercent")

            if change is None and prev_close and price:
                change = price - prev_close
            if change_percent is None and prev_close and price and prev_close > 0:
                change_percent = ((price - prev_close) / prev_close) * 100

            indicators = result.get("indicators", {}).get("quote", [{}])[0]
            closes = indicators.get("close", [])
            sparkline = []
            if closes:
                for c in closes:
                    if c is not None:
                        sparkline.append(round(c, 2 if c > 1 else 4))
                sparkline = sparkline[-20:]

            currency = meta.get("currency", "THB" if symbol.endswith(".BK") else "USD")

            return {
                "symbol": symbol,
                "name": meta.get("longName") or meta.get("shortName") or symbol,
                "currency": currency,
                "exchange": meta.get("exchangeName", "SET" if symbol.endswith(".BK") else "US"),
                "price": round(float(price), 2 if price >= 1 else 4),
                "previous_close": round(float(prev_close), 2 if prev_close and prev_close >= 1 else 4) if prev_close else None,
                "change": round(float(change), 2) if change is not None else 0.0,
                "change_percent": round(float(change_percent), 2) if change_percent is not None else 0.0,
                "day_high": meta.get("regularMarketDayHigh"),
                "day_low": meta.get("regularMarketDayLow"),
                "volume": meta.get("regularMarketVolume"),
                "high_52w": meta.get("fiftyTwoWeekHigh"),
                "low_52w": meta.get("fiftyTwoWeekLow"),
                "sparkline": sparkline,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        except Exception as e:
            logger.error(f"Error fetching quote for {symbol}: {e}")
            return None

    async def fetch_multiple_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(self.executor, self.fetch_quote, s)
            for s in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        quotes = {}
        for s, res in zip(symbols, results):
            if isinstance(res, dict):
                quotes[s] = res
        return quotes

    @staticmethod
    def calculate_proximity(current_price: float, target_price: float, threshold_pct: float = 1.0, direction: str = "both") -> Dict[str, Any]:
        if target_price <= 0:
            return {
                "diff": 0, "diff_percent": 0, "abs_diff_percent": 0,
                "is_approaching": False, "is_reached": False, "status": "INVALID_TARGET"
            }

        diff = current_price - target_price
        diff_pct = (diff / target_price) * 100.0
        abs_diff_pct = abs(diff_pct)

        is_reached = False
        is_approaching = False

        if abs_diff_pct <= 0.1:
            is_reached = True

        if direction == "below":
            if current_price >= target_price:
                is_reached = True
            elif abs_diff_pct <= threshold_pct and diff < 0:
                is_approaching = True
        elif direction == "above":
            if current_price <= target_price:
                is_reached = True
            elif abs_diff_pct <= threshold_pct and diff > 0:
                is_approaching = True
        else:
            if abs_diff_pct <= threshold_pct:
                is_approaching = True

        status = "NORMAL"
        if is_reached:
            status = "REACHED"
        elif is_approaching:
            status = "APPROACHING"

        return {
            "diff": round(diff, 4),
            "diff_percent": round(diff_pct, 2),
            "abs_diff_percent": round(abs_diff_pct, 2),
            "is_approaching": is_approaching,
            "is_reached": is_reached,
            "status": status,
            "threshold_pct": threshold_pct,
            "direction": direction
        }

stock_service = StockService()
quote_cache: Dict[str, Dict[str, Any]] = {}
# Map of user_id -> list of active WebSocket connections
user_connections: Dict[str, List[WebSocket]] = {}
background_task: Optional[asyncio.Task] = None
is_refreshing = False

def get_user_watchlist(user_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, symbol, name, target_price, alert_threshold_pct, direction, notes, is_active, last_alert_at, alert_acknowledged, created_at
        FROM watchlists
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_user_enriched_watchlist(user_id: str) -> List[Dict[str, Any]]:
    watchlist = get_user_watchlist(user_id)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT default_threshold_pct FROM user_settings WHERE user_id = ?", (user_id,))
    s_row = cursor.fetchone()
    conn.close()
    default_threshold = s_row["default_threshold_pct"] if s_row else 1.0

    enriched = []
    for item in watchlist:
        quote = quote_cache.get(item["symbol"])
        current_price = quote["price"] if quote else None
        target_price = item["target_price"]
        threshold = item.get("alert_threshold_pct") or default_threshold
        direction = item.get("direction") or "both"

        if current_price is not None:
            prox = StockService.calculate_proximity(current_price, target_price, threshold, direction)
        else:
            prox = {
                "diff": None, "diff_percent": None, "abs_diff_percent": None,
                "is_approaching": False, "is_reached": False, "status": "WAITING_DATA"
            }
        enriched.append({**item, "quote": quote, "proximity": prox})
    return enriched

async def check_and_update_all_stocks():
    global is_refreshing, quote_cache
    if is_refreshing:
        return
    is_refreshing = True
    try:
        conn = get_db()
        cursor = conn.cursor()
        # Find all distinct symbols watched across all active users
        cursor.execute("SELECT DISTINCT symbol FROM watchlists WHERE is_active = 1")
        symbols = [row["symbol"] for row in cursor.fetchall()]

        if not symbols:
            conn.close()
            return

        quotes = await stock_service.fetch_multiple_quotes(symbols)
        quote_cache.update(quotes)

        # Get all users with watchlists
        cursor.execute("SELECT DISTINCT user_id FROM watchlists WHERE is_active = 1")
        user_ids = [row["user_id"] for row in cursor.fetchall()]

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for u_id in user_ids:
            cursor.execute("SELECT default_threshold_pct, sound_enabled, desktop_toast FROM user_settings WHERE user_id = ?", (u_id,))
            s_row = cursor.fetchone()
            def_threshold = s_row["default_threshold_pct"] if s_row else 1.0
            sound_on = bool(s_row["sound_enabled"]) if s_row else True
            toast_on = bool(s_row["desktop_toast"]) if s_row else True

            cursor.execute("SELECT * FROM watchlists WHERE user_id = ? AND is_active = 1", (u_id,))
            user_items = cursor.fetchall()
            new_alerts = []

            for item_row in user_items:
                item = dict(item_row)
                sym = item["symbol"]
                quote = quotes.get(sym)
                if not quote:
                    continue

                current_price = quote["price"]
                target_price = item["target_price"]
                threshold = item["alert_threshold_pct"] or def_threshold
                direction = item["direction"] or "both"

                prox = StockService.calculate_proximity(current_price, target_price, threshold, direction)
                is_alerting = prox["is_approaching"] or prox["status"] == "REACHED"
                already_acknowledged = bool(item["alert_acknowledged"])

                if is_alerting and not already_acknowledged:
                    last_alert = item.get("last_alert_at")
                    should_trigger = False
                    if not last_alert:
                        should_trigger = True
                    else:
                        try:
                            elapsed = (datetime.now() - datetime.strptime(last_alert, "%Y-%m-%d %H:%M:%S")).total_seconds()
                            if elapsed > 300:
                                should_trigger = True
                        except Exception:
                            should_trigger = True

                    if should_trigger:
                        title_text = f"🚨 {sym} เข้าใกล้เป้าหมาย!" if prox["status"] == "APPROACHING" else f"🎯 {sym} ถึงเป้าหมายแล้ว!"
                        currency = quote.get("currency", "THB")
                        msg_text = f"ราคา: {current_price:,.2f} {currency} (เป้า {target_price:,.2f} ห่าง {prox['abs_diff_percent']:.2f}%)"
                        alert_id = str(uuid.uuid4())

                        cursor.execute("""
                            INSERT INTO alerts (id, user_id, watchlist_id, symbol, name, current_price, target_price, currency, diff_percent, abs_diff_percent, status, title, message, timestamp)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            alert_id, u_id, item["id"], sym, item.get("name", sym),
                            current_price, target_price, currency,
                            prox["diff_percent"], prox["abs_diff_percent"],
                            prox["status"], title_text, msg_text, now_str
                        ))

                        cursor.execute("UPDATE watchlists SET last_alert_at = ? WHERE id = ?", (now_str, item["id"]))

                        new_alerts.append({
                            "id": alert_id, "symbol": sym, "title": title_text,
                            "message": msg_text, "status": prox["status"], "timestamp": now_str
                        })

                        if toast_on and sys.platform == "win32":
                            # Windows toast (desktop only)
                            pass

            conn.commit()

            # Push WebSocket update to this specific user
            conns = user_connections.get(u_id, [])
            if conns:
                enriched = get_user_enriched_watchlist(u_id)
                msg_payload = json.dumps({
                    "type": "STOCKS_UPDATE",
                    "data": enriched,
                    "alerts": new_alerts,
                    "sound": sound_on and len(new_alerts) > 0,
                    "timestamp": datetime.now().strftime("%H:%M:%S")
                }, ensure_ascii=False)

                dead_conns = []
                for c in conns:
                    try:
                        await c.send_text(msg_payload)
                    except Exception:
                        dead_conns.append(c)
                for dc in dead_conns:
                    if dc in conns:
                        conns.remove(dc)

        conn.close()
    except Exception as e:
        logger.error(f"Error checking all stocks: {e}", exc_info=True)
    finally:
        is_refreshing = False

async def background_poller():
    while True:
        try:
            await check_and_update_all_stocks()
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poller error: {e}")
            await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global background_task
    init_db()
    background_task = asyncio.create_task(background_poller())
    yield
    if background_task:
        background_task.cancel()

app = FastAPI(title="Stock Alert Pro (Multi-User)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic Request Models ---
class UserRegister(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None

class UserLogin(BaseModel):
    username: str
    password: str

class StockCreate(BaseModel):
    symbol: str
    target_price: float
    name: Optional[str] = None
    alert_threshold_pct: Optional[float] = 1.0
    direction: Optional[str] = "both"
    notes: Optional[str] = ""

class StockUpdate(BaseModel):
    target_price: Optional[float] = None
    alert_threshold_pct: Optional[float] = None
    direction: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None
    alert_acknowledged: Optional[bool] = None

class SettingsUpdate(BaseModel):
    refresh_interval_sec: Optional[int] = None
    default_threshold_pct: Optional[float] = None
    sound_enabled: Optional[bool] = None
    browser_notifications: Optional[bool] = None
    desktop_toast: Optional[bool] = None

# --- Auth Routes ---
@app.post("/api/auth/register")
def register(data: UserRegister):
    username = data.username.strip().lower()
    if not username or len(username) < 3:
        raise HTTPException(status_code=400, detail="ชื่อผู้ใช้ต้องมีอย่างน้อย 3 ตัวอักษร")
    if not data.password or len(data.password) < 4:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="ชื่อผู้ใช้นี้ถูกใช้งานแล้ว กรุณาเลือกชื่ออื่น")

    user_id = str(uuid.uuid4())
    pw_hash = hash_password(data.password)
    disp_name = data.display_name.strip() if data.display_name else username
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, pw_hash, disp_name, now_str)
    )
    cursor.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))

    # Give newly registered users default starter stocks so their watchlist is ready
    starters = [
        ("PTT.BK", "PTT Public Company Limited", 42.50, 1.0, "both", "แนวต้านสำคัญ รอดูลุ้น Breakout"),
        ("DELTA.BK", "Delta Electronics (Thailand)", 235.00, 1.0, "both", "ทดสอบระดับเป้าหมาย 235 บาท"),
        ("NVDA", "NVIDIA Corporation", 218.00, 1.0, "both", "จับตาดูบริเวณแนวรับ $218")
    ]
    for s, n, tp, at, d, notes in starters:
        cursor.execute(
            """INSERT INTO watchlists (id, user_id, symbol, name, target_price, alert_threshold_pct, direction, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), user_id, s, n, tp, at, d, notes, now_str)
        )

    # Generate session token
    token = secrets.token_urlsafe(32)
    cursor.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, now_str))
    conn.commit()
    conn.close()

    return {
        "success": True,
        "token": token,
        "user": {"id": user_id, "username": username, "display_name": disp_name}
    }

@app.post("/api/auth/login")
def login(data: UserLogin):
    username = data.username.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password_hash, display_name FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    if not user or not verify_password(data.password, user["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")

    user_id = user["id"]
    token = secrets.token_urlsafe(32)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, now_str))
    conn.commit()
    conn.close()

    return {
        "success": True,
        "token": token,
        "user": {"id": user_id, "username": user["username"], "display_name": user["display_name"]}
    }

@app.get("/api/auth/me")
def get_me(authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    return user

@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization:
        parts = authorization.split()
        if len(parts) == 2:
            token = parts[1]
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            conn.close()
    return {"success": True}

# --- Watchlist API (User Scoped) ---
@app.get("/api/stocks")
def list_stocks(authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    return get_user_enriched_watchlist(user["id"])

@app.post("/api/stocks")
async def add_stock(data: StockCreate, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    user_id = user["id"]
    sym = data.symbol.strip().upper()
    if not sym.endswith(".BK") and "." not in sym:
        search_res = stock_service.search(sym)
        for r in search_res:
            if r["symbol"] == f"{sym}.BK":
                sym = f"{sym}.BK"
                if not data.name:
                    data.name = r["name"]
                break

    quote = stock_service.fetch_quote(sym)
    if quote:
        quote_cache[sym] = quote
        if not data.name:
            data.name = quote.get("name", sym)

    new_id = str(uuid.uuid4())
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO watchlists (id, user_id, symbol, name, target_price, alert_threshold_pct, direction, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (new_id, user_id, sym, data.name or sym, data.target_price, data.alert_threshold_pct or 1.0, data.direction or "both", data.notes or "", now_str))
    conn.commit()
    conn.close()

    asyncio.create_task(check_and_update_all_stocks())
    return {"id": new_id, "symbol": sym, "target_price": data.target_price, "success": True}

@app.put("/api/stocks/{item_id}")
async def update_stock(item_id: str, data: StockUpdate, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM watchlists WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Stock not found")

    updates = data.model_dump(exclude_none=True)
    if updates:
        fields = []
        values = []
        for k, v in updates.items():
            fields.append(f"{k} = ?")
            values.append(int(v) if isinstance(v, bool) else v)
        values.extend([item_id, user["id"]])
        cursor.execute(f"UPDATE watchlists SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values)
        conn.commit()
    conn.close()

    asyncio.create_task(check_and_update_all_stocks())
    return {"success": True}

@app.delete("/api/stocks/{item_id}")
async def delete_stock(item_id: str, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlists WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/api/stocks/{item_id}/ack")
def acknowledge_alert(item_id: str, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE watchlists SET alert_acknowledged = 1 WHERE id = ? AND user_id = ?", (item_id, user["id"]))
    conn.commit()
    conn.close()
    return {"success": True}

@app.get("/api/search")
def search_stocks(q: str = ""):
    return stock_service.search(q)

@app.get("/api/alerts")
def get_alerts(limit: int = 50, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM alerts WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user["id"], limit))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/alerts")
def clear_alerts(authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM alerts WHERE user_id = ?", (user["id"],))
    conn.commit()
    conn.close()
    return {"success": True}

@app.get("/api/settings")
def get_settings(authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_settings WHERE user_id = ?", (user["id"],))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "refresh_interval_sec": 15,
        "default_threshold_pct": 1.0,
        "sound_enabled": True,
        "browser_notifications": True,
        "desktop_toast": True
    }

@app.put("/api/settings")
def update_settings(data: SettingsUpdate, authorization: Optional[str] = Header(None)):
    user = authenticate_request(authorization)
    updates = data.model_dump(exclude_none=True)
    if updates:
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for k, v in updates.items():
            fields.append(f"{k} = ?")
            values.append(int(v) if isinstance(v, bool) else v)
        values.append(user["id"])
        cursor.execute(f"UPDATE user_settings SET {', '.join(fields)} WHERE user_id = ?", values)
        conn.commit()
        conn.close()
    return {"success": True}

@app.post("/api/refresh")
async def force_refresh():
    await check_and_update_all_stocks()
    return {"success": True, "count": len(quote_cache)}

@app.post("/api/test-alert")
def test_alert():
    return {"success": True}

@app.get("/api/tunnel")
def get_tunnel():
    tunnel_file = os.path.join(DATA_DIR, "tunnel.json")
    url = None
    if os.path.exists(tunnel_file):
        try:
            with open(tunnel_file, "r") as f:
                url = json.load(f).get("url")
        except Exception:
            pass
    return {"url": url}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = Query(None)):
    await websocket.accept()
    user = get_user_from_token(token) if token else None
    if not user:
        await websocket.close(code=1008, reason="Authentication failed")
        return

    user_id = user["id"]
    if user_id not in user_connections:
        user_connections[user_id] = []
    user_connections[user_id].append(websocket)

    try:
        # Send initial watchlist specifically for this user
        initial_data = get_user_enriched_watchlist(user_id)
        await websocket.send_text(json.dumps({
            "type": "INIT",
            "data": initial_data,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }, ensure_ascii=False))

        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        if user_id in user_connections and websocket in user_connections[user_id]:
            user_connections[user_id].remove(websocket)
    except Exception:
        if user_id in user_connections and websocket in user_connections[user_id]:
            user_connections[user_id].remove(websocket)

# Serve Frontend
app.mount("/static", StaticFiles(directory=FRONTEND_DIR, check_dir=False), name="static")

@app.get("/")
def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Stock Alert Pro Backend is Running!"}

@app.get("/{full_path:path}")
def serve_static(full_path: str):
    file_path = os.path.join(FRONTEND_DIR, full_path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Stock Alert Pro"}
