import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.exists(os.path.join(BASE_DIR, "data")) else BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Determine Frontend Directory
if os.path.exists(os.path.join(BASE_DIR, "frontend")):
    FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
else:
    FRONTEND_DIR = BASE_DIR

DEFAULT_SETTINGS = {
    "refresh_interval_sec": 15,
    "default_threshold_pct": 1.0,
    "sound_enabled": True,
    "browser_notifications": True,
    "desktop_toast": True
}

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

# --- JSON Storage ---
class Storage:
    def __init__(self):
        self._init_files()

    def _init_files(self):
        if not os.path.exists(WATCHLIST_FILE):
            initial_watchlist = [
                {
                    "id": str(uuid.uuid4()),
                    "symbol": "PTT.BK",
                    "name": "PTT Public Company Limited",
                    "target_price": 42.50,
                    "alert_threshold_pct": 1.0,
                    "direction": "both",
                    "notes": "แนวต้านสำคัญ รอดูลุ้น Breakout",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_active": True,
                    "last_alert_at": None,
                    "alert_acknowledged": False
                },
                {
                    "id": str(uuid.uuid4()),
                    "symbol": "DELTA.BK",
                    "name": "Delta Electronics (Thailand)",
                    "target_price": 235.00,
                    "alert_threshold_pct": 1.0,
                    "direction": "both",
                    "notes": "ทดสอบระดับเป้าหมาย 235 บาท",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_active": True,
                    "last_alert_at": None,
                    "alert_acknowledged": False
                },
                {
                    "id": str(uuid.uuid4()),
                    "symbol": "NVDA",
                    "name": "NVIDIA Corporation",
                    "target_price": 218.00,
                    "alert_threshold_pct": 1.0,
                    "direction": "both",
                    "notes": "จับตาดูบริเวณแนวรับ $218",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "is_active": True,
                    "last_alert_at": None,
                    "alert_acknowledged": False
                }
            ]
            self._write_json(WATCHLIST_FILE, initial_watchlist)

        if not os.path.exists(ALERTS_FILE):
            self._write_json(ALERTS_FILE, [])

        if not os.path.exists(SETTINGS_FILE):
            self._write_json(SETTINGS_FILE, DEFAULT_SETTINGS)

    def _read_json(self, path: str, default: Any) -> Any:
        try:
            if not os.path.exists(path):
                return default
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _write_json(self, path: str, data: Any):
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)

    def get_watchlist(self) -> List[Dict[str, Any]]:
        return self._read_json(WATCHLIST_FILE, [])

    def add_item(self, item_data: Dict[str, Any]) -> Dict[str, Any]:
        watchlist = self.get_watchlist()
        new_item = {
            "id": str(uuid.uuid4()),
            "symbol": item_data["symbol"].strip().upper(),
            "name": item_data.get("name", item_data["symbol"].strip().upper()),
            "target_price": float(item_data["target_price"]),
            "alert_threshold_pct": float(item_data.get("alert_threshold_pct", 1.0)),
            "direction": item_data.get("direction", "both"),
            "notes": item_data.get("notes", "").strip(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_active": True,
            "last_alert_at": None,
            "alert_acknowledged": False
        }
        watchlist.append(new_item)
        self._write_json(WATCHLIST_FILE, watchlist)
        return new_item

    def update_item(self, item_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        watchlist = self.get_watchlist()
        for idx, item in enumerate(watchlist):
            if item["id"] == item_id:
                for k, v in updates.items():
                    item[k] = v
                watchlist[idx] = item
                self._write_json(WATCHLIST_FILE, watchlist)
                return item
        return None

    def delete_item(self, item_id: str) -> bool:
        watchlist = self.get_watchlist()
        filtered = [x for x in watchlist if x["id"] != item_id]
        if len(filtered) < len(watchlist):
            self._write_json(WATCHLIST_FILE, filtered)
            return True
        return False

    def sync_backup(self, stocks: List[Dict[str, Any]]) -> int:
        watchlist = self.get_watchlist()
        existing_syms = set([w["symbol"] for w in watchlist])
        added = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for s in stocks:
            sym = s.get("symbol", "").strip().upper()
            if sym and sym not in existing_syms:
                watchlist.append({
                    "id": str(uuid.uuid4()),
                    "symbol": sym,
                    "name": s.get("name", sym),
                    "target_price": float(s.get("target_price", 0)),
                    "alert_threshold_pct": float(s.get("alert_threshold_pct", 1.0)),
                    "direction": s.get("direction", "both"),
                    "notes": s.get("notes", ""),
                    "created_at": now_str,
                    "is_active": True,
                    "last_alert_at": None,
                    "alert_acknowledged": False
                })
                existing_syms.add(sym)
                added += 1
        if added > 0:
            self._write_json(WATCHLIST_FILE, watchlist)
        return added

    def get_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        alerts = self._read_json(ALERTS_FILE, [])
        return alerts[-limit:][::-1]

    def log_alert(self, alert_entry: Dict[str, Any]):
        alerts = self._read_json(ALERTS_FILE, [])
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **alert_entry
        }
        alerts.append(entry)
        if len(alerts) > 200:
            alerts = alerts[-200:]
        self._write_json(ALERTS_FILE, alerts)
        return entry

    def clear_alerts(self):
        self._write_json(ALERTS_FILE, [])

    def get_settings(self) -> Dict[str, Any]:
        return self._read_json(SETTINGS_FILE, DEFAULT_SETTINGS)

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.get_settings()
        settings.update(updates)
        self._write_json(SETTINGS_FILE, settings)
        return settings

# --- Windows Toast Notifier ---
def send_windows_toast(title: str, message: str):
    if sys.platform != "win32":
        return
    try:
        def _run():
            clean_title = title.replace("'", "''").replace('"', '`"')
            clean_msg = message.replace("'", "''").replace('"', '`"')
            ps_script = f"""
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $template = @"
<toast><visual><binding template="ToastGeneric"><text>{clean_title}</text><text>{clean_msg}</text></binding></visual></toast>
"@
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Stock Monitor").Show($toast)
            """
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script], timeout=3, capture_output=True)
        import threading
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass

# --- Initialize App & State ---
storage = Storage()
stock_service = StockService()
quote_cache: Dict[str, Dict[str, Any]] = {}
active_connections: List[WebSocket] = []
background_task: Optional[asyncio.Task] = None
is_refreshing = False

async def broadcast_ws(message: Dict[str, Any]):
    if not active_connections:
        return
    dead = []
    text = json.dumps(message, ensure_ascii=False)
    for c in active_connections:
        try:
            await c.send_text(text)
        except Exception:
            dead.append(c)
    for d in dead:
        if d in active_connections:
            active_connections.remove(d)

def get_enriched_watchlist() -> List[Dict[str, Any]]:
    watchlist = storage.get_watchlist()
    settings = storage.get_settings()
    default_threshold = settings.get("default_threshold_pct", 1.0)
    enriched = []
    for item in watchlist:
        sym = item["symbol"]
        quote = quote_cache.get(sym)
        current_price = quote["price"] if quote else None
        target_price = item["target_price"]
        threshold = item.get("alert_threshold_pct", default_threshold)
        direction = item.get("direction", "both")

        if current_price is not None:
            prox = StockService.calculate_proximity(current_price, target_price, threshold, direction)
        else:
            prox = {
                "diff": None, "diff_percent": None, "abs_diff_percent": None,
                "is_approaching": False, "is_reached": False, "status": "WAITING_DATA"
            }
        enriched.append({**item, "quote": quote, "proximity": prox})
    return enriched

async def check_and_update_stocks():
    global is_refreshing, quote_cache
    if is_refreshing:
        return
    is_refreshing = True
    try:
        watchlist = storage.get_watchlist()
        symbols = list(set([item["symbol"] for item in watchlist if item.get("is_active", True)]))
        if not symbols:
            return

        quotes = await stock_service.fetch_multiple_quotes(symbols)
        quote_cache.update(quotes)

        settings = storage.get_settings()
        default_threshold = settings.get("default_threshold_pct", 1.0)
        sound_enabled = settings.get("sound_enabled", True)
        desktop_toast = settings.get("desktop_toast", True)

        new_alerts = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for item in watchlist:
            sym = item["symbol"]
            quote = quotes.get(sym)
            if not quote:
                continue

            current_price = quote["price"]
            target_price = item["target_price"]
            threshold = item.get("alert_threshold_pct", default_threshold)
            direction = item.get("direction", "both")

            prox = StockService.calculate_proximity(current_price, target_price, threshold, direction)
            is_alerting = prox["is_approaching"] or prox["is_reached"]
            already_acknowledged = item.get("alert_acknowledged", False)

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

                    alert_record = storage.log_alert({
                        "watchlist_id": item["id"],
                        "symbol": sym,
                        "name": item.get("name", sym),
                        "current_price": current_price,
                        "target_price": target_price,
                        "currency": currency,
                        "diff_percent": prox["diff_percent"],
                        "abs_diff_percent": prox["abs_diff_percent"],
                        "status": prox["status"],
                        "title": title_text,
                        "message": msg_text
                    })
                    new_alerts.append(alert_record)
                    storage.update_item(item["id"], {"last_alert_at": now_str})
                    if desktop_toast:
                        send_windows_toast(title_text, msg_text)

        await broadcast_ws({
            "type": "STOCKS_UPDATE",
            "data": get_enriched_watchlist(),
            "alerts": new_alerts,
            "sound": sound_enabled and len(new_alerts) > 0,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        })
    except Exception as e:
        logger.error(f"Error checking stocks: {e}")
    finally:
        is_refreshing = False

async def background_poller():
    while True:
        try:
            settings = storage.get_settings()
            interval = max(5, int(settings.get("refresh_interval_sec", 15)))
            await check_and_update_stocks()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Poller error: {e}")
            await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global background_task
    watchlist = storage.get_watchlist()
    syms = [i["symbol"] for i in watchlist]
    if syms:
        quotes = await stock_service.fetch_multiple_quotes(syms)
        quote_cache.update(quotes)

    background_task = asyncio.create_task(background_poller())
    yield
    if background_task:
        background_task.cancel()

app = FastAPI(title="Stock Alert Pro", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Models ---
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

class SyncRequest(BaseModel):
    stocks: List[Dict[str, Any]]

# --- API Endpoints ---
@app.get("/api/stocks")
def list_stocks():
    return get_enriched_watchlist()

@app.post("/api/stocks")
async def add_stock(data: StockCreate):
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

    new_item = storage.add_item({
        "symbol": sym,
        "name": data.name or sym,
        "target_price": data.target_price,
        "alert_threshold_pct": data.alert_threshold_pct or 1.0,
        "direction": data.direction or "both",
        "notes": data.notes or ""
    })
    asyncio.create_task(check_and_update_stocks())
    return new_item

@app.put("/api/stocks/{item_id}")
async def update_stock(item_id: str, data: StockUpdate):
    updates = data.model_dump(exclude_none=True)
    updated = storage.update_item(item_id, updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Stock not found")
    asyncio.create_task(check_and_update_stocks())
    return updated

@app.delete("/api/stocks/{item_id}")
async def delete_stock(item_id: str):
    success = storage.delete_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Stock not found")
    asyncio.create_task(broadcast_ws({
        "type": "STOCKS_UPDATE",
        "data": get_enriched_watchlist()
    }))
    return {"success": True}

@app.post("/api/stocks/{item_id}/ack")
def acknowledge_alert(item_id: str):
    return {"success": bool(storage.update_item(item_id, {"alert_acknowledged": True}))}

@app.post("/api/stocks/sync-backup")
async def sync_backup(data: SyncRequest):
    added = storage.sync_backup(data.stocks)
    if added > 0:
        asyncio.create_task(check_and_update_stocks())
    return {"success": True, "added": added}

@app.get("/api/search")
def search_stocks(q: str = ""):
    return stock_service.search(q)

@app.get("/api/alerts")
def get_alerts(limit: int = 50):
    return storage.get_alerts(limit=limit)

@app.delete("/api/alerts")
def clear_alerts():
    storage.clear_alerts()
    return {"success": True}

@app.get("/api/settings")
def get_settings():
    return storage.get_settings()

@app.put("/api/settings")
def update_settings(data: SettingsUpdate):
    return storage.update_settings(data.model_dump(exclude_none=True))

@app.post("/api/refresh")
async def force_refresh():
    await check_and_update_stocks()
    return {"success": True, "count": len(quote_cache)}

@app.post("/api/test-alert")
def test_alert():
    send_windows_toast("🔔 ทดสอบการแจ้งเตือนหุ้น", "ระบบแจ้งเตือนทำงานได้สมบูรณ์แบบ!")
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
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "INIT",
            "data": get_enriched_watchlist(),
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }, ensure_ascii=False))
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        if websocket in active_connections:
            active_connections.remove(websocket)
    except Exception:
        if websocket in active_connections:
            active_connections.remove(websocket)

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
