"""
0_📊_台股掃描器0730.py (原 app.py)
=======
台股掃描器主程式：只保留 UI 與主迴圈。
新增：支援由本地 twse_ohlcv.db (SQLite) 讀取歷史資料的選項。
"""

import re
import os
import json
import time
import gc
import requests
import base64
from html import escape
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from common_fubon import (
    REFRESH_SEC,
    FORCE_SCAN_ALL_STOCKS_FROM_FILE,
    ALL_STOCK_GROUP_NAME,
    STOCK_SCAN_FILE,
    FubonSDK,
    yf,
    load_all_stock_group_from_file,
    render_price_source_selector,
    bulk_download_db_history,          # 歷史資料一律走本地資料庫批次讀取
    bulk_download_yfinance_today,
    download_stock_data_by_source,
    normalize_ohlc,
    get_last_price_by_source,
    get_stock_name,
)
from signals import (
    compute_indicators,
    get_signal_registry,
)

from scoring import (
    LOCAL_DATABASE_DIR,
    TRACKING_FILE,
    SIGNAL_SCORE_MIN,
    PRIORITY_SCORE_MIN,
    ensure_local_database_dir,
    safe_float,
    classify_signal_grade,
    calc_signal_quality_score,
    build_priority_rows,
    append_signal_tracking,
)

# ===== Streamlit UI 基本設定（一定要放最前面）=====
st.set_page_config(layout="wide")

# ===== 常數設定 =====
GROUPS_FILE = "stock_groups.json"
APP_LOGO = "dog.jpg"

GITHUB_DATABASE_DIR = st.secrets.get("GITHUB_DATABASE_DIR", "Database")
AUTO_UPLOAD_GITHUB = bool(st.secrets.get("AUTO_UPLOAD_GITHUB", False))

# ===== UI 下拉選單相容工具 =====
def open_dropdown(label: str):
    if hasattr(st, "popover"):
        return st.popover(label, use_container_width=True)
    return st.expander(label, expanded=False)

# ===== Telegram 設定 =====
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

# ===== 分組讀寫 =====
def load_stock_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    # 股票分組現在改由「股票列表編輯器」頁面管理 stock_groups.json，
    # 這裡找不到檔案時就回傳空字典，不再內建預設分組。
    return {}

# ===== Telegram 工具 =====
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def send_telegram_document(file_bytes: bytes, filename: str, caption: str = "") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    files = {"document": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    try:
        res = requests.post(url, data=data, files=files, timeout=20)
        return res.status_code == 200
    except Exception:
        return False

def check_telegram_push_command():
    if not TELEGRAM_BOT_TOKEN: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1} 
    if "tg_last_update_id" in st.session_state and st.session_state.tg_last_update_id:
        params["offset"] = st.session_state.tg_last_update_id + 1
    try:
        res = requests.get(url, params=params, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("ok") and data.get("result"):
                triggered = False
                for item in data["result"]:
                    update_id = item["update_id"]
                    st.session_state.tg_last_update_id = update_id 
                    message_text = item.get("message", {}).get("text", "").strip().lower()
                    if message_text == "push": triggered = True
                return triggered
    except Exception:
        pass
    return False

# ===== GitHub Database 上傳工具 =====
def github_repo_config():
    return {
        "token": st.secrets.get("GITHUB_TOKEN", ""),
        "owner": st.secrets.get("GITHUB_OWNER", "henglunlin"),
        "repo": st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN"),
        "branch": st.secrets.get("GITHUB_BRANCH", "main"),
    }

def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_repo_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo: return False

    github_path = github_path.strip("/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{github_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha = None
    try:
        get_res = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")

        payload = {
            "message": commit_message,
            "content": base64.b64encode(file_bytes).decode("utf-8"),
            "branch": branch,
        }
        if sha: payload["sha"] = sha

        put_res = requests.put(url, headers=headers, json=payload, timeout=30)
        return put_res.status_code in (200, 201)
    except Exception:
        return False

def tracking_github_filename(dt=None) -> str:
    if dt is None: dt = datetime.now(ZoneInfo("Asia/Taipei"))
    elif isinstance(dt, str):
        try: dt = datetime.strptime(dt[:10], "%Y-%m-%d")
        except Exception: dt = datetime.now(ZoneInfo("Asia/Taipei"))
    return f"signal_tracking_{dt.strftime('%Y%m%d')}.csv"

def tracking_github_path(dt=None) -> str:
    return f"{GITHUB_DATABASE_DIR}/{tracking_github_filename(dt)}"

def upload_tracking_file_to_github(commit_suffix: str = "") -> bool:
    if not os.path.exists(TRACKING_FILE): return False
    with open(TRACKING_FILE, "rb") as f: data = f.read()
    upload_dt = datetime.now(ZoneInfo("Asia/Taipei"))
    suffix = f" {commit_suffix}" if commit_suffix else ""
    return upload_file_to_github(data, tracking_github_path(upload_dt), f"Update {tracking_github_filename(upload_dt)}{suffix}")

# ===== Excel 匯出工具 =====
def normalize_rows_for_excel(rows):
    columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "波動率%", "RS加權報酬%", "訊號分數", "追蹤等級", "MA位置", "MA排列", "訊號方向", "訊號類型", "訊號說明", "來源"]
    if not rows: return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows).drop_duplicates(subset=["代碼"]).copy()
    if "代碼網址" in df.columns: df.drop(columns=["代碼網址"], inplace=True)
    for col in columns:
        if col not in df.columns: df[col] = ""
    return df[columns]

def contains_cjk(text) -> bool:
    if text is None: return False
    s = str(text)
    return any(("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf") or ("\uf900" <= ch <= "\ufaff") for ch in s)

def apply_excel_fonts(workbook):
    from openpyxl.styles import Font
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None: cell.font = Font(name="Calibri")
                elif contains_cjk(cell.value): cell.font = Font(name="Microsoft JhengHei")
                else: cell.font = Font(name="Calibri")

def build_signal_excel_bytes(signal_buckets: dict) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for tab_name, rows in signal_buckets.items():
            sheet_name = str(tab_name)[:31]  # Excel 分頁名稱長度限制為 31 字元
            normalize_rows_for_excel(rows).to_excel(writer, sheet_name=sheet_name, index=False)
        apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()

# ===== 輔助工具函式 =====
def make_anchor_id(group_name: str) -> str:
    return f"group-{re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '-', group_name).strip('-')}"

def yahoo_quote_url(symbol: str) -> str:
    return f"https://tw.stock.yahoo.com/quote/{str(symbol).split('.')[0]}"

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def record_missing_stock(missing_stock_details, fetch_errors, symbol, stock_name=None, reason="未知原因", group_name="", source=""):
    symbol = str(symbol).strip()
    try: resolved_name = stock_name or get_stock_name(symbol, st.session_state.get("fubon_sdk"))
    except Exception: resolved_name = stock_name or ""
    reason_text = str(reason).strip() or "未知原因"
    missing_stock_details.append({"代碼": symbol, "股票名稱": str(resolved_name or ""), "分類": str(group_name or ""), "原因": reason_text, "來源": str(source or "")})
    fetch_errors[symbol] = reason_text

def build_top3_html(valid_stock_stats):
    if not valid_stock_stats: return '<span style="color:#666666;">無可用資料</span>'
    top3_sorted = sorted(valid_stock_stats, key=lambda x: x["pct"], reverse=True)[:3]
    parts = []
    for item in top3_sorted:
        pct = float(item["pct"])
        pct_color = "#cf1322" if pct > 0 else "#389e0d" if pct < 0 else "#333333"
        parts.append(f'<span style="color:#000000;">{escape(str(item["code"]))} {escape(str(item["name"]))} </span><span style="color:{pct_color}; font-weight:600;">{pct:+.1f}%</span>')
    return " | ".join(parts)

def compact_name_list(names, max_show=3):
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names: return "無"
    if len(names) <= max_show: return "、".join(names)
    return "、".join(names[:max_show]) + f" 等{len(names)}檔"

# ===== Session State 初始化 =====
if "auto_refresh_enabled" not in st.session_state: st.session_state.auto_refresh_enabled = False
if "refresh_sec" not in st.session_state: st.session_state.refresh_sec = REFRESH_SEC
if "tg_push_enabled" not in st.session_state: st.session_state.tg_push_enabled = False 
if "scheduled_push_enabled" not in st.session_state: st.session_state.scheduled_push_enabled = True 
if "processed_time_slots" not in st.session_state: st.session_state.processed_time_slots = set() 
if "stock_groups" not in st.session_state: st.session_state.stock_groups = load_all_stock_group_from_file() if FORCE_SCAN_ALL_STOCKS_FROM_FILE else load_stock_groups()

if FORCE_SCAN_ALL_STOCKS_FROM_FILE:
    st.session_state.stock_groups = load_all_stock_group_from_file()
if "price_source_mode" not in st.session_state: st.session_state.price_source_mode = "自動"
if "scan_enabled" not in st.session_state: st.session_state.scan_enabled = False
if "scan_requested" not in st.session_state: st.session_state.scan_requested = False
if "fubon_sdk" not in st.session_state: st.session_state.fubon_sdk = None
if "fubon_logged_in" not in st.session_state: st.session_state.fubon_logged_in = False
if "notified_stocks" not in st.session_state: st.session_state.notified_stocks = set()
if "tg_last_update_id" not in st.session_state: st.session_state.tg_last_update_id = None

# ===== UI 元件 =====
def render_auto_refresh_settings():
    with st.sidebar.expander("🔄 自動刷新設定", expanded=True):
        st.toggle("啟用自動刷新", key="auto_refresh_enabled", help="開啟後會依照下方秒數自動重新整理；分組編輯解鎖或編輯中會自動暫停。")
        st.number_input("刷新秒數", min_value=1, max_value=300, step=1, key="refresh_sec", help="自動刷新間隔秒數，預設 3 秒。")

def render_fubon_login():
    st.sidebar.markdown("## 🔑 富邦 API 設定 (Fubon Neo)")
    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 API 已成功連線")
        if st.sidebar.button("登出 / 重新連線", use_container_width=True):
            st.session_state.fubon_sdk = None
            st.session_state.fubon_logged_in = False
            st.rerun()
        return
    try:
        fubon_secrets = st.secrets["fubon"]
        pfx_base64 = fubon_secrets["pfx_base64"]
    except KeyError:
        st.sidebar.error("❌ 找不到 Streamlit Secrets 中的 pfx_base64 憑證資料。")
        return
    st.sidebar.info("請輸入富邦證券登入資訊")
    f_id = st.sidebar.text_input("身分證字號", key="f_id_input")
    f_pw = st.sidebar.text_input("富邦登入密碼", key="f_pw_input", type="password")
    f_cert_pw = st.sidebar.text_input("憑證密碼", key="f_cert_pw_input", type="password")
    if st.sidebar.button("連線行情伺服器", use_container_width=True):
        if not f_id or not f_pw or not f_cert_pw: st.sidebar.warning("請填寫完整的身分證字號與密碼！")
        else:
            try:
                temp_cert_path = "temp_cloud_cert.pfx"
                with open(temp_cert_path, "wb") as f: f.write(base64.b64decode(pfx_base64))
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

def format_color(val):
    if isinstance(val, (int, float)):
        return f"🔴 +{val:.2f}%" if val > 0 else f"🟢 {val:.2f}%" if val < 0 else f"{val:.2f}%"
    return val

def format_volume(val):
    try: return f"{float(val):,.1f}"
    except Exception: return val

def format_volatility(val):
    try: return f"{float(val):.2f}%"
    except Exception: return val

def render_scan_progress_card(placeholder, pct: float, status_text: str = "掃描進度"):
    pct = max(0.0, min(float(pct), 100.0))
    placeholder.markdown(
        f'<div style="width: 120px; min-height: 78px; border: none; border-radius: 0; padding: 8px 10px; text-align: left; background: transparent; box-sizing: border-box;"><div style="font-size: 30px; line-height: 1; font-weight: 800;">{pct:.0f}%</div><div style="font-size: 13px; margin-top: 8px;">{status_text}</div></div>',
        unsafe_allow_html=True,
    )

# ==================== 主畫面開始 ====================
st.markdown('<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)

title_icon_col, title_text_col, scan_progress_col = st.columns([0.45, 7.55, 1])

with title_icon_col:
    if os.path.exists(APP_LOGO): st.image(APP_LOGO, width=58)
    else: st.markdown('<div style="font-size:42px; line-height:1.2;">📊</div>', unsafe_allow_html=True)

with title_text_col:
    st.markdown('<h1 style="margin:0; padding-top:4px; font-size:42px; font-weight:800; line-height:1.2;">台股掃描器 - 告訴我你會買日月光</h1>', unsafe_allow_html=True)

with scan_progress_col: scan_progress_card_placeholder = st.empty()

render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)

gc.collect()

render_fubon_login()

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_price_source = render_price_source_selector(tw_now)
render_auto_refresh_settings()

if FORCE_SCAN_ALL_STOCKS_FROM_FILE:
    all_symbols_count = len(st.session_state.stock_groups.get(ALL_STOCK_GROUP_NAME, []))
    st.sidebar.success(f"✅ 全市場掃描模式：已從 {STOCK_SCAN_FILE} 載入 {all_symbols_count} 檔股票")
    st.sidebar.caption("此模式會忽略 stock_groups.json 與手動分組，直接掃描 txt 內全部股票。")
else:
    st.sidebar.caption(f"股票分組共 {len(st.session_state.stock_groups)} 組，請至「股票列表編輯器」頁面新增/編輯。")

st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}")

rise_threshold = st.number_input("儀表板漲幅達標門檻 (%)", min_value=0.0, max_value=10.0, value=7.0, step=0.5, format="%.2f")

# 訊號登記表：來自 signal_module/ 底下的可編輯訊號檔案 (可到「🛠️ 訊號編輯」分頁調整)
signal_registry = get_signal_registry()

st.markdown("### 🎯 掃描條件")
scan_btn_col1, scan_btn_col2, scan_setting_col, scan_status_col = st.columns([0.9, 0.9, 1.25, 5.95])
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
with scan_setting_col:
    with open_dropdown("⚙️ Setting"):
        st.caption("掃描條件設定（訊號清單可到「🛠️ 訊號編輯」分頁新增/修改/停用）")
        show_only_signal_rows = st.toggle("只顯示訊號股票", value=True, key="setting_show_only_signal_rows")
        for sig_key, cfg in signal_registry.items():
            kind_tag = "🟢買" if cfg.get("kind", "buy") == "buy" else "🔴賣"
            st.checkbox(
                f"{kind_tag} {cfg['label']}",
                value=True,
                key=f"setting_include_signal_{sig_key}",
                help=cfg.get("description", ""),
            )
        min_volume_lots = st.number_input("成交量(張)下限", min_value=0, value=1000, step=100, key="setting_min_volume_lots")
with scan_status_col:
    scan_action_placeholder = st.empty()

if st.session_state.scan_enabled: st.caption("🟢 掃描狀態：執行中")
elif "last_scan_result" in st.session_state:
    st.caption(f"✅ 掃描狀態：已完成，上次完成時間：{st.session_state.last_scan_result.get('scan_completed_at', '-')}｜成交量下限：{st.session_state.last_scan_result.get('min_volume_lots', 1000)} 張")
else: st.caption("⚪ 掃描狀態：已停止，按「開始掃描」才會抓取資料。")

selected_signal_keys = [
    sig_key for sig_key in signal_registry
    if st.session_state.get(f"setting_include_signal_{sig_key}", True)
]
selected_signal_names = [signal_registry[k]["label"] for k in selected_signal_keys]

if not selected_signal_names: st.warning("請至少勾選一種掃描訊號，否則不會列出訊號股票。")

if active_price_source == "WebSocket" and not st.session_state.fubon_logged_in:
    st.warning("⚠️ 目前價格來源為 WebSocket，請先至左側面板連線「富邦 API」，才能開始抓取行情資料。")
    st.stop()
if active_price_source == "Yfinance" and yf is None:
    st.warning("⚠️ 目前價格來源為 Yfinance，請先安裝套件：pip install yfinance")
    st.stop()

should_run_scan = bool(st.session_state.pop("scan_requested", False))
has_last_scan_result = "last_scan_result" in st.session_state

if not should_run_scan and not has_last_scan_result:
    render_scan_progress_card(scan_progress_card_placeholder, 0, "掃描進度")
    st.info("請按「開始掃描」開始抓取股票資料。")
    st.stop()

if should_run_scan:
    can_push_now = False
    current_schedule_key = None
    manual_push_triggered = False

    if st.session_state.tg_push_enabled:
        manual_push_triggered = check_telegram_push_command()
        if manual_push_triggered:
            can_push_now = True
            st.session_state.notified_stocks = set() 
            st.toast("🚀 收到 'push' 指令，強制觸發推播！")
            send_telegram_message("🤖 <b>收到指令，開始為您掃描並強制推播強勢股...</b>")
        elif st.session_state.scheduled_push_enabled:
            TARGET_TIMES = [
                tw_now.replace(hour=9, minute=40, second=0, microsecond=0),
                tw_now.replace(hour=10, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=11, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=12, minute=0, second=0, microsecond=0),
                tw_now.replace(hour=13, minute=0, second=0, microsecond=0)
            ]
            for target_dt in TARGET_TIMES:
                if abs((tw_now - target_dt).total_seconds()) <= 45:
                    current_schedule_key = f"slot_{tw_now.strftime('%Y%m%d')}_{target_dt.strftime('%H%M')}"
                    if current_schedule_key not in st.session_state.processed_time_slots:
                        can_push_now = True
                        break

    group_tables = {}
    group_up_summary = []
    all_signal_rows = []
    signal_buckets = {"優先追蹤": []}
    for _cfg in signal_registry.values():
        signal_buckets.setdefault(_cfg["label"], [])
    missing_stock_details = []
    fetch_errors = {}
    scan_total_count = sum(len(stocks) for stocks in st.session_state.stock_groups.values())

    # 🚀 批次預先抓取
    # 2026 效能改版：歷史資料(今天以前)一律固定從本地 twse_ohlcv.db 批次讀取，
    # 不再依照「今日價格來源」切換去打 Yfinance/富邦 的歷史K線 API，
    # 大幅減少 API 呼叫次數、降低被限流(429)進而誤用到「昨天價格」的風險。
    # 只有選 Yfinance 當「今日價格來源」時，才需要額外批次抓 Yfinance 的今日資料；
    # 其他來源(WebSocket/本地資料庫)的「今日」資料改成逐檔即時抓
    # (在 download_stock_data_by_source 內部處理，非常輕量)。
    scan_today_str = tw_now.strftime("%Y-%m-%d")
    all_unique_symbols = tuple(sorted({s for stocks in st.session_state.stock_groups.values() for s in stocks}))

    history_map = bulk_download_db_history(all_unique_symbols, scan_today_str)
    yf_today_map = bulk_download_yfinance_today(all_unique_symbols, scan_today_str) if (yf is not None and active_price_source == "Yfinance") else {}

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
                progress_pct = (processed_count / scan_total_count) * 100
                render_scan_progress_card(scan_progress_card_placeholder, progress_pct, "掃描進度")
                progress_bar.progress(processed_count / scan_total_count, text=f"掃描進度：{progress_pct:.1f}%（{processed_count}/{scan_total_count}：{symbol}）")
            try:
                raw_df = download_stock_data_by_source(
                    symbol, st.session_state.fubon_sdk, active_price_source, scan_today_str,
                    history_map=history_map, yf_today_map=yf_today_map,
                )
                if raw_df is None or raw_df.empty: raise ValueError("查無歷史資料 / API 回傳空資料")

                df = normalize_ohlc(raw_df)
                if df is None or df.empty: raise ValueError("normalize_ohlc 後無有效 K 線資料")
                if len(df) < 60: raise ValueError(f"歷史資料不足：僅 {len(df)} 筆，少於 60 筆")

                price = get_last_price_by_source(symbol, df, st.session_state.fubon_sdk, active_price_source)
                stock_name = get_stock_name(symbol, st.session_state.fubon_sdk)
                data = compute_indicators(df, price, symbol=symbol, name=stock_name, rise_threshold=rise_threshold)

                signal_types = data["signal_types"]
                signal_kinds = data["signal_kinds"]
                signal_sublabels = data.get("signal_sublabels", {})

                def _display_signal_type(s: str) -> str:
                    """把 sub_label (例如下降趨勢線突破的短期/中短期/中長期) 動態接在訊號名稱後面顯示。"""
                    return f"{s}{signal_sublabels.get(s, '')}"

                signal_score = calc_signal_quality_score(data, signal_types, signal_kinds)
                signal_grade = classify_signal_grade(signal_score)
                passes_volume_filter = float(data.get("volume_lots", 0)) >= float(min_volume_lots)
                is_selected_signal = any(sig in selected_signal_names for sig in signal_types) and passes_volume_filter and signal_score >= SIGNAL_SCORE_MIN

                if (data["pct"] >= 5 or is_selected_signal) and passes_volume_filter:
                    notify_key = f"{symbol}_{tw_now.strftime('%Y-%m-%d')}"
                    if can_push_now and (notify_key not in st.session_state.notified_stocks):
                        signal_type_text = "、".join(_display_signal_type(s) for s in signal_types) if signal_types else "-"
                        msg = (f"🔔 <b>全市場掃描訊號：{stock_name} (<a href='https://tw.stock.yahoo.com/quote/{symbol.split('.')[0]}'>{symbol}</a>)</b>\n\n"
                               f"📈 價格：{data['price']}\n🔥 漲幅：{data['pct']}%\n📦 成交量：{data['volume_lots']:,.1f} 張\n"
                               f"⭐ 訊號分數：{signal_score} / {signal_grade}\n🌊 波動率：{data['volatility_pct']}%\n"
                               f"📊 訊號類型：{signal_type_text}\n🔌 來源：{active_price_source}")
                        send_telegram_message(msg)
                        st.session_state.notified_stocks.add(notify_key)

                if data["pct"] >= rise_threshold: hit_count += 1; hit_names.append(stock_name)
                if data["pct"] > 0: up_count += 1
                elif data["pct"] < 0: down_count += 1
                else: flat_count += 1

                valid_stock_stats.append({"symbol": symbol, "code": symbol_to_code(symbol), "name": stock_name, "pct": float(data["pct"])})

                signal_direction_labels = []
                if any(signal_kinds.get(s) == "buy" for s in signal_types): signal_direction_labels.append("買")
                if any(signal_kinds.get(s) == "sell" for s in signal_types): signal_direction_labels.append("賣")
                signal_direction_text = "/".join(signal_direction_labels) if signal_direction_labels else "-"
                signal_detail_text = "；".join(f"{k}：{v}" for k, v in data["signal_details"].items())

                row = {
                    "代碼": symbol, "代碼網址": yahoo_quote_url(symbol), "股票名稱": stock_name,
                    "價格": f"{data['price']:.2f}", "漲跌%": data["pct"], "成交量(張)": data["volume_lots"],
                    "波動率%": data["volatility_pct"], "RS加權報酬%": data["rs_raw"], "訊號分數": signal_score,
                    "追蹤等級": signal_grade, "MA位置": data["ma_range"], "MA排列": data["ma_trend"],
                    "訊號方向": signal_direction_text,
                    "訊號類型": "、".join(_display_signal_type(s) for s in signal_types) if signal_types else "-",
                    "訊號說明": signal_detail_text if signal_detail_text else "-",
                    "來源": active_price_source,
                }
                if (not show_only_signal_rows or is_selected_signal) and passes_volume_filter: rows.append(row)

                # 「漲幅達標」分頁：獨立於 SIGNAL_SCORE_MIN 訊號分數門檻之外，
                # 只要「漲幅達標」訊號有觸發（漲跌% >= 側邊欄設定的門檻）且成交量符合下限，就列入。
                # 「訊號分數」欄位仍會照算、照顯示在表格中，只是不再拿來當作是否列入此分頁的篩選條件。
                if "漲幅達標" in signal_types and "漲幅達標" in selected_signal_names and passes_volume_filter:
                    signal_buckets["漲幅達標"].append(row.copy())

                if is_selected_signal:
                    all_signal_rows.append(row.copy())
                    if signal_score >= PRIORITY_SCORE_MIN: signal_buckets["優先追蹤"].append(row.copy())
                    append_signal_tracking(row, scan_today_str)
                    for sig in signal_types:
                        if sig == "漲幅達標":
                            continue  # 已在上面獨立處理，這裡跳過避免重複加入同一筆資料
                        if sig in signal_buckets and sig in selected_signal_names: signal_buckets[sig].append(row.copy())

            except Exception as e:
                error_count += 1
                error_stock_name = get_stock_name(symbol, st.session_state.fubon_sdk)
                record_missing_stock(missing_stock_details, fetch_errors, symbol, error_stock_name, f"{type(e).__name__}: {e}", group_name, active_price_source)
                if not show_only_signal_rows:
                    rows.append({"代碼": symbol, "代碼網址": "", "股票名稱": error_stock_name, "價格": "錯誤", "漲跌%": "-", "成交量(張)": "-", "波動率%": "-", "RS加權報酬%": "-", "訊號分數": "-", "追蹤等級": "-", "MA位置": "-", "MA排列": "-", "訊號方向": "-", "訊號類型": "錯誤", "訊號說明": str(e), "來源": active_price_source})

        group_tables[group_name] = {"count": len(stocks), "table": pd.DataFrame(rows)}
        group_up_summary.append({
            "分類": group_name, "達標數": hit_count, "達標股票名稱": compact_name_list(hit_names, 4),
            "前三名HTML": build_top3_html(valid_stock_stats), "上漲數": up_count, "下跌數": down_count,
            "平盤數": flat_count, "錯誤數": error_count, "總數": len(stocks)
        })

    render_scan_progress_card(scan_progress_card_placeholder, 100, "掃描進度")
    progress_bar.empty()
    if can_push_now and st.session_state.scheduled_push_enabled and current_schedule_key and not manual_push_triggered:
        st.session_state.processed_time_slots.add(current_schedule_key)

    signal_buckets["優先追蹤"] = build_priority_rows(all_signal_rows, PRIORITY_SCORE_MIN)

    st.session_state.last_scan_result = {
        "group_tables": group_tables, "group_up_summary": group_up_summary, "all_signal_rows": all_signal_rows,
        "signal_buckets": signal_buckets, "missing_stock_details": missing_stock_details,
        "fetch_errors": fetch_errors, "excel_filename": f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx",
        "scan_completed_at": tw_now.strftime('%Y-%m-%d %H:%M:%S'), "progress_pct": 100, "min_volume_lots": min_volume_lots,
    }
    if AUTO_UPLOAD_GITHUB:
        upload_file_to_github(build_signal_excel_bytes(signal_buckets), f"{GITHUB_DATABASE_DIR}/{st.session_state.last_scan_result['excel_filename']}", f"Auto upload TW stock scan result {tw_now.strftime('%Y-%m-%d %H:%M:%S')}")
        if os.path.exists(TRACKING_FILE): upload_tracking_file_to_github(tw_now.strftime('%Y-%m-%d %H:%M:%S'))

    st.session_state.scan_enabled = False
else:
    last_scan_result = st.session_state.last_scan_result
    group_tables = last_scan_result.get("group_tables", {})
    group_up_summary = last_scan_result.get("group_up_summary", [])
    all_signal_rows = last_scan_result.get("all_signal_rows", [])
    default_signal_buckets = {"優先追蹤": []}
    for _cfg in signal_registry.values():
        default_signal_buckets.setdefault(_cfg["label"], [])
    signal_buckets = last_scan_result.get("signal_buckets", default_signal_buckets)
    missing_stock_details = last_scan_result.get("missing_stock_details", [])
    fetch_errors = last_scan_result.get("fetch_errors", {})
    render_scan_progress_card(scan_progress_card_placeholder, last_scan_result.get("progress_pct", 100), "掃描進度")

excel_bytes = build_signal_excel_bytes(signal_buckets)
excel_filename = st.session_state.get("last_scan_result", {}).get("excel_filename", f"TWstock_signal_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx")

with scan_action_placeholder.container():
    download_col, info_col = st.columns([1.15, 6.85])
    with download_col:
        with open_dropdown("📁 Download"):
            st.caption("下載 / 推播 / GitHub 上傳")
            st.download_button("下載 Excel", data=excel_bytes, file_name=excel_filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            if st.button("推送到 Telegram", use_container_width=True):
                if send_telegram_document(excel_bytes, excel_filename, caption=f"TWstock 訊號掃描結果｜成交量下限 {st.session_state.get('last_scan_result', {}).get('min_volume_lots', min_volume_lots)} 張｜{tw_now.strftime('%Y-%m-%d %H:%M:%S')}"):
                    st.success("已將 Excel 推送到 Telegram。")
            if st.button("上傳 Excel 到 GitHub", use_container_width=True):
                upload_file_to_github(excel_bytes, f"{GITHUB_DATABASE_DIR}/{excel_filename}", f"Upload TW stock scan result {tw_now.strftime('%Y-%m-%d %H:%M:%S')}")
            if st.button("上傳追蹤 CSV", use_container_width=True):
                upload_tracking_file_to_github(tw_now.strftime('%Y-%m-%d %H:%M:%S'))
            st.caption(f"今日追蹤CSV檔名：{tracking_github_filename(tw_now)}")
    with info_col:
        st.caption(f"Excel：{excel_filename} ｜ 追蹤CSV GitHub 目標：{tracking_github_path(tw_now)}")

st.markdown("### 🔎 訊號掃描結果")

total_scanned = sum(item.get("總數", 0) for item in group_up_summary)
missing_df = pd.DataFrame(missing_stock_details).drop_duplicates(subset=["代碼"], keep="first").reset_index(drop=True) if missing_stock_details else pd.DataFrame(columns=["代碼", "股票名稱", "分類", "原因", "來源"])
total_errors = len(missing_df) if not missing_df.empty else sum(item.get("錯誤數", 0) for item in group_up_summary)
total_success = max(total_scanned - total_errors, 0)
unique_signal_count = len(pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"])) if all_signal_rows else 0

stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
with stat_col1: st.metric("掃描資料總數", total_scanned)
with stat_col2: st.metric("得到資料數", total_success)
with stat_col3: st.metric("缺少資料數", total_errors)
with stat_col4: st.metric("符合勾選條件數", unique_signal_count)

if not missing_df.empty:
    st.warning(f"⚠️ 本次掃描共有 {len(missing_df)} 檔股票缺少資料或處理失敗。")
    with st.expander(f"⚠️ 缺少資料股票列表（{len(missing_df)} 檔）", expanded=True):
        st.dataframe(missing_df, use_container_width=True, hide_index=True)
        dl_col1, dl_col2 = st.columns(2)
        with dl_col1: st.download_button("📥 下載缺資料股票代碼 TXT", data="\n".join(missing_df["代碼"].astype(str).tolist()), file_name=f"missing_stock_codes_{tw_now.strftime('%Y%m%d_%H%M%S')}.txt", mime="text/plain", use_container_width=True)
        with dl_col2: st.download_button("📥 下載缺資料明細 CSV", data=missing_df.to_csv(index=False).encode("utf-8-sig"), file_name=f"missing_stock_details_{tw_now.strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv", use_container_width=True)

if fetch_errors:
    with st.expander(f"🔧 抓取/處理失敗除錯資訊（{len(fetch_errors)} 檔）", expanded=False):
        st.json(dict(list(fetch_errors.items())[:100]))

display_columns = ["代碼", "股票名稱", "價格", "漲跌%", "成交量(張)", "波動率%", "RS加權報酬%", "訊號分數", "追蹤等級", "MA位置", "MA排列", "訊號方向", "訊號類型", "來源"]

if all_signal_rows:
    signal_display_df = pd.DataFrame(all_signal_rows).drop_duplicates(subset=["代碼"]).copy()
    for col, func in [("漲跌%", format_color), ("成交量(張)", format_volume)]: signal_display_df[col] = signal_display_df[col].apply(func)
    if "波動率%" in signal_display_df.columns: signal_display_df["波動率%"] = signal_display_df["波動率%"].apply(format_volatility)
    signal_display_df["代碼"] = signal_display_df["代碼網址"]
    for col in display_columns:
        if col not in signal_display_df.columns: signal_display_df[col] = "-"
            
    st.dataframe(signal_display_df[display_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱"),
        "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
    })

    st.markdown("### 📑 依訊號分頁查看")
    signal_tab_specs = [("優先追蹤", "優先追蹤")] + [(cfg["label"], cfg["label"]) for cfg in signal_registry.values()]
    signal_tabs = st.tabs([f"{name}（{len(pd.DataFrame(signal_buckets.get(key, [])).drop_duplicates(subset=['代碼'])) if signal_buckets.get(key) else 0}）" for name, key in signal_tab_specs])
    
    for tab, (display_name, bucket_key) in zip(signal_tabs, signal_tab_specs):
        with tab:
            bucket_rows = signal_buckets.get(bucket_key, [])
            st.markdown(f"#### {display_name}（{len(pd.DataFrame(bucket_rows).drop_duplicates(subset=['代碼'])) if bucket_rows else 0} 檔）")
            if bucket_rows:
                bucket_display_df = pd.DataFrame(bucket_rows).drop_duplicates(subset=["代碼"]).copy()
                for col, func in [("漲跌%", format_color), ("成交量(張)", format_volume)]: bucket_display_df[col] = bucket_display_df[col].apply(func)
                if "波動率%" in bucket_display_df.columns: bucket_display_df["波動率%"] = bucket_display_df["波動率%"].apply(format_volatility)
                bucket_display_df["代碼"] = bucket_display_df["代碼網址"]
                for col in display_columns:
                    if col not in bucket_display_df.columns: bucket_display_df[col] = "-"
                        
                st.dataframe(bucket_display_df[display_columns], use_container_width=True, column_config={
                    "代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
                    "股票名稱": st.column_config.TextColumn("股票名稱"),
                    "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
                })
            else:
                st.caption(f"目前沒有符合「{display_name}」的股票。")
else:
    st.info("目前沒有掃描到符合勾選條件的股票。")

st.divider()

for group_name, info in group_tables.items():
    st.markdown(f'<div id="{make_anchor_id(group_name)}" style="scroll-margin-top: 80px;"></div>', unsafe_allow_html=True)
    header_col1, header_col2 = st.columns([8, 2])
    with header_col1: st.subheader(f"【{group_name}】({info['count']}檔)")
    with header_col2: st.markdown("""<div style="text-align:right; padding-top:0.4rem;"><a href="#dashboard-top" class="back-to-dashboard-btn">⬆ 回到儀表板</a></div>""", unsafe_allow_html=True)
    
    table_df = info["table"].copy()
    if not table_df.empty and "代碼網址" in table_df.columns: table_df["代碼"] = table_df["代碼網址"]
    for col in display_columns:
        if col not in table_df.columns: table_df[col] = "-"
            
    st.dataframe(table_df[display_columns], use_container_width=True, column_config={
        "代碼": st.column_config.LinkColumn("代碼", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
        "股票名稱": st.column_config.TextColumn("股票名稱"),
        "訊號分數": st.column_config.NumberColumn("訊號分數", format="%.1f"),
    })
    st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)

if st.session_state.auto_refresh_enabled:
    time.sleep(max(1, int(st.session_state.get("refresh_sec", REFRESH_SEC))))
    st.rerun()
