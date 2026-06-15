import re
import os
import gc
import requests
from html import escape
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ===== yfinance 選用資料源 =====
try:
    import yfinance as yf
except ImportError:
    yf = None

# ===== Streamlit UI 基本設定（一定要放最前面）=====
st.set_page_config(layout="wide")

if yf is None:
    st.error("請先安裝 yfinance 套件：執行 `pip install yfinance`")
    st.stop()

# ===== 常數設定 =====
REFRESH_SEC = 60
ENABLE_GAP_SIGNAL = True
STOCK_NAME_FILE = "TWstocklistname2.txt"
STOCK_SCAN_FILE = "TWstocklistname2.txt"
ALL_STOCK_GROUP_NAME = "台股掃描器Ver.YF_告訴我你會買日月光"

# ===== Telegram 設定（請替換為你的資訊）=====
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "")  

# ===== CSS =====
st.markdown("""
<style>
.dashboard-scroll { overflow-x: auto; overflow-y: hidden; width: 100%; padding-bottom: 8px; }
.dashboard-grid { display: grid; grid-template-columns: repeat(4, minmax(260px, 1fr)); gap: 12px; min-width: 1120px; }
.dashboard-card { border-radius: 12px; padding: 14px 16px; min-height: 180px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); box-sizing: border-box; }
.dashboard-title { font-size: 18px; font-weight: 700; margin-bottom: 10px; color: #000000 !important; }
.dashboard-main { font-size: 28px; font-weight: 800; margin-bottom: 6px; }
.dashboard-sub { font-size: 14px; color: #000000 !important; margin-bottom: 10px; }
.dashboard-detail { font-size: 14px; line-height: 1.7; color: #000000 !important; }
.dashboard-extra { font-size: 13px; line-height: 1.6; color: #000000 !important; margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(0,0,0,0.12); word-break: break-word; }
.dashboard-link, .dashboard-link:link, .dashboard-link:visited, .dashboard-link:hover, .dashboard-link:active { text-decoration: none !important; color: inherit !important; }
.back-to-dashboard-btn { display: inline-block; padding: 6px 12px; border-radius: 8px; border: 1px solid #999; background: #f5f5f5; color: #000 !important; text-decoration: none !important; font-size: 14px; font-weight: 600; text-align: center; }
.back-to-dashboard-btn:hover { background: #eaeaea; }
</style>
""", unsafe_allow_html=True)

# ===== Telegram 工具 =====
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            st.error(f"Telegram 傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 連線失敗: {e}")


def send_telegram_document(file_bytes: bytes, filename: str, caption: str = "") -> bool:
    """把 Excel 等檔案傳送到 Telegram。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        st.error("Telegram Bot Token 或 Chat ID 尚未設定，無法推送檔案。")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    files = {
        "document": (
            filename,
            file_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    }
    try:
        res = requests.post(url, data=data, files=files, timeout=20)
        if res.status_code == 200:
            return True
        st.error(f"Telegram 檔案傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 檔案傳送連線失敗: {e}")
    return False

def check_telegram_push_command():
    if not TELEGRAM_BOT_TOKEN:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1} 
    
    if "tg_last_update_id" in st.session_state and st.session_state.tg_last_update_id:
        params["offset"] = st.session_state.tg_last_update_id + 1

    try:
        res = requests.get(url, params=params, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("ok") and data.get("result"):
                st.sidebar.info(f"👀 偷看到 {len(data['result'])} 則新訊息") 
                
                triggered = False
                for item in data["result"]:
                    update_id = item["update_id"]
                    st.session_state.tg_last_update_id = update_id 
                    
                    message_text = item.get("message", {}).get("text", "").strip().lower()
                    st.sidebar.write(f"💬 內容: {message_text}") 
                    
                    if message_text == "push":
                        triggered = True
                return triggered
    except Exception as e:
        pass
    return False

# ===== Yahoo Finance / yfinance 行情工具 =====
def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if set(required_cols).issubset(df.columns):
        return df[required_cols].copy()
    return pd.DataFrame()

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
    """從 TWstocklistname2.txt 讀取所有股票代碼，支援 Tab/空白分隔，並去除重複與異常空白。"""
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

@st.cache_data(ttl=REFRESH_SEC)
def download_stock_data_yfinance(symbol: str):
    """使用 yfinance / Yahoo Finance 取得歷史日 K。"""
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(str(symbol).strip().upper(), period="4mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.reset_index()
        required_cols = ["Open", "High", "Low", "Close", "Volume"]
        if not set(required_cols).issubset(df.columns):
            return pd.DataFrame()
        for col in required_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[required_cols].dropna(subset=["Open", "High", "Low", "Close"]).reset_index(drop=True)
    except Exception as e:
        print(f"yfinance 抓取 {symbol} 歷史 K 線失敗: {e}")
        return pd.DataFrame()

def resolve_price_source(now_dt=None) -> str:
    """固定使用 Yfinance。"""
    return "Yfinance"

def render_price_source_selector(now_dt):
    """固定使用 Yfinance，不提供資料來源切換。"""
    return "Yfinance"

def download_stock_data_by_source(symbol: str, _sdk=None, source: str = "Yfinance"):
    """固定使用 yfinance / Yahoo Finance 取得日 K。"""
    return download_stock_data_yfinance(symbol)

def get_last_price_by_source(symbol: str, df, _sdk=None, source: str = "Yfinance"):
    """固定使用 yfinance 日 K 最新 Close 作為價格。"""
    if df is not None and not df.empty and "Close" in df.columns:
        price = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if not price.empty:
            return float(price.iloc[-1])
    raise ValueError("yfinance / Yahoo TW 無法取得價格")

def normalize_rows_for_excel(rows):
    columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "MA位置", "MA排列", "K值", "D值", "KD訊號", "跳空訊號", "訊號類型", "來源"]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows).drop_duplicates(subset=["代碼"]).copy()
    if "代碼網址" in df.columns:
        df.drop(columns=["代碼網址"], inplace=True)
    for col in columns:
        if col not in df.columns:
            df[col] = ""
    return df[columns]


def contains_cjk(text) -> bool:
    """判斷儲存格文字是否包含中文/日文/韓文，用以套用中文字型。"""
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
    """輸出 Excel 字型：中文使用微軟正黑體，英文/數字使用 Calibri。"""
    from openpyxl.styles import Font

    chinese_font_name = "Microsoft JhengHei"  # 微軟正黑體
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

def build_signal_excel_bytes(signal_buckets: dict) -> bytes:
    gap_rows = signal_buckets.get("跳空", [])
    golden_rows = signal_buckets.get("黃金交叉", []) + signal_buckets.get("即將黃金交叉", [])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        normalize_rows_for_excel(gap_rows).to_excel(writer, sheet_name="跳空", index=False)
        normalize_rows_for_excel(golden_rows).to_excel(writer, sheet_name="黃金交叉", index=False)
        apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()

@st.cache_data(ttl=86400)
def get_stock_name(symbol: str, _sdk=None) -> str:
    """股票名稱固定從 TWstocklistname2.txt 讀取；找不到則回傳股票代碼。"""
    name_map = load_stock_name_map(STOCK_NAME_FILE)
    if symbol in name_map:
        return name_map[symbol]
    return str(symbol).split(".")[0]

# ===== 輔助工具函式 =====
def make_anchor_id(group_name: str) -> str:
    anchor = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", group_name).strip("-")
    return f"group-{anchor}"

def yahoo_quote_url(symbol: str) -> str:
    quote_symbol = str(symbol).split(".")[0]
    return f"https://tw.stock.yahoo.com/quote/{quote_symbol}"

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def build_top3_html(valid_stock_stats):
    if not valid_stock_stats:
        return '<span style="color:#666666;">無可用資料</span>'
    top3_sorted = sorted(valid_stock_stats, key=lambda x: x["pct"], reverse=True)[:3]
    parts = []
    for item in top3_sorted:
        pct = float(item["pct"])
        if pct > 0:
            pct_color = "#cf1322"
        elif pct < 0:
            pct_color = "#389e0d"
        else:
            pct_color = "#333333"
        code_text = escape(str(item["code"]))
        name_text = escape(str(item["name"]))
        pct_text = f"{pct:+.1f}%"
        parts.append(
            f'<span style="color:#000000;">{code_text} {name_text} </span>'
            f'<span style="color:{pct_color}; font-weight:600;">{pct_text}</span>'
        )
    return " | ".join(parts)

def compact_name_list(names, max_show=3):
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names:
        return "無"
    if len(names) <= max_show:
        return "、".join(names)
    return "、".join(names[:max_show]) + f" 等{len(names)}檔"

# ===== Session State 初始化 =====
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = False

if "tg_push_enabled" not in st.session_state:
    st.session_state.tg_push_enabled = False

if "scheduled_push_enabled" not in st.session_state:
    st.session_state.scheduled_push_enabled = True

if "processed_time_slots" not in st.session_state:
    st.session_state.processed_time_slots = set()

# 全部股票來源固定為 TWstocklistname2.txt
st.session_state.stock_groups = load_all_stock_group_from_file()

if "scan_enabled" not in st.session_state:
    st.session_state.scan_enabled = False
if "scan_requested" not in st.session_state:
    st.session_state.scan_requested = False

if "notified_stocks" not in st.session_state:
    st.session_state.notified_stocks = set()

if "tg_last_update_id" not in st.session_state:
    st.session_state.tg_last_update_id = None

# ===== UI 元件 =====
def compute_indicators(df, price):
    if df is None or df.empty:
        raise ValueError("下載資料為空")
    if len(df) < 20:
        raise ValueError("歷史資料不足（至少需要 20 筆）")

    close = pd.to_numeric(df["Close"].squeeze(), errors="coerce")
    low = pd.to_numeric(df["Low"].squeeze(), errors="coerce")
    high = pd.to_numeric(df["High"].squeeze(), errors="coerce")
    volume = pd.to_numeric(df["Volume"].squeeze(), errors="coerce") if "Volume" in df.columns else pd.Series(dtype="float64")
    if close.isna().all() or low.isna().all() or high.isna().all():
        raise ValueError("OHLC 資料格式異常")

    yesterday_close = float(close.iloc[-2])
    if pd.isna(yesterday_close) or yesterday_close == 0:
        raise ValueError("昨收資料異常")

    price_val = float(price)
    change_pct = float((price_val / yesterday_close - 1) * 100)
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())

    if price_val > ma5: ma_range = ">MA5"
    elif ma5 >= price_val > ma10: ma_range = "MA5~10"
    elif ma10 >= price_val > ma20: ma_range = "MA10~20"
    else: ma_range = "<MA20"

    if ma5 > ma10 > ma20: ma_trend = "多頭"
    elif ma5 < ma10 < ma20: ma_trend = "空頭"
    else: ma_trend = "糾結"

    low_9 = low.rolling(9).min()
    high_9 = high.rolling(9).max()
    denominator = (high_9 - low_9).replace(0, pd.NA)

    rsv = ((close - low_9) / denominator) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    if len(k.dropna()) < 2 or len(d.dropna()) < 2:
        raise ValueError("KD 計算資料不足")

    k_t = float(k.iloc[-1])
    d_t = float(d.iloc[-1])
    k_y = float(k.iloc[-2])
    d_y = float(d.iloc[-2])

    if k_y <= d_y and k_t > d_t: kd_signal = "黃金交叉"
    elif k_y >= d_y and k_t < d_t: kd_signal = "死亡交叉"
    elif k_t < d_t and (d_t - k_t) < 3: kd_signal = "即將黃金交叉"
    elif k_t > d_t and (k_t - d_t) < 3: kd_signal = "即將死亡交叉"
    elif k_t < 25: kd_signal = "超賣"
    else: kd_signal = "-"

    latest_volume = 0.0
    if not volume.empty and pd.notna(volume.iloc[-1]):
        latest_volume = float(volume.iloc[-1])
    volume_lots = latest_volume / 1000

    gap_signal = "-"
    today_low = float(low.iloc[-1])
    yesterday_high = float(high.iloc[-2])
    if ENABLE_GAP_SIGNAL and pd.notna(today_low) and pd.notna(yesterday_high) and today_low > yesterday_high:
        gap_signal = "跳空"

    return {
        "price": round(price_val, 2),
        "pct": round(change_pct, 2),
        "ma_range": ma_range,
        "ma_trend": ma_trend,
        "k": round(k_t, 1),
        "d": round(d_t, 1),
        "kd_signal": kd_signal,
        "gap_signal": gap_signal,
        "volume": int(latest_volume),
        "volume_lots": round(volume_lots, 1),
    }

def format_color(val):
    if isinstance(val, (int, float)):
        if val > 0: return f"🔴 +{val:.2f}%"
        elif val < 0: return f"🟢 {val:.2f}%"
        else: return f"{val:.2f}%"
    return val

def format_k(val):
    if isinstance(val, (int, float)):
        if val >= 74: return f"🔴 {val:.1f}"
        elif val >= 50: return f"🟡 {val:.1f}"
        else: return f"🟢 {val:.1f}"
    return val

def format_gap(val):
    if val == "跳空": return "🔴 跳空"
    return "-"

def format_volume(val):
    try:
        return f"{float(val):,.1f}"
    except Exception:
        return val


def render_scan_progress_card(placeholder, pct: float, status_text: str = "掃描進度"):
    """右上角掃描進度卡片：以百分比顯示，進度條仍另外保留。"""
    pct = max(0.0, min(float(pct), 100.0))
    placeholder.markdown(
        f"""
        <div style="
            width: 120px;
            min-height: 78px;
            border: none;
            border-radius: 0;
            padding: 8px 10px;
            text-align: left;
            background: transparent;
            box-sizing: border-box;
        ">
            <div style="font-size: 30px; line-height: 1; font-weight: 800;">{pct:.0f}%</div>
            <div style="font-size: 13px; margin-top: 8px;">{status_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

def render_summary_dashboard(group_up_summary, rise_threshold):
    st.markdown("### 📌 漲幅儀表板")
    st.caption(f"目前儀表板統計門檻：漲幅 ≥ {rise_threshold}%")
    html_parts = []
    html_parts.append('<div class="dashboard-scroll"><div class="dashboard-grid">')

    for item in group_up_summary:
        group_name = escape(str(item["分類"]))
        anchor_id = make_anchor_id(group_name)
        hit_count = item["達標數"]
        total_count = item["總數"]
        up_count = item["上漲數"]
        down_count = item["下跌數"]
        hit_names_text = escape(str(item["達標股票名稱"]))
        top3_html = item["前三名HTML"]

        hit_ratio = (hit_count / total_count * 100) if total_count > 0 else 0
        if hit_ratio >= 60: bg_color = "#fff1f0"; border_color = "#ff7875"; accent_color = "#cf1322"
        elif hit_ratio > 0: bg_color = "#fff7e6"; border_color = "#ffa940"; accent_color = "#d46b08"
        else: bg_color = "#f6ffed"; border_color = "#95de64"; accent_color = "#389e0d"

        card_html = (
            f'<a href="#{anchor_id}" class="dashboard-link">'
            f'<div class="dashboard-card" style="background-color:{bg_color}; border:1px solid {border_color}; cursor:pointer;">'
            f'<div class="dashboard-title">{group_name}</div>'
            f'<div class="dashboard-main" style="color:{accent_color};">{hit_count} / {total_count}</div>'
            f'<div class="dashboard-sub">漲幅達標比例（≥{rise_threshold}%）：{hit_ratio:.0f}%</div>'
            f'<div class="dashboard-detail">'
            f'🎯 達標：<b>{hit_count}</b> 檔（{hit_names_text}）<br>'
            f'🔴 一般上漲：<b>{up_count}</b><br>'
            f'🟢 下跌：<b>{down_count}</b>'
            f'</div>'
            f'<div class="dashboard-extra">▶ {top3_html}</div>'
            f'</div></a>'
        )
        html_parts.append(card_html)
    html_parts.append("</div></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)

# ==================== 主畫面開始 ====================
title_col, scan_progress_col = st.columns([8, 1])
with title_col:
    st.title("📊 台股掃描器Ver.YF_告訴我你會買日月光")
with scan_progress_col:
    scan_progress_card_placeholder = st.empty()
render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
st.markdown('<div id="dashboard-top"></div>', unsafe_allow_html=True)

gc.collect()

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_price_source = render_price_source_selector(tw_now)


st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}")

rise_threshold = st.slider("儀表板漲幅達標門檻 (%)", min_value=5, max_value=9, value=5, step=1)

st.markdown("### 🎯 掃描條件")
scan_btn_col1, scan_btn_col2, scan_col1, scan_col2, scan_col3, scan_vol_col, scan_col4 = st.columns([0.9, 0.9, 1.3, 0.9, 1.4, 1.2, 1.8])
with scan_btn_col1:
    if st.button("▶️ 開始掃描", use_container_width=True, disabled=st.session_state.scan_enabled):
        st.session_state.scan_enabled = True
        st.session_state.scan_requested = True
        st.cache_data.clear()
        st.rerun()
with scan_btn_col2:
    if st.button("⏹️ 停止掃描", use_container_width=True, disabled=not st.session_state.scan_enabled):
        st.session_state.scan_enabled = False
        st.session_state.scan_requested = False
        st.rerun()
with scan_col1:
    show_only_signal_rows = st.toggle("只顯示訊號股票", value=True, help="開啟後，主表只列出：跳空、黃金交叉、即將黃金交叉")
with scan_col2:
    include_gap_signal_filter = st.checkbox("跳空", value=True)
with scan_col3:
    include_kd_signal_filter = st.checkbox("黃金交叉 / 即將黃金交叉", value=True)
with scan_vol_col:
    min_volume_lots = st.number_input(
        "成交量(張)下限",
        min_value=0,
        value=1000,
        step=100,
        help="只保留成交量(張) >= 此數值的掃描結果；預設 1000 張。"
    )
with scan_col4:
    scan_action_placeholder = st.empty()

if st.session_state.scan_enabled:
    st.caption("🟢 掃描狀態：執行中")
elif "last_scan_result" in st.session_state:
    st.caption(
        f"✅ 掃描狀態：已完成，上次完成時間：{st.session_state.last_scan_result.get('scan_completed_at', '-')}｜成交量下限：{st.session_state.last_scan_result.get('min_volume_lots', 1000)} 張"
    )
else:
    st.caption("⚪ 掃描狀態：已停止，按「開始掃描」才會抓取資料。")

selected_signal_names = []
if include_gap_signal_filter:
    selected_signal_names.append("跳空")
if include_kd_signal_filter:
    selected_signal_names.extend(["黃金交叉", "即將黃金交叉"])
if not selected_signal_names:
    st.warning("請至少勾選一種掃描訊號，否則不會列出訊號股票。")

# 依價格來源檢查必要套件
if yf is None:
    st.warning("⚠️ 目前版本固定使用 Yfinance，請先安裝套件：pip install yfinance")
    st.stop()

should_run_scan = bool(st.session_state.pop("scan_requested", False))
has_last_scan_result = "last_scan_result" in st.session_state

if not should_run_scan and not has_last_scan_result:
    render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
    st.info("請按「開始掃描」開始抓取股票資料。")
    st.stop()

if should_run_scan:
    # ===== 推送時間與手動指令邏輯判斷 =====
    can_push_now = False
    current_schedule_key = None
    manual_push_triggered = False

    if st.session_state.tg_push_enabled:
        # 偷偷去問 Telegram 有沒有收到 push 指令
        manual_push_triggered = check_telegram_push_command()
    
        if manual_push_triggered:
            can_push_now = True
            st.session_state.notified_stocks = set() # 清空今日已通知紀錄，強制重發
            st.toast("🚀 收到 'push' 指令，強制觸發推播！")
            send_telegram_message("🤖 <b>收到指令，開始為您掃描並強制推播強勢股...</b>")
        elif st.session_state.scheduled_push_enabled:
            # 定義每天的目標發送時間
            TARGET_TIMES = [
                tw_now.replace(hour=9, minute=40, second=0, microsecond=0),
                tw_now.replace(hour=10, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=11, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=12, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=13, minute=0, second=0, microsecond=0)
            ]

            for target_dt in TARGET_TIMES:
                # 計算當下時間與目標時間的差距（秒）
                diff_seconds = (tw_now - target_dt).total_seconds()
            
                # 若時間差距在正負 60 秒以內
                if abs(diff_seconds) <= 45:
                    # 產生唯一的排程 Key，例如 slot_20260609_0940
                    time_str = target_dt.strftime("%H%M")
                    today_str = tw_now.strftime("%Y%m%d")
                    current_schedule_key = f"slot_{today_str}_{time_str}"
                
                    # 檢查該時段今天是否已經觸發過
                    if current_schedule_key not in st.session_state.processed_time_slots:
                        can_push_now = True
                        break  # 條件符合就跳出迴圈
        else:
            # 修正：關閉排程時不應預設推播，否則 Streamlit 重刷就會一直送訊息
            can_push_now = False

    group_tables = {}
    group_up_summary = []
    all_signal_rows = []
    signal_buckets = {"跳空": [], "黃金交叉": [], "即將黃金交叉": []}
    scan_total_count = sum(len(stocks) for stocks in st.session_state.stock_groups.values())
    render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
    progress_bar = st.progress(0, text=f"掃描進度：0.0%（準備掃描 {scan_total_count} 檔股票）")
    processed_count = 0

    for group_name, stocks in st.session_state.stock_groups.items():
        rows = []
        hit_count = up_count = down_count = flat_count = error_count = 0
        valid_stock_stats = []
        hit_names = []

        for symbol in stocks:
            if not st.session_state.scan_enabled:
                progress_bar.empty()
                st.warning("掃描已停止。")
                st.stop()
            processed_count += 1
            if scan_total_count > 0:
                progress_value = min(processed_count / scan_total_count, 1.0)
                progress_pct = progress_value * 100
                render_scan_progress_card(scan_progress_card_placeholder, progress_pct, "掃描進度")
                progress_bar.progress(progress_value, text=f"掃描進度：{progress_pct:.1f}%（{processed_count}/{scan_total_count}：{symbol}）")
            try:
                df = download_stock_data_by_source(symbol)
                df = normalize_ohlc(df)
                if df.empty: raise ValueError("無效的 K 線資料")

                price = get_last_price_by_source(symbol, df)
                stock_name = get_stock_name(symbol)
                data = compute_indicators(df, price)

                signal_types = []
                if data["gap_signal"] == "跳空":
                    signal_types.append("跳空")
                if data["kd_signal"] in ["黃金交叉", "即將黃金交叉"]:
                    signal_types.append(data["kd_signal"])
                passes_volume_filter = float(data.get("volume_lots", 0)) >= float(min_volume_lots)
                is_selected_signal = any(sig in selected_signal_names for sig in signal_types) and passes_volume_filter

                # ===== 執行推播檢查 =====
                is_high_gain = data["pct"] >= 5
                if (is_high_gain or is_selected_signal) and passes_volume_filter:
                    base_symbol = symbol.split('.')[0]
                    yahoo_url = f"https://tw.stock.yahoo.com/quote/{base_symbol}"
                    symbol_link = f'<a href="{yahoo_url}">{symbol}</a>'
                    today_str = tw_now.strftime("%Y-%m-%d")
                    notify_key = f"{symbol}_{today_str}"
                    if can_push_now and (notify_key not in st.session_state.notified_stocks):
                        msg = (
                            f"🔔 <b>全市場掃描訊號：{stock_name} ({symbol_link})</b>\n\n"
                            f"📈 價格：{data['price']}\n"
                            f"🔥 漲幅：{data['pct']}%\n"
                            f"📦 成交量：{data['volume_lots']:,.1f} 張\n"
                            f"📊 KD訊號：{data['kd_signal']}\n"
                            f"🚀 跳空訊號：{data['gap_signal']}\n"
                            f"🔌 來源：{active_price_source}"
                        )
                        send_telegram_message(msg)
                        st.session_state.notified_stocks.add(notify_key)
                # =======================

                if data["pct"] >= rise_threshold:
                    hit_count += 1
                    hit_names.append(stock_name)
                if data["pct"] > 0: up_count += 1
                elif data["pct"] < 0: down_count += 1
                else: flat_count += 1

                valid_stock_stats.append({"symbol": symbol, "code": symbol_to_code(symbol), "name": stock_name, "pct": float(data["pct"])})
                row = {
                    "代碼": symbol, "代碼網址": yahoo_quote_url(symbol), "股票名稱": stock_name,
                    "價格": f"{data['price']:.2f}", "漲跌%": data["pct"],
                    "成交量(張)": data["volume_lots"],
                    "MA位置": data["ma_range"], "MA排列": data["ma_trend"],
                    "K值": data["k"], "D值": f"{data['d']:.1f}",
                    "KD訊號": data["kd_signal"], "跳空訊號": data["gap_signal"],
                    "訊號類型": "、".join(signal_types) if signal_types else "-",
                    "來源": active_price_source,
                }
                if ((not show_only_signal_rows) or is_selected_signal) and passes_volume_filter:
                    rows.append(row)
                if is_selected_signal:
                    all_signal_rows.append(row.copy())
                    for sig in signal_types:
                        if sig in signal_buckets and sig in selected_signal_names:
                            signal_buckets[sig].append(row.copy())
            except Exception as e:
                error_count += 1
                if not show_only_signal_rows:
                    rows.append({
                        "代碼": symbol, "代碼網址": "", "股票名稱": get_stock_name(symbol),
                        "價格": "錯誤", "漲跌%": "-", "成交量(張)": "-",
                        "MA位置": "-", "MA排列": "-", "K值": "-", "D值": "-",
                        "KD訊號": "-", "跳空訊號": str(e), "訊號類型": "錯誤", "來源": active_price_source,
                    })

        hit_names_text = compact_name_list(hit_names, max_show=4)
        top3_html = build_top3_html(valid_stock_stats)
        df_table = pd.DataFrame(rows)
        display_df = df_table.copy()
        if not display_df.empty:
            display_df["漲跌%"] = display_df["漲跌%"].apply(format_color)
            display_df["K值"] = display_df["K值"].apply(format_k)
            display_df["成交量(張)"] = display_df["成交量(張)"].apply(format_volume)
            display_df["跳空訊號"] = display_df["跳空訊號"].apply(format_gap)
        group_tables[group_name] = {"count": len(stocks), "table": display_df}
        group_up_summary.append({
            "分類": group_name, "達標數": hit_count, "達標股票名稱": hit_names_text,
            "前三名HTML": top3_html, "上漲數": up_count, "下跌數": down_count,
            "平盤數": flat_count, "錯誤數": error_count, "總數": len(stocks)
        })

    render_scan_progress_card(scan_progress_card_placeholder, 100, "掃描進度")
    progress_bar.empty()
    if can_push_now and st.session_state.scheduled_push_enabled and current_schedule_key and not manual_push_triggered:
        st.session_state.processed_time_slots.add(current_schedule_key)


    # 掃描完成後將結果保存在 session_state；下載或 Telegram 推送造成 rerun 時，不會重新進入掃描。
    st.session_state.last_scan_result = {
        "group_tables": group_tables,
        "group_up_summary": group_up_summary,
        "all_signal_rows": all_signal_rows,
        "signal_buckets": signal_buckets,
        "excel_filename": f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx",
        "scan_completed_at": tw_now.strftime('%Y-%m-%d %H:%M:%S'),
        "progress_pct": 100,
        "min_volume_lots": min_volume_lots,
    }
    st.session_state.scan_enabled = False
else:
    last_scan_result = st.session_state.last_scan_result
    group_tables = last_scan_result.get("group_tables", {})
    group_up_summary = last_scan_result.get("group_up_summary", [])
    all_signal_rows = last_scan_result.get("all_signal_rows", [])
    signal_buckets = last_scan_result.get("signal_buckets", {"跳空": [], "黃金交叉": [], "即將黃金交叉": []})
    render_scan_progress_card(scan_progress_card_placeholder, last_scan_result.get("progress_pct", 100), "掃描進度")
excel_bytes = build_signal_excel_bytes(signal_buckets)
excel_filename = st.session_state.get("last_scan_result", {}).get(
    "excel_filename",
    f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx"
)

with scan_action_placeholder.container():
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        st.download_button("下載", data=excel_bytes, file_name=excel_filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, key="download_signal_excel_btn")
    with bcol2:
        if st.button("推送到telegram", use_container_width=True, key="push_signal_excel_to_tg_btn"):
            ok = send_telegram_document(excel_bytes, excel_filename, caption=f"TWstock 訊號掃描結果：跳空 / 黃金交叉｜成交量下限 {st.session_state.get('last_scan_result', {}).get('min_volume_lots', min_volume_lots)} 張｜{tw_now.strftime('%Y-%m-%d %H:%M:%S')}")
            if ok:
                st.success("已將 Excel 推送到 Telegram。")

st.markdown("### 🔎 訊號掃描結果")
unique_signal_count = len(pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])) if all_signal_rows else 0
st.metric("符合勾選訊號股票數", unique_signal_count)
if all_signal_rows:
    signal_df = pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])
    signal_display_df = signal_df.copy()
    signal_display_df["漲跌%"] = signal_display_df["漲跌%"].apply(format_color)
    signal_display_df["K值"] = signal_display_df["K值"].apply(format_k)
    signal_display_df["成交量(張)"] = signal_display_df["成交量(張)"].apply(format_volume)
    signal_display_df["跳空訊號"] = signal_display_df["跳空訊號"].apply(format_gap)
    signal_display_df["代碼"] = signal_display_df["代碼網址"]
    signal_columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "K值", "D值", "KD訊號", "跳空訊號", "訊號類型", "來源"]
    st.dataframe(signal_display_df[signal_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱")
    })
    with st.expander("依訊號分類查看", expanded=True):
        for sig_name, bucket_rows in signal_buckets.items():
            if sig_name not in selected_signal_names:
                continue
            unique_count = len(pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"])) if bucket_rows else 0
            st.markdown(f"#### {sig_name}（{unique_count} 檔）")
            if bucket_rows:
                bucket_df = pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"])
                bucket_display_df = bucket_df.copy()
                bucket_display_df["漲跌%"] = bucket_display_df["漲跌%"].apply(format_color)
                bucket_display_df["K值"] = bucket_display_df["K值"].apply(format_k)
                bucket_display_df["成交量(張)"] = bucket_display_df["成交量(張)"].apply(format_volume)
                bucket_display_df["跳空訊號"] = bucket_display_df["跳空訊號"].apply(format_gap)
                bucket_display_df["代碼"] = bucket_display_df["代碼網址"]
                st.dataframe(bucket_display_df[signal_columns], use_container_width=True, column_config={
                    "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
                    "股票名稱": st.column_config.TextColumn("股票名稱")
                })
            else:
                st.caption("目前沒有符合此訊號的股票。")
else:
    st.info("目前沒有掃描到符合勾選條件的股票。")

st.divider()
render_summary_dashboard(group_up_summary, rise_threshold)
st.divider()

for group_name, info in group_tables.items():
    anchor_id = make_anchor_id(group_name)
    st.markdown(f'<div id="{anchor_id}" style="scroll-margin-top: 80px;"></div>', unsafe_allow_html=True)
    header_col1, header_col2 = st.columns([8, 2])
    with header_col1: st.subheader(f"【{group_name}】({info['count']}檔)")
    with header_col2: st.markdown("""<div style="text-align:right; padding-top:0.4rem;"><a href="#dashboard-top" class="back-to-dashboard-btn">⬆ 回到儀表板</a></div>""", unsafe_allow_html=True)
    table_df = info["table"].copy()
    if not table_df.empty and "代碼網址" in table_df.columns: table_df["代碼"] = table_df["代碼網址"]
    display_columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "MA位置", "MA排列", "K值", "D值", "KD訊號", "跳空訊號", "訊號類型", "來源"]
    st.dataframe(table_df[display_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱")
    })
    st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)

