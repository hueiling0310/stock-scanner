# -*- coding: utf-8 -*-
"""
common_fubon.py
================
台股資料源模組：富邦(Fubon Neo) WebSocket/REST 行情 + Yfinance 備援/批次抓取 + 本地SQLite。
已整併缺失的 UI 輔助函式 (Session State, Sidebar) 與匯出工具 (Telegram, Excel)。
"""

import os
import re
import requests
import sqlite3
from io import BytesIO
from html import escape
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ===== yfinance 選用資料源 =====
try:
    import yfinance as yf
except ImportError:
    yf = None

# ===== 富邦 API 引入 =====
try:
    from fubon_neo.sdk import FubonSDK, Mode
except ImportError:
    pass  # 交由 UI 的 render_fubon_login_sidebar 顯示錯誤

# ===== 資料源相關常數 =====
REFRESH_SEC = 3
# 90天歷史K線在盤中根本不會變，之前用 REFRESH_SEC(3秒)當快取時間，
# 等於每 3 秒就把幾百檔股票的 90 天歷史資料全部重抓一次，
# 對富邦 API 造成不必要的巨量請求、很容易被限流(429)。
# 改成獨立的、長很多的快取時間，只有「今日」相關的資料才需要用短快取。
HISTORY_CACHE_TTL_SEC = 5 * 60  # 歷史K線：5分鐘內不重抓
QUOTE_CACHE_TTL_SEC = 5         # 即時報價：5秒內不重抓 (原本完全沒快取)
YFINANCE_HISTORY_CACHE_TTL_SEC = 60 * 60
STOCK_NAME_FILE = "TWstocklistname2.txt"
STOCK_SCAN_FILE = "TWstocklistname2.txt"
FORCE_SCAN_ALL_STOCKS_FROM_FILE = True
ALL_STOCK_GROUP_NAME = "TWstocklistname2 全股票掃描"
AUTO_YFINANCE_AFTER_HOUR = 13
AUTO_YFINANCE_AFTER_MINUTE = 30

# ===== Telegram 常數 =====
try:
    TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")
except Exception:
    TELEGRAM_BOT_TOKEN = ""
    TELEGRAM_CHAT_ID = ""


# ==========================================
# 1. 富邦 API 行情與基礎資料工具
# ==========================================
def _fetch_fubon_candles(symbol: str, _sdk, start_date, end_date) -> pd.DataFrame:
    if _sdk is None:
        raise ValueError("富邦 API 尚未連線")

    fubon_symbol = str(symbol).split(".")[0]
    try:
        res = _sdk.marketdata.rest_client.stock.historical.candles(**{
            "symbol": fubon_symbol,
            "from": start_date.strftime("%Y-%m-%d"),
            "to": end_date.strftime("%Y-%m-%d"),
            "timeframe": "D",
            "fields": "open,high,low,close,volume"
        })

        if res and "data" in res and isinstance(res["data"], list) and res["data"]:
            df = pd.DataFrame(res["data"])
            df.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume", "date": "Date",
            }, inplace=True)

            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
                df = df.sort_values("Date").reset_index(drop=True)

            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            keep_cols = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
            return df[keep_cols].dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
    except Exception as e:
        print(f"富邦 API 抓取 {fubon_symbol} K 線失敗: {e}")

    return pd.DataFrame()

@st.cache_data(ttl=HISTORY_CACHE_TTL_SEC)
def download_stock_data(symbol: str, _sdk):
    end_date = date.today()
    start_date = end_date - timedelta(days=90)
    return _fetch_fubon_candles(symbol, _sdk, start_date, end_date)

@st.cache_data(ttl=QUOTE_CACHE_TTL_SEC)
def download_stock_data_fubon_today(symbol: str, _sdk, today_str: str):
    if _sdk is None:
        return pd.DataFrame()
    today = date.today()
    return _fetch_fubon_candles(symbol, _sdk, today, today)

def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    if "date" in df.columns and "Date" not in df.columns:
        df.rename(columns={"date": "Date"}, inplace=True)

    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if set(required_cols).issubset(df.columns):
        cols = (["Date"] if "Date" in df.columns else []) + required_cols
        out = df[cols].copy()
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.date
        for col in required_cols:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out.dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
    return pd.DataFrame()

@st.cache_data(ttl=QUOTE_CACHE_TTL_SEC)
def _fetch_fubon_snapshot_price(symbol: str, _sdk):
    """
    單獨把「打富邦即時報價 API」這個動作抽出來獨立快取。
    原本 get_last_price() 完全沒有快取，等於每次刷新、每一檔股票都重打一次 API，
    全市場掃描時很容易把富邦 API 打到 429 限流，一旦限流就會靜默地退回歷史資料，
    把「昨天的收盤價」誤當成「今天的價格」使用，漲跌%也就跟著算錯。
    這裡加上短秒數快取，大幅降低請求量、降低被限流的機率。
    回傳 (price, is_today) tuple；is_today 用來讓呼叫端知道這筆報價是否真的是「今天」的資料。
    """
    fubon_symbol = str(symbol).split(".")[0]
    if _sdk is None:
        return None, False
    try:
        res = _sdk.marketdata.rest_client.stock.snapshot.quotes(symbol=fubon_symbol)
        if res and "data" in res and len(res["data"]) > 0:
            quote = res["data"][0]
            # 防呆: 有些情況下 snapshot.quotes 在只帶 symbol 參數時仍可能回傳非預期的資料，
            # 如果回傳的 symbol 對不上，直接視為抓取失敗，不要誤用到別檔股票的報價。
            if str(quote.get("symbol", fubon_symbol)).strip() != fubon_symbol:
                return None, False
            price = quote.get("closePrice")
            if price is not None and pd.notna(price):
                quote_date = res.get("date") or quote.get("date")
                is_today = True
                if quote_date:
                    try:
                        is_today = pd.to_datetime(quote_date).date() == date.today()
                    except Exception:
                        is_today = True
                return float(price), is_today
    except Exception:
        pass
    return None, False


def get_last_price(symbol, df, _sdk):
    """
    取得「目前」股價。
    優先使用富邦即時報價 (snapshot.quotes 的 closePrice 欄位，經查證官方文件，
    這個欄位在盤中代表的就是最新成交價，不是昨收，欄位本身沒有問題)。
    只有在即時報價真的抓不到（例如被限流、非交易時段）時，才退回歷史資料的最後一筆，
    並且會盡量選「日期確實是今天」的那一筆，避免誤用到昨天的收盤價當今天的即時價。
    """
    price, is_today = _fetch_fubon_snapshot_price(symbol, _sdk)
    if price is not None and is_today:
        return price

    if not df.empty and "Close" in df.columns:
        if "Date" in df.columns:
            today = date.today()
            today_rows = df[pd.to_datetime(df["Date"], errors="coerce").dt.date == today]
            if not today_rows.empty:
                return float(today_rows["Close"].iloc[-1])
        # 找不到「今天」這一筆，退回最後一筆 (可能是昨天的收盤價，僅作為最後手段)
        if price is not None:
            return price  # 即時報價抓到了，只是日期對不上，仍優先信任它
        return float(df["Close"].iloc[-1])

    if price is not None:
        return price

    raise ValueError("無法取得即時價格")

@st.cache_data(ttl=86400)
def load_stock_name_map(file_path: str = STOCK_NAME_FILE) -> dict:
    name_map = {}
    if not os.path.exists(file_path):
        return name_map
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line: continue
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    name_map[parts[0].upper()] = parts[1].strip()
                    continue
            m = re.match(r"^([^\s]+)\s+(.+)$", line)
            if m:
                name_map[m.group(1).strip().upper()] = m.group(2).strip()
    return name_map

@st.cache_data(ttl=86400)
def load_stock_symbols_from_file(file_path: str = STOCK_SCAN_FILE) -> list:
    symbols = []
    seen = set()
    if not os.path.exists(file_path):
        return symbols
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", "")
            if not line:
                continue
            symbol = re.split(r"\s+", line, maxsplit=1)[0].strip().upper()
            if not re.match(r"^[0-9A-Z]+\.(TW|TWO)$", symbol):
                continue
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols

def load_all_stock_group_from_file() -> dict:
    symbols = load_stock_symbols_from_file(STOCK_SCAN_FILE)
    return {ALL_STOCK_GROUP_NAME: symbols}

def _normalize_yfinance_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.reset_index()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if not set(required_cols).issubset(df.columns):
        return pd.DataFrame()
    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df["Date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
    for col in required_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["Date"] + required_cols].dropna(subset=["Date", "Open", "High", "Low", "Close"]).reset_index(drop=True)

@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def download_stock_data_yfinance_history(symbol: str, today_str: str):
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="4mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty:
            return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] < today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_yfinance_today(symbol: str, today_str: str):
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="5d", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = _normalize_yfinance_ohlcv(df)
        if df.empty:
            return pd.DataFrame()
        today = pd.to_datetime(today_str).date()
        return df[df["Date"] >= today].reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

# ==========================================
# 新增: 本地資料庫 (SQLite) 讀取工具
# ==========================================
def _get_db_path():
    if os.path.exists("twse_ohlcv.db"):
        return "twse_ohlcv.db"
    if os.path.exists("twse_ohlcv.dB"):
        return "twse_ohlcv.dB"
    return "twse_ohlcv.db"

@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def download_stock_data_db_history(symbol: str, today_str: str):
    """單檔股票從本地 SQLite 抓取歷史資料"""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return pd.DataFrame()
    
    code = symbol.split(".")[0]
    try:
        with sqlite3.connect(db_path) as conn:
            query = f"SELECT Date, Open, High, Low, Close, Volume FROM ohlcv_data WHERE SecurityCode='{code}'"
            df = pd.read_sql(query, conn)
            
            if df.empty:
                return pd.DataFrame()
            
            df["Date"] = pd.to_datetime(df["Date"]).dt.date
            today = pd.to_datetime(today_str).date()
            df = df[df["Date"] < today].sort_values("Date").reset_index(drop=True)
            return df
    except Exception as e:
        print(f"本地資料庫讀取失敗 ({symbol}): {e}")
        return pd.DataFrame()

@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def bulk_download_db_history(symbols: tuple, today_str: str) -> dict:
    """批次從本地 SQLite 抓取歷史資料，大幅提升全市場掃描速度"""
    db_path = _get_db_path()
    if not os.path.exists(db_path) or not symbols:
        return {s: pd.DataFrame() for s in symbols}
    
    codes = [str(s).split(".")[0] for s in symbols]
    placeholders = ",".join("?" * len(codes))
    
    try:
        with sqlite3.connect(db_path) as conn:
            query = f"SELECT Date, Open, High, Low, Close, Volume, SecurityCode FROM ohlcv_data WHERE SecurityCode IN ({placeholders})"
            df = pd.read_sql(query, conn, params=codes)
            
            if df.empty:
                return {s: pd.DataFrame() for s in symbols}
            
            df["Date"] = pd.to_datetime(df["Date"]).dt.date
            today = pd.to_datetime(today_str).date()
            df = df[df["Date"] < today]
            
            result = {}
            grouped = df.groupby("SecurityCode")
            for sym in symbols:
                c = sym.split(".")[0]
                if c in grouped.groups:
                    sub = grouped.get_group(c)[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date").reset_index(drop=True)
                    result[sym] = sub
                else:
                    result[sym] = pd.DataFrame()
            return result
    except Exception as e:
        print(f"本地資料庫批次讀取失敗: {e}")
        return {s: pd.DataFrame() for s in symbols}

def _split_yfinance_bulk_result(raw: pd.DataFrame, symbols: tuple) -> dict:
    result = {}
    if raw is None or raw.empty:
        return {s: pd.DataFrame() for s in symbols}
    is_multi = isinstance(raw.columns, pd.MultiIndex)
    for symbol in symbols:
        try:
            if is_multi:
                if symbol not in raw.columns.get_level_values(0):
                    result[symbol] = pd.DataFrame()
                    continue
                sub = raw[symbol].copy()
            else:
                sub = raw.copy()
            result[symbol] = _normalize_yfinance_ohlcv(sub)
        except Exception:
            result[symbol] = pd.DataFrame()
    return result

@st.cache_data(ttl=YFINANCE_HISTORY_CACHE_TTL_SEC)
def bulk_download_yfinance_history(symbols: tuple, today_str: str) -> dict:
    if yf is None or not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=list(symbols), period="4mo", interval="1d",
            auto_adjust=False, group_by="ticker", threads=True, progress=False,
        )
    except Exception:
        return {s: pd.DataFrame() for s in symbols}

    today = pd.to_datetime(today_str).date()
    per_symbol = _split_yfinance_bulk_result(raw, symbols)
    return {
        s: (df[df["Date"] < today].reset_index(drop=True) if not df.empty else df)
        for s, df in per_symbol.items()
    }

@st.cache_data(ttl=REFRESH_SEC)
def bulk_download_yfinance_today(symbols: tuple, today_str: str) -> dict:
    if yf is None or not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=list(symbols), period="5d", interval="1d",
            auto_adjust=False, group_by="ticker", threads=True, progress=False,
        )
    except Exception:
        return {s: pd.DataFrame() for s in symbols}

    today = pd.to_datetime(today_str).date()
    per_symbol = _split_yfinance_bulk_result(raw, symbols)
    return {
        s: (df[df["Date"] >= today].reset_index(drop=True) if not df.empty else df)
        for s, df in per_symbol.items()
    }

def resolve_price_source(now_dt=None) -> str:
    mode = st.session_state.get("price_source_mode", "自動")
    if mode in ["WebSocket", "Yfinance", "本地資料庫(twse_ohlcv.db)"]:
        return mode
    if now_dt is None:
        now_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    cutoff = now_dt.replace(hour=AUTO_YFINANCE_AFTER_HOUR, minute=AUTO_YFINANCE_AFTER_MINUTE, second=0, microsecond=0)
    return "Yfinance" if now_dt >= cutoff else "WebSocket"

def render_price_source_selector(now_dt):
    active_source = resolve_price_source(now_dt)
    source_mode = st.session_state.get("price_source_mode", "自動")
    with st.sidebar.expander("🧭 資料來源開關", expanded=True):
        st.markdown(
            f'''
            <div style="background:#2f4563; color:#35a8ff; border-radius:8px; padding:14px 16px; line-height:1.8; font-weight:600;">
            目前資料來源模式：{source_mode}；<br>
            實際使用：{active_source}
            </div>
            ''',
            unsafe_allow_html=True,
        )
        st.caption(
            f"自動模式：{AUTO_YFINANCE_AFTER_HOUR}:{AUTO_YFINANCE_AFTER_MINUTE:02d} 前為富邦+Yfinance，之後全轉 Yfinance。<br>"
            f"本地資料庫模式：歷史資料從 DB 讀取，今日資料從 Yfinance/富邦 獲取。"
        )
        mode_options = ["自動", "WebSocket", "Yfinance", "本地資料庫(twse_ohlcv.db)"]
        selected_mode = st.radio(
            "資料來源開關",
            options=mode_options,
            index=mode_options.index(source_mode) if source_mode in mode_options else 0,
            horizontal=True,
            key="price_source_mode_radio",
            label_visibility="collapsed",
        )
        if selected_mode != source_mode:
            st.session_state.price_source_mode = selected_mode
            st.cache_data.clear()
            st.rerun()
    return active_source

def download_stock_data_by_source(
    symbol: str, _sdk, source: str, today_str: str,
    history_map: dict = None, yf_today_map: dict = None,
):
    """
    2026 效能改版：
    「今天以前」的歷史資料一律固定改用本地 twse_ohlcv.db 讀取——
    這份資料庫本來就有 GitHub Actions 每天自動同步最新收盤價，免費、不佔用
    富邦/Yfinance 的 API 額度，也不會被限流(429)。
    程式只需要額外抓「今天」這一筆即時資料，大幅減少對外部 API 的呼叫次數。
    不管使用者選的是哪一種「今日價格來源」，歷史區段都一律走本地資料庫；
    只有「今天」這筆資料的來源會依照 source 參數決定要打富邦還是 Yfinance。
    """
    history_map = history_map or {}
    yf_today_map = yf_today_map or {}

    def _combine(history_df, today_df):
        frames = [d for d in [history_df, today_df] if d is not None and not d.empty]
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        if "Date" in combined.columns:
            combined = combined.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
        return combined

    # 歷史資料(今天以前)一律用本地資料庫，不再打富邦/Yfinance 的歷史K線 API
    history_df = history_map.get(symbol)
    if history_df is None:
        history_df = download_stock_data_db_history(symbol, today_str)

    # 只有「今天」這一筆需要即時抓，依 source 決定要用哪個即時來源
    if source == "Yfinance":
        today_df = yf_today_map.get(symbol)
        if today_df is None:
            today_df = download_stock_data_yfinance_today(symbol, today_str)
    else:
        # "WebSocket" / "本地資料庫(twse_ohlcv.db)" / 其他：優先用富邦即時K線，
        # 沒登入富邦或抓不到才退回 Yfinance 補today。
        today_df = download_stock_data_fubon_today(symbol, _sdk, today_str) if _sdk is not None else pd.DataFrame()
        if today_df is None or today_df.empty:
            today_df = yf_today_map.get(symbol)
            if today_df is None:
                today_df = download_stock_data_yfinance_today(symbol, today_str)

    df = _combine(history_df, today_df)
    if not df.empty:
        return df

    # 最終備援 (理論上很少走到): 本地資料庫也抓不到時，才退回直接打富邦抓90天。
    if _sdk is not None:
        return download_stock_data(symbol, _sdk)
    return pd.DataFrame()

def get_last_price_by_source(symbol: str, df, _sdk, source: str):
    if source == "Yfinance":
        if df is not None and not df.empty and "Close" in df.columns:
            price = pd.to_numeric(df["Close"], errors="coerce").dropna()
            if not price.empty:
                return float(price.iloc[-1])
        if _sdk is not None:
            return get_last_price(symbol, df, _sdk)
        raise ValueError("yfinance 無法取得價格")
    return get_last_price(symbol, df, _sdk)

@st.cache_data(ttl=86400)
def get_stock_name(symbol: str, _sdk) -> str:
    name_map = load_stock_name_map(STOCK_NAME_FILE)
    if symbol in name_map:
        return name_map[symbol]
        
    fubon_symbol = str(symbol).split(".")[0]
    if _sdk is not None:
        try:
            res = _sdk.marketdata.rest_client.stock.historical.stats(symbol=fubon_symbol)
            if res and "name" in res:
                return res["name"].strip()
        except Exception:
            pass
            
    return fubon_symbol

# ==========================================
# 2. 缺失的 UI、匯出與輔助工具
# ==========================================

def ensure_fubon_session_state():
    if "fubon_sdk" not in st.session_state:
        st.session_state.fubon_sdk = None
    if "fubon_logged_in" not in st.session_state:
        st.session_state.fubon_logged_in = False
    if "price_source_mode" not in st.session_state:
        st.session_state.price_source_mode = "自動"

def render_fubon_login_sidebar():
    ensure_fubon_session_state()
    st.sidebar.markdown("## 🔑 富邦 API 設定 (Fubon Neo)")

    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 API 已成功連線")
        if st.sidebar.button("登出 / 重新連線", use_container_width=True, key="qmd_fubon_logout_btn"):
            st.session_state.fubon_sdk = None
            st.session_state.fubon_logged_in = False
            st.rerun()
        return

    try:
        from fubon_neo.sdk import FubonSDK
    except ImportError:
        st.sidebar.error("請先安裝富邦 API 套件：執行 `pip install fubon-neo`")
        return

    try:
        if hasattr(st, "secrets") and "fubon" in st.secrets:
            fubon_secrets = st.secrets["fubon"]
            pfx_base64 = fubon_secrets["pfx_base64"]
        else:
            raise KeyError("secrets not found")
    except KeyError:
        st.sidebar.error("❌ 找不到 Streamlit Secrets 中的 pfx_base64 憑證資料。")
        return

    st.sidebar.info("請輸入富邦證券登入資訊")
    f_id = st.sidebar.text_input("身分證字號", key="qmd_f_id_input")
    f_pw = st.sidebar.text_input("富邦登入密碼", key="qmd_f_pw_input", type="password")
    f_cert_pw = st.sidebar.text_input("憑證密碼", key="qmd_f_cert_pw_input", type="password")

    if st.sidebar.button("連線行情伺服器", use_container_width=True, key="qmd_fubon_login_btn"):
        if not f_id or not f_pw or not f_cert_pw:
            st.sidebar.warning("請填寫完整的身分證字號與密碼！")
        else:
            try:
                import base64
                temp_cert_path = "temp_cloud_cert.pfx"
                with open(temp_cert_path, "wb") as f:
                    f.write(base64.b64decode(pfx_base64))
                with st.spinner("連線富邦 API 中..."):
                    sdk = FubonSDK()
                    sdk.login(f_id.strip().upper(), f_pw, temp_cert_path, f_cert_pw)
                    sdk.init_realtime()
                    st.session_state.fubon_sdk = sdk
                    st.session_state.fubon_logged_in = True
                st.sidebar.success("✅ 富邦 API 連線成功！")
                st.rerun()
            except Exception as e:
                st.sidebar.error(f"❌ 登入失敗: {e}")

def render_price_source_selector_sidebar(now_dt):
    ensure_fubon_session_state()
    active_source = resolve_price_source(now_dt)
    source_mode = st.session_state.get("price_source_mode", "自動")
    with st.sidebar.expander("🧭 資料來源開關", expanded=False):
        st.markdown(
            f'''
            <div style="background:#2f4563; color:#35a8ff; border-radius:8px; padding:14px 16px; line-height:1.8; font-weight:600;">
            目前資料來源模式：{source_mode}；<br>
            實際使用：{active_source}
            </div>
            ''',
            unsafe_allow_html=True,
        )
        st.caption(
            f"自動模式：{AUTO_YFINANCE_AFTER_HOUR}:{AUTO_YFINANCE_AFTER_MINUTE:02d} 前為富邦+Yfinance，之後全轉 Yfinance。<br>"
            f"本地資料庫模式：歷史資料從 DB 讀取，今日資料從 Yfinance/富邦 獲取。"
        )
        mode_options = ["自動", "WebSocket", "Yfinance", "本地資料庫(twse_ohlcv.db)"]
        selected_mode = st.radio(
            "資料來源開關",
            options=mode_options,
            index=mode_options.index(source_mode) if source_mode in mode_options else 0,
            horizontal=True,
            key="qmd_price_source_mode_radio",
            label_visibility="collapsed",
        )
        if selected_mode != source_mode:
            st.session_state.price_source_mode = selected_mode
            st.cache_data.clear()
            st.rerun()
    return active_source

def yahoo_quote_url(symbol: str) -> str:
    fubon_symbol = str(symbol).split(".")[0]
    return f"https://tw.stock.yahoo.com/quote/{fubon_symbol}"

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def contains_cjk(text) -> bool:
    if text is None:
        return False
    s = str(text)
    return any(
        ("\u4e00" <= ch <= "\u9fff") or
        ("\u3400" <= ch <= "\u4dbf") or
        ("\uf900" <= ch <= "\ufaff")
        for ch in s
    )

def apply_excel_fonts(workbook):
    from openpyxl.styles import Font
    chinese_font_name = "Microsoft JhengHei"
    english_font_name = "Calibri"
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    cell.font = Font(name=english_font_name)
                elif contains_cjk(cell.value):
                    cell.font = Font(name=chinese_font_name)
                else:
                    cell.font = Font(name=english_font_name)

def format_color(val):
    if isinstance(val, (int, float)):
        if val > 0: return f"🔴 +{val:.2f}%"
        elif val < 0: return f"🟢 {val:.2f}%"
        else: return f"{val:.2f}%"
    return val

def format_volume(val):
    try:
        return f"{float(val):,.1f}"
    except Exception:
        return val

def _parse_symbol_lines(lines):
    symbols = []
    seen = set()
    for raw_line in lines:
        line = str(raw_line).strip().replace("\ufeff", "").replace("\u3000", "")
        if not line: continue
        symbol = re.split(r"[\s,，]+", line, maxsplit=1)[0].strip().upper()
        if not re.match(r"^[0-9A-Z]+\.(TW|TWO)$", symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols

def parse_stock_symbols_from_text(text: str) -> list:
    if not text: return []
    return _parse_symbol_lines(text.splitlines())

def normalize_symbol_quick(input_text: str):
    s = str(input_text).strip().upper()
    if not s: return None
    if "." in s: return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")): return f"{s}.TWO"
        return f"{s}.TW"
    return s

def parse_manual_symbols(text: str) -> list:
    if not text: return []
    text = text.replace("，", ",")
    tokens = []
    for raw_line in text.splitlines():
        for part in raw_line.split(","):
            part = part.strip()
            if part: tokens.append(part)
    symbols = []
    seen = set()
    for t in tokens:
        sym = normalize_symbol_quick(t)
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols

@st.cache_data(ttl=86400)
def load_code_to_ticker_map(file_path: str = STOCK_NAME_FILE) -> dict:
    mapping = {}
    if not os.path.exists(file_path): return mapping
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\u3000", "")
            if not line: continue
            parts = re.split(r"[\t]+", line) if "\t" in line else line.split(None, 1)
            if not parts: continue
            ticker = parts[0].strip().upper()
            if "." in ticker: mapping[ticker.split(".")[0]] = ticker
    return mapping

def resolve_ticker_suffix(raw_code, code_map: dict = None) -> str:
    code_map = code_map or {}
    raw = str(raw_code).strip().upper()
    if not raw: return ""
    if "." in raw: return raw
    if raw in code_map: return code_map[raw]
    return normalize_symbol_quick(raw) or raw
