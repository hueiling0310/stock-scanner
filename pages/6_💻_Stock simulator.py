"""
台股訊號模擬器 (Signal Simulator) - 含進階模擬回測功能 (可開關)
支援多重買入訊號、獨立張數設定、買入冷卻期、獨立單筆停損停利、多重複選出場訊號、完整交易明細、未實現損益。
新增：側邊欄自動抓取今日數值更新本地資料檔 (支援 yfinance 單股更新 與 TWSE/TPEX 官方 API 全市場更新)。

執行方式:
    streamlit run app.py
"""
import os
import io
import re
import base64
import tempfile
import sqlite3
import time
import random
import requests
import urllib3
import urllib.parse
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

import db_utils
import indicators
import module_loader
import benchmark_utils
from scoring import calc_signal_quality_score, classify_signal_grade

# 忽略憑證警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

st.set_page_config(page_title="台股訊號模擬器", layout="wide")

# ====== 定義賣出訊號與自訂樣式 ======
# 「上升趨勢線跌破」(signal_module/trendline_breakout_sell.py) 歸類為賣出型訊號，
# 加進 SELL_LABELS 後，下面的 _sell_tag_css 會自動把多選框(圖表標記訊號勾選/賣出條件1)
# 裡這個標籤的底色變成綠色，跟其他賣出訊號一致，不用再另外寫一次 CSS 規則。
SELL_LABELS = ["反向島狀", "廣義下降三法", "跌停", "移動停利", "上升趨勢線跌破"]

# 針對多選框中的賣出訊號標籤變更為綠色。
# 透過瀏覽器 DevTools 實際檢查過完整 DOM 結構後發現一個關鍵細節：
#   - 目前有焦點(tabindex="0")的第一個標籤，外層 <span> 上「沒有」aria-label
#   - 其餘標籤(tabindex="-1")的外層 <span> 才有 aria-label="標籤文字"
#   - 但「所有」標籤內層文字 <span> 都一定有 title="標籤文字" (最穩定可靠)
# 過去誤用了不存在的 `[data-baseweb="tag"]` 屬性，導致完全選不到任何元素。
# 這裡改用 aria-label 直接比對 + `:has()` 從內層 title 往外抓父層容器 做雙重保險，
# 涵蓋「有無 aria-label」兩種情況，確保每一個標籤(包含目前有焦點的那個)都能正確上色。
# 另外，「賣出條件1」「圖表標記訊號勾選」下拉選單會依買/賣分類排序，中間以一條分隔線區分
# (見下方 JS 區塊)，選單顯示文字本身維持純標籤文字，不加前綴。
_sell_label_variants = list(SELL_LABELS)

_sell_tag_css = "\n".join(
    f'span[aria-label="{lbl}"] {{ background-color: #27ae60 !important; border-color: #27ae60 !important; color: white !important; }}\n'
    f'span:has(> span[title="{lbl}"]) {{ background-color: #27ae60 !important; border-color: #27ae60 !important; color: white !important; }}\n'
    f'span[title="{lbl}"] {{ color: white !important; }}'
    for lbl in _sell_label_variants
)

st.markdown(
    f"""
    <style>
    [data-testid="stMetricValue"] {{ font-size: 1.2rem !important; }}
    [data-testid="stMetricLabel"] {{ font-size: 0.9rem !important; }}
    {_sell_tag_css}
    </style>
    """,
    unsafe_allow_html=True
)

# 這個檔案放在 repo 的 pages/ 資料夾內執行，__file__ 指向 pages/6_Stock simulator.py，
# 所以要往上找「兩層」(pages/ -> repo 根目錄) 才能對到實際放在根目錄的 twse_ohlcv.db /
# Trading Journal.xlsx，跟 1_🛠️_signal editor.py 抓 signal_module 資料夾用的是同一種寫法。
_REPO_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_REPO_ROOT_DIR, "twse_ohlcv.db")
DEFAULT_JOURNAL_PATH = os.path.join(_REPO_ROOT_DIR, "Trading Journal.xlsx")
# 週K/月K 訊號開關 + MA週期控制表 (2026-09-04 新增)：跟 db/journal 同一層目錄，
# 使用者直接用 Excel 編輯「哪些訊號要開放給日K/週K/月K使用」與「MA10/20/60 在
# 週K/月K下實際要換算成第幾根K棒的均線」，App 啟動時讀取一次、快取起來，
# 之後改表想套用新設定，側邊欄按「🔄 重新載入訊號開關表」即可，不用重啟程式。
DEFAULT_SIGNAL_CONTROL_TABLE_PATH = os.path.join(_REPO_ROOT_DIR, "signal_module_controltable.xlsx")
JOURNAL_LOG_SHEET = "log"
JOURNAL_COLUMNS = ["交易日期", "股票代碼", "股票名稱", "進出場價格", "買賣方向", "進出手法", "買賣張數", "Note"]
JOURNAL_ACTIONS = ["買入", "賣出"]
JOURNAL_FONT_NAME = "微軟正黑體"
JOURNAL_FONT_SIZE = 11
JOURNAL_GITHUB_PATH = "Trading Journal.xlsx"  # 對應 GitHub repo 根目錄下的檔名


# --------------------------------------------------------------------------
# GitHub 同步工具 (交易紀錄 Trading Journal.xlsx)
# --------------------------------------------------------------------------
# 2026-08-16 新增：交易紀錄編輯器原本只讀寫這個部署容器本機磁碟的 Trading Journal.xlsx
# (DEFAULT_JOURNAL_PATH)，跟 GitHub repo 根目錄裡的同名檔案完全沒有同步——使用者在
# 編輯器按「儲存並關閉」只會寫到本機，而本機磁碟不是永久保存的：Streamlit Cloud
# 重新部署/重啟這個 app 時會用 GitHub 上的版本重新複製一份蓋過去，等於編輯器存的
# 內容隨時可能被悄悄蓋掉、遺失，使用者也完全看不到任何提示。
# 這裡比照 0_📊_台股掃描器.py 的 upload_file_to_github()/github_repo_config() 寫法
# (同一組 GITHUB_TOKEN/OWNER/REPO/BRANCH secrets)，讓交易紀錄編輯器：
#   1. 開啟時 (只在讀取的是預設路徑、也就是對應 repo 檔案時) 先嘗試從 GitHub 抓最新
#      版本蓋過本機那份，確保一定是從最新版本開始編輯，不會編輯到舊資料又存回去、
#      覆蓋掉别人(或自己在別台裝置上)較新的紀錄。抓不到就靜默退回本機現有版本，
#      使用者仍可正常編輯，不會比原本行為更差。
#   2. 儲存時本機寫入成功後，自動再推一版回 GitHub repo，這樣 GitHub 上的檔案才是
#      真正的資料來源；若 GITHUB_TOKEN 未設定或推送失敗，會另外顯示明確的警告，
#      而不是靜默失敗讓使用者誤以為已經同步。
#   3. 若使用者是從側邊欄「讀取 交易紀錄」自行上傳了檔案在編輯 (journal_path 不是
#      DEFAULT_JOURNAL_PATH)，代表這份本來就不是 repo 檔案，不會嘗試抓取/推送 GitHub，
#      避免誤用上傳的內容覆蓋掉 repo 裡真正的 Trading Journal.xlsx。
def journal_github_config():
    return {
        "token": st.secrets.get("GITHUB_TOKEN", ""),
        "owner": st.secrets.get("GITHUB_OWNER", "henglunlin"),
        "repo": st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN"),
        "branch": st.secrets.get("GITHUB_BRANCH", "main"),
    }


def fetch_journal_meta_from_github():
    """從 GitHub repo 根目錄抓最新的 Trading Journal.xlsx，回傳 {"bytes":..., "sha":...}，
    抓不到回傳 None。sha 是這個版本在 GitHub 上的版本號，用於「儲存時偵測衝突」——見
    upload_journal_to_github() 的 expected_sha 參數與 journal_editor_dialog() 的儲存流程。

    2026-08-24 修正：原本用 raw.githubusercontent.com 抓檔案，這個網址背後是 GitHub 的
    Fastly CDN，同一個 URL 預設會被快取數分鐘——跟這支 app 有沒有用 st.cache 完全無關，
    純粹是 GitHub 那一層快取，所以剛推上去的新版檔案，用同一個網址抓下來還是可能拿到
    快取的舊版本，造成「要等一段時間才讀到新的」。改用 GitHub Contents API
    (api.github.com，跟 upload_journal_to_github() 拿 sha 用的是同一組 API) 不會被同一層
    CDN 快取。若有設定 GITHUB_TOKEN 也一併帶上 Authorization，除了支援私有 repo，也能
    避免撞到未登入 API 呼叫每小時 60 次的限制。

    2026-08-28 修正：原本用 Accept: application/vnd.github.raw+json 直接拿 bytes，但這個
    媒體類型的回應不會附帶 sha，沒辦法做衝突偵測；改回標準 JSON 回應 (含 content 的
    base64 字串 + sha)，自己解 base64，換取能拿到 sha。
    """
    cfg = journal_github_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not owner or not repo:
        return None
    encoded_path = urllib.parse.quote(JOURNAL_GITHUB_PATH, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Cache-Control": "no-cache",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            content = base64.b64decode(data.get("content", ""))
            return {"bytes": content, "sha": data.get("sha")}
    except Exception:
        pass
    return None


def fetch_journal_bytes_from_github():
    """向下相容用途：只需要檔案內容 bytes、不需要 sha 時呼叫。"""
    meta = fetch_journal_meta_from_github()
    return meta["bytes"] if meta else None


def upload_journal_to_github(file_bytes: bytes, commit_message: str, expected_sha: str = None, force: bool = False) -> dict:
    """把交易紀錄 Excel 內容推回 GitHub repo 根目錄的 Trading Journal.xlsx (需要 GITHUB_TOKEN)。

    回傳 {"success": bool, "sha": 這次推送後(或目前偵測到)的最新 sha, "conflict": bool}。

    2026-08-28 新增併發保護：expected_sha 是使用者「上次讀取/清除快取」當下記住的版本號。
    推送前重新抓一次 GitHub 目前的 sha，如果跟 expected_sha 不一樣，代表這份檔案在使用者
    編輯期間已經被別人（或自己在別的裝置上）更新過，直接覆蓋會弄丟那個版本的內容——這裡
    改成回傳 conflict=True 並中止推送，不再無聲覆蓋；force=True 時代表使用者已看過警告、
    明確選擇要覆蓋，略過這個檢查（沿用原本「後存的人贏」的行為）。
    """
    cfg = journal_github_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo:
        return {"success": False, "sha": None, "conflict": False}

    encoded_path = urllib.parse.quote(JOURNAL_GITHUB_PATH, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        get_res = requests.get(url, headers=headers, params={"ref": branch}, timeout=15)
        current_sha = get_res.json().get("sha") if get_res.status_code == 200 else None

        if not force and expected_sha and current_sha and expected_sha != current_sha:
            return {"success": False, "sha": current_sha, "conflict": True}

        payload = {
            "message": commit_message,
            "content": base64.b64encode(file_bytes).decode("utf-8"),
            "branch": branch,
        }
        if current_sha:
            payload["sha"] = current_sha

        put_res = requests.put(url, headers=headers, json=payload, timeout=30)
        if put_res.status_code in (200, 201):
            new_sha = put_res.json().get("content", {}).get("sha")
            return {"success": True, "sha": new_sha, "conflict": False}
        return {"success": False, "sha": current_sha, "conflict": False}
    except Exception:
        return {"success": False, "sha": None, "conflict": False}


# --------------------------------------------------------------------------
# GitHub Actions 觸發工具 (側邊欄「☁️ 觸發雲端更新」按鈕用，2026-09-01 新增)
# --------------------------------------------------------------------------
# 側邊欄本來就有一個「執行更新」按鈕，但那是在「這個 Streamlit app 執行的容器內」直接
# 呼叫 yfinance/TWSE/TPEX API 更新本機的 twse_ohlcv.db——這份 db 只存在容器裡，app
# 重新部署/重啟就會被 GitHub repo 裡的版本蓋掉，等於本機更新的內容留不住。
# repo 裡另外有一個「Auto Update TWSE OHLCV DB」GitHub Actions workflow，會真的把更新
# 結果 commit 回 repo 才是永久保存的方式；這裡新增的按鈕是透過 GitHub REST API 觸發
# 「這個 workflow」執行 (workflow_dispatch)，並輪詢執行進度顯示進度條，
# 讓使用者不用跳去 GitHub 網頁手動按「Run workflow」。
# 沿用跟交易紀錄 GitHub 同步同一組 secrets (GITHUB_TOKEN/OWNER/REPO/BRANCH)，
# 但 GITHUB_TOKEN 必須額外具備 Actions 的讀寫權限 (classic token 需要 "workflow" scope；
# fine-grained token 需要 "Actions: Read and write")，否則觸發/查詢會失敗。
GITHUB_ACTIONS_TWSE_WORKFLOW_NAME = "Auto Update TWSE OHLCV DB"


def _github_actions_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def find_github_workflow_id(workflow_name: str):
    """依照 GitHub Actions 頁面上顯示的 workflow 名稱 (yaml 裡的 name: 欄位) 查出對應的
    workflow id——觸發 (workflow_dispatch) 與查詢執行紀錄的 API 都要用 id，不能直接用
    畫面上看到的名稱。查不到 (名稱不符、token 沒權限、網路問題等) 回傳 None。"""
    cfg = journal_github_config()
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    if not token or not owner or not repo:
        return None
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows"
    try:
        resp = requests.get(url, headers=_github_actions_headers(token), timeout=15)
        if resp.status_code == 200:
            for wf in resp.json().get("workflows", []):
                if wf.get("name") == workflow_name:
                    return wf.get("id")
    except Exception:
        pass
    return None


def trigger_github_workflow(workflow_id, ref: str) -> bool:
    """觸發指定 workflow 的 workflow_dispatch 事件 (相當於網頁上按「Run workflow」)。"""
    cfg = journal_github_config()
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    if not token or not owner or not repo or not workflow_id:
        return False
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches"
    try:
        resp = requests.post(url, headers=_github_actions_headers(token), json={"ref": ref}, timeout=15)
        return resp.status_code == 204
    except Exception:
        return False


def get_latest_workflow_run(workflow_id, branch: str):
    """取得指定 workflow 在該分支上最新一筆執行紀錄 (run)，用來判斷「剛剛觸發的是哪一筆」
    (比對觸發前後最新 run 的 id 是否不同) 與後續輪詢狀態。查不到回傳 None。"""
    cfg = journal_github_config()
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    if not token or not owner or not repo or not workflow_id:
        return None
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs"
    try:
        resp = requests.get(
            url, headers=_github_actions_headers(token),
            params={"branch": branch, "per_page": 1}, timeout=15,
        )
        if resp.status_code == 200:
            runs = resp.json().get("workflow_runs", [])
            return runs[0] if runs else None
    except Exception:
        pass
    return None


def get_workflow_run_by_id(run_id):
    """取得單一 run 目前的完整狀態 (status: queued/in_progress/completed，
    conclusion: success/failure/... 只有 completed 時才有值)。"""
    cfg = journal_github_config()
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
    try:
        resp = requests.get(url, headers=_github_actions_headers(token), timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_workflow_run_progress_fraction(run_id):
    """GitHub Actions API 本身沒有直接提供「執行到幾%」的數字，只有 queued/in_progress/
    completed 這種粗略狀態。這裡改抓這個 run 底下所有 job 的 steps，用「已完成步驟數 /
    總步驟數」換算出一個 0~1 的粗略進度，比單純顯示狀態文字更有「進度條」的感覺。
    步驟資訊還沒出現 (run 剛建立、步驟數為 0) 時回傳 None，呼叫端可以退回用固定速度估計。"""
    cfg = journal_github_config()
    token, owner, repo = cfg["token"], cfg["owner"], cfg["repo"]
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    try:
        resp = requests.get(url, headers=_github_actions_headers(token), timeout=15)
        if resp.status_code == 200:
            jobs = resp.json().get("jobs", [])
            total_steps, done_steps = 0, 0
            for job in jobs:
                steps = job.get("steps") or []
                total_steps += len(steps)
                done_steps += sum(1 for s in steps if s.get("status") == "completed")
            if total_steps > 0:
                return done_steps / total_steps
    except Exception:
        pass
    return None


def _stable_upload_path(uploaded_file, cache_key: str, tmp_prefix: str, tmp_filename: str) -> str:
    """
    把上傳的檔案寫到暫存路徑，並用 Streamlit UploadedFile 的 file_id 快取住這個路徑。

    修正 (2026-08-13)：原本每次 Streamlit rerun（例如打開/儲存交易紀錄編輯器、切換
    股票、按任何按鈕都會觸發整份程式重跑一次）只要側邊欄的上傳欄位還留著同一個檔案，
    就會重新呼叫一次 tempfile.mkdtemp() 產生「全新」的暫存路徑，把使用者剛存好的編輯
    內容整個蓋回「上傳當下的原始版本」——也就是「明明存檔成功、但下一次互動整批消失」，
    看起來就像完全無法寫入。改用 file_id 判斷是否為同一個上傳檔案，是的話直接沿用同一
    個暫存路徑，不再重新複製覆蓋。
    """
    cache = st.session_state.setdefault("_stable_upload_cache", {})
    cached = cache.get(cache_key)
    if cached and cached.get("file_id") == uploaded_file.file_id and os.path.exists(cached.get("path", "")):
        return cached["path"]

    tmp_dir = tempfile.mkdtemp(prefix=tmp_prefix)
    path = os.path.join(tmp_dir, tmp_filename)
    with open(path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    cache[cache_key] = {"file_id": uploaded_file.file_id, "path": path}
    return path


def classify_journal_action(method_text: str) -> str:
    """依「進出手法」文字判斷買入/賣出的舊規則：
    優先比對是否為系統既有的「賣出型」訊號 (SELL_LABELS，如 移動停利、跌停 等文字本身不含「賣」字)，
    若不在清單內的自訂文字，才退回用「文字中是否含有『賣』字」判斷 (例如自訂輸入「反向3K反轉賣出」)。

    2026-08-28：交易紀錄新增了獨立的「買賣方向」欄位後，這個函式不再是判斷方向的主要依據，
    改當成「買賣方向」欄位缺漏時 (例如這個欄位加入之前的舊紀錄) 的自動回填規則，
    見 load_journal_log() 與 match_journal_trades_fifo()。"""
    text = str(method_text)
    if text in SELL_LABELS:
        return "賣出"
    return "賣出" if "賣" in text else "買入"


def resolve_journal_action(row) -> str:
    """取得一筆交易紀錄的買賣方向：優先使用使用者在「買賣方向」欄位明確選的值，
    只有這個欄位缺漏或不是合法值 (買入/賣出) 時，才退回用 classify_journal_action() 對
    「進出手法」文字做猜測——主要是為了相容「買賣方向」欄位加入之前建立的舊紀錄。"""
    action = row.get("買賣方向") if hasattr(row, "get") else None
    if action in JOURNAL_ACTIONS:
        return action
    return classify_journal_action(row.get("進出手法") if hasattr(row, "get") else "")


def load_journal_log(path: str) -> pd.DataFrame:
    """讀取交易紀錄 Excel 的 log 分頁，若檔案不存在或分頁不存在則回傳空表"""
    if not path or not os.path.exists(path):
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    try:
        df = pd.read_excel(path, sheet_name=JOURNAL_LOG_SHEET, engine="openpyxl")
    except Exception:
        return pd.DataFrame(columns=JOURNAL_COLUMNS)
    for col in JOURNAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[JOURNAL_COLUMNS].copy()
    df["交易日期"] = pd.to_datetime(df["交易日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["買賣張數"] = pd.to_numeric(df["買賣張數"], errors="coerce")
    # 「買賣方向」是 2026-08-28 新增的獨立方向欄位，取代原本純靠「進出手法」文字猜測買/賣的
    # 做法。既有 (這個欄位加入之前) 的舊紀錄、或這欄被清空的紀錄，用 classify_journal_action()
    # 自動回填一個預設值，使用者仍可在交易紀錄編輯器裡自行修正回填錯誤的筆數。
    invalid_action = ~df["買賣方向"].isin(JOURNAL_ACTIONS)
    if invalid_action.any():
        df.loc[invalid_action, "買賣方向"] = df.loc[invalid_action, "進出手法"].apply(classify_journal_action)
    df = df.dropna(subset=["交易日期", "股票代碼"])
    return df.reset_index(drop=True)


def match_journal_trades_fifo(journal_for_stock: pd.DataFrame, latest_price):
    """對單一股票的交易紀錄做「張數感知的 FIFO 配對」，回傳 (配對後的交易 list, 異常警告 list)。

    每一筆買入視為一個持倉批次 (帶「剩餘張數」)，依日期排序後加入佇列尾端；遇到賣出時，
    用賣出張數依先進先出、逐批扣減佇列最前面買入批次的剩餘張數，每扣一批就產生一筆
    「已平倉」紀錄 (買入張數=賣出張數=實際成交的那部分)，該批買入剩餘張數歸零才會從佇列
    移除，否則留在佇列繼續等下一筆賣出配對；一筆賣出可能跨多筆買入批次。迴圈跑完後，
    佇列裡還有剩餘張數的批次一律視為「未平倉」，用 latest_price 計算未實現報酬率
    (latest_price 為 None 時只標未平倉、不算報酬率)。「賣出張數超過目前持有張數」時
    (理論上不該發生，除非紀錄本身缺漏買入或張數填錯) 不會硬湊配對產生假資料，
    改成回傳一則警告文字讓呼叫端顯示提醒。"""
    if journal_for_stock.empty:
        return [], []

    journal_sorted = journal_for_stock.copy()
    journal_sorted["交易日期_dt"] = pd.to_datetime(journal_sorted["交易日期"], errors="coerce")
    journal_sorted = journal_sorted.sort_values("交易日期_dt")

    def _shares(row):
        v = row.get("買賣張數")
        try:
            v = float(v)
            if v > 0:
                return v
        except Exception:
            pass
        return 1.0  # 未填寫張數時，比對預設以 1 張計算

    buy_queue = []  # 每筆: {"buy_date", "buy_method", "buy_price", "remaining"}
    closed_trades = []
    warnings = []

    for _, jr in journal_sorted.iterrows():
        action = resolve_journal_action(jr)
        if action == "買入":
            buy_queue.append({
                "buy_date": jr["交易日期"], "buy_method": jr["進出手法"],
                "buy_price": float(jr["進出場價格"]), "remaining": _shares(jr),
            })
        elif action == "賣出":
            sell_remaining = _shares(jr)
            sell_price = float(jr["進出場價格"])
            if not buy_queue:
                warnings.append(f"{jr['交易日期']} 賣出 {sell_remaining:g} 張，但目前沒有可配對的買入紀錄（可能漏登買入，或這筆賣出本身填錯）。")
                continue
            while sell_remaining > 1e-9 and buy_queue:
                batch = buy_queue[0]
                matched = min(sell_remaining, batch["remaining"])
                pnl_pct = (sell_price - batch["buy_price"]) / batch["buy_price"] * 100 if batch["buy_price"] else 0
                closed_trades.append({
                    "買入日期": batch["buy_date"], "買入手法": batch["buy_method"],
                    "買入張數": round(matched, 4), "買入價": batch["buy_price"],
                    "賣出日期": jr["交易日期"], "賣出手法": jr["進出手法"],
                    "賣出張數": round(matched, 4), "賣出價": sell_price,
                    "狀態": "已平倉", "報酬率(%)": round(pnl_pct, 2),
                })
                batch["remaining"] -= matched
                sell_remaining -= matched
                if batch["remaining"] <= 1e-9:
                    buy_queue.pop(0)
            if sell_remaining > 1e-9:
                warnings.append(f"{jr['交易日期']} 賣出張數超過目前持有張數，還有 {sell_remaining:g} 張賣出紀錄配對不到買入（可能漏登買入，或張數填錯）。")

    open_trades = []
    for batch in buy_queue:
        if batch["remaining"] <= 1e-9:
            continue
        pnl_pct = None
        if latest_price is not None and batch["buy_price"]:
            pnl_pct = round((latest_price - batch["buy_price"]) / batch["buy_price"] * 100, 2)
        open_trades.append({
            "買入日期": batch["buy_date"], "買入手法": batch["buy_method"],
            "買入張數": round(batch["remaining"], 4), "買入價": batch["buy_price"],
            "賣出日期": "-", "賣出手法": "-", "賣出張數": None, "賣出價": None,
            "狀態": "未平倉", "報酬率(%)": pnl_pct,
        })

    all_trades = closed_trades + open_trades
    all_trades.sort(key=lambda t: t["買入日期"])
    return all_trades, warnings


def latest_close_price_for_code(conn, code: str):
    """取得某股票在 DB 裡最新一筆收盤價，查不到回傳 None (供交易紀錄績效總表計算未平倉部位用)。"""
    try:
        cur = conn.cursor()
        cur.execute("SELECT Close FROM ohlcv_data WHERE SecurityCode = ? ORDER BY Date DESC LIMIT 1", (str(code),))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def save_journal_log(path: str, log_df: pd.DataFrame):
    """將編輯後的 log 分頁寫回 Excel，並保留原有的 stocklist 分頁。
    格式跟隨提供的範本：字體統一為微軟正黑體、欄寬依內容自動調整寬度。"""
    other_sheets = {}
    if os.path.exists(path):
        try:
            existing = pd.read_excel(path, sheet_name=None, engine="openpyxl")
            other_sheets = {k: v for k, v in existing.items() if k != JOURNAL_LOG_SHEET}
        except Exception:
            other_sheets = {}
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        log_df.to_excel(writer, sheet_name=JOURNAL_LOG_SHEET, index=False)
        for sheet_name, sheet_df in other_sheets.items():
            sheet_df.to_excel(writer, sheet_name=sheet_name, index=False)

        # 套用字體 + 依內容自動調整欄寬 (log 分頁)
        from openpyxl.styles import Font
        ws = writer.sheets[JOURNAL_LOG_SHEET]
        font = Font(name=JOURNAL_FONT_NAME, size=JOURNAL_FONT_SIZE)
        for row in ws.iter_rows():
            for cell in row:
                cell.font = font
        for col_cells in ws.columns:
            col_letter = col_cells[0].column_letter
            max_len = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
            ws.column_dimensions[col_letter].width = max(8, max_len + 4)


@st.dialog("📝 交易紀錄編輯器 (Trading Journal)", width="large")
def journal_editor_dialog():
    active_journal_path = st.session_state.get("journal_path", DEFAULT_JOURNAL_PATH)
    cap_col, refresh_col = st.columns([5, 1])
    cap_col.caption(f"編輯目前讀取的檔案: {active_journal_path}")
    # 「清除快取」按鈕：只有目前讀取的是預設路徑 (對應 GitHub repo 的 Trading Journal.xlsx)
    # 才有意義——側邊欄自行上傳的檔案本來就不會跟 GitHub 同步，沒有「快取」可清。
    # 平常開啟編輯器時已經會自動抓一次 GitHub 最新版 (見下方側邊欄按鈕邏輯)，這顆按鈕是
    # 讓使用者在編輯器「已經開著」的情況下，不用關掉重開，也能手動強制重新抓取最新內容，
    # 例如剛在其他地方存檔、想立刻確認這裡有沒有同步到。
    if active_journal_path == DEFAULT_JOURNAL_PATH:
        if refresh_col.button(
            "🔄 清除快取", use_container_width=True, key="journal_clear_cache_btn",
            help="強制重新從 GitHub 抓取最新的 Trading Journal.xlsx，忽略任何快取",
        ):
            with st.spinner("正在清除快取並重新從 GitHub 抓取最新交易紀錄..."):
                meta = fetch_journal_meta_from_github()
            if meta:
                try:
                    with open(DEFAULT_JOURNAL_PATH, "wb") as f:
                        f.write(meta["bytes"])
                    st.session_state.journal_log_df = load_journal_log(DEFAULT_JOURNAL_PATH)
                    st.session_state.journal_github_sha = meta["sha"]
                    # 剛拿到最新版本，之前偵測到的儲存衝突(如果有)已經不成立了，重設掉。
                    st.session_state.journal_save_conflict = False
                    # st.data_editor 的編輯狀態是依 key ("journal_editor_widget") 快取的，
                    # 光是換掉底層資料不會自動重置畫面上的內容，這裡明確清掉該 key，
                    # 讓表格改用剛抓到的最新資料重繪，不會停留在原本(可能是舊的)畫面上。
                    st.session_state.pop("journal_editor_widget", None)
                    st.toast("已清除快取，成功抓取 GitHub 最新版本！", icon="✅")
                    st.rerun()
                except Exception as e:
                    st.warning(f"⚠️ 抓到最新內容，但寫入本機失敗：{e}")
            else:
                st.warning(
                    "⚠️ 清除快取失敗：無法從 GitHub 取得最新的 Trading Journal.xlsx"
                    "（可能是網路問題，或 GITHUB_TOKEN／repo 設定有誤）。目前畫面仍是上次讀取的版本。"
                )

    # 儲存時若偵測到衝突 (見下方「儲存並關閉」流程)，會把這個 flag 設成 True，
    # 這裡在對話框最上方持續顯示警告，直到使用者清除快取重新整理、或選擇強制覆蓋儲存為止。
    if st.session_state.get("journal_save_conflict"):
        st.warning(
            "⚠️ 這份交易紀錄在你編輯期間，已經被別人（或你自己在別的裝置上）更新並存回 GitHub，"
            "直接儲存會蓋掉那個版本。建議先按上方「🔄 清除快取」重新抓取最新版本，"
            "確認不會弄丟別處的紀錄，再把你的編輯內容補回去；如果你確定要用目前畫面上的內容"
            "覆蓋掉 GitHub 上的版本，可以在下方按「強制覆蓋儲存」。"
        )

    # 股票代碼→名稱對照表 (供「股票代碼」欄位可搜尋選單 + 自動帶入「股票名稱」欄位使用)
    code_to_name = dict(zip(stock_list_df["SecurityCode"], stock_list_df["SecurityName"])) if not stock_list_df.empty else {}
    stock_code_options = stock_list_df["SecurityCode"].tolist() if not stock_list_df.empty else []

    # 「進出手法」清單：模組已知訊號名稱 + 使用者自訂新增過的名稱 + 目前資料中已存在的名稱
    if "journal_custom_methods" not in st.session_state:
        st.session_state.journal_custom_methods = []

    with st.form("journal_add_method_form", clear_on_submit=True):
        c1, c2 = st.columns([3, 1])
        new_method = c1.text_input("新增自訂「進出手法」選項 (輸入後會加入下方表格的下拉清單)", label_visibility="collapsed", placeholder="輸入後按「加入選項」，會加進下方下拉清單供選擇")
        add_clicked = c2.form_submit_button("➕ 加入選項", use_container_width=True)
        if add_clicked and new_method.strip():
            if new_method.strip() not in st.session_state.journal_custom_methods:
                st.session_state.journal_custom_methods.append(new_method.strip())

    existing_methods = st.session_state.journal_log_df["進出手法"].dropna().unique().tolist()
    method_options = sorted(set(signal_labels.values()) | set(st.session_state.journal_custom_methods) | set(existing_methods))

    # 交易日期轉為 datetime 型別供「月曆選單」的 DateColumn 使用
    edit_source = st.session_state.journal_log_df.copy()
    edit_source["交易日期"] = pd.to_datetime(edit_source["交易日期"], errors="coerce")
    # 文字欄位在資料為空時 pandas 會推斷成 float(NaN) 型別，與 TextColumn/SelectboxColumn 不相容，
    # 這裡明確轉為 string 型別避免 StreamlitAPIException
    for _col in ["股票代碼", "股票名稱", "買賣方向", "進出手法", "Note"]:
        edit_source[_col] = edit_source[_col].astype("string")
    edit_source["買賣張數"] = pd.to_numeric(edit_source["買賣張數"], errors="coerce")
    # 這裡刻意 .astype(float)：如果目前紀錄裡的價格剛好都是整數 (例如 100、250)，
    # pd.to_numeric() 會把整欄自動推斷成 int64，Streamlit 的 data_editor 會依照
    # 欄位的 pandas dtype 決定編輯格子能不能輸入小數，即使 NumberColumn 有設定
    # format="%.2f" 也一樣會被 int64 擋掉小數點，只能整數。強制轉成 float 避免這個問題。
    edit_source["進出場價格"] = pd.to_numeric(edit_source["進出場價格"], errors="coerce").astype(float)

    edited_df = st.data_editor(
        edit_source,
        num_rows="dynamic",
        use_container_width=True,
        key="journal_editor_widget",
        column_config={
            "交易日期": st.column_config.DateColumn("交易日期", format="YYYY-MM-DD"),
            "股票代碼": st.column_config.SelectboxColumn("股票代碼 (可搜尋)", options=stock_code_options, required=True),
            "股票名稱": st.column_config.TextColumn("股票名稱 (依代碼自動帶入)", disabled=True),
            "進出場價格": st.column_config.NumberColumn("進出場價格", format="%.2f", step=0.01),
            "買賣方向": st.column_config.SelectboxColumn("買賣方向", options=JOURNAL_ACTIONS, required=True),
            "進出手法": st.column_config.SelectboxColumn("進出手法 (清單選擇，上方可新增自訂選項)", options=method_options),
            "買賣張數": st.column_config.NumberColumn("買賣張數", format="%d", min_value=0, step=1),
            "Note": st.column_config.TextColumn("Note"),
        },
    )

    # 依「股票代碼」欄位自動帶入對應的「股票名稱」
    if not edited_df.empty:
        edited_df["股票名稱"] = edited_df["股票代碼"].map(code_to_name).fillna(edited_df["股票名稱"])

    col_save, col_cancel = st.columns(2)
    with col_save:
        save_clicked = st.button("💾 儲存並關閉", use_container_width=True, type="primary", key="journal_editor_save_btn")
    with col_cancel:
        if st.button("取消", use_container_width=True, key="journal_editor_cancel_btn"):
            st.session_state.journal_save_conflict = False
            st.rerun()

    # 「強制覆蓋儲存」只在偵測到衝突之後才出現，平常存檔走一般的「儲存並關閉」即可，
    # 避免這顆有風險的按鈕平常礙眼、甚至被誤按。
    force_save_clicked = False
    if st.session_state.get("journal_save_conflict"):
        force_save_clicked = st.button(
            "⚠️ 強制覆蓋儲存（略過衝突檢查，用目前畫面內容覆蓋 GitHub 上的版本）",
            use_container_width=True, key="journal_editor_force_save_btn",
        )

    if save_clicked or force_save_clicked:
        save_df = edited_df.copy()
        save_df["交易日期"] = pd.to_datetime(save_df["交易日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        save_target_path = st.session_state.get("journal_path", DEFAULT_JOURNAL_PATH)
        try:
            save_journal_log(save_target_path, save_df)
            st.session_state.journal_log_df = load_journal_log(save_target_path)

            # 只有存的是預設路徑 (對應 GitHub repo 根目錄的 Trading Journal.xlsx) 時，
            # 才需要同步回 GitHub；若目前編輯的是側邊欄上傳的自訂檔案，那份本來就不是
            # repo 檔案，不會推送 (見上方 journal_github_config 區塊的說明)。
            if save_target_path == DEFAULT_JOURNAL_PATH:
                with open(save_target_path, "rb") as f:
                    journal_bytes = f.read()
                commit_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                result = upload_journal_to_github(
                    journal_bytes, f"Update Trading Journal.xlsx {commit_time}",
                    expected_sha=st.session_state.get("journal_github_sha"),
                    force=force_save_clicked,
                )
                if result["success"]:
                    st.session_state.journal_github_sha = result["sha"]
                    st.session_state.journal_save_conflict = False
                    st.success("已儲存交易紀錄，並同步回 GitHub repo！")
                    st.rerun()
                elif result["conflict"]:
                    # 記住目前 GitHub 上最新的 sha，讓使用者接下來不管是「清除快取」還是
                    # 「強制覆蓋儲存」，比對的都是最新狀態。立刻 rerun 一次，讓上方持續顯示的
                    # 警告區塊、以及下方新出現的「強制覆蓋儲存」按鈕馬上呈現在畫面上
                    # （警告本身是依 session_state 常駐顯示的，不會因為 rerun 就被清掉）。
                    st.session_state.journal_github_sha = result["sha"]
                    st.session_state.journal_save_conflict = True
                    st.rerun()
                else:
                    # 故意不在這裡呼叫 st.rerun()：如果馬上 rerun，這則警告訊息會在
                    # 使用者還沒看清楚之前就被清掉，變成「同步失敗了但完全沒人發現」。
                    # 保留對話框開著、警告留在畫面上，使用者可以確認 secrets 設定後
                    # 再按一次「儲存並關閉」重試，或自行按「取消」關閉。
                    st.session_state.journal_save_conflict = False
                    st.warning(
                        "⚠️ 已儲存到本機，但同步回 GitHub 失敗（可能是 GITHUB_TOKEN 未設定、權限不足，"
                        "或網路問題）。這次的編輯目前只存在本機，app 之後若重新部署/重啟，可能會被 "
                        "GitHub 上的舊版覆蓋而遺失，請確認 Streamlit 部署的 secrets 裡有設定 "
                        "GITHUB_TOKEN 後再試一次。"
                    )
            else:
                st.session_state.journal_save_conflict = False
                st.success("已儲存交易紀錄！（目前編輯的是側邊欄上傳的自訂檔案，不會同步回 GitHub repo）")
                st.rerun()
        except PermissionError:
            st.error(f"儲存失敗：檔案可能正被 Excel 或其他程式開啟中，請先關閉「{save_target_path}」後再試一次。")
        except Exception as e:
            st.error(f"儲存失敗: {e}")
            # 儲存失敗時，提供下載備份，避免編輯內容遺失
            try:
                import io
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    save_df.to_excel(writer, sheet_name=JOURNAL_LOG_SHEET, index=False)
                st.download_button(
                    "⬇️ 下載目前編輯內容 (寫入失敗時的備份)",
                    data=buf.getvalue(),
                    file_name="Trading Journal (備份).xlsx",
                    key="journal_save_fallback_download",
                )
            except Exception:
                pass


@st.dialog("📊 交易紀錄績效總表", width="large")
def journal_summary_dialog():
    """彙總「交易紀錄編輯器」裡所有股票的交易紀錄，套用跟單股頁面同一套張數感知 FIFO 配對
    (match_journal_trades_fifo)，一次看到整體績效，不用切到 Stock simulator 逐檔股票查看。"""
    journal_df_all = st.session_state.get("journal_log_df", pd.DataFrame(columns=JOURNAL_COLUMNS))
    if journal_df_all.empty:
        st.info("目前交易紀錄是空的，請先在「📝 開啟交易紀錄編輯器」新增紀錄。")
        return

    all_trades = []
    all_warnings = []
    for stock_code_i, group in journal_df_all.groupby("股票代碼"):
        latest_price = latest_close_price_for_code(conn, stock_code_i)
        stock_trades, stock_warnings = match_journal_trades_fifo(group, latest_price)
        name_series = group["股票名稱"].dropna()
        name_i = name_series.iloc[0] if not name_series.empty else ""
        for t in stock_trades:
            all_trades.append({"股票代碼": stock_code_i, "股票名稱": name_i, **t})
        for w in stock_warnings:
            all_warnings.append(f"{stock_code_i} {name_i}：{w}")

    if all_warnings:
        with st.expander(f"⚠️ 有 {len(all_warnings)} 筆配對異常，點此查看", expanded=False):
            for w in all_warnings:
                st.warning(w)

    if not all_trades:
        st.info("目前交易紀錄中沒有可配對的買入紀錄。")
        return

    df_all = pd.DataFrame(all_trades)
    df_all = df_all[["股票代碼", "股票名稱", "買入日期", "買入手法", "買入張數", "買入價",
                      "賣出日期", "賣出手法", "賣出張數", "賣出價", "狀態", "報酬率(%)"]]

    n_closed = int((df_all["狀態"] == "已平倉").sum())
    n_open = int((df_all["狀態"] == "未平倉").sum())
    has_pnl = df_all["報酬率(%)"].notna()
    avg_all = df_all.loc[has_pnl, "報酬率(%)"].mean()
    win_rate_all = (df_all.loc[has_pnl, "報酬率(%)"] > 0).mean() * 100 if has_pnl.any() else None

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("總筆數", f"{len(df_all)} 筆")
    m2.metric("已平倉 / 未平倉", f"{n_closed} / {n_open}")
    m3.metric("平均報酬率 (含未平倉)", f"{avg_all:.2f} %" if pd.notna(avg_all) else "-")
    m4.metric("勝率 (含未平倉)", f"{win_rate_all:.1f} %" if win_rate_all is not None else "-")

    # ============ 全部交易明細（可篩選股票代碼 / 狀態 / 買入日期區間）============
    st.markdown("**全部交易明細**")
    buy_dates_all = pd.to_datetime(df_all["買入日期"], errors="coerce")
    min_buy_date = buy_dates_all.min().date() if buy_dates_all.notna().any() else datetime.now(timezone(timedelta(hours=8))).date()
    today_date = datetime.now(timezone(timedelta(hours=8))).date()

    fcol1, fcol2, fcol3 = st.columns([1, 1, 1.4])
    filter_codes = fcol1.multiselect(
        "篩選股票代碼 (預設全部)", options=sorted(df_all["股票代碼"].unique().tolist()),
        key="journal_summary_filter_codes",
    )
    filter_status = fcol2.multiselect(
        "篩選狀態", options=["已平倉", "未平倉"], default=["已平倉", "未平倉"],
        key="journal_summary_filter_status",
    )
    # 篩選依「買入日期」區間，預設從最早一筆買入紀錄到今天，涵蓋全部紀錄；
    # 結束日期預設為今日，方便直接把區間往前拉就能看「最近幾天」的紀錄。
    date_range = fcol3.date_input(
        "篩選買入日期區間",
        value=(min_buy_date, today_date),
        key="journal_summary_filter_date_range",
    )

    df_show = df_all.copy()
    if filter_codes:
        df_show = df_show[df_show["股票代碼"].isin(filter_codes)]
    if filter_status:
        df_show = df_show[df_show["狀態"].isin(filter_status)]
    # date_input 給範圍值時，使用者只點選了區間其中一端 (還沒點第二端) 的當下會回傳
    # 長度為1的 tuple，這裡先不套用日期篩選，避免中途出現「暫時性」的錯誤篩選結果，
    # 等使用者把兩端都選好 (長度為2) 才實際套用。
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        start_d, end_d = date_range
        show_buy_dates = pd.to_datetime(df_show["買入日期"], errors="coerce")
        df_show = df_show[(show_buy_dates.dt.date >= start_d) & (show_buy_dates.dt.date <= end_d)]

    st.dataframe(df_show.sort_values("買入日期", ascending=False), use_container_width=True, hide_index=True)

    st.markdown("---")
    with st.expander("📈 依「進出手法」分類的績效（只計入已能算出報酬率的筆數）", expanded=False):
        df_scored = df_all.loc[has_pnl]
        if df_scored.empty:
            st.caption("目前沒有可計算報酬率的紀錄。")
        else:
            method_stats = df_scored.groupby("買入手法").agg(
                筆數=("報酬率(%)", "count"),
                平均報酬率=("報酬率(%)", "mean"),
            )
            method_stats["勝率"] = df_scored.groupby("買入手法")["報酬率(%)"].apply(lambda s: (s > 0).mean() * 100)
            method_stats = method_stats.reset_index().sort_values("平均報酬率", ascending=False)
            method_stats["平均報酬率"] = method_stats["平均報酬率"].round(2)
            method_stats["勝率"] = method_stats["勝率"].round(1)
            st.dataframe(method_stats, use_container_width=True, hide_index=True)

    if st.button("關閉", use_container_width=True, key="journal_summary_close_btn"):
        st.rerun()


MARK_COLORS = [
    "#1f4fd6", "#c0392b", "#8e44ad", "#16a085", "#d68910",
    "#2471a3", "#a93226", "#6c3483", "#0e6655", "#b9770e",
    "#1a5276", "#7b241c", "#4a235a", "#0b5345", "#7d6608",
]
REVERSE_3K_SIGNAL_KEY = "reverse_3k_reversal"
REVERSE_3K_MIN_PROFIT_PCT = 10.0

TREND_SIGNAL_KEY = "trendline_breakout"
TREND_TIER_STYLE = {
    "short": {"color": "#111111", "label": "短期下降趨勢線", "hit_label": "短期突破"},
    "mid": {"color": "#c0392b", "label": "中短期下降趨勢線", "hit_label": "中短期突破"},
    "long": {"color": "#1f4fd6", "label": "中長期下降趨勢線", "hit_label": "中長期突破"},
}

# 「上升趨勢線跌破」(signal_module/trendline_breakout_sell.py, key="asc_trendline_breakdown")：
# 屬於賣出型訊號 (SELL_LABELS)，畫在圖上的支撐線/跌破標籤統一用「綠色」系列 (三個等級用不同
# 深淺的綠區分短/中短/中長期，而不是像下降趨勢線那樣三個等級各用不同色系)。
ASC_TREND_SIGNAL_KEY = "asc_trendline_breakdown"
ASC_TREND_TIER_STYLE = {
    "short": {"color": "#27ae60", "label": "短期上升趨勢線", "hit_label": "短期跌破"},
    "mid": {"color": "#1e8449", "label": "中短期上升趨勢線", "hit_label": "中短期跌破"},
    "long": {"color": "#145a32", "label": "中長期上升趨勢線", "hit_label": "中長期跌破"},
}

# --------------------------------------------------------------------------
# 週K/月K 功能 (2026-09-04 新增)
# --------------------------------------------------------------------------
# 「週1K」訊號模組 (Weekly_1K.py, key="weekly_1k") 內部本來就會自己把日K resample成
# 週K再判斷，因此不論目前選的是日K/週K/月K，執行它時一律要餵「原始日K」的資料給它，
# 不能餵已經被外層resample過的df(否則等於對週K再resample一次，語意錯亂)。
WEEKLY_1K_SIGNAL_KEY = "weekly_1k"

KLINE_TIMEFRAMES = ["日K", "週K", "月K"]
# 月K的resample offset別名 2026-09-04 改用 "ME"（月底，Month-End）：pandas 2.2+ 已將舊別名
# "M" 更名為 "ME"，且新版pandas(3.0+)已直接移除"M"、改用"M"會直接丟例外，
# 因此固定用新別名 "ME" 才能同時相容pandas 2.2以後的所有版本。
KLINE_TIMEFRAME_TO_FREQ = {"日K": None, "週K": "W-FRI", "月K": "ME"}
KLINE_TIMEFRAME_UNIT_LABEL = {"日K": "交易日", "週K": "週", "月K": "月"}

# 控制表xlsx若缺失、或「MA計算」區塊解析失敗時的退回預設值 (對應使用者的規格：
# 日K=10/20/60、週K=12/26/52、月K=4/6/12，分別對應 MA10/MA20/MA60 這三個欄位名稱)
DEFAULT_MA_PERIODS = {
    "日K": (10, 20, 60),
    "週K": (12, 26, 52),
    "月K": (4, 6, 12),
}

_SIGNAL_CONTROL_KEY_RE = re.compile(r"\(([a-zA-Z0-9_]+)\)")


def load_signal_control_table(path: str):
    """
    讀取「訊號模組控制表」xlsx (使用者自訂格式：A欄是「編號. 標籤(key)【賣】」文字，
    B/C/D欄是日K/週K/月K是否開放(打V)；下方「MA計算」列開始的三列，B/C/D欄是
    日K/週K/月K各自的(短期,中期,長期)三個MA根數)。

    回傳 (enabled_map, ma_periods, error_msg)：
    - enabled_map: {signal_key: {"日K": bool, "週K": bool, "月K": bool}}，
      單一列若同時列出多個 key(如「Buy01 (buy01)、強勢延伸回測 (buy02)」)，兩個key共用同一組開關。
    - ma_periods: {"日K": (short,mid,long), "週K": (...), "月K": (...)}，解析失敗的timeframe
      會個別退回 DEFAULT_MA_PERIODS 對應值。
    - error_msg: 讀取/解析發生錯誤時的說明文字，成功則為 None。
      找不到檔案時「不」視為錯誤 (回傳空 enabled_map + 預設 ma_periods + None)，
      因為使用者可以選擇不建立控制表，此時所有訊號在三個K線週期下都預設開放。
    """
    enabled_map = {}
    ma_periods = dict(DEFAULT_MA_PERIODS)
    if not path or not os.path.exists(path):
        return enabled_map, ma_periods, None

    try:
        raw = pd.read_excel(path, header=None, engine="openpyxl")
    except Exception as e:
        return enabled_map, ma_periods, f"讀取 {os.path.basename(path)} 失敗：{e}"

    ma_row_idx = None
    for idx, row in raw.iterrows():
        first_cell = row.iloc[0]
        if not isinstance(first_cell, str) or not first_cell.strip():
            continue
        if "MA" in first_cell:
            ma_row_idx = idx
            continue
        keys = _SIGNAL_CONTROL_KEY_RE.findall(first_cell)
        if not keys:
            continue
        day_v = str(row.iloc[1]).strip().upper() == "V"
        week_v = str(row.iloc[2]).strip().upper() == "V"
        month_v = str(row.iloc[3]).strip().upper() == "V"
        for k in keys:
            enabled_map[k] = {"日K": day_v, "週K": week_v, "月K": month_v}

    if ma_row_idx is not None:
        ma_vals = {"日K": [], "週K": [], "月K": []}
        for r in range(ma_row_idx, min(ma_row_idx + 3, len(raw))):
            row = raw.iloc[r]
            for col_idx, tf in ((1, "日K"), (2, "週K"), (3, "月K")):
                v = row.iloc[col_idx]
                if pd.notna(v):
                    try:
                        ma_vals[tf].append(int(v))
                    except (TypeError, ValueError):
                        pass
        for tf in ("日K", "週K", "月K"):
            if len(ma_vals[tf]) == 3:
                ma_periods[tf] = tuple(ma_vals[tf])

    return enabled_map, ma_periods, None


def is_signal_enabled_for_timeframe(signal_key: str, timeframe: str, enabled_map: dict) -> bool:
    """訊號沒被列在控制表裡(含控制表本身不存在)時，預設三個K線週期都開放。"""
    entry = enabled_map.get(signal_key)
    if entry is None:
        return True
    return bool(entry.get(timeframe, True))


def resample_ohlcv(daily_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    把日K OHLCV(index為YYYY-MM-DD字串)依日曆週(W-FRI)/月(M)聚合成週K/月K。
    Open=首筆、High=最高、Low=最低、Close=末筆、Volume=加總；每根K棒的標籤(index)
    採用「該週期內實際最後一個交易日」的日期字串 (而不是pandas預設的週日/月曆底日)，
    確保標籤永遠是真實存在的交易日，跟其餘程式碼(訊號模組用日期字串索引、圖表X軸
    是"category"類型只認實際存在的日期)相容。日K(timeframe="日K")原樣傳回，不處理。
    """
    freq = KLINE_TIMEFRAME_TO_FREQ.get(timeframe)
    if not freq or daily_df.empty:
        return daily_df

    df = daily_df.copy()
    df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[~df.index.isna()].sort_index()

    grouped = df.resample(freq)
    out = grouped.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    # 每個桶(bucket)實際最後一個交易日 = 該桶內原始日期的最大值，用它取代 resample 預設的
    # 週期底日標籤 (例如週日/月底calendar日期，很可能根本不是交易日)。
    # 注意：這裡刻意只對單一欄位(Close)呼叫 .apply()，而不是對整個多欄DataFrame的
    # resampler呼叫 —— 新版pandas(3.x)裡，對多欄DataFrame resampler做「回傳純量」的
    # .apply()，會把同一個值廣播貼到「每一欄」、回傳一個形狀跟原始df一樣的DataFrame，
    # 而不是預期中「每個桶一個值」的Series，導致後面 out.index = ... 賦值時維度對不上而出錯。
    last_real_date = grouped["Close"].apply(lambda s: s.index.max() if len(s) else pd.NaT)
    out.index = last_real_date.reindex(out.index)
    out = out[~out.index.isna()]
    out.index = out.index.strftime("%Y-%m-%d")
    return out


def snap_date_to_bar_label(bar_index, raw_date) -> str:
    """
    週K/月K模式下，把使用者在日期選擇器挑的「日曆日期」對應到它所屬的那一根K棒——
    也就是 bar_index(週K/月K的日期字串索引，由 resample_ohlcv 產生，每個標籤都是
    該根K棒最後一個實際交易日) 裡，第一個 >= raw_date 的日期，因為一根K棒涵蓋
    「上一根K棒標籤(不含) ~ 這一根K棒標籤(含)」的區間。若 raw_date 比所有K棒標籤都晚
    (代表落在「尚未收盤」的當前週期)，回傳 None 表示找不到對應K棒。
    """
    target = pd.Timestamp(raw_date).normalize()
    candidates = sorted(pd.to_datetime(list(bar_index)))
    for d in candidates:
        if d >= target:
            return d.strftime("%Y-%m-%d")
    return None


def estimate_buffer_days(timeframe: str, longest_ma_period: int) -> int:
    """
    依目前K線週期與該週期最長的MA根數，動態估算「往前需要多拉多少天的日K資料」，
    才夠讓resample後的週K/月K有足夠根數計算出最長那條均線(如週K的MA52週、月K的MA12月)。
    日K維持原本固定90天緩衝；週K/月K則依根數換算實際日曆天數，並多留一些安全邊際
    (週K多留8週、月K多留3個月)，避免掃描期間本身、或假日/停牌造成的根數短缺。
    """
    if timeframe == "日K":
        return 90
    if timeframe == "週K":
        return (longest_ma_period + 8) * 7
    if timeframe == "月K":
        return (longest_ma_period + 3) * 31
    return 90


# --------------------------------------------------------------------------
# API 抓取工具函數 (TWSE / TPEX / yfinance)
# --------------------------------------------------------------------------
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}

def number(value) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}: return 0.0
    try: return float(text)
    except ValueError: return 0.0

def get_json(url: str, params: dict = None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=30, verify=False)
    response.raise_for_status()
    return response.json()

def check_status(payload: dict, source: str) -> bool:
    status = str(payload.get("stat", ""))
    if status in {"", "OK"}: return True
    if "沒有符合條件的資料" in status: return False
    raise RuntimeError(f"{source} status: {status}")

def unique_columns(fields: list) -> list:
    seen, result = {}, []
    for field in fields:
        base = str(field).strip()
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result

def fetch_twse_daily(report_date: str, return_payload: bool = False):
    """
    return_payload=True 時改回傳 (df, payload) tuple——2026-08-17 新增：
    讓呼叫端可以把這裡已經打過一次的官方 MI_INDEX payload 直接轉給
    benchmark_utils.update_benchmark_daily() 解析大盤指數，不用為了大盤
    又對同一個官方端點多打一次一模一樣的請求，降低被限流(429)的風險。
    """
    payload = None
    try:
        payload = get_json(TWSE_URL, {"date": report_date, "type": "ALLBUT0999", "response": "json"})
        if not check_status(payload, "上市"):
            return (pd.DataFrame(), payload) if return_payload else pd.DataFrame()
        for table in payload.get("tables", []):
            columns = unique_columns(table.get("fields", []))
            if "證券代號" in columns and "收盤價" in columns:
                df = pd.DataFrame(table.get("data", []), columns=columns)
                target_cols = ["證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "成交股數"]
                df = df[target_cols].copy()
                df.columns = ["SecurityCode", "SecurityName", "Open", "High", "Low", "Close", "Volume"]
                df["SecurityCode"] = df["SecurityCode"].astype(str).str.strip()
                df["SecurityName"] = df["SecurityName"].astype(str).str.strip()
                for col in ["Open", "High", "Low", "Close", "Volume"]:
                    df[col] = df[col].map(number)
                df.insert(0, "Date", pd.to_datetime(report_date, format="%Y%m%d").date())
                df.insert(1, "Market", "上市")
                result_df = df[df["Close"] > 0].drop_duplicates("SecurityCode")
                return (result_df, payload) if return_payload else result_df
        return (pd.DataFrame(), payload) if return_payload else pd.DataFrame()
    except Exception:
        return (pd.DataFrame(), payload) if return_payload else pd.DataFrame()

def fetch_tpex_daily(report_date: str) -> pd.DataFrame:
    dt = datetime.strptime(report_date, "%Y%m%d")
    roc_date = f"{dt.year - 1911}/{dt.strftime('%m/%d')}"
    try:
        payload = get_json(TPEX_URL, {"l": "zh-tw", "d": roc_date, "se": "EW", "o": "json"})
        data_list = []
        if "tables" in payload and payload["tables"]:
            for table in payload["tables"]: data_list.extend(table.get("data", []))
        elif "aaData" in payload: data_list = payload["aaData"]
        if not data_list: return pd.DataFrame()
        
        rows = []
        for row in data_list:
            if len(row) >= 8:
                code = str(row[0]).strip()
                if len(code) > 6: continue
                close_p = number(row[2])
                if close_p > 0:
                    rows.append({
                        "Date": dt.date(), "Market": "上櫃", "SecurityCode": code, "SecurityName": str(row[1]).strip(),
                        "Open": number(row[4]), "High": number(row[5]), "Low": number(row[6]), "Close": close_p, "Volume": number(row[7])
                    })
        df = pd.DataFrame(rows)
        return df.drop_duplicates("SecurityCode") if not df.empty else df
    except Exception as e:
        return pd.DataFrame()

def fetch_yfinance_range_single(stock_code: str, stock_name: str, market: str, start_date_str: str, end_date_str: str) -> pd.DataFrame:
    """
    抓取一檔股票在指定日期區間內 (含頭尾) 的 yfinance 資料，支援單日或多日區間。
    單日更新時只要傳入 start_date_str == end_date_str 即可 (等同舊版單日抓取)。
    """
    try:
        import yfinance as yf
    except ImportError:
        st.error("請先安裝 yfinance 套件 (pip install yfinance)")
        return pd.DataFrame()

    ticker = f"{stock_code}.TW" if market == "上市" else f"{stock_code}.TWO"
    start_dt = pd.to_datetime(start_date_str, format="%Y%m%d")
    # yfinance 的 end 參數為「不含當天」，所以要 +1 天才能把結束日本身也涵蓋進去
    end_dt = pd.to_datetime(end_date_str, format="%Y%m%d") + pd.Timedelta(days=1)
    data = yf.download(ticker, start=start_dt, end=end_dt, ignore_tz=True)
    if data.empty: return pd.DataFrame()

    try:
        if isinstance(data.columns, pd.MultiIndex):
            data = data.copy()
            data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]
        data = data.reset_index()
        date_col = "Date" if "Date" in data.columns else data.columns[0]

        rows = []
        for _, r in data.iterrows():
            c, o, h, l, v = r.get("Close"), r.get("Open"), r.get("High"), r.get("Low"), r.get("Volume")
            if pd.isna(c) or c == 0:
                continue
            rows.append({
                "Date": pd.to_datetime(r[date_col]).date(), "Market": market,
                "SecurityCode": stock_code, "SecurityName": stock_name,
                "Open": float(o), "High": float(h), "Low": float(l), "Close": float(c), "Volume": float(v),
            })
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()

def save_to_database(db_path: str, df: pd.DataFrame):
    if df.empty: return
    # 加上 timeout 與 WAL 模式以解決 database is locked 的問題
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        db_utils.ensure_indexes(conn)  # 確保 (SecurityCode, Date) 索引存在，下面的刪除/查詢才吃得到索引
        # 2026-08-17 修正：journal_mode=WAL 是記錄在 .db 檔案本身、跨連線持續生效的設定，
        # 這個檔案之後會被提交進 git repo，但 WAL 模式需要的 -wal/-shm side-car 檔案不會
        # 一起被提交，導致 Streamlit Cloud 之後重新打開這個檔案時可能整個讀取失敗
        # （這次「pandas.errors.DatabaseError」的根因）。寫入完成後主動切回 DELETE 模式，
        # 避免把 WAL 模式留在被提交的 db 檔案裡；寫入當下仍保留 WAL 以避免鎖定問題。

        # 效能備註 (2026-08-12)：原本用 executemany 對「每一列」各發一條
        # DELETE ... WHERE Date=? AND SecurityCode=?，全市場更新時等於逐檔
        # (約1,700~2,000檔) 各刪一次，比 1_💾_Database Editor.py「抓今日資料→
        # 上傳本地檔案合併」那種按「日期(+市場)」整批刪除的作法慢很多。
        # 改成依「日期」分組，同一天要寫入的股票一次用 IN (...) 刪除，
        # 大幅減少 SQL 執行次數；SQLite 單一語句參數上限約999，這裡分批處理避免超過限制。
        # 用 (Date, SecurityCode) 精準比對而非整批用日期刪除，是為了保留「只更新單一
        # 股票 (yfinance單股模式)」時，不會誤刪同一天其他股票已存在的資料。
        CHUNK_SIZE = 500
        for date_val, group in df.groupby("Date"):
            codes = group["SecurityCode"].astype(str).unique().tolist()
            for i in range(0, len(codes), CHUNK_SIZE):
                chunk = codes[i:i + CHUNK_SIZE]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(
                    f"DELETE FROM ohlcv_data WHERE Date = ? AND SecurityCode IN ({placeholders})",
                    [str(date_val), *chunk],
                )

        # 寫入新資料
        df.to_sql("ohlcv_data", conn, if_exists="append", index=False)
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.commit()


# --------------------------------------------------------------------------
# Session state 初始化
# --------------------------------------------------------------------------
if "signal_registry" not in st.session_state:
    st.session_state.signal_registry, st.session_state.signal_errors = (module_loader.load_default_signal_modules())

if "journal_log_df" not in st.session_state:
    st.session_state.journal_log_df = load_journal_log(DEFAULT_JOURNAL_PATH)
if "journal_path" not in st.session_state:
    st.session_state.journal_path = DEFAULT_JOURNAL_PATH
st.session_state.setdefault("journal_github_sha", None)
st.session_state.setdefault("journal_save_conflict", False)
if "run_results" not in st.session_state: st.session_state.run_results = None

if "signal_control_enabled_map" not in st.session_state:
    _scm, _scma, _scerr = load_signal_control_table(DEFAULT_SIGNAL_CONTROL_TABLE_PATH)
    st.session_state.signal_control_enabled_map = _scm
    st.session_state.signal_control_ma_periods = _scma
    st.session_state.signal_control_load_error = _scerr

# --------------------------------------------------------------------------
# 取得預設 Buy/Sell 條件陣列
# --------------------------------------------------------------------------
signal_keys = list(st.session_state.signal_registry.keys())
signal_labels = {k: st.session_state.signal_registry[k]["label"] for k in signal_keys}

# 目前選定的K線週期 (側邊欄「K線週期」選單元件本身在下面才會渲染，但session_state的值
# 在渲染之前就可以先讀到——第一次執行(還沒有這個key)時退回"日K"，跟該元件的預設值一致)
current_kline_timeframe = st.session_state.get("chart_kline_timeframe", "日K")
_signal_enabled_map = st.session_state.get("signal_control_enabled_map", {})


def is_signal_enabled_now(k: str) -> bool:
    return is_signal_enabled_for_timeframe(k, current_kline_timeframe, _signal_enabled_map)


default_buy_labels = ["3K反轉", "島狀反轉", "KD高腳", "下降趨勢線突破"]
default_buy_keys = [k for k, lbl in signal_labels.items() if lbl in default_buy_labels]


default_sell_labels = ["反向島狀", "跌停", "移動停利"]
default_sell_keys = [k for k, lbl in signal_labels.items() if lbl in default_sell_labels]

# 買入條件選單：僅列出「非賣出型」訊號，且依控制表在「目前K線週期」下是否開放做過濾
buy_option_keys = [k for k in signal_keys if signal_labels[k] not in SELL_LABELS and is_signal_enabled_now(k)]
# 賣出條件選單：僅列出「賣出型」訊號 + 可作為出場依據的反轉型態訊號 (如 反向3K反轉)，同樣依控制表過濾
sell_option_keys = [k for k in signal_keys if is_signal_enabled_now(k)]

# default_buy_keys / default_sell_keys 也要同步過濾，否則若預設訊號剛好在目前K線週期被關閉，
# st.multiselect(default=...) 裡出現不在 options 裡的值會直接丟例外
default_buy_keys = [k for k in default_buy_keys if k in buy_option_keys]
default_sell_keys = [k for k in default_sell_keys if k in sell_option_keys]


def is_sell_key(k: str) -> bool:
    return signal_labels.get(k, k) in SELL_LABELS


def sorted_by_category(keys: list) -> list:
    """排序：買入型訊號排在前面、賣出型訊號排在後面，各自區塊內維持原順序"""
    return sorted(keys, key=lambda k: is_sell_key(k))


# 供「圖表標記訊號勾選」與「賣出條件1」等混合買/賣類型的選單使用（依分類排序，
# 買入型訊號在前、賣出型訊號在後；下拉選單中間會有一條分隔線區分兩類，見下方CSS/JS）
# 「圖表標記訊號勾選」也依控制表在目前K線週期下的開放狀態過濾，避免勾選到不會被評估的訊號。
signal_keys_categorized = sorted_by_category([k for k in signal_keys if is_signal_enabled_now(k)])
sell_option_keys_categorized = sorted_by_category(sell_option_keys)

# --------------------------------------------------------------------------
# 切換K線週期時，把「已勾選但在新週期下不合格」的訊號自動從選單狀態中移除，並提示使用者
# (不清掉的話，st.multiselect 的 session_state 值裡若殘留不在 options 內的 key 會直接丟例外)。
# --------------------------------------------------------------------------
_kline_cleanup_targets = [
    ("chart_buy_signals", buy_option_keys),
    ("chart_sell_signals", sell_option_keys_categorized),
    ("chart_mark_signal_keys", signal_keys_categorized),
]
if st.session_state.get("_prev_kline_timeframe") != current_kline_timeframe:
    _removed_labels = []
    for _key, _allowed in _kline_cleanup_targets:
        _cur_val = st.session_state.get(_key)
        if isinstance(_cur_val, list):
            _kept = [v for v in _cur_val if v in _allowed]
            if len(_kept) != len(_cur_val):
                _removed_labels.extend(signal_labels.get(v, v) for v in _cur_val if v not in _allowed)
                st.session_state[_key] = _kept
    if st.session_state.get("_prev_kline_timeframe") is not None and _removed_labels:
        st.session_state["_kline_switch_notice"] = (
            f"已切換為「{current_kline_timeframe}」，以下訊號在此週期下未開放，已自動從勾選中移除："
            + "、".join(sorted(set(_removed_labels)))
        )
    st.session_state["_prev_kline_timeframe"] = current_kline_timeframe


# --------------------------------------------------------------------------
# 訊號評分 (scoring.py) 整合：組出 calc_signal_quality_score() 需要的 data dict
# --------------------------------------------------------------------------
def _classify_ma_range(close, ma5, ma10, ma20):
    """比照主掃描器 context.py 對均線位置的分類方式重建的近似版本。"""
    if pd.isna(close) or pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
        return ""
    if close > ma5:
        return ">MA5"
    if close > ma10:
        return "MA5~10"
    if close > ma20:
        return "MA10~20"
    return "<MA20"


def _classify_ma_trend(ma5, ma10, ma20):
    if pd.isna(ma5) or pd.isna(ma10) or pd.isna(ma20):
        return ""
    if ma5 > ma10 > ma20:
        return "多頭"
    if ma5 < ma10 < ma20:
        return "空頭"
    return "糾結"


def build_score_input(full_df: pd.DataFrame, d, idx: int) -> dict:
    """
    組出 scoring.calc_signal_quality_score() 需要的 data dict。

    ⚠️ ma_range / ma_trend 的分類方式、volatility_pct 的計算公式，是依照主掃描器
    context.py 的分類邏輯重新實作的「近似版本」(因為 context.py 沒有貼給我看過，
    無法直接 import 同一份程式碼)。如果要讓回測分數跟正式掃描器完全一致，
    把 context.py 貼給我，我再改成直接呼叫同一份函式。

    rs_raw(相對強度)在單股回測工具裡沒有大盤/族群資料可比較，固定回傳 0，
    這一項評分不加不扣分，跟正式掃描器不同，請留意。
    """
    row = full_df.loc[d]
    close = row["Close"]
    prev_close = full_df["Close"].iloc[idx - 1] if idx > 0 else close
    pct = (close - prev_close) / prev_close * 100 if prev_close else 0.0
    volume_lots = row.get("Volume", 0) / 1000.0

    window = full_df["Close"].iloc[max(0, idx - 20): idx + 1]
    daily_ret_pct = window.pct_change().dropna() * 100
    volatility_pct = float(daily_ret_pct.std()) if len(daily_ret_pct) >= 2 else 0.0

    ma5, ma10, ma20 = row.get("MA5"), row.get("MA10"), row.get("MA20")

    return {
        "ma_range": _classify_ma_range(close, ma5, ma10, ma20),
        "ma_trend": _classify_ma_trend(ma5, ma10, ma20),
        "rs_raw": 0,
        "volume_lots": volume_lots,
        "pct": pct,
        "volatility_pct": volatility_pct,
        "MA10": ma10 if pd.notna(ma10) else None,
        "MA20": ma20 if pd.notna(ma20) else None,
        "VolRatioYesterday": row.get("VolRatioYesterday") if "VolRatioYesterday" in full_df.columns and pd.notna(row.get("VolRatioYesterday")) else None,
    }

# 在下拉選單「展開的候選清單」中，於賣出型訊號的第一個選項上方加一條分隔線，
# 用來區分「買入型訊號」與「賣出型訊號」兩個區塊(選項本身已依 sorted_by_category 排序，
# 買入型在前、賣出型在後，這裡只需找到每個候選清單中第一個賣出型訊號並加上框線)。
# 下拉選項清單是動態展開/收合的 DOM，且採用 BaseWeb 的 role="listbox"/"option"，
# 用 JS 依文字內容比對(不依賴特定 class/attribute 名稱，避免版本更新後選擇器失效)，
# 並用 MutationObserver + 定時輪詢確保選單展開當下就能即時套用。
_sell_labels_js = ", ".join([f'"{lbl}"' for lbl in SELL_LABELS])
components.html(
    f"""
    <script>
    const SELL_LABELS = [{_sell_labels_js}];

    function addSignalDividers() {{
        const roots = [];
        try {{ if (window.parent && window.parent.document) roots.push(window.parent.document); }} catch (e) {{}}
        try {{ roots.push(document); }} catch (e) {{}}

        roots.forEach(doc => {{
            try {{
                const listboxes = doc.querySelectorAll('[role="listbox"]');
                listboxes.forEach(listbox => {{
                    const opts = listbox.querySelectorAll('[role="option"]');
                    let dividerPlaced = false;
                    opts.forEach(opt => {{
                        const text = (opt.innerText || opt.textContent || "").trim();
                        const isSell = SELL_LABELS.some(lbl => text.indexOf(lbl) !== -1);
                        opt.style.removeProperty("border-top");
                        opt.style.removeProperty("margin-top");
                        opt.style.removeProperty("padding-top");
                        if (isSell && !dividerPlaced) {{
                            opt.style.setProperty("border-top", "2px solid #999999", "important");
                            opt.style.setProperty("margin-top", "4px", "important");
                            opt.style.setProperty("padding-top", "4px", "important");
                            dividerPlaced = true;
                        }}
                    }});
                }});
            }} catch (e) {{}}
        }});
    }}

    addSignalDividers();
    try {{
        const _observer = new MutationObserver(() => addSignalDividers());
        const _target = (window.parent && window.parent.document && window.parent.document.body) || document.body;
        _observer.observe(_target, {{ childList: true, subtree: true }});
    }} catch (e) {{}}
    setInterval(addSignalDividers, 400);
    </script>
    """,
    height=1,
)


# --------------------------------------------------------------------------
# Sidebar 前半部: UI & 上傳 DB
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("參數設定")

    st.subheader("K線週期")
    st.radio(
        "K線週期",
        options=KLINE_TIMEFRAMES,
        index=KLINE_TIMEFRAMES.index(current_kline_timeframe) if current_kline_timeframe in KLINE_TIMEFRAMES else 0,
        horizontal=True,
        key="chart_kline_timeframe",
        label_visibility="collapsed",
    )
    st.caption("切換後，K線圖、技術指標(MA/KD/RSI/布林通道)、訊號判斷、模擬回測會整頁一起改用該週期重新計算，請重新按 RUN。")

    if st.session_state.get("_kline_switch_notice"):
        st.info(st.session_state.pop("_kline_switch_notice"))

    with st.expander("🔧 訊號開關表 / MA週期設定", expanded=False):
        if st.session_state.get("signal_control_load_error"):
            st.warning(f"⚠️ {st.session_state.signal_control_load_error}（此時所有訊號預設三個週期皆開放）")
        elif not st.session_state.get("signal_control_enabled_map"):
            st.caption(f"目前找不到 {os.path.basename(DEFAULT_SIGNAL_CONTROL_TABLE_PATH)}，所有訊號預設三個週期皆開放。若要自訂各訊號在日K/週K/月K下是否開放、以及MA10/20/60的實際根數，請在跟資料庫同一層目錄放這份控制表。")
        else:
            _ma_now = st.session_state.get("signal_control_ma_periods", DEFAULT_MA_PERIODS).get(current_kline_timeframe, DEFAULT_MA_PERIODS[current_kline_timeframe])
            st.caption(f"目前「{current_kline_timeframe}」的 MA10 / MA20 / MA60 實際根數：{_ma_now[0]} / {_ma_now[1]} / {_ma_now[2]}（欄位名稱不變，僅內部計算根數依控制表設定）")
        if st.button("🔄 重新載入訊號開關表", use_container_width=True, key="reload_signal_control_table_btn"):
            _scm, _scma, _scerr = load_signal_control_table(DEFAULT_SIGNAL_CONTROL_TABLE_PATH)
            st.session_state.signal_control_enabled_map = _scm
            st.session_state.signal_control_ma_periods = _scma
            st.session_state.signal_control_load_error = _scerr
            st.success("已重新載入訊號開關表設定。")
            st.rerun()

    st.markdown("---")

    st.subheader("圖表顯示設定")
    show_ma10 = st.checkbox("顯示 10MA", value=False, key="chart_show_ma10")
    show_ma20 = st.checkbox("顯示 20MA", value=False, key="chart_show_ma20")
    show_ma60 = st.checkbox("顯示 60MA", value=True, key="chart_show_ma60")
    show_bband = st.checkbox("顯示布林通道 (BBand)", value=False, key="chart_show_bband")
    sub_indicator = st.radio(
        "下方副圖顯示",
        options=["KD", "RSI(9)", "都顯示"],
        index=0,
        horizontal=True,
        key="chart_sub_indicator",
    )

    with st.expander("模擬回測設定", expanded=True):
        enable_backtest = st.toggle("啟用模擬回測功能", value=True, key="chart_enable_backtest")

        buy_signals = []
        buy_shares_dict = {}
        stop_loss_dict = {}
        take_profit_dict = {}
        buy_cooldown = 0
        sell_signals = []
        enable_take_profit = False

        if enable_backtest:
            buy_signals = st.multiselect("買入條件 (可複選)", options=buy_option_keys, default=default_buy_keys, format_func=lambda k: signal_labels.get(k, k), key="chart_buy_signals")
            st.caption("此清單僅列出可作為進場依據之訊號，不含賣出型訊號。")
            if buy_signals:
                # 停損/停利改為依「買入訊號」個別設定 (2026-09-03)：每個買入訊號可以有自己的
                # 停損%/停利%，取代舊版全域共用一組數字的做法，讓不同訊號可依其特性分別調校。
                for i, sig in enumerate(buy_signals):
                    lbl = signal_labels.get(sig, sig)
                    buy_shares_dict[sig] = st.number_input(f"➤ 【{lbl}】買入張數", value=1, min_value=1, step=1, key=f"buy_share_{i}_{sig}")
                    sl_col, tp_col = st.columns(2)
                    stop_loss_dict[sig] = sl_col.number_input("　停損 (%)", value=10.0, min_value=0.0, step=1.0, key=f"buy_sl_{i}_{sig}")
                    take_profit_dict[sig] = tp_col.number_input("　停利 (%)", value=10.0, min_value=0.0, step=1.0, key=f"buy_tp_{i}_{sig}")
                st.write("")
                buy_cooldown = st.number_input(
                    f"同訊號再次買入冷卻期 ({KLINE_TIMEFRAME_UNIT_LABEL[current_kline_timeframe]})",
                    value=0, min_value=0, step=1, key="chart_buy_cooldown",
                )

                st.write("")
                enable_bias60_filter = st.checkbox("啟用買入限制：60MA乖離率過高不買", value=False, key="chart_enable_bias60_filter")
                max_bias60_buy_pct = st.number_input("買入限制: 60MA乖離率上限 (%)", value=20.0, step=1.0, disabled=not enable_bias60_filter, key="chart_max_bias60_buy_pct")
                enable_min_score_filter = st.checkbox("啟用買入限制：訊號評分低於門檻不買", value=False, key="chart_enable_min_score_filter")
                min_entry_score = st.number_input("買入限制: 最低訊號評分門檻", value=55.0, step=1.0, disabled=not enable_min_score_filter, key="chart_min_entry_score")

            st.markdown("---")
            sell_signals = st.multiselect("賣出條件1 (訊號出場，可複選)", options=sell_option_keys_categorized, default=default_sell_keys, format_func=lambda k: signal_labels.get(k, k), key="chart_sell_signals")
            st.caption("此清單包含賣出型訊號 (綠色標籤)，亦可額外選用反轉型態訊號 (如反向3K反轉) 作為出場依據。")
            enable_take_profit = st.checkbox("啟用賣出條件2（漲幅達標賣出）", value=False, key="chart_enable_take_profit")
            st.caption("停利/停損百分比請於上方各買入訊號下方個別設定。反向3K反轉：每筆持倉須先獲利超過 10%，才會依該訊號賣出。")

    st.markdown("---")

    # ================= 處理資料庫讀取 =================
    with st.expander("讀取 database", expanded=False):
        db_file = st.file_uploader("讀取database", type=["db"], label_visibility="collapsed", key="db_uploader")
        st.caption("未上傳則自動讀取同資料夾內 twse_ohlcv.db")

    # ================= 處理交易紀錄讀取 =================
    with st.expander("讀取 交易紀錄", expanded=False):
        journal_file = st.file_uploader("讀取交易紀錄", type=["xlsx"], label_visibility="collapsed", key="journal_uploader")
        st.caption(f"未上傳則自動讀取同資料夾內 {os.path.basename(DEFAULT_JOURNAL_PATH)} 的「{JOURNAL_LOG_SHEET}」分頁")

db_path = DEFAULT_DB_PATH
if db_file is not None:
    db_path = _stable_upload_path(db_file, "db_upload", "db_upload_", "uploaded.db")

if not os.path.exists(db_path):
    st.error(f"找不到資料庫檔案: {db_path}，請於左側上傳 twse_ohlcv.db")
    st.stop()

# 交易紀錄檔案路徑：優先使用上傳檔案，否則預設讀取同資料夾的 Trading Journal.xlsx
journal_path = DEFAULT_JOURNAL_PATH
if journal_file is not None:
    journal_path = _stable_upload_path(journal_file, "journal_upload", "journal_upload_", "uploaded_journal.xlsx")

if "journal_log_df" not in st.session_state:
    st.session_state.journal_log_df = load_journal_log(journal_path)
if st.session_state.get("journal_path") != journal_path:
    st.session_state.journal_path = journal_path
    st.session_state.journal_log_df = load_journal_log(journal_path)

@st.cache_resource(show_spinner=False)
def _get_conn(path: str, mtime: float):
    return db_utils.get_connection(path)

conn = _get_conn(db_path, os.path.getmtime(db_path))
stock_list_df = db_utils.get_stock_list(conn)
stock_options = [f"{r.SecurityCode} {r.SecurityName}" for r in stock_list_df.itertuples()]

# 判斷目前選擇的股票 (提供給 yfinance 更新時參考)
current_selected = st.session_state.get("main_stock_choice", stock_options[0] if stock_options else "")
current_code = current_selected.split(" ")[0] if current_selected else ""
current_market = "上市"
current_name = ""
if current_code and not stock_list_df.empty:
    row = stock_list_df[stock_list_df["SecurityCode"] == current_code]
    if not row.empty:
        current_name = row.iloc[0].get("SecurityName", "")
        # 防呆機制: 若 db_utils 回傳表格無 Market，則向 db 查詢
        if "Market" in row.columns:
            current_market = row.iloc[0]["Market"]
        else:
            try:
                mk_df = pd.read_sql(f"SELECT Market FROM ohlcv_data WHERE SecurityCode='{current_code}' LIMIT 1", conn)
                if not mk_df.empty:
                    current_market = mk_df.iloc[0]["Market"]
                else:
                    current_market = "上櫃" if current_code.startswith(("3", "4", "5", "6", "7", "8", "9")) else "上市"
            except Exception:
                current_market = "上櫃" if current_code.startswith(("3", "4", "5", "6", "7", "8", "9")) else "上市"

# --------------------------------------------------------------------------
# Sidebar 後半部: 更新 DB & 訊號模組 & 日期
# --------------------------------------------------------------------------
with st.sidebar:
    with st.expander("交易紀錄", expanded=False):
        if st.button("📝 開啟交易紀錄編輯器", use_container_width=True, key="open_journal_editor_btn"):
            # 只有目前讀取的是預設路徑 (對應 GitHub repo 的 Trading Journal.xlsx) 時，
            # 才嘗試先拉 GitHub 最新版蓋過本機那份，確保編輯器一開就是最新內容，
            # 不會編輯到本機殘留的舊版又存回去、覆蓋掉別處已經更新過的紀錄。
            # 若使用者是讀取自己上傳的檔案，維持原樣不動 (不覆寫使用者上傳的內容)。
            active_journal_path = st.session_state.get("journal_path", DEFAULT_JOURNAL_PATH)
            if active_journal_path == DEFAULT_JOURNAL_PATH:
                with st.spinner("正在從 GitHub 抓取最新的交易紀錄..."):
                    meta = fetch_journal_meta_from_github()
                if meta:
                    try:
                        with open(DEFAULT_JOURNAL_PATH, "wb") as f:
                            f.write(meta["bytes"])
                        st.session_state.journal_log_df = load_journal_log(DEFAULT_JOURNAL_PATH)
                        st.session_state.journal_github_sha = meta["sha"]
                        st.session_state.journal_save_conflict = False
                    except Exception:
                        pass  # 抓到了但寫入本機失敗，靜默退回使用本機現有版本
                # 抓不到 (meta 為 None，例如 repo 裡還沒有這個檔案、或網路問題)
                # 就靜默退回目前本機已載入的版本，使用者仍可正常編輯，不會比原本行為更差。
            journal_editor_dialog()

        if st.button("📊 開啟交易紀錄績效總表", use_container_width=True, key="open_journal_summary_btn"):
            journal_summary_dialog()

    st.subheader("🔄 更新資料庫")
    update_source = st.radio(
        "訊號來源",
        ["1. yfinance (單股)", "2. TWSE/TPEX 官方 API (全市場)", "3. yfinance 批次更新 (全部資料庫股票)"],
        key="chart_update_source",
    )

    # ================= 更新日期區間 (預設起訖皆為今天) =================
    _tw_today = datetime.now(timezone(timedelta(hours=8))).date()
    col_upd_start, col_upd_end = st.columns(2)
    with col_upd_start:
        update_start_date = st.date_input("更新起始日期", value=_tw_today, key="chart_update_start_date")
    with col_upd_end:
        update_end_date = st.date_input("更新結束日期", value=_tw_today, key="chart_update_end_date")

    date_range_valid = update_start_date <= update_end_date
    if not date_range_valid:
        st.error("更新起始日期不能晚於結束日期")
        update_dates = []
    else:
        update_dates = list(pd.date_range(update_start_date, update_end_date, freq="D"))
        if len(update_dates) > 1:
            st.caption(f"將依序更新 {update_start_date} ~ {update_end_date}，共 {len(update_dates)} 天（假日或無交易資料的日子會自動略過）。")

    if update_source.startswith("1."):
        st.caption(f"Yfinance 單股模式：僅抓取目前選擇的股票\n👉 **{current_code} {current_name}**")
    elif update_source.startswith("2."):
        st.caption("官方 API 模式：每天 2 次 HTTP 請求即可取得全市場上市櫃資料，速度最快、建議優先使用。")
    else:
        st.caption(f"批次模式：逐檔抓取資料庫內全部 {len(stock_list_df)} 檔股票的 yfinance 資料，較耗時，並附進度顯示。")

    if st.button("執行更新", use_container_width=True, key="chart_run_update_btn", disabled=not date_range_valid):
        date_list = [d.strftime("%Y%m%d") for d in update_dates]
        range_label = update_start_date.strftime("%Y-%m-%d") if len(date_list) == 1 else f"{update_start_date} ~ {update_end_date}"

        if update_source.startswith("2."):
            # TWSE/TPEX 官方 API：每天都是「一次請求取得全市場資料」的批次端點，
            # 不是逐檔迴圈抓取，效率已經是最佳做法；多天區間時逐日呼叫，這裡僅加上簡易重試以提升穩定度。
            with st.spinner(f"正在從 {update_source} 抓取並更新資料庫 ({range_label})..."):
                progress_bar = st.progress(0.0) if len(date_list) > 1 else None
                status_text = st.empty()
                all_frames = []
                total_twse, total_tpex = 0, 0
                total_benchmark_days = 0
                skipped_days = []
                for i, (d_obj, date_str) in enumerate(zip(update_dates, date_list)):
                    status_text.text(f"抓取進度：{i + 1} / {len(date_list)}　日期：{date_str}")

                    if d_obj.weekday() >= 5:
                        # 週六日必為休市，直接略過不發送請求
                        skipped_days.append(f"{date_str}(假日)")
                    else:
                        twse_df = pd.DataFrame()
                        tpex_df = pd.DataFrame()
                        mi_payload = None
                        for attempt in range(2):
                            # 2026-08-17 修改：改用 return_payload=True 拿回原始 MI_INDEX payload，
                            # 下面同步大盤指數時可以直接複用，不用再多打一次官方 API。
                            twse_df, mi_payload = fetch_twse_daily(date_str, return_payload=True)
                            if not twse_df.empty:
                                break
                            time.sleep(1)
                        for attempt in range(2):
                            tpex_df = fetch_tpex_daily(date_str)
                            if not tpex_df.empty:
                                break
                            time.sleep(1)
                        daily_df = pd.concat([twse_df, tpex_df], ignore_index=True)
                        if not daily_df.empty:
                            all_frames.append(daily_df)
                            total_twse += len(twse_df)
                            total_tpex += len(tpex_df)
                        else:
                            skipped_days.append(date_str)

                        # 2026-08-16 新增：這裡跟 update_db.py 一樣是打官方 TWSE MI_INDEX 端點抓全市場資料，
                        # 同步把「大盤（加權指數）」這天的收盤值也存進同一個 db_path，
                        # 失敗不擋主流程（上市櫃資料已經抓到就照樣寫入），只是這天的比較欄位可能會是 "-"。
                        try:
                            if benchmark_utils.update_benchmark_daily(db_path, date_str, mi_index_payload=mi_payload):
                                total_benchmark_days += 1
                        except Exception:
                            pass

                    if progress_bar is not None:
                        progress_bar.progress((i + 1) / len(date_list))
                    if i < len(date_list) - 1:
                        time.sleep(1.5)  # 多天更新時，天與天之間也稍微停頓，避免高頻請求
                status_text.empty()

                if all_frames:
                    combined_df = pd.concat(all_frames, ignore_index=True)
                    st.cache_resource.clear() # 寫入前先切斷緩存讀取連線
                    save_to_database(db_path, combined_df)
                    st.success(f"全市場更新成功！共 {len(date_list) - len(skipped_days)} 天，上市 {total_twse} 筆 / 上櫃 {total_tpex} 筆；大盤指數同步 {total_benchmark_days} 天")
                    if skipped_days:
                        st.caption(f"以下日期無交易資料，已略過：{'、'.join(skipped_days)}")
                else:
                    st.warning(f"{range_label} 區間內皆無交易資料 (可能為假日或尚未收盤)")

        elif update_source.startswith("1."):
            if not current_code:
                st.warning("請先在主畫面選擇一檔股票再更新。")
            else:
                with st.spinner(f"正在從 {update_source} 抓取並更新資料庫 ({range_label})..."):
                    range_df = fetch_yfinance_range_single(current_code, current_name, current_market, date_list[0], date_list[-1])
                    if range_df.empty:
                        # 單次失敗時重試一次，避免暫時性網路/速率限制問題
                        time.sleep(1)
                        range_df = fetch_yfinance_range_single(current_code, current_name, current_market, date_list[0], date_list[-1])
                    if not range_df.empty:
                        st.cache_resource.clear() # 寫入前先切斷緩存讀取連線
                        save_to_database(db_path, range_df)
                        # 2026-08-16 新增：yfinance 單股模式不會經過官方 TWSE 端點，
                        # 順便確保「大盤」資料至少跟這次更新的最新日期一樣新（不足才會補，成本很低）。
                        try:
                            benchmark_utils.ensure_benchmark_history(db_path, report_date=date_list[-1])
                        except Exception:
                            pass
                        st.success(f"單股更新成功！已更新 {current_code} {current_name}，共 {len(range_df)} 筆交易日資料")
                    else:
                        st.warning(f"無法從 yfinance 取得 {range_label} 的資料 (可能尚未開盤、代碼錯誤，或區間內無交易日)")

        else:
            # 批次模式：逐檔更新，並即時顯示「進度條 + 目前抓取到第幾檔/股票名稱」
            # 每檔股票用同一次 yfinance 請求涵蓋整個日期區間，不會因為區間變長而增加請求次數。
            total = len(stock_list_df)
            if total == 0:
                st.warning("資料庫內尚無股票清單，無法批次更新。")
            else:
                progress_bar = st.progress(0.0)
                status_text = st.empty()
                success_rows = []
                fail_list = []
                for i, row in enumerate(stock_list_df.itertuples()):
                    code = row.SecurityCode
                    name = getattr(row, "SecurityName", "")
                    market = getattr(row, "Market", None)
                    if not market:
                        market = "上櫃" if code.startswith(("3", "4", "5", "6", "7", "8", "9")) else "上市"

                    status_text.text(f"抓取進度：{i + 1} / {total}　目前：{code} {name}（{range_label}）")
                    try:
                        d = fetch_yfinance_range_single(code, name, market, date_list[0], date_list[-1])
                        if not d.empty:
                            success_rows.append(d)
                        else:
                            fail_list.append(f"{code} {name}")
                    except Exception:
                        fail_list.append(f"{code} {name}")

                    progress_bar.progress((i + 1) / total)
                    # 節流延遲(附隨機抖動)，降低被 yfinance/Yahoo 端限速的風險
                    time.sleep(0.3 + random.random() * 0.3)

                status_text.text(f"抓取完成：成功 {len(success_rows)} / {total} 檔")
                if success_rows:
                    all_df = pd.concat(success_rows, ignore_index=True)
                    st.cache_resource.clear() # 寫入前先切斷緩存讀取連線
                    save_to_database(db_path, all_df)
                    # 2026-08-16 新增：批次模式一樣不會經過官方 TWSE 端點，同步確保「大盤」資料夠新。
                    try:
                        benchmark_utils.ensure_benchmark_history(db_path, report_date=date_list[-1])
                    except Exception:
                        pass
                    st.success(f"批次更新完成！成功 {len(success_rows)} 檔，共 {len(all_df)} 筆交易日資料，失敗 {len(fail_list)} 檔。")
                    if fail_list:
                        with st.expander(f"查看失敗的 {len(fail_list)} 檔股票"):
                            st.write("、".join(fail_list))
                else:
                    st.warning("批次更新未取得任何資料，請確認網路連線或稍後再試。")

    st.markdown("---")

    # ================= 觸發 GitHub Actions：Auto Update TWSE OHLCV DB =================
    # 跟上面的「執行更新」不同：上面是在這個 app 的容器內直接呼叫 API 更新本機 db，
    # 容器重啟/重新部署後會被 repo 裡的舊版蓋掉；這裡是觸發 repo 裡真正會把結果 commit
    # 回 GitHub 的 workflow，才是「永久保存」的更新方式。
    st.caption("☁️ 觸發雲端更新：執行 GitHub repo 裡的「Auto Update TWSE OHLCV DB」workflow，完成後會直接 commit 回 repo（跟上面「執行更新」是分開的兩件事）。")
    if st.button("☁️ 觸發 GitHub Actions：Auto Update TWSE OHLCV DB", use_container_width=True, key="trigger_gha_twse_update_btn"):
        gha_branch = journal_github_config()["branch"]
        with st.spinner("正在查詢 workflow 資訊..."):
            gha_workflow_id = find_github_workflow_id(GITHUB_ACTIONS_TWSE_WORKFLOW_NAME)

        if not gha_workflow_id:
            st.error(
                f"找不到名為「{GITHUB_ACTIONS_TWSE_WORKFLOW_NAME}」的 GitHub Actions workflow，"
                "請確認 GITHUB_TOKEN 是否有 Actions 讀取權限 (classic token 需要 workflow scope；"
                "fine-grained token 需要 Actions: Read)，或 workflow 名稱是否有變更。"
            )
        else:
            before_run = get_latest_workflow_run(gha_workflow_id, gha_branch)
            before_run_id = before_run["id"] if before_run else None

            triggered = trigger_github_workflow(gha_workflow_id, gha_branch)
            if not triggered:
                st.error(
                    "觸發失敗：請確認 GITHUB_TOKEN 是否有 Actions 的寫入權限 "
                    "(classic token 需要 workflow scope；fine-grained token 需要 Actions: Read and write)。"
                )
            else:
                gha_progress_bar = st.progress(0.0)
                gha_status_text = st.empty()
                gha_status_text.info("已觸發執行，正在等待 GitHub 建立新的執行紀錄...")

                # 剛觸發完，GitHub 那邊通常要 1~2 秒才會出現對應的新 run，
                # 用短輪詢等待「最新 run 的 id」跟觸發前不一樣，藉此鎖定這次觸發的 run。
                new_run = None
                for _ in range(15):
                    time.sleep(2)
                    candidate = get_latest_workflow_run(gha_workflow_id, gha_branch)
                    if candidate and candidate.get("id") != before_run_id:
                        new_run = candidate
                        break

                if new_run is None:
                    gha_status_text.warning(
                        "已觸發，但暫時查不到對應的新執行紀錄，請直接到 GitHub 的 Actions 頁面確認結果。"
                    )
                else:
                    run_id = new_run["id"]
                    run_url = new_run.get("html_url", "")
                    # 最多輪詢 90 次、每次間隔 2 秒 (約 3 分鐘)，避免 workflow 卡住時 app 無限等待；
                    # 依畫面觀察這個 workflow 平常約 1~2 分鐘完成，3 分鐘已留有餘裕。
                    max_polls = 90
                    finished = False
                    for poll_i in range(max_polls):
                        run_detail = get_workflow_run_by_id(run_id)
                        if run_detail is None:
                            gha_status_text.warning("查詢執行狀態時發生問題，稍後會自動重試...")
                            time.sleep(2)
                            continue

                        status = run_detail.get("status")  # queued / in_progress / completed
                        conclusion = run_detail.get("conclusion")

                        if status == "completed":
                            gha_progress_bar.progress(1.0)
                            if conclusion == "success":
                                gha_status_text.success(f"✅ 執行完成！已將最新資料 commit 回 GitHub repo。[查看執行紀錄]({run_url})")
                            else:
                                gha_status_text.error(f"❌ 執行結束，結果為「{conclusion}」，請查看紀錄排查問題。[查看執行紀錄]({run_url})")
                            finished = True
                            break

                        # GitHub API 沒有直接給百分比，改用「已完成步驟數/總步驟數」概算進度；
                        # 步驟資訊還沒出現(通常是 queued 剛開始的幾秒)時，退回用「輪詢次數」概算一個
                        # 緩慢往前走的進度(最高只到 95%，避免看起來像卡死或提早顯示 100%)。
                        progress_frac = get_workflow_run_progress_fraction(run_id)
                        if progress_frac is None:
                            progress_frac = min(0.05 + poll_i * 0.01, 0.95)
                        gha_progress_bar.progress(progress_frac)
                        gha_status_text.info(f"目前狀態: {status}（第 {poll_i + 1} 次查詢，[查看執行紀錄]({run_url})）")
                        time.sleep(2)

                    if not finished:
                        gha_status_text.warning(f"⏱️ 輪詢已逾時，執行可能仍在進行中，請到 GitHub 查看最新狀態：{run_url}")

    st.markdown("---")

    with st.expander("讀取 signal module", expanded=False):
        signal_files = st.file_uploader("讀取signal module", type=["py"], accept_multiple_files=True, label_visibility="collapsed", key="signal_uploader")
        if signal_files:
            if st.button("套用上傳的 signal module", use_container_width=True, key="chart_apply_signal_upload_btn"):
                registry, errors = module_loader.load_uploaded_signal_modules(signal_files)
                st.session_state.signal_registry = registry
                st.session_state.signal_errors = errors
                st.rerun()
        if st.session_state.signal_errors:
            for e in st.session_state.signal_errors: st.error(f"載入失敗: {e}")

    st.subheader("日期設定")
    scan_start_date = st.date_input("掃描起始日期", value=pd.to_datetime("2026-04-27"), key="chart_scan_start_date")
    # 「結束日期」= 圖表要畫到哪一天為止。原本「📋 掃描結果瀏覽」有一個獨立的「K線日期」
    # 欄位可以覆寫這個值，但因為使用者要展開側邊欄才看得到有沒有生效、效果又不明顯
    # (常常只差一兩根K棒)，2026-08-16 依需求移除該欄位，圖表結束日期統一只由這裡控制。
    scan_end_date = st.date_input("結束日期", value=datetime.today().date(), key="chart_scan_end_date")


# --------------------------------------------------------------------------
# 📋 掃描結果瀏覽
# --------------------------------------------------------------------------
# 原本設計是掃描器跟這個頁面共用同一份 twse_ohlcv.db，掃描完直接寫表、這裡直接讀表。
# 但實測發現兩邊其實是分開部署、各自獨立的磁碟 (不是同一個容器)，掃描器寫進自己那份
# db 的 signal_scan_results 表，這個頁面讀到的是自己那份 db，兩者對不上，一直是空的。
#
# 兩邊真正共用、有實際同步的地方只有 GitHub repo：掃描器每次掃描完成後，會把
# Database/signal_tracking.csv 上傳成 Database/signal_tracking_{YYYYMMDD}.csv
# (詳見 0_📊_台股掃描器.py 的 upload_tracking_file_to_github)。所以這裡改成：
#   1. 先試本地 signal_scan_results 表 (萬一以後兩邊真的合併成同一個部署，優先讀這個，不用打網路)
#   2. 讀不到才改成從 GitHub 抓當天上傳的 signal_tracking_{date}.csv 當備援資料來源
# 這份 CSV 是累積寫入的 (不會每天重置)，抓回來後還要再依 scan_date 欄位篩選一次，
# 不能直接假設整份檔案內容都等於檔名那天的資料。
#
# 排版邏輯 (2026-08-16 依使用者需求重新設計，同日移除「K線日期」欄位)：
#   單行 3 個欄位 + 2 個按鈕，取代原本的卡片網格瀏覽：
#     掃描資料日期 → 訊號類型 (多選) → 股票代碼/名稱 (單選)　三者階層連動篩選，
#     ✅ 查看K線圖 (自動帶入下方 Main 控制列並觸發 RUN)、🔄 重新整理 (清快取重抓 GitHub)。
#   圖表要畫到哪一天，統一交給側邊欄「日期設定」的「結束日期」控制 (不在這裡重複)。
GITHUB_TRACKING_OWNER = st.secrets.get("GITHUB_OWNER", "henglunlin")
GITHUB_TRACKING_REPO = st.secrets.get("GITHUB_REPO", "stock-scanner-FUBAN")
GITHUB_TRACKING_BRANCH = st.secrets.get("GITHUB_BRANCH", "main")
GITHUB_TRACKING_DIR = st.secrets.get("GITHUB_DATABASE_DIR", "Database")


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_tracking_csv_from_github(date_str: str) -> pd.DataFrame:
    """抓 GitHub 上 Database/signal_tracking_{date}.csv，抓不到 (例如那天沒掃描/沒上傳) 回傳空表。"""
    filename = f"signal_tracking_{date_str.replace('-', '')}.csv"
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_TRACKING_OWNER}/{GITHUB_TRACKING_REPO}"
        f"/{GITHUB_TRACKING_BRANCH}/{GITHUB_TRACKING_DIR}/{filename}"
    )
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return pd.DataFrame()
        return pd.read_csv(io.BytesIO(resp.content), encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def _find_latest_github_tracking_date(probe_days: int = 10) -> str:
    """
    從今天開始往回試探最近 `probe_days` 天，找出第一個「GitHub 上確實存在
    signal_tracking_{date}.csv」的日期 (掃描器每次掃描都會用當天日期上傳這個檔名，
    所以檔案存在 = 那天有掃描紀錄)。找到第一個存在的就停止，不會把每一天都打一輪。

    這裡刻意不用 GitHub Contents API 列目錄 (api.github.com)，改成沿用已經驗證過
    可正常運作的 raw.githubusercontent.com 逐日探測方式：
      1. 兩者行為等價，但 api.github.com 對未認證請求有每小時 60 次的速率限制，
         Streamlit Cloud 上多個 app 常共用對外 IP，容易被其他服務排擠而超過額度。
      2. `_fetch_tracking_csv_from_github` 已經是本頁面既有、已被使用者實測成功的
         抓檔路徑，這裡直接重用同一個函式 (且共用同一份 5 分鐘快取)，不用再多維護
         一條獨立的網路呼叫邏輯。
    找不到 (例如最近 probe_days 天內都沒掃描/沒上傳) 則回傳空字串，由呼叫端退回今天。
    """
    today = datetime.today().date()
    for i in range(probe_days):
        candidate = today - timedelta(days=i)
        candidate_str = candidate.strftime("%Y-%m-%d")
        if not _fetch_tracking_csv_from_github(candidate_str).empty:
            return candidate_str
    return ""


def _bare_stock_code(raw_code) -> str:
    """去掉 .TW/.TWO 後綴，統一成跟 stock_options / SecurityCode 一致的純數字代碼。"""
    return str(raw_code).strip().split(".")[0]


def _default_browse_scan_date():
    """
    「掃描資料日期」預設值：優先抓本機資料庫最新的掃描日期；抓不到再看 GitHub 上
    實際存在的最新 signal_tracking 檔案日期；兩邊都沒有才退回今天。
    （日期選擇器本身仍是自由選擇，使用者可隨時手動改選任何一天。）
    """
    try:
        with sqlite3.connect(db_path) as _c:
            local_dates = db_utils.get_scan_result_dates(_c, limit=1)
        if local_dates:
            return pd.to_datetime(local_dates[0]).date()
    except Exception:
        pass
    latest_github_date = _find_latest_github_tracking_date()
    if latest_github_date:
        return pd.to_datetime(latest_github_date).date()
    return datetime.today().date()


st.markdown("### 📋 Sotck Simulator")

with st.expander("展開瀏覽掃描結果", expanded=False):
    sel_col1, sel_col3, sel_col4, btn_col1, btn_col2 = st.columns([1, 1.6, 1.8, 0.9, 0.9])

    # --- 欄位1：掃描資料日期 ---
    if "browse_scan_date" not in st.session_state:
        st.session_state["browse_scan_date"] = _default_browse_scan_date()
    with sel_col1:
        browse_scan_date = st.date_input("掃描資料日期", key="browse_scan_date")
    browse_date_str = pd.to_datetime(browse_scan_date).strftime("%Y-%m-%d")

    # 掃描資料日期一改變，底下依賴它的「訊號類型」「股票代碼/名稱」選擇就失去意義，
    # 直接清掉讓使用者在新的日期重新選，避免殘留舊日期的選項造成 widget 選項不合法的錯誤。
    if st.session_state.get("_browse_last_scan_date") != browse_date_str:
        st.session_state["_browse_last_scan_date"] = browse_date_str
        st.session_state.pop("browse_signal_type_filter", None)
        st.session_state.pop("browse_stock_pick", None)

    # --- 依「掃描資料日期」抓當天掃描結果：本機資料庫優先，讀不到才用 GitHub CSV 備援 ---
    data_source_note = ""
    try:
        with sqlite3.connect(db_path) as _scan_conn:
            scan_results_df = db_utils.get_scan_results(_scan_conn, browse_date_str)
        if not scan_results_df.empty:
            data_source_note = "（來源：本機資料庫）"
    except Exception:
        scan_results_df = pd.DataFrame()

    if scan_results_df.empty:
        github_df = _fetch_tracking_csv_from_github(browse_date_str)
        if not github_df.empty and "scan_date" in github_df.columns:
            day_df = github_df[github_df["scan_date"].astype(str) == browse_date_str].copy()
            if not day_df.empty:
                scan_results_df = pd.DataFrame({
                    "code": day_df["代碼"].apply(_bare_stock_code),
                    "name": day_df.get("股票名稱", ""),
                    "signal_types": day_df.get("訊號類型", ""),
                    "signal_score": pd.to_numeric(day_df.get("訊號分數"), errors="coerce"),
                    "signal_grade": day_df.get("追蹤等級", ""),
                    "price": pd.to_numeric(day_df.get("entry_price"), errors="coerce"),
                    "pct": pd.NA,  # signal_tracking.csv 沒有存漲跌%，這裡留空不顯示
                    "volume_lots": pd.to_numeric(day_df.get("成交量(張)"), errors="coerce"),
                }).sort_values("signal_score", ascending=False).reset_index(drop=True)
                data_source_note = "（來源：GitHub Database/signal_tracking，兩邊部署分開、非即時同步，可能落後於最新一次掃描）"

    has_data = not scan_results_df.empty

    # --- 欄位3：訊號類型 (多選)，選項來自「掃描資料日期」當天實際出現過的類型 ---
    # signal_types 欄位是掃描器用「、」把單一股票當天觸發的多個訊號類型串成一個字串
    # (例如「3K反轉、島狀反轉」)，這裡拆開取聯集。篩選邏輯是 AND：選了多個訊號類型時，
    # 該股票當天要「同時」命中所有已選類型才會留下 (單純訊號濾波器邏輯)，不是命中任一個
    # 就算數。不會把同一檔股票拆成多列 (股票代碼/名稱清單本來就已經是一檔股票一列)。
    all_signal_types = sorted({
        t for types_str in (scan_results_df["signal_types"].fillna("").tolist() if has_data else [])
        for t in types_str.split("、") if t and t != "-"
    })
    with sel_col3:
        selected_types = st.multiselect(
            "訊號類型", options=all_signal_types, key="browse_signal_type_filter", disabled=not has_data,
        )

    if has_data and selected_types:
        type_filtered_df = scan_results_df[
            scan_results_df["signal_types"].fillna("").apply(
                lambda s: all(t in s.split("、") for t in selected_types)
            )
        ]
    else:
        type_filtered_df = scan_results_df

    # --- 欄位4：股票代碼/名稱 (單選)，同一檔股票只顯示一次 ---
    stock_pick_options = [
        f"{_bare_stock_code(r['code'])} {r.get('name', '')}"
        for r in type_filtered_df.drop_duplicates(subset=["code"]).to_dict("records")
    ] if has_data else []
    select_options = stock_pick_options if stock_pick_options else ["(無符合股票)"]
    if st.session_state.get("browse_stock_pick") not in select_options:
        st.session_state.pop("browse_stock_pick", None)
    with sel_col4:
        picked_stock = st.selectbox(
            "股票代碼/名稱", options=select_options, disabled=not stock_pick_options, key="browse_stock_pick",
        )

    # --- 按鈕1：查看K線圖 (自動帶入下方 Main 控制列並觸發 RUN) ---
    with btn_col1:
        st.write("")
        view_clicked = st.button(
            "✅ 查看K線圖", use_container_width=True,
            key="browse_view_btn", disabled=not stock_pick_options,
        )

    # --- 按鈕2：重新整理 (清快取重抓 GitHub) ---
    with btn_col2:
        st.write("")
        if st.button("🔄 重新整理", use_container_width=True, key="browse_refresh_btn"):
            _fetch_tracking_csv_from_github.clear()
            _find_latest_github_tracking_date.clear()
            st.rerun()

    if view_clicked and stock_pick_options:
        picked_code = picked_stock.split(" ")[0]
        matched_option = next(
            (opt for opt in stock_options if opt.startswith(f"{picked_code} ")), None
        )
        if matched_option:
            st.session_state["main_stock_choice"] = matched_option
            st.session_state["chart_scan_target_date"] = pd.to_datetime(browse_date_str).date()
            # 圖表結束日期不在這裡覆寫，維持使用者在側邊欄「日期設定」設定的「結束日期」
            # (K線日期欄位已於 2026-08-16 移除，避免兩個地方都能改同一個效果、卻只有
            # 側邊欄那個看得到目前的值)。
            st.session_state["_pending_chart_run"] = True
            st.rerun()
        else:
            st.warning(f"twse_ohlcv.db 裡找不到股票代碼 {picked_code}，可能尚未更新這檔的價格資料。")

    if not has_data:
        st.info(
            f"{browse_date_str} 目前沒有掃描結果（本機資料庫跟 GitHub 上的 "
            f"Database/signal_tracking_{browse_date_str.replace('-', '')}.csv 都讀不到資料）。"
            f"請確認當天有跑過「台股掃描器」的掃描，且該次掃描已自動上傳到 GitHub。"
        )
    else:
        st.caption(f"共 {len(stock_pick_options)} 檔（{browse_date_str}，符合已選訊號類型）{data_source_note}")


# --------------------------------------------------------------------------
# Main: 控制列
# --------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns([1, 1.4, 1, 0.6])

with col1:
    default_idx = 0
    for i, opt in enumerate(stock_options):
        if opt.startswith("8261"):
            default_idx = i
            break
    stock_choice = st.selectbox("股票代碼", stock_options, index=default_idx, key="main_stock_choice")
    stock_code = stock_choice.split(" ")[0]

with col2:
    selected_signal_keys = st.multiselect(
        "圖表標記訊號勾選 (單日驗證用)",
        options=signal_keys_categorized,
        default=signal_keys_categorized,
        format_func=lambda k: signal_labels.get(k, k),
        key="chart_mark_signal_keys",
    )

with col3:
    # 掃描日期預設改為今日
    scan_target_date = st.date_input("訊號掃描日期 (單日驗證用)", value=datetime.today().date(), key="chart_scan_target_date")

with col4:
    st.write("")
    st.write("")
    # `_pending_chart_run` 由上方「📋 掃描結果瀏覽」的「✅ 查看K線圖」按鈕設定：
    # 按下該按鈕時會先把 main_stock_choice / chart_scan_target_date 帶入 session_state
    # 再 st.rerun()，這裡讀到旗標後視同使用者按下了 RUN，沿用下面完整的既有 RUN 邏輯
    # (指標計算＋模擬回測＋K線＋B/S/停利標記)，不用另外重寫一套圖表繪製流程。
    run_clicked = st.button("RUN", use_container_width=True, type="primary", key="chart_run_btn") \
        or st.session_state.pop("_pending_chart_run", False)


# --------------------------------------------------------------------------
# 回測引擎 (2026-09-03 抽出)：原本整段寫在 RUN 按鈕的 if 區塊內，跟「單次執行」耦合在一起。
# 抽成獨立函式後，「單次執行(RUN)」與「參數批次測試(parameter sweep)」都呼叫同一份邏輯，
# 不必維護兩份幾乎相同的回測程式碼。除了原本的 trades / active_positions / max_capital_used /
# signal_error_log，額外多回傳 equity_curve（逐日「已實現損益累計 + 未平倉部位未實現損益」的
# 權益序列，供畫出權益曲線與計算最大回撤），以及每筆已平倉交易新增「持有天數」欄位（供平均持有
# 天數統計使用）。停損/停利改為依「買入訊號」個別傳入 dict（stop_loss_dict / take_profit_dict），
# 取代舊版全域共用一組數字的做法；找不到對應訊號時退回 10% 預設值。
# --------------------------------------------------------------------------
def run_backtest_simulation(
    display_df, full_df, full_df_close, full_df_bias60, full_df_pos,
    stock_code, stock_name,
    buy_signals, buy_shares_dict, buy_cooldown,
    sell_signals, enable_take_profit,
    stop_loss_dict, take_profit_dict,
    enable_bias60_filter=False, max_bias60_buy_pct=20.0,
    enable_min_score_filter=False, min_entry_score=55.0,
    default_stop_loss_pct=10.0, default_take_profit_pct=10.0,
    daily_full_df=None,
):
    from signal_module.base import SignalContext

    # 2026-09-04 新增：週K/月K模式下，full_df 已經是resample後的資料，但「週1K」訊號
    # 模組(WEEKLY_1K_SIGNAL_KEY)內部本來就會自己把「原始日K」再resample一次判斷週線，
    # 若餵它已經被resample過的df等於resample兩次、語意錯亂，因此它一律要吃原始日K
    # (daily_full_df)。日K模式下 daily_full_df 就是 full_df 本身，行為不變。
    _daily_ctx_df = daily_full_df if daily_full_df is not None else full_df

    def _ctx_df_for(sig):
        return _daily_ctx_df if sig == WEEKLY_1K_SIGNAL_KEY else full_df

    trades = []
    active_positions = []
    last_buy_idx = {}
    max_capital_used = 0
    signal_error_log = []
    equity_curve = []  # [{"date": d, "equity": 已實現損益累計 + 未平倉未實現損益}, ...]
    realized_pnl_cum = 0

    for i, d in enumerate(display_df.index):
        current_price = full_df_close[d]

        # --- 賣出檢查 ---
        if len(active_positions) > 0:
            triggered_sell_signals = []
            if sell_signals:
                for sig in sell_signals:
                    ctx_sell = SignalContext(code=stock_code, name=stock_name, df=_ctx_df_for(sig), scan_date=d)
                    try:
                        if st.session_state.signal_registry[sig]["func"](ctx_sell).hit:
                            triggered_sell_signals.append(sig)
                    except Exception as e:
                        signal_error_log.append({
                            "訊號": signal_labels.get(sig, sig), "日期": d,
                            "動作": "賣出檢查", "錯誤": str(e),
                        })

            remaining_positions = []

            for pos in active_positions:
                profit_pct = (current_price - pos["buy_price"]) / pos["buy_price"] * 100
                sell_reason = None

                eligible_signal = next((sig for sig in triggered_sell_signals if sig != REVERSE_3K_SIGNAL_KEY or profit_pct > REVERSE_3K_MIN_PROFIT_PCT), None)

                pos_stop_loss = stop_loss_dict.get(pos["signal_key"], default_stop_loss_pct)
                pos_take_profit = take_profit_dict.get(pos["signal_key"], default_take_profit_pct)

                if profit_pct <= -pos_stop_loss:
                    sell_reason = f"停損出場 ({profit_pct:.1f}%)"
                elif enable_take_profit and profit_pct >= pos_take_profit:
                    sell_reason = f"停利達標 ({profit_pct:.1f}%)"
                elif eligible_signal is not None:
                    sell_reason = f"訊號出場 ({signal_labels.get(eligible_signal, eligible_signal)})"

                if sell_reason:
                    pnl = (current_price - pos["buy_price"]) * pos["shares"] * 1000
                    holding_days = (pd.to_datetime(d) - pd.to_datetime(pos["buy_date"])).days
                    realized_pnl_cum += pnl
                    trades.append({
                        "買入日期": pos["buy_date"], "買入理由": pos["signal_label"],
                        "進場評分": pos.get("entry_score"), "評分等級": pos.get("entry_grade"),
                        "同日觸發訊號": pos.get("entry_signals", ""),
                        "買入價": pos["buy_price"], "張數": pos["shares"],
                        "賣出日期": d, "賣出理由": sell_reason, "賣出價": current_price,
                        "損益(元)": round(pnl), "報酬率(%)": round(profit_pct, 2),
                        "持有天數": holding_days,
                    })
                else:
                    remaining_positions.append(pos)

            active_positions = remaining_positions
            # 2026-08-16 修正：原本「當天只要有賣出就直接 continue」，導致同一天
            # 停損/停利/訊號出場後，即使當天另有買入訊號觸發，也結構性地不可能同日
            # 再進場。改成賣出檢查結束後照常往下走買入檢查，讓同日再進場成為可能；
            # 「同訊號再次買入冷卻期」(last_buy_idx/buy_cooldown) 不受影響，仍會正常
            # 擋下同一個訊號在冷卻期內的重複買入 (含當天賣出、當天又觸發同訊號的情況)。

        # --- 買入檢查 ---
        if buy_signals:
            current_bias60 = full_df_bias60.get(d, 0)
            if enable_bias60_filter and pd.notna(current_bias60) and current_bias60 > max_bias60_buy_pct:
                pass
            else:
                # 先跑過今天所有勾選的買入條件，收集「今天實際觸發的訊號」。
                # 評分要看的是今天整體訊號共振強度，跟個別訊號是否還在冷卻期無關；
                # 冷卻期只決定「要不要真的建倉」，放在後面單獨判斷。
                hit_today = []
                for sig in buy_signals:
                    ctx_buy = SignalContext(code=stock_code, name=stock_name, df=_ctx_df_for(sig), scan_date=d)
                    try:
                        if st.session_state.signal_registry[sig]["func"](ctx_buy).hit:
                            hit_today.append(sig)
                    except Exception as e:
                        signal_error_log.append({
                            "訊號": signal_labels.get(sig, sig), "日期": d,
                            "動作": "買入檢查", "錯誤": str(e),
                        })

                entry_score, entry_grade, entry_signals_text = None, None, ""
                if hit_today:
                    score_data = build_score_input(full_df, d, full_df_pos[d])
                    score_labels = [signal_labels.get(s, s) for s in hit_today]
                    score_kinds = {lbl: "buy" for lbl in score_labels}
                    entry_score = calc_signal_quality_score(score_data, score_labels, score_kinds)
                    entry_grade = classify_signal_grade(entry_score)
                    entry_signals_text = "、".join(score_labels)

                for sig in hit_today:
                    if sig in last_buy_idx and (i - last_buy_idx[sig]) <= buy_cooldown: continue
                    if enable_min_score_filter and entry_score is not None and entry_score < min_entry_score: continue

                    active_positions.append({
                        "buy_date": d, "buy_price": current_price, "shares": buy_shares_dict[sig],
                        "signal_key": sig, "signal_label": signal_labels.get(sig, sig),
                        "entry_score": entry_score, "entry_grade": entry_grade,
                        "entry_signals": entry_signals_text,
                    })
                    last_buy_idx[sig] = i
                    current_invested = sum(p["buy_price"] * p["shares"] * 1000 for p in active_positions)
                    max_capital_used = max(max_capital_used, current_invested)

        # --- 權益曲線 (Equity Curve)：當天已實現損益累計 + 目前所有未平倉部位的未實現損益 ---
        unrealized_pnl = sum((current_price - p["buy_price"]) * p["shares"] * 1000 for p in active_positions)
        equity_curve.append({"date": d, "equity": realized_pnl_cum + unrealized_pnl})

    return trades, active_positions, max_capital_used, signal_error_log, equity_curve


# --------------------------------------------------------------------------
# 圖表標記防重疊 (2026-09-03)：K線圖上總共有 4 套各自獨立的文字標記系統
# (①一般訊號標記 ②趨勢線突破/跌破的觸發標籤 ③模擬回測 B/S 標記 ④交易紀錄菱形標記)，
# 原本各自只在「自己這套系統內、同一天」做防重疊錯開，彼此之間互不相通，導致行情轉折時
# (常常正好是多套系統同時觸發的時候) 疊出一團文字。這裡改用一個「全部共用」的堆疊計數器：
# 用 display_pos(日期在圖表可視範圍內的整數位置) // ANNOTATION_CLUSTER_WINDOW 分組，讓
# 相鄰1~2個交易日內、不管來自哪一套系統的標記，都算進同一疊，依序往外一層一層疊上去；
# 且一律固定 ax=0（不再左右歪斜），純粹用 ay 垂直堆疊，畫出來才會是整齊的一排文字，
# 而不是東一個西一個的散開。買入方向(direction="down")往下疊、賣出方向("up")往上疊。
# --------------------------------------------------------------------------
ANNOTATION_CLUSTER_WINDOW = 2
ANNOTATION_BASE_AY = 26
ANNOTATION_STEP_AY = 24


def _next_annotation_slot(stack_state, display_pos, d, direction, cluster_window=ANNOTATION_CLUSTER_WINDOW):
    bucket = (display_pos.get(d, 0) // cluster_window, direction)
    slot = stack_state.get(bucket, 0)
    stack_state[bucket] = slot + 1
    return slot


def _stacked_ay(stack_state, display_pos, d, direction, base=ANNOTATION_BASE_AY, step=ANNOTATION_STEP_AY):
    slot = _next_annotation_slot(stack_state, display_pos, d, direction)
    sign = 1 if direction == "down" else -1
    return (base + slot * step) * sign


# --------------------------------------------------------------------------
# RUN: 讀取資料 + 執行訊號判斷 + 執行回測
# --------------------------------------------------------------------------
if run_clicked:
    try:
        from signal_module.base import SignalContext

        # 2026-09-04：依目前選定的K線週期(日K/週K/月K)，決定要用哪一組 MA10/20/60 根數、
        # 需要往前多抓多少天的日K緩衝資料 (週K/月K需要比日K多抓，才夠算出52週/12月的均線)。
        _ma_periods_map = st.session_state.get("signal_control_ma_periods", DEFAULT_MA_PERIODS)
        ma_periods = _ma_periods_map.get(current_kline_timeframe, DEFAULT_MA_PERIODS[current_kline_timeframe])
        buffer_days = estimate_buffer_days(current_kline_timeframe, max(ma_periods))

        # 資料抓取範圍需同時涵蓋「圖表顯示範圍(scan_start_date~scan_end_date)」與「單日驗證掃描日期(scan_target_date)」，
        # 避免掃描日期落在圖表範圍之外時，被誤判為「掃描日不在資料範圍內」。
        # 注意：fetch_end_str 只用來決定「抓取多少資料」，圖表實際顯示範圍仍以使用者設定的 scan_end_date 為準 (見下方 chart_end_str)。
        effective_start = min(pd.to_datetime(scan_start_date), pd.to_datetime(scan_target_date))
        effective_end = max(pd.to_datetime(scan_end_date), pd.to_datetime(scan_target_date))
        buffer_start = (effective_start - pd.Timedelta(days=buffer_days)).strftime("%Y-%m-%d")
        fetch_end_str = effective_end.strftime("%Y-%m-%d")
        scan_start_str = pd.to_datetime(scan_start_date).strftime("%Y-%m-%d")
        chart_end_str = pd.to_datetime(scan_end_date).strftime("%Y-%m-%d")
        scan_target_str = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")
        # 保留使用者原始選的日曆日期字串 (未經週K/月K snap)，供下方「結果是否已過期」提示比對用，
        # 因為 scan_target_str 後續在週K/月K模式下會被改寫成snap後的K棒標籤日期。
        raw_scan_target_str = scan_target_str

        # 若更新後快取被清空，這裡會重新建立連線
        conn = _get_conn(db_path, os.path.getmtime(db_path))

        daily_raw_df = db_utils.get_stock_ohlcv(conn, stock_code, buffer_start, fetch_end_str)
        stock_name = db_utils.get_stock_name(conn, stock_code)

        if daily_raw_df.empty:
            st.error("查無此股票在該期間的資料")
        elif scan_target_str not in daily_raw_df.index:
            st.error(f"訊號掃描日期 {scan_target_str} 不在資料範圍內 (可能為非交易日，或該股票在此日期尚無資料)")
        else:
            # 「週1K」訊號模組永遠吃原始日K資料 (詳見 run_backtest_simulation 說明)，
            # 這裡固定用日K自己的 MA10/20/60 根數計算指標，不受目前K線週期選擇影響。
            daily_full_df = indicators.add_indicators(daily_raw_df, ma_periods=DEFAULT_MA_PERIODS["日K"])
            daily_full_df["VolMA5"] = daily_full_df["Volume"].rolling(5, min_periods=1).mean()
            if "VolMA10" not in daily_full_df.columns:
                daily_full_df["VolMA10"] = daily_full_df["Volume"].rolling(10, min_periods=1).mean()

            # 依目前K線週期把原始日K resample成週K/月K (日K模式下 resample_ohlcv 直接原樣回傳)
            full_df = resample_ohlcv(daily_raw_df, current_kline_timeframe)

            # 週K/月K模式下，使用者在日期選擇器挑的是「日曆日期」，要對應到它所屬的那一根
            # K棒(該K棒最後一個實際交易日的標籤)；日K模式則維持原樣，不需要snap。
            if current_kline_timeframe == "日K":
                snapped_scan_target_str = scan_target_str
            else:
                snapped_scan_target_str = snap_date_to_bar_label(full_df.index, scan_target_date)

            if full_df.empty or snapped_scan_target_str is None or snapped_scan_target_str not in full_df.index:
                st.error(
                    f"訊號掃描日期 {scan_target_str} 在「{current_kline_timeframe}」週期下資料不足"
                    "(可能該週期尚未收盤，或往前資料不夠算出所需均線，請往前調整掃描日期或日期範圍)"
                )
            else:
                scan_target_str = snapped_scan_target_str
                full_df = indicators.add_indicators(full_df, ma_periods=ma_periods)
                full_df["VolMA5"] = full_df["Volume"].rolling(5, min_periods=1).mean()
                if "VolMA10" not in full_df.columns:
                    full_df["VolMA10"] = full_df["Volume"].rolling(10, min_periods=1).mean()

                # 1. 執行單日訊號驗證 (「週1K」訊號一律吃 daily_full_df，其餘吃目前週期的 full_df)
                results = {}
                for key in selected_signal_keys:
                    entry = st.session_state.signal_registry[key]
                    _ctx_df = daily_full_df if key == WEEKLY_1K_SIGNAL_KEY else full_df
                    ctx = SignalContext(code=stock_code, name=stock_name, df=_ctx_df, scan_date=scan_target_str)
                    try:
                        results[key] = entry["func"](ctx)
                    except Exception as e:
                        from signal_module.base import SignalResult
                        results[key] = SignalResult(hit=False, detail=f"執行錯誤: {e}")

                # 圖表顯示範圍維持使用者在「日期設定」設定的 scan_start_date ~ scan_end_date，
                # 不會因為單日驗證掃描日期(scan_target_date)超出此範圍而被意外撐大。
                # 週K/月K模式下 full_df.index 是各K棒「最後一個實際交易日」的標籤，用字串比較
                # 直接篩選即可 (不需要snap；只有精確比對的單日驗證掃描才需要snap)。
                display_df = full_df[(full_df.index >= scan_start_str) & (full_df.index <= chart_end_str)]

                # 2. 執行區間模擬回測邏輯 (2026-09-03：抽成 run_backtest_simulation() 共用函式，
                # 詳見函式定義處的說明；這裡只負責準備輸入資料、呼叫函式、與存放結果)
                full_df_close = full_df["Close"].to_dict()
                full_df_bias60 = full_df["Bias60"].to_dict() if "Bias60" in full_df.columns else {}
                full_df_pos = {date: pos for pos, date in enumerate(full_df.index)}

                if enable_backtest:
                    trades, active_positions, max_capital_used, signal_error_log, equity_curve = run_backtest_simulation(
                        display_df=display_df, full_df=full_df,
                        full_df_close=full_df_close, full_df_bias60=full_df_bias60, full_df_pos=full_df_pos,
                        stock_code=stock_code, stock_name=stock_name,
                        buy_signals=buy_signals, buy_shares_dict=buy_shares_dict, buy_cooldown=buy_cooldown,
                        sell_signals=sell_signals, enable_take_profit=enable_take_profit,
                        stop_loss_dict=stop_loss_dict, take_profit_dict=take_profit_dict,
                        enable_bias60_filter=enable_bias60_filter if buy_signals else False,
                        max_bias60_buy_pct=max_bias60_buy_pct if buy_signals else 20.0,
                        enable_min_score_filter=enable_min_score_filter if buy_signals else False,
                        min_entry_score=min_entry_score if buy_signals else 55.0,
                        daily_full_df=daily_full_df,
                    )
                    # 供下方「參數批次測試(parameter sweep)」重複使用同一份資料，
                    # 不必重新讀取資料庫/重算指標，只需替換停損/停利/冷卻期參數即可批次回測。
                    st.session_state.backtest_ctx = {
                        "display_df": display_df, "full_df": full_df,
                        "full_df_close": full_df_close, "full_df_bias60": full_df_bias60, "full_df_pos": full_df_pos,
                        "stock_code": stock_code, "stock_name": stock_name,
                        "buy_signals": buy_signals, "buy_shares_dict": buy_shares_dict,
                        "sell_signals": sell_signals, "enable_take_profit": enable_take_profit,
                        "enable_bias60_filter": enable_bias60_filter if buy_signals else False,
                        "max_bias60_buy_pct": max_bias60_buy_pct if buy_signals else 20.0,
                        "enable_min_score_filter": enable_min_score_filter if buy_signals else False,
                        "min_entry_score": min_entry_score if buy_signals else 55.0,
                        "daily_full_df": daily_full_df,
                    }
                else:
                    trades, active_positions, max_capital_used, signal_error_log, equity_curve = [], [], 0, [], []

                st.session_state.run_results = (
                    display_df, results, stock_code, stock_name, scan_target_str,
                    trades, active_positions, enable_backtest, max_capital_used, signal_error_log,
                    equity_curve, current_kline_timeframe, raw_scan_target_str,
                )
    except Exception as e:
        st.exception(e)


# --------------------------------------------------------------------------
# 圖表繪製
# --------------------------------------------------------------------------
chart_placeholder = st.container()
with chart_placeholder:
    if st.session_state.run_results is not None:
        display_df, results, code, name, scan_target_str, trades, active_positions, enable_backtest, max_capital_used, signal_error_log, equity_curve, run_kline_timeframe, raw_scan_target_str = st.session_state.run_results

        _cur_target_str = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")
        # 2026-09-04：週K/月K模式下 scan_target_str 已被改寫成snap後的K棒標籤日期，不能直接跟
        # 使用者目前選的日曆日期比較，因此改用未經snap的 raw_scan_target_str 判斷是否過期；
        # 另外也要偵測「K線週期本身」是否已切換(同一天但週期不同，結果也需要重跑)。
        if code != stock_code or raw_scan_target_str != _cur_target_str or run_kline_timeframe != current_kline_timeframe:
            st.warning(
                f"⚠️ 目前顯示的是「{code} / {run_kline_timeframe} / 掃描日期 {scan_target_str}」按下 RUN 當下的結果，"
                f"與你目前選擇的「{stock_code} / {current_kline_timeframe} / {_cur_target_str}」不同，請重新按下 RUN 以更新圖表與訊號結果。"
            )

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.6, 0.18, 0.22])
        fig.add_trace(go.Candlestick(
            x=display_df.index, open=display_df["Open"], high=display_df["High"],
            low=display_df["Low"], close=display_df["Close"],
            increasing_line_color="#c0392b", decreasing_line_color="#1e8449",
            increasing_fillcolor="#c0392b", decreasing_fillcolor="#1e8449", name=code
        ), row=1, col=1)
        
        if show_ma10 and "MA10" in display_df.columns: fig.add_trace(go.Scatter(x=display_df.index, y=display_df["MA10"], line=dict(color="#f39c12", width=1.3), name="10MA"), row=1, col=1)
        if show_ma20 and "MA20" in display_df.columns: fig.add_trace(go.Scatter(x=display_df.index, y=display_df["MA20"], line=dict(color="#8e44ad", width=1.3), name="20MA"), row=1, col=1)
        if show_ma60 and "MA60" in display_df.columns: fig.add_trace(go.Scatter(x=display_df.index, y=display_df["MA60"], line=dict(color="#7f8c8d", width=1.3), name="60MA"), row=1, col=1)

        if show_bband and "BB_UB" in display_df.columns:
            fig.add_trace(go.Scatter(x=display_df.index, y=display_df["BB_UB"], line=dict(color="rgba(155, 89, 182, 0.6)", width=1.5, dash="dot"), name="布林上軌"), row=1, col=1)
            fig.add_trace(go.Scatter(x=display_df.index, y=display_df["BB_LB"], line=dict(color="rgba(155, 89, 182, 0.6)", width=1.5, dash="dot"), name="布林下軌"), row=1, col=1)
            touch_mask = display_df["High"] >= display_df["BB_UB"]
            touch_df = display_df[touch_mask]
            if not touch_df.empty:
                fig.add_trace(go.Scatter(
                    x=touch_df.index, y=touch_df["High"], mode="markers",
                    marker=dict(symbol="circle-open", size=8, color="#9b59b6", line=dict(width=2)),
                    name="觸碰上軌", customdata=touch_df["BB_BW"],
                    hovertemplate="觸碰上軌<br>高點: %{y:.2f}<br>帶寬: %{customdata:.2f}%<extra></extra>"
                ), row=1, col=1)

        # 成交量單位換算為「張」(1000股 = 1張)，僅影響圖表顯示，不影響回測邏輯計算
        vol_lots = display_df["Volume"] / 1000
        vol_colors = ["#c0392b" if c >= o else "#1e8449" for o, c in zip(display_df["Open"], display_df["Close"])]
        fig.add_trace(go.Bar(
            x=display_df.index, y=vol_lots, marker_color=vol_colors, name="成交量(張)",
            hovertemplate="成交量: %{y:,.0f} 張<extra></extra>"
        ), row=2, col=1)
        
        if "VolMA5" in display_df.columns:
            fig.add_trace(go.Scatter(
                x=display_df.index, y=display_df["VolMA5"] / 1000, line=dict(color="#3498db", width=1.2), name="5日均量(張)",
                hovertemplate="5日均量: %{y:,.0f} 張<extra></extra>"
            ), row=2, col=1)
        if "VolMA10" in display_df.columns:
            fig.add_trace(go.Scatter(
                x=display_df.index, y=display_df["VolMA10"] / 1000, line=dict(color="#e67e22", width=1.2), name="10日均量(張)",
                hovertemplate="10日均量: %{y:,.0f} 張<extra></extra>"
            ), row=2, col=1)
        fig.update_yaxes(title_text="張數 (張)", row=2, col=1)

        show_kd_panel = sub_indicator in ("KD", "都顯示")
        show_rsi_panel = sub_indicator in ("RSI(9)", "都顯示")

        if show_kd_panel and "K" in display_df.columns and "D" in display_df.columns:
            fig.add_trace(go.Scatter(x=display_df.index, y=display_df["K"], line=dict(color="#c0392b", width=1.3), name="K9"), row=3, col=1)
            fig.add_trace(go.Scatter(x=display_df.index, y=display_df["D"], line=dict(color="#2980b9", width=1.3), name="D9"), row=3, col=1)
        if show_rsi_panel and "RSI9" in display_df.columns:
            fig.add_trace(go.Scatter(x=display_df.index, y=display_df["RSI9"], line=dict(color="#8e44ad", width=1.3, dash="dot"), name="RSI9"), row=3, col=1)
            # RSI 判別參考線：65(超買)/50(中性)/35(超賣)
            fig.add_hline(y=65, line=dict(color="#e74c3c", width=1, dash="dash"), annotation_text="65", annotation_position="right", row=3, col=1)
            fig.add_hline(y=50, line=dict(color="#7f8c8d", width=1, dash="dash"), annotation_text="50", annotation_position="right", row=3, col=1)
            fig.add_hline(y=35, line=dict(color="#27ae60", width=1, dash="dash"), annotation_text="35", annotation_position="right", row=3, col=1)
        fig.update_yaxes(title_text=sub_indicator, row=3, col=1)

        # 2026-09-03：display_dates/display_pos 提前到這裡計算(原本在訊號標記迴圈之後)，
        # 因為下面統一的「防重疊堆疊計數器」annotation_stack 需要用 display_pos 判斷哪些日期
        # 算「鄰近」。annotation_stack 由本區塊內全部 4 套標記系統(訊號標記/趨勢線觸發標籤/
        # B-S標記/交易紀錄菱形)共用，見函式定義處說明。
        display_dates = display_df.index.tolist()
        display_pos = {dt: i for i, dt in enumerate(display_dates)}
        annotation_stack = {}

        color_idx = 0
        for key, res in results.items():
            # trendline_breakout / asc_trendline_breakdown 的 marks 是
            # (tier_key, anchor1_date, anchor2_date, tier_hit) + ("scan", date) 的特殊結構，
            # 不是單純日期列表，下面通用的「單日訊號標記」邏輯無法處理，改用各自專屬的
            # 繪圖區塊 (見下方)，這裡先跳過避免跑進通用邏輯出錯。
            if key in (TREND_SIGNAL_KEY, ASC_TREND_SIGNAL_KEY): continue
            if not res.hit or not res.marks: continue

            label = st.session_state.signal_registry[key]["label"]

            # 若為賣出訊號，字在K線上方往下指；買入訊號，字在K線下方往上指
            if label in SELL_LABELS:
                color = "#27ae60" # 綠色
                y_col = "High"
                direction = "up"
            else:
                color = MARK_COLORS[color_idx % len(MARK_COLORS)]
                color_idx += 1
                y_col = "Low"
                direction = "down"

            last_idx = len(res.marks) - 1
            for i, d in enumerate(res.marks):
                if d not in display_df.index: continue
                is_trigger = (i == last_idx)

                if is_trigger:
                    # 只有「觸發日」才顯示文字標籤，因此只有它需要納入堆疊計數，
                    # 統一固定 ax=0，靠 ay 垂直堆疊排整齊。
                    ay_val = _stacked_ay(annotation_stack, display_pos, d, direction)
                    fig.add_annotation(
                        x=d, y=display_df.loc[d, y_col], text=label, showarrow=True,
                        arrowhead=2, arrowcolor=color, font=dict(color=color, size=12),
                        ax=0, ay=ay_val, row=1, col=1
                    )
                else:
                    # 非觸發日：只畫小箭頭不顯示文字，不佔用堆疊版位
                    fig.add_annotation(
                        x=d, y=display_df.loc[d, y_col], text="", showarrow=True,
                        arrowhead=1, arrowcolor=color, ax=0,
                        ay=22 if direction == "down" else -22, row=1, col=1
                    )

        tres = results.get(TREND_SIGNAL_KEY)
        if tres is not None and tres.marks:
            scan_d = None
            for item in tres.marks:
                if item[0] == "scan":
                    scan_d = item[1]
                    continue
                tier_key, a1, a2, tier_hit = item
                style = TREND_TIER_STYLE.get(tier_key)
                if style is None or a1 is None or a2 is None: continue
                if a1 not in display_dates or a2 not in display_dates: continue
                x1p, x2p = display_dates.index(a1), display_dates.index(a2)
                if x2p == x1p: continue
                y1, y2 = display_df.loc[a1, "High"], display_df.loc[a2, "High"]
                slope = (y2 - y1) / (x2p - x1p)
                end_pos = len(display_dates) - 1
                end_date = display_dates[end_pos]
                end_val = y1 + slope * (end_pos - x1p)
                fig.add_trace(go.Scatter(
                    x=[a1, end_date], y=[y1, end_val], mode="lines",
                    line=dict(color=style["color"], width=2), name=style["label"], hoverinfo="skip",
                ), row=1, col=1)
                if tier_hit and scan_d is not None and scan_d in display_df.index:
                    fig.add_annotation(
                        x=scan_d, y=display_df.loc[scan_d, "Close"],
                        text=style["hit_label"], showarrow=True, arrowhead=2,
                        arrowcolor=style["color"], font=dict(color=style["color"], size=12),
                        ax=0, ay=_stacked_ay(annotation_stack, display_pos, scan_d, "down"), row=1, col=1,
                    )

        # ===== 上升趨勢線跌破 (asc_trendline_breakdown)：畫出支撐線 + 跌破標籤 =====
        # 邏輯跟上面「下降趨勢線突破」完全對稱，差異只在於：
        #   1. 連的是「低點(Low)」而不是「高點(High)」(上升趨勢線 = 低點與低點的連線)。
        #   2. 三個等級統一用綠色系 (ASC_TREND_TIER_STYLE)，呼應這是賣出型訊號。
        #   3. 線用虛線 (dash="dash") 表示「支撐線」，跟下降趨勢線的實線做視覺區分。
        #   4. 跌破標籤文字放在K線上方、箭頭往下指 (ay 負值)，因為是賣出訊號觸發點。
        asc_tres = results.get(ASC_TREND_SIGNAL_KEY)
        if asc_tres is not None and asc_tres.marks:
            scan_d = None
            for item in asc_tres.marks:
                if item[0] == "scan":
                    scan_d = item[1]
                    continue
                tier_key, a1, a2, tier_hit = item
                style = ASC_TREND_TIER_STYLE.get(tier_key)
                if style is None or a1 is None or a2 is None: continue
                if a1 not in display_dates or a2 not in display_dates: continue
                x1p, x2p = display_dates.index(a1), display_dates.index(a2)
                if x2p == x1p: continue
                y1, y2 = display_df.loc[a1, "Low"], display_df.loc[a2, "Low"]
                slope = (y2 - y1) / (x2p - x1p)
                end_pos = len(display_dates) - 1
                end_date = display_dates[end_pos]
                end_val = y1 + slope * (end_pos - x1p)
                fig.add_trace(go.Scatter(
                    x=[a1, end_date], y=[y1, end_val], mode="lines",
                    line=dict(color=style["color"], width=2, dash="dash"),
                    name=style["label"], hoverinfo="skip",
                ), row=1, col=1)
                if tier_hit and scan_d is not None and scan_d in display_df.index:
                    fig.add_annotation(
                        x=scan_d, y=display_df.loc[scan_d, "Close"],
                        text=style["hit_label"], showarrow=True, arrowhead=2,
                        arrowcolor=style["color"], font=dict(color=style["color"], size=12),
                        ax=0, ay=_stacked_ay(annotation_stack, display_pos, scan_d, "up"), row=1, col=1,
                    )

        if enable_backtest:
            # 2026-09-03：B/S 標記錨點改為當天 Low(買)/High(賣)，不再用實際成交價當Y座標，
            # 避免箭頭指向的點剛好落在K線蠟燭實體內部造成重疊、看不清楚。
            # 同時原本用「日期: 價格」的 dict 會讓同一天第2筆以後的交易直接覆蓋掉第1筆(資料遺失)，
            # 改成「日期: [價格清單]」，同一天多筆時合併顯示為「B×2」這種形式，不再遺失任何一筆。
            # ay 一律改用跟其他標記系統共用的 annotation_stack 堆疊計數器(見函式定義處說明)，
            # 讓 B/S 標記與訊號標記/趨勢線標籤/交易紀錄菱形彼此之間也會互相錯開，不再各畫各的。
            buy_markers = {}
            sell_markers = {}
            for t in trades:
                buy_markers.setdefault(t["買入日期"], []).append(t["買入價"])
                sell_markers.setdefault(t["賣出日期"], []).append(t["賣出價"])
            for pos in active_positions:
                buy_markers.setdefault(pos["buy_date"], []).append(pos["buy_price"])
            for d, prices in buy_markers.items():
                if d not in display_df.index: continue
                text = "B" if len(prices) == 1 else f"B×{len(prices)}"
                fig.add_annotation(
                    x=d, y=display_df.loc[d, "Low"], text=text, showarrow=True, arrowhead=1,
                    arrowcolor="#e74c3c", font=dict(color="white", size=10), bgcolor="#e74c3c",
                    ax=0, ay=_stacked_ay(annotation_stack, display_pos, d, "down"), row=1, col=1,
                )
            for d, prices in sell_markers.items():
                if d not in display_df.index: continue
                text = "S" if len(prices) == 1 else f"S×{len(prices)}"
                fig.add_annotation(
                    x=d, y=display_df.loc[d, "High"], text=text, showarrow=True, arrowhead=1,
                    arrowcolor="#2ecc71", font=dict(color="white", size=10), bgcolor="#2ecc71",
                    ax=0, ay=_stacked_ay(annotation_stack, display_pos, d, "up"), row=1, col=1,
                )

        # ============ 交易紀錄 (Trading Journal) 標記：實際手動交易的買/賣點 ============
        # 用菱形符號區分於上方模擬回測的 B/S 方框標記，避免混淆「模擬」與「實際」交易
        # 顏色改用藍色系
        # 2026-09-03：菱形點本身維持在真實成交價(jp)不動 —— 這是使用者實際成交的價格，
        # 移到Low/High會失真；只調整旁邊的文字標籤，一樣改用共用的 annotation_stack 堆疊計數器，
        # 讓交易紀錄跟其他 3 套標記系統統一排版；因為交易紀錄是本區塊最後畫的，
        # 天然會疊在最外層(離K線最遠)，不會蓋住比較靠近價格的訊號/B-S標記。
        journal_df_all = st.session_state.get("journal_log_df", pd.DataFrame(columns=JOURNAL_COLUMNS))
        journal_for_stock = journal_df_all[journal_df_all["股票代碼"].astype(str) == str(code)] if not journal_df_all.empty else journal_df_all

        # 2026-09-04：週K/月K模式下，交易紀錄菱形標記的顯示規則 (使用者指定)——
        # 週K：只顯示「進出手法」等於「週1K」訊號標籤(訊號模組實際登記的標籤是「周1K」)的紀錄；
        # 月K：完全不顯示交易紀錄菱形標記。日K模式維持原樣，不受影響。
        if run_kline_timeframe == "月K":
            journal_for_stock = journal_for_stock.iloc[0:0]
        elif run_kline_timeframe == "週K" and not journal_for_stock.empty:
            _weekly1k_label = signal_labels.get(WEEKLY_1K_SIGNAL_KEY, "周1K")
            journal_for_stock = journal_for_stock[journal_for_stock["進出手法"] == _weekly1k_label]

        if not journal_for_stock.empty:
            for _, jr in journal_for_stock.iterrows():
                jd = jr["交易日期"]
                if jd not in display_df.index:
                    continue
                jp = jr["進出場價格"]
                method = jr["進出手法"] if pd.notna(jr["進出手法"]) else ""
                shares = jr["買賣張數"] if pd.notna(jr["買賣張數"]) else None
                shares_text = f"{shares:g} 張" if shares is not None else "未填"
                action = resolve_journal_action(jr)
                is_sell = action == "賣出"
                color = "#1565c0" if not is_sell else "#00acc1"  # 買入: 深藍 / 賣出: 藍綠(同屬藍色系但可區分方向)
                fig.add_trace(go.Scatter(
                    x=[jd], y=[jp], mode="markers", marker=dict(symbol="diamond", size=11, color=color, line=dict(color="white", width=1)),
                    name=f"交易紀錄({action})",
                    hovertemplate=f"交易紀錄 [{action}]<br>日期: {jd}<br>價格: {jp}<br>手法: {method}<br>張數: {shares_text}<br>Note: {jr['Note'] if pd.notna(jr['Note']) else ''}<extra></extra>",
                    showlegend=False,
                ), row=1, col=1)

                direction = "down" if not is_sell else "up"
                ay_val = _stacked_ay(annotation_stack, display_pos, jd, direction, base=60)

                fig.add_annotation(
                    x=jd, y=jp, text=method, showarrow=True, arrowhead=1, arrowcolor=color,
                    font=dict(color=color, size=10), ax=0, ay=ay_val, row=1, col=1,
                )

        # 查價線(垂直十字線)設定：
        # hoversubplots="axis" 是 Plotly 官方用來讓查價線橫跨多個子圖(subplot)的機制(plotly.js 2.20+)。
        # 若瀏覽器端 plotly.js 版本較舊而不支援此屬性，該設定會被忽略，
        # 退回成「每個子圖各自顯示自己的查價線，但因X軸同步(matches)，三條線會對齊在同一個X座標上」的效果。
        fig.update_layout(
            title=f"{code} {name}", 
            xaxis_rangeslider_visible=False, 
            height=800, 
            showlegend=True, 
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            hovermode="x unified",   # 將三個子圖的資訊整合在同一個標籤中顯示
            hoversubplots="axis",    # 讓查價線與hover資訊橫跨所有子圖(需較新版plotly.js支援)
            spikedistance=-1,        # 不限制觸發距離，滑鼠在圖表任一處皆可觸發查價線
        )
        
        # 設定垂直查價線 (三個子圖的X軸皆套用，X軸已透過 shared_xaxes 同步範圍)
        fig.update_xaxes(
            type="category",
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            showline=False,
            showgrid=False,
            spikedash="solid",
            spikecolor="#3498db",  # 藍色查價線
            spikethickness=1
        )
        
        if not display_df.empty:
            price_low = float(display_df["Low"].min())
            price_high = float(display_df["High"].max())
            price_pad = max((price_high - price_low) * 0.08, price_high * 0.01, 0.5)
            y_range_min = max(0.0, price_low - price_pad)
            y_range_max = price_high + price_pad
            fig.update_yaxes(range=[y_range_min, y_range_max], row=1, col=1)

        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("設定參數後按下 RUN 以顯示圖表與訊號結果")


# --------------------------------------------------------------------------
# 下方顯示區塊: 訊號結果 & 回測績效
# --------------------------------------------------------------------------
if st.session_state.run_results is not None:
    display_df, results, code, name, scan_target_str, trades, active_positions, enable_backtest, max_capital_used, signal_error_log, equity_curve, run_kline_timeframe, raw_scan_target_str = st.session_state.run_results

    _cur_target_str2 = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")
    if code != stock_code or raw_scan_target_str != _cur_target_str2 or run_kline_timeframe != current_kline_timeframe:
        st.info(f"⚠️ 以下為「{code} / {run_kline_timeframe} / {scan_target_str}」的舊結果，請按 RUN 更新為目前選擇的「{stock_code} / {current_kline_timeframe} / {_cur_target_str2}」。")

    if enable_backtest: col_text, col_backtest = st.columns([1, 2.5])
    else: col_text, col_backtest = st.container(), None

    with col_text:
        st.markdown("**單日驗證訊號結果:**")
        lines = [f"掃描日期: {scan_target_str}", ""]
        for key, res in results.items():
            label = st.session_state.signal_registry[key]["label"]
            status = "✅ 成立" if res.hit else "❌ 不成立"
            lines.append(f"【{label}】{status}\n {res.detail}\n")
        st.text_area("訊號結果", value="\n".join(lines), height=250 if enable_backtest else 150, label_visibility="collapsed", key=f"chart_signal_result_text_{code}_{scan_target_str}")

    if enable_backtest and col_backtest is not None:
        with col_backtest:
            st.markdown("**模擬回測績效 (清單顯示)**")

            # 回測迴圈裡訊號模組執行失敗的警告 (2026-08-16 新增)：原本是 except: pass
            # 整個吞掉，使用者完全不知道某個訊號模組在回測期間偶爾/持續執行失敗。
            # 現在集中列在交易明細上方，不中斷回測本身，但讓使用者知道哪些訊號、
            # 哪些日期發生了錯誤，自行判斷該結果是否可信。
            if signal_error_log:
                st.warning(f"⚠️ 回測期間有 {len(signal_error_log)} 次訊號執行失敗，以下交易結果可能不完整：")
                with st.expander(f"查看訊號執行失敗明細 ({len(signal_error_log)} 筆)"):
                    st.dataframe(pd.DataFrame(signal_error_log), use_container_width=True, hide_index=True)

            if trades:
                df_trades = pd.DataFrame(trades)
                total_pnl = df_trades["損益(元)"].sum()
                win_rate = (len(df_trades[df_trades["損益(元)"] > 0]) / len(df_trades)) * 100 if len(df_trades) > 0 else 0
                total_pnl_pct = (total_pnl / max_capital_used) * 100 if max_capital_used > 0 else 0
                
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("已實現總損益", f"{total_pnl:,} 元")
                m2.metric("交易次數", f"{len(trades)} 次")
                m3.metric("勝率", f"{win_rate:.1f} %")
                m4.metric("最大動用資金", f"{round(max_capital_used):,} 元")
                m5.metric("總損益百分比", f"{total_pnl_pct:.2f} %")

                # ============ 權益曲線(Equity Curve) + 最大回撤(Max Drawdown) + 平均持有天數 ============
                # 權益曲線 = 逐日「已實現損益累計 + 未平倉部位未實現損益」，由 run_backtest_simulation()
                # 逐日計算後回傳；最大回撤 = 權益曲線相對於「當時為止歷史新高」的最大跌幅(取最負值)。
                equity_df = pd.DataFrame(equity_curve) if equity_curve else pd.DataFrame(columns=["date", "equity"])
                if not equity_df.empty:
                    equity_df["歷史新高"] = equity_df["equity"].cummax()
                    equity_df["回撤"] = equity_df["equity"] - equity_df["歷史新高"]
                    max_drawdown = equity_df["回撤"].min()
                else:
                    max_drawdown = 0
                avg_holding_days = df_trades["持有天數"].mean() if "持有天數" in df_trades.columns and df_trades["持有天數"].notna().any() else None

                n1, n2 = st.columns(2)
                n1.metric("最大回撤 (Max Drawdown)", f"{round(max_drawdown):,} 元")
                n2.metric("平均持有天數", f"{avg_holding_days:.1f} 天" if avg_holding_days is not None and pd.notna(avg_holding_days) else "-")

                if not equity_df.empty:
                    eq_fig = go.Figure()
                    eq_fig.add_trace(go.Scatter(
                        x=equity_df["date"], y=equity_df["equity"], mode="lines",
                        line=dict(color="#2980b9", width=2), name="權益(累計損益)",
                    ))
                    eq_fig.add_trace(go.Scatter(
                        x=equity_df["date"], y=equity_df["歷史新高"], mode="lines",
                        line=dict(color="#bdc3c7", width=1, dash="dot"), name="歷史新高",
                    ))
                    eq_fig.update_layout(
                        title="權益曲線 (含未平倉部位未實現損益)", height=260,
                        margin=dict(t=40, b=20, l=10, r=10), showlegend=True,
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    )
                    st.plotly_chart(eq_fig, use_container_width=True, key=f"equity_curve_chart_{code}_{scan_target_str}")

                if "進場評分" in df_trades.columns and df_trades["進場評分"].notna().any():
                    win_score = df_trades.loc[df_trades["損益(元)"] > 0, "進場評分"].mean()
                    lose_score = df_trades.loc[df_trades["損益(元)"] <= 0, "進場評分"].mean()
                    sc1, sc2, sc3 = st.columns(3)
                    sc1.metric("全部交易平均進場評分", f"{df_trades['進場評分'].mean():.1f}")
                    sc2.metric("獲利交易平均進場評分", f"{win_score:.1f}" if pd.notna(win_score) else "-")
                    sc3.metric("虧損交易平均進場評分", f"{lose_score:.1f}" if pd.notna(lose_score) else "-")
                    st.caption("若獲利交易平均評分明顯高於虧損交易，代表評分機制對這組訊號有區分能力；差不多的話，代表目前評分還篩不出強弱，可能需要調整權重。")

                st.dataframe(df_trades, use_container_width=True, hide_index=True)
            else:
                st.info("此區間內無任何已平倉之交易紀錄。")
                
            if active_positions:
                latest_date = display_df.index[-1]
                latest_price = display_df.iloc[-1]["Close"]
                
                st.markdown(f"**(目前尚未平倉之部位 - 以 {latest_date} 收盤價 {latest_price} 結算)**")
                
                open_data = []
                total_unrealized_pnl = 0
                total_open_cost = 0
                
                for p in active_positions:
                    cost = p["buy_price"] * p["shares"] * 1000
                    unrealized_pnl = (latest_price - p["buy_price"]) * p["shares"] * 1000
                    total_open_cost += cost
                    total_unrealized_pnl += unrealized_pnl
                    profit_pct = (latest_price - p["buy_price"]) / p["buy_price"] * 100
                    
                    open_data.append({
                        "買入日期": p["buy_date"], "買入理由": p["signal_label"],
                        "進場評分": p.get("entry_score"), "評分等級": p.get("entry_grade"),
                        "買入價": p["buy_price"], "現價": latest_price,
                        "張數": p["shares"], "未實現損益(元)": round(unrealized_pnl), "報酬率(%)": round(profit_pct, 2)
                    })
                
                total_open_pct = (total_unrealized_pnl / total_open_cost) * 100 if total_open_cost > 0 else 0
                
                om1, om2, om3 = st.columns(3)
                om1.metric("未實現總損益", f"{round(total_unrealized_pnl):,} 元")
                om2.metric("當前投入成本", f"{round(total_open_cost):,} 元")
                om3.metric("總損益百分比", f"{total_open_pct:.2f} %")
                
                st.dataframe(pd.DataFrame(open_data), use_container_width=True, hide_index=True)

    # ============ 交易紀錄 (Trading Journal) 與 模擬回測 損益比對 ============
    st.markdown("---")
    st.markdown("**📒 交易紀錄 vs 模擬回測 損益比對**")
    journal_df_all = st.session_state.get("journal_log_df", pd.DataFrame(columns=JOURNAL_COLUMNS))
    journal_for_stock = journal_df_all[journal_df_all["股票代碼"].astype(str) == str(code)].copy() if not journal_df_all.empty else journal_df_all

    # 2026-09-04：跟上方K線圖菱形標記同一套規則——週K模式下只拿「進出手法」等於「週1K」訊號
    # 標籤(訊號模組實際登記的標籤是「周1K」)的紀錄來比對；月K模式完全不比對交易紀錄。
    if run_kline_timeframe == "月K":
        journal_for_stock = journal_for_stock.iloc[0:0]
    elif run_kline_timeframe == "週K" and not journal_for_stock.empty:
        _weekly1k_label = signal_labels.get(WEEKLY_1K_SIGNAL_KEY, "周1K")
        journal_for_stock = journal_for_stock[journal_for_stock["進出手法"] == _weekly1k_label]

    if run_kline_timeframe == "月K":
        st.info("「月K」模式下不進行交易紀錄比對（交易紀錄僅在日K / 週K模式下顯示）。")
    elif journal_for_stock.empty:
        st.info(f"目前交易紀錄中沒有 {code} 的資料，無法進行比對。可於側邊欄「讀取 交易紀錄」上傳/編輯。")
    elif not enable_backtest or not trades:
        st.info("請先啟用模擬回測功能並產生已平倉交易，才能與交易紀錄比對。")
    else:
        # 交易紀錄的實際買賣配對：改用共用的 match_journal_trades_fifo()（張數感知的 FIFO
        # 配對，2026-08-28），依「買賣方向」欄位(缺漏時退回用「進出手法」文字判斷)決定買/賣，
        # 每筆買入視為獨立持倉批次、依張數精確扣抵，不再是「一買配一賣」的簡化配對，
        # 分批買、分批賣也能正確算出已平倉/未平倉的張數與報酬率。
        # 尚未賣出的批次，會以「最新收盤價」計算目前報酬率，並標註為「未平倉」一併納入比對。
        latest_price = float(display_df.iloc[-1]["Close"]) if not display_df.empty else None
        journal_trades, journal_warnings = match_journal_trades_fifo(journal_for_stock, latest_price)
        for w in journal_warnings:
            st.warning(f"⚠️ {w}")

        if not journal_trades:
            st.info("交易紀錄中尚無可比對的買入紀錄。")
        else:
            df_journal_trades = pd.DataFrame(journal_trades)
            df_journal_trades = df_journal_trades[["買入日期", "買入手法", "買入張數", "買入價", "賣出日期", "賣出手法", "賣出張數", "賣出價", "狀態", "報酬率(%)"]]
            df_backtest_trades = pd.DataFrame(trades)
            n_open = (df_journal_trades["狀態"] == "未平倉").sum()
            n_closed = (df_journal_trades["狀態"] == "已平倉").sum()

            colj, colb = st.columns(2)
            with colj:
                st.markdown(f"**實際交易紀錄** (共 {len(df_journal_trades)} 筆，已平倉 {n_closed} / 未平倉 {n_open})")
                j_avg = df_journal_trades["報酬率(%)"].mean()
                j_win = (df_journal_trades["報酬率(%)"] > 0).mean() * 100
                st.metric("平均報酬率 (含未平倉)", f"{j_avg:.2f} %")
                st.metric("勝率 (含未平倉)", f"{j_win:.1f} %")
                st.dataframe(df_journal_trades, use_container_width=True, hide_index=True)
            with colb:
                st.markdown(f"**模擬回測交易** (共 {len(df_backtest_trades)} 筆)")
                b_avg = df_backtest_trades["報酬率(%)"].mean()
                b_win = (df_backtest_trades["報酬率(%)"] > 0).mean() * 100
                st.metric("平均報酬率", f"{b_avg:.2f} %")
                st.metric("勝率", f"{b_win:.1f} %")
                st.dataframe(df_backtest_trades, use_container_width=True, hide_index=True)

            diff = j_avg - b_avg
            diff_msg = f"實際交易紀錄平均報酬率較模擬回測{'高' if diff >= 0 else '低'} {abs(diff):.2f} 個百分點"
            st.caption(diff_msg)


# --------------------------------------------------------------------------
# 參數批次測試 (Parameter Sweep) — 2026-09-03 新增
# 針對「最近一次按下 RUN」所使用的股票/日期區間/買賣訊號設定 (存在 st.session_state.backtest_ctx
# 裡)，批次測試不同的停損%/停利%/冷卻期天數組合，找出表現較好的參數組合。批次測試時停損/停利
# 對所有勾選的買入訊號套用同一組數字(不再是每個訊號各自的值)，因為若連個別訊號的停損/停利都
# 一起排列組合，總組合數會爆炸；冷卻期則仍是全域一個值(原本就是全域設定，非個別訊號)。
# 重複呼叫的是與「單次執行(RUN)」完全相同的 run_backtest_simulation()，確保批次測試跑出來的
# 每一組結果，跟你把同樣參數帶回上面 RUN 一次算出來的結果一致。
# --------------------------------------------------------------------------
st.markdown("---")
with st.expander("🧪 參數批次測試 (Parameter Sweep)", expanded=False):
    st.caption("請先於上方設定好股票、日期區間、買入/賣出訊號後按一次「RUN」，再到這裡設定停損/停利/冷卻期的候選範圍，批次測試哪組參數表現較好。批次測試時，停損%與停利%會套用到所有勾選的買入訊號(不分訊號個別設定)。")

    sw1, sw2, sw3 = st.columns(3)
    with sw1:
        st.markdown("**停損 (%) 範圍**")
        sweep_sl_start = st.number_input("起始", value=5.0, step=1.0, min_value=0.0, key="sweep_sl_start")
        sweep_sl_end = st.number_input("結束", value=15.0, step=1.0, min_value=0.0, key="sweep_sl_end")
        sweep_sl_step = st.number_input("間距", value=5.0, step=1.0, min_value=0.5, key="sweep_sl_step")
    with sw2:
        st.markdown("**停利 (%) 範圍**")
        sweep_tp_start = st.number_input("起始", value=5.0, step=1.0, min_value=0.0, key="sweep_tp_start")
        sweep_tp_end = st.number_input("結束", value=15.0, step=1.0, min_value=0.0, key="sweep_tp_end")
        sweep_tp_step = st.number_input("間距", value=5.0, step=1.0, min_value=0.5, key="sweep_tp_step")
    with sw3:
        st.markdown(f"**冷卻期 ({KLINE_TIMEFRAME_UNIT_LABEL[current_kline_timeframe]}) 範圍**")
        sweep_cd_start = st.number_input("起始", value=0, step=1, min_value=0, key="sweep_cd_start")
        sweep_cd_end = st.number_input("結束", value=5, step=1, min_value=0, key="sweep_cd_end")
        sweep_cd_step = st.number_input("間距", value=5, step=1, min_value=1, key="sweep_cd_step")

    sweep_sort_by = st.radio("結果排序依據", ["總損益(元)", "勝率(%)"], horizontal=True, key="sweep_sort_by")
    run_sweep_clicked = st.button("🧪 執行批次測試", key="run_param_sweep_btn")

    if run_sweep_clicked:
        ctx = st.session_state.get("backtest_ctx")
        if ctx is None or not ctx.get("buy_signals"):
            st.warning("請先在上方設定買入訊號並按下「RUN」執行過一次模擬回測，才能進行批次測試。")
        else:
            def _sweep_frange(start, end, step):
                vals = []
                v = float(start)
                while v <= float(end) + 1e-9:
                    vals.append(round(v, 2))
                    v += float(step)
                return vals if vals else [round(float(start), 2)]

            sl_list = _sweep_frange(sweep_sl_start, sweep_sl_end, sweep_sl_step)
            tp_list = _sweep_frange(sweep_tp_start, sweep_tp_end, sweep_tp_step)
            cd_step_int = max(1, int(sweep_cd_step))
            cd_list = list(range(int(sweep_cd_start), int(sweep_cd_end) + 1, cd_step_int))
            if not cd_list:
                cd_list = [int(sweep_cd_start)]

            combos = [(sl, tp, cd) for sl in sl_list for tp in tp_list for cd in cd_list]
            sweep_progress = st.progress(0.0, text=f"批次測試執行中... (0/{len(combos)})")
            sweep_rows = []

            for idx, (sl, tp, cd) in enumerate(combos):
                uniform_sl_dict = {sig: sl for sig in ctx["buy_signals"]}
                uniform_tp_dict = {sig: tp for sig in ctx["buy_signals"]}
                s_trades, s_active, s_max_cap, _s_err, s_equity = run_backtest_simulation(
                    display_df=ctx["display_df"], full_df=ctx["full_df"],
                    full_df_close=ctx["full_df_close"], full_df_bias60=ctx["full_df_bias60"], full_df_pos=ctx["full_df_pos"],
                    stock_code=ctx["stock_code"], stock_name=ctx["stock_name"],
                    buy_signals=ctx["buy_signals"], buy_shares_dict=ctx["buy_shares_dict"], buy_cooldown=cd,
                    sell_signals=ctx["sell_signals"], enable_take_profit=ctx["enable_take_profit"],
                    stop_loss_dict=uniform_sl_dict, take_profit_dict=uniform_tp_dict,
                    enable_bias60_filter=ctx["enable_bias60_filter"], max_bias60_buy_pct=ctx["max_bias60_buy_pct"],
                    enable_min_score_filter=ctx["enable_min_score_filter"], min_entry_score=ctx["min_entry_score"],
                    daily_full_df=ctx.get("daily_full_df"),
                )
                s_df = pd.DataFrame(s_trades)
                s_total_pnl = s_df["損益(元)"].sum() if not s_df.empty else 0
                s_win_rate = (len(s_df[s_df["損益(元)"] > 0]) / len(s_df) * 100) if not s_df.empty else 0
                s_avg_hold = s_df["持有天數"].mean() if (not s_df.empty and "持有天數" in s_df.columns and s_df["持有天數"].notna().any()) else None

                s_eq_df = pd.DataFrame(s_equity)
                if not s_eq_df.empty:
                    s_eq_df["歷史新高"] = s_eq_df["equity"].cummax()
                    s_max_dd = (s_eq_df["equity"] - s_eq_df["歷史新高"]).min()
                else:
                    s_max_dd = 0

                sweep_rows.append({
                    "停損(%)": sl, "停利(%)": tp, "冷卻期(日)": cd,
                    "交易次數": len(s_trades), "總損益(元)": round(s_total_pnl), "勝率(%)": round(s_win_rate, 1),
                    "最大回撤(元)": round(s_max_dd),
                    "平均持有天數": round(s_avg_hold, 1) if s_avg_hold is not None and pd.notna(s_avg_hold) else None,
                })
                sweep_progress.progress((idx + 1) / len(combos), text=f"批次測試執行中... ({idx + 1}/{len(combos)})")

            sweep_progress.empty()
            sweep_df = pd.DataFrame(sweep_rows)
            sweep_sort_col = "總損益(元)" if sweep_sort_by == "總損益(元)" else "勝率(%)"
            sweep_df = sweep_df.sort_values(sweep_sort_col, ascending=False).reset_index(drop=True)
            st.session_state.param_sweep_result = sweep_df
            st.session_state.param_sweep_sort_label = sweep_sort_by

    if st.session_state.get("param_sweep_result") is not None:
        _sweep_result_df = st.session_state.param_sweep_result
        _sweep_sort_label = st.session_state.get("param_sweep_sort_label", "總損益(元)")
        st.markdown(f"**批次測試結果** (共 {len(_sweep_result_df)} 組合，依「{_sweep_sort_label}」由高到低排序)")
        st.dataframe(_sweep_result_df, use_container_width=True, hide_index=True)
