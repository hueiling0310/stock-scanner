"""
台股訊號模擬器 (Signal Simulator) - 含進階模擬回測功能 (可開關)
支援多重買入訊號、獨立張數設定、買入冷卻期、獨立單筆停損停利、多重複選出場訊號、完整交易明細、未實現損益。
新增：側邊欄自動抓取今日數值更新本地資料檔 (支援 yfinance 單股更新 與 TWSE/TPEX 官方 API 全市場更新)。

執行方式:
    streamlit run app.py
"""
import os
import io
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
JOURNAL_LOG_SHEET = "log"
JOURNAL_COLUMNS = ["交易日期", "股票代碼", "股票名稱", "進出場價格", "進出手法", "買賣張數", "Note"]
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
        "owner": st.secrets.get("GITHUB_OWNER", "hueiling0310"),
        "repo": st.secrets.get("GITHUB_REPO", "stock-scanner"),
        "branch": st.secrets.get("GITHUB_BRANCH", "main"),
    }


def fetch_journal_bytes_from_github():
    """從 GitHub repo 根目錄抓最新的 Trading Journal.xlsx 內容 (bytes)，抓不到回傳 None。"""
    cfg = journal_github_config()
    encoded_path = urllib.parse.quote(JOURNAL_GITHUB_PATH, safe="/")
    url = f"https://raw.githubusercontent.com/{cfg['owner']}/{cfg['repo']}/{cfg['branch']}/{encoded_path}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None


def upload_journal_to_github(file_bytes: bytes, commit_message: str) -> bool:
    """把交易紀錄 Excel 內容推回 GitHub repo 根目錄的 Trading Journal.xlsx (需要 GITHUB_TOKEN)。"""
    cfg = journal_github_config()
    token, owner, repo, branch = cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]
    if not token or not owner or not repo:
        return False

    encoded_path = urllib.parse.quote(JOURNAL_GITHUB_PATH, safe="/")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
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
        if sha:
            payload["sha"] = sha

        put_res = requests.put(url, headers=headers, json=payload, timeout=30)
        return put_res.status_code in (200, 201)
    except Exception:
        return False


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
    df = df.dropna(subset=["交易日期", "股票代碼"])
    return df.reset_index(drop=True)


def classify_journal_action(method_text: str) -> str:
    """判斷此筆記錄屬於買入或賣出：
    優先比對是否為系統既有的「賣出型」訊號 (SELL_LABELS，如 移動停利、跌停 等文字本身不含「賣」字)，
    若不在清單內的自訂文字，才退回用「文字中是否含有『賣』字」判斷 (例如自訂輸入「反向3K反轉賣出」)。"""
    text = str(method_text)
    if text in SELL_LABELS:
        return "賣出"
    return "賣出" if "賣" in text else "買入"


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
    st.caption(f"編輯目前讀取的檔案: {st.session_state.get('journal_path', DEFAULT_JOURNAL_PATH)}")

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
    for _col in ["股票代碼", "股票名稱", "進出手法", "Note"]:
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
        if st.button("💾 儲存並關閉", use_container_width=True, type="primary", key="journal_editor_save_btn"):
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
                    synced = upload_journal_to_github(journal_bytes, f"Update Trading Journal.xlsx {commit_time}")
                    if synced:
                        st.success("已儲存交易紀錄，並同步回 GitHub repo！")
                        st.rerun()
                    else:
                        # 故意不在這裡呼叫 st.rerun()：如果馬上 rerun，這則警告訊息會在
                        # 使用者還沒看清楚之前就被清掉，變成「同步失敗了但完全沒人發現」。
                        # 保留對話框開著、警告留在畫面上，使用者可以確認 secrets 設定後
                        # 再按一次「儲存並關閉」重試，或自行按「取消」關閉。
                        st.warning(
                            "⚠️ 已儲存到本機，但同步回 GitHub 失敗（可能是 GITHUB_TOKEN 未設定、權限不足，"
                            "或網路問題）。這次的編輯目前只存在本機，app 之後若重新部署/重啟，可能會被 "
                            "GitHub 上的舊版覆蓋而遺失，請確認 Streamlit 部署的 secrets 裡有設定 "
                            "GITHUB_TOKEN 後再試一次。"
                        )
                else:
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
    with col_cancel:
        if st.button("取消", use_container_width=True, key="journal_editor_cancel_btn"):
            st.rerun()
MARK_COLORS = ["#1f4fd6", "#c0392b", "#8e44ad", "#16a085", "#d68910"]
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
        # 2026-08-22 修正：journal_mode=WAL 是記錄在 .db 檔案本身、跨連線持續生效的設定，
        # 這個檔案之後會被提交進 git repo，但 WAL 模式需要的 -wal/-shm side-car 檔案不會
        # 一起被提交，導致 Streamlit Cloud 之後重新打開這個檔案時可能整個讀取失敗
        # （這正是「database disk image is malformed」的根因）。寫入完成後主動切回 DELETE
        # 模式，避免把 WAL 模式留在被提交的 db 檔案裡；寫入當下仍保留 WAL 以避免鎖定問題。

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
if "run_results" not in st.session_state: st.session_state.run_results = None

# --------------------------------------------------------------------------
# 取得預設 Buy/Sell 條件陣列
# --------------------------------------------------------------------------
signal_keys = list(st.session_state.signal_registry.keys())
signal_labels = {k: st.session_state.signal_registry[k]["label"] for k in signal_keys}

default_buy_labels = ["3K反轉", "島狀反轉", "KD高腳", "下降趨勢線突破"]
default_buy_keys = [k for k, lbl in signal_labels.items() if lbl in default_buy_labels]


default_sell_labels = ["反向島狀", "跌停", "移動停利"]
default_sell_keys = [k for k, lbl in signal_labels.items() if lbl in default_sell_labels]

# 買入條件選單：僅列出「非賣出型」訊號，避免與賣出訊號混雜在同一份清單中
buy_option_keys = [k for k in signal_keys if signal_labels[k] not in SELL_LABELS]
# 賣出條件選單：僅列出「賣出型」訊號 + 可作為出場依據的反轉型態訊號 (如 反向3K反轉)
sell_option_keys = signal_keys


def is_sell_key(k: str) -> bool:
    return signal_labels.get(k, k) in SELL_LABELS


def sorted_by_category(keys: list) -> list:
    """排序：買入型訊號排在前面、賣出型訊號排在後面，各自區塊內維持原順序"""
    return sorted(keys, key=lambda k: is_sell_key(k))


# 供「圖表標記訊號勾選」與「賣出條件1」等混合買/賣類型的選單使用（依分類排序，
# 買入型訊號在前、賣出型訊號在後；下拉選單中間會有一條分隔線區分兩類，見下方CSS/JS）
signal_keys_categorized = sorted_by_category(signal_keys)
sell_option_keys_categorized = sorted_by_category(sell_option_keys)


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
        buy_cooldown = 0
        sell_signals = []
        enable_take_profit = False
        take_profit_pct = 10.0
        stop_loss_pct = 10.0

        if enable_backtest:
            buy_signals = st.multiselect("買入條件 (可複選)", options=buy_option_keys, default=default_buy_keys, format_func=lambda k: signal_labels.get(k, k), key="chart_buy_signals")
            st.caption("此清單僅列出可作為進場依據之訊號，不含賣出型訊號。")
            if buy_signals:
                for i, sig in enumerate(buy_signals):
                    lbl = signal_labels.get(sig, sig)
                    buy_shares_dict[sig] = st.number_input(f"➤ 【{lbl}】買入張數", value=1, min_value=1, step=1, key=f"buy_share_{i}_{sig}")
                st.write("")
                buy_cooldown = st.number_input("同訊號再次買入冷卻期 (交易日)", value=0, min_value=0, step=1, key="chart_buy_cooldown")

                st.write("")
                enable_bias60_filter = st.checkbox("啟用買入限制：60MA乖離率過高不買", value=False, key="chart_enable_bias60_filter")
                max_bias60_buy_pct = st.number_input("買入限制: 60MA乖離率上限 (%)", value=20.0, step=1.0, disabled=not enable_bias60_filter, key="chart_max_bias60_buy_pct")
                enable_min_score_filter = st.checkbox("啟用買入限制：訊號評分低於門檻不買", value=False, key="chart_enable_min_score_filter")
                min_entry_score = st.number_input("買入限制: 最低訊號評分門檻", value=55.0, step=1.0, disabled=not enable_min_score_filter, key="chart_min_entry_score")

            st.markdown("---")
            sell_signals = st.multiselect("賣出條件1 (訊號出場，可複選)", options=sell_option_keys_categorized, default=default_sell_keys, format_func=lambda k: signal_labels.get(k, k), key="chart_sell_signals")
            st.caption("此清單包含賣出型訊號 (綠色標籤)，亦可額外選用反轉型態訊號 (如反向3K反轉) 作為出場依據。")
            enable_take_profit = st.checkbox("啟用賣出條件2（漲幅達標賣出）", value=False, key="chart_enable_take_profit")
            take_profit_pct = st.number_input("賣出條件2: 停利達標 (%)", value=10.0, step=1.0, disabled=not enable_take_profit, key="chart_take_profit_pct")
            st.caption("反向3K反轉：每筆持倉須先獲利超過 10%，才會依該訊號賣出。")

            # 停損預設改為 10.0%
            stop_loss_pct = st.number_input("停損達標 (%) -> 單筆賣出", value=10.0, step=1.0, key="chart_stop_loss_pct")

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

# 2026-08-22 新增：database disk image is malformed 容錯處理。
# 這個錯誤通常發生在 Streamlit Cloud 容器裡——twse_ohlcv.db 被 git pull 更新到新版本，
# 但同資料夾殘留的舊版 -wal/-shm 暫存檔沒有一起被換掉，導致 SQLite 打開時發現主檔案
# 跟暫存檔對不上而回報「損毀」。db_utils.get_connection() 已經會自動偵測並清除這類
# 過期的暫存檔重試一次；這裡是最後一道防線——如果自動修復仍然失敗，給使用者清楚的
# 中文說明與重試按鈕，而不是讓整頁噴出原始 traceback。
try:
    conn = _get_conn(db_path, os.path.getmtime(db_path))
    stock_list_df = db_utils.get_stock_list(conn)
except Exception as e:
    err_msg = str(e).lower()
    if "malformed" in err_msg or "disk image" in err_msg:
        st.error(
            "❌ 資料庫檔案讀取失敗：database disk image is malformed\n\n"
            "這通常代表 Streamlit Cloud 容器裡的 twse_ohlcv.db 跟殘留的暫存檔"
            "（-wal / -shm）對不上，已嘗試自動清除暫存檔並重試一次仍然失敗。\n\n"
            "請先點下方「清除快取並重試」；若還是失敗，代表容器內的檔案本身已損毀，"
            "請到 Streamlit Cloud 的 Manage app 執行 Reboot app，讓它重新從 GitHub "
            "clone 一份乾淨的 twse_ohlcv.db。"
        )
        if st.button("清除快取並重試"):
            _get_conn.clear()
            st.rerun()
        st.stop()
    else:
        raise
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
    with st.expander("交易紀錄編輯", expanded=False):
        if st.button("📝 開啟交易紀錄編輯器", use_container_width=True, key="open_journal_editor_btn"):
            # 只有目前讀取的是預設路徑 (對應 GitHub repo 的 Trading Journal.xlsx) 時，
            # 才嘗試先拉 GitHub 最新版蓋過本機那份，確保編輯器一開就是最新內容，
            # 不會編輯到本機殘留的舊版又存回去、覆蓋掉別處已經更新過的紀錄。
            # 若使用者是讀取自己上傳的檔案，維持原樣不動 (不覆寫使用者上傳的內容)。
            active_journal_path = st.session_state.get("journal_path", DEFAULT_JOURNAL_PATH)
            if active_journal_path == DEFAULT_JOURNAL_PATH:
                with st.spinner("正在從 GitHub 抓取最新的交易紀錄..."):
                    github_bytes = fetch_journal_bytes_from_github()
                if github_bytes:
                    try:
                        with open(DEFAULT_JOURNAL_PATH, "wb") as f:
                            f.write(github_bytes)
                        st.session_state.journal_log_df = load_journal_log(DEFAULT_JOURNAL_PATH)
                    except Exception:
                        pass  # 抓到了但寫入本機失敗，靜默退回使用本機現有版本
                # 抓不到 (github_bytes 為 None，例如 repo 裡還沒有這個檔案、或網路問題)
                # 就靜默退回目前本機已載入的版本，使用者仍可正常編輯，不會比原本行為更差。
            journal_editor_dialog()

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
GITHUB_TRACKING_OWNER = st.secrets.get("GITHUB_OWNER", "hueiling0310")
GITHUB_TRACKING_REPO = st.secrets.get("GITHUB_REPO", "stock-scanner")
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
# RUN: 讀取資料 + 執行訊號判斷 + 執行回測
# --------------------------------------------------------------------------
if run_clicked:
    try:
        from signal_module.base import SignalContext

        # 資料抓取範圍需同時涵蓋「圖表顯示範圍(scan_start_date~scan_end_date)」與「單日驗證掃描日期(scan_target_date)」，
        # 避免掃描日期落在圖表範圍之外時，被誤判為「掃描日不在資料範圍內」。
        # 注意：fetch_end_str 只用來決定「抓取多少資料」，圖表實際顯示範圍仍以使用者設定的 scan_end_date 為準 (見下方 chart_end_str)。
        effective_start = min(pd.to_datetime(scan_start_date), pd.to_datetime(scan_target_date))
        effective_end = max(pd.to_datetime(scan_end_date), pd.to_datetime(scan_target_date))
        buffer_start = (effective_start - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        fetch_end_str = effective_end.strftime("%Y-%m-%d")
        scan_start_str = pd.to_datetime(scan_start_date).strftime("%Y-%m-%d")
        chart_end_str = pd.to_datetime(scan_end_date).strftime("%Y-%m-%d")
        scan_target_str = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")

        # 若更新後快取被清空，這裡會重新建立連線
        conn = _get_conn(db_path, os.path.getmtime(db_path))

        full_df = db_utils.get_stock_ohlcv(conn, stock_code, buffer_start, fetch_end_str)
        stock_name = db_utils.get_stock_name(conn, stock_code)

        if full_df.empty:
            st.error("查無此股票在該期間的資料")
        elif scan_target_str not in full_df.index:
            st.error(f"訊號掃描日期 {scan_target_str} 不在資料範圍內 (可能為非交易日，或該股票在此日期尚無資料)")
        else:
            full_df = indicators.add_indicators(full_df)
            full_df["VolMA5"] = full_df["Volume"].rolling(5, min_periods=1).mean()
            if "VolMA10" not in full_df.columns:
                full_df["VolMA10"] = full_df["Volume"].rolling(10, min_periods=1).mean()

            # 1. 執行單日訊號驗證
            results = {}
            for key in selected_signal_keys:
                entry = st.session_state.signal_registry[key]
                ctx = SignalContext(code=stock_code, name=stock_name, df=full_df, scan_date=scan_target_str)
                try:
                    results[key] = entry["func"](ctx)
                except Exception as e:
                    from signal_module.base import SignalResult
                    results[key] = SignalResult(hit=False, detail=f"執行錯誤: {e}")

            # 圖表顯示範圍維持使用者在「日期設定」設定的 scan_start_date ~ scan_end_date，
            # 不會因為單日驗證掃描日期(scan_target_date)超出此範圍而被意外撐大
            display_df = full_df[(full_df.index >= scan_start_str) & (full_df.index <= chart_end_str)]
        
            # 2. 執行區間模擬回測邏輯
            trades = []
            active_positions = []
            last_buy_idx = {}
            max_capital_used = 0
            # 收集回測迴圈裡「訊號模組執行失敗」的紀錄 (訊號名稱/日期/錯誤訊息)，
            # 之前是 except: pass 整個吞掉、使用者完全看不到；現在改成不中斷回測，
            # 但把每一筆失敗都記下來，跑完後在「模擬回測績效」上方集中顯示一段警告。
            signal_error_log = []

            # 效能最佳化 (2026-08-16)：原本迴圈裡「每一天」都對 full_df 做 .loc[d, col]
            # 標籤查找 (Close、Bias60)，label-based .loc 在迴圈內重複呼叫的開銷不小；
            # 這裡改成迴圈開始前先把需要的欄位一次性轉成 {日期: 值} 的 dict、以及
            # {日期: 在full_df裡的位置} 的 dict，迴圈內全部改用 dict 查找。純粹是查找方式
            # 改變，不影響任何計算邏輯或回測結果 (不動訊號模組本身的執行方式)。
            full_df_close = full_df["Close"].to_dict()
            full_df_bias60 = full_df["Bias60"].to_dict() if "Bias60" in full_df.columns else {}
            full_df_pos = {date: pos for pos, date in enumerate(full_df.index)}

            if enable_backtest:
                for i, d in enumerate(display_df.index):
                    current_price = full_df_close[d]

                    # --- 賣出檢查 ---
                    if len(active_positions) > 0:
                        triggered_sell_signals = []
                        if sell_signals:
                            ctx_sell = SignalContext(code=stock_code, name=stock_name, df=full_df, scan_date=d)
                            for sig in sell_signals:
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

                            if profit_pct <= -stop_loss_pct:
                                sell_reason = f"停損出場 ({profit_pct:.1f}%)"
                            elif enable_take_profit and profit_pct >= take_profit_pct:
                                sell_reason = f"停利達標 ({profit_pct:.1f}%)"
                            elif eligible_signal is not None:
                                sell_reason = f"訊號出場 ({signal_labels.get(eligible_signal, eligible_signal)})"

                            if sell_reason:
                                pnl = (current_price - pos["buy_price"]) * pos["shares"] * 1000
                                trades.append({
                                    "買入日期": pos["buy_date"], "買入理由": pos["signal_label"],
                                    "進場評分": pos.get("entry_score"), "評分等級": pos.get("entry_grade"),
                                    "同日觸發訊號": pos.get("entry_signals", ""),
                                    "買入價": pos["buy_price"], "張數": pos["shares"],
                                    "賣出日期": d, "賣出理由": sell_reason, "賣出價": current_price,
                                    "損益(元)": round(pnl), "報酬率(%)": round(profit_pct, 2)
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
                                ctx_buy = SignalContext(code=stock_code, name=stock_name, df=full_df, scan_date=d)
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
                                # 2026-08-16 修正：build_score_input() 內部用第三個參數當「在
                                # full_df 裡的位置」去抓前1天/前20天的收盤價算漲跌幅%與波動率，
                                # 但這裡原本傳的是 i (在 display_df 裡的位置)。full_df 為了讓
                                # 指標(MA/KD等)在區間起點就有值，會比 display_df 往前多抓90天
                                # 緩衝資料，兩邊位置對不上，導致算出來的評分其實是抓到緩衝期
                                # (跟掃描區間無關的更早日期)的資料，評分本身是錯的，連帶「訊號
                                # 評分低於門檻不買」這個過濾器也是用錯的數字在判斷。改用
                                # full_df_pos[d] 取得 d 在 full_df 裡的正確位置。
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

            st.session_state.run_results = (
                display_df, results, stock_code, stock_name, scan_target_str,
                trades, active_positions, enable_backtest, max_capital_used, signal_error_log,
            )
    except Exception as e:
        st.exception(e)


# --------------------------------------------------------------------------
# 圖表繪製
# --------------------------------------------------------------------------
chart_placeholder = st.container()
with chart_placeholder:
    if st.session_state.run_results is not None:
        display_df, results, code, name, scan_target_str, trades, active_positions, enable_backtest, max_capital_used, signal_error_log = st.session_state.run_results

        _cur_target_str = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")
        if code != stock_code or scan_target_str != _cur_target_str:
            st.warning(
                f"⚠️ 目前顯示的是「{code} / 掃描日期 {scan_target_str}」按下 RUN 當下的結果，"
                f"與你目前選擇的「{stock_code} / {_cur_target_str}」不同，請重新按下 RUN 以更新圖表與訊號結果。"
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

        color_idx = 0
        for key, res in results.items():
            # trendline_breakout / asc_trendline_breakdown 的 marks 是
            # (tier_key, anchor1_date, anchor2_date, tier_hit) + ("scan", date) 的特殊結構，
            # 不是單純日期列表，下面通用的「單日訊號標記」邏輯無法處理，改用各自專屬的
            # 繪圖區塊 (見下方)，這裡先跳過避免跑進通用邏輯出錯。
            if key in (TREND_SIGNAL_KEY, ASC_TREND_SIGNAL_KEY): continue
            if not res.hit or not res.marks: continue
            
            label = st.session_state.signal_registry[key]["label"]
            
            # 若為賣出訊號，字在K線上方(ay負值)往下指；買入訊號，字在K線下方(ay正值)往上指
            if label in SELL_LABELS:
                color = "#27ae60" # 綠色
                y_col = "High"
                ay_dir = -1
            else:
                color = MARK_COLORS[color_idx % len(MARK_COLORS)]
                color_idx += 1
                y_col = "Low"
                ay_dir = 1
                
            last_idx = len(res.marks) - 1
            for i, d in enumerate(res.marks):
                if d not in display_df.index: continue
                is_trigger = (i == last_idx)
                
                ay_val = (40 if is_trigger else 22) * ay_dir
                
                fig.add_annotation(
                    x=d, y=display_df.loc[d, y_col], text=label if is_trigger else "", showarrow=True,
                    arrowhead=2 if is_trigger else 1, arrowcolor=color, font=dict(color=color, size=12),
                    ax=0, ay=ay_val, row=1, col=1
                )

        display_dates = display_df.index.tolist()
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
                        ax=0, ay=30, row=1, col=1,
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
                        ax=0, ay=-30, row=1, col=1,
                    )

        if enable_backtest:
            buy_markers = {}
            sell_markers = {}
            for t in trades:
                if t["買入日期"] not in buy_markers: buy_markers[t["買入日期"]] = t["買入價"]
                if t["賣出日期"] not in sell_markers: sell_markers[t["賣出日期"]] = t["賣出價"]
            for pos in active_positions:
                if pos["buy_date"] not in buy_markers: buy_markers[pos["buy_date"]] = pos["buy_price"]
            for d, p in buy_markers.items():
                fig.add_annotation(x=d, y=p, text="B", showarrow=True, arrowhead=1, arrowcolor="#e74c3c", font=dict(color="white", size=10), bgcolor="#e74c3c", ax=0, ay=30, row=1, col=1)
            for d, p in sell_markers.items():
                fig.add_annotation(x=d, y=p, text="S", showarrow=True, arrowhead=1, arrowcolor="#2ecc71", font=dict(color="white", size=10), bgcolor="#2ecc71", ax=0, ay=-30, row=1, col=1)

        # ============ 交易紀錄 (Trading Journal) 標記：實際手動交易的買/賣點 ============
        # 用菱形符號區分於上方模擬回測的 B/S 方框標記，避免混淆「模擬」與「實際」交易
        # 顏色改用藍色系；位移距離加大，避免跟模擬買賣訊號(B/S方框)的位置重疊
        journal_df_all = st.session_state.get("journal_log_df", pd.DataFrame(columns=JOURNAL_COLUMNS))
        journal_for_stock = journal_df_all[journal_df_all["股票代碼"].astype(str) == str(code)] if not journal_df_all.empty else journal_df_all
        if not journal_for_stock.empty:
            for _, jr in journal_for_stock.iterrows():
                jd = jr["交易日期"]
                if jd not in display_df.index:
                    continue
                jp = jr["進出場價格"]
                method = jr["進出手法"] if pd.notna(jr["進出手法"]) else ""
                shares = jr["買賣張數"] if pd.notna(jr["買賣張數"]) else None
                shares_text = f"{shares:g} 張" if shares is not None else "未填"
                action = classify_journal_action(method)
                is_sell = action == "賣出"
                color = "#1565c0" if not is_sell else "#00acc1"  # 買入: 深藍 / 賣出: 藍綠(同屬藍色系但可區分方向)
                fig.add_trace(go.Scatter(
                    x=[jd], y=[jp], mode="markers", marker=dict(symbol="diamond", size=11, color=color, line=dict(color="white", width=1)),
                    name=f"交易紀錄({action})",
                    hovertemplate=f"交易紀錄 [{action}]<br>日期: {jd}<br>價格: {jp}<br>手法: {method}<br>張數: {shares_text}<br>Note: {jr['Note'] if pd.notna(jr['Note']) else ''}<extra></extra>",
                    showlegend=False,
                ), row=1, col=1)
                fig.add_annotation(
                    x=jd, y=jp, text=method, showarrow=True, arrowhead=1, arrowcolor=color,
                    font=dict(color=color, size=10), ax=0, ay=(75 if not is_sell else -75), row=1, col=1,
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
    display_df, results, code, name, scan_target_str, trades, active_positions, enable_backtest, max_capital_used, signal_error_log = st.session_state.run_results

    _cur_target_str2 = pd.to_datetime(scan_target_date).strftime("%Y-%m-%d")
    if code != stock_code or scan_target_str != _cur_target_str2:
        st.info(f"⚠️ 以下為「{code} / {scan_target_str}」的舊結果，請按 RUN 更新為目前選擇的「{stock_code} / {_cur_target_str2}」。")

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

    if journal_for_stock.empty:
        st.info(f"目前交易紀錄中沒有 {code} 的資料，無法進行比對。可於側邊欄「讀取 交易紀錄」上傳/編輯。")
    elif not enable_backtest or not trades:
        st.info("請先啟用模擬回測功能並產生已平倉交易，才能與交易紀錄比對。")
    else:
        # 交易紀錄的實際買賣配對：假設同一檔股票的紀錄依日期排序後，買入/賣出交替出現 (FIFO 配對)，
        # 依「進出手法」判斷買賣方向：優先比對是否為 SELL_LABELS 清單內的賣出型訊號，其餘文字才用是否含「賣」字判斷。
        # 若你的紀錄慣例不同，這裡的配對邏輯可能需要調整。
        # 尚未有對應賣出紀錄的買入，會以「最新收盤價」計算目前報酬率，並標註為「未平倉」一併納入比對。
        journal_for_stock["交易日期_dt"] = pd.to_datetime(journal_for_stock["交易日期"])
        journal_for_stock = journal_for_stock.sort_values("交易日期_dt")
        journal_for_stock["動作"] = journal_for_stock["進出手法"].apply(classify_journal_action)

        latest_price = float(display_df.iloc[-1]["Close"]) if not display_df.empty else None

        def _journal_shares(row):
            v = row.get("買賣張數")
            try:
                v = float(v)
                if v > 0:
                    return v
            except Exception:
                pass
            return 1.0  # 未填寫張數時，比對預設以 1 張計算

        journal_trades = []
        pending_buy = None
        for _, jr in journal_for_stock.iterrows():
            if jr["動作"] == "買入":
                if pending_buy is not None and latest_price is not None:
                    # 前一筆買入尚未有對應賣出紀錄，視為未平倉先納入比對
                    buy_price = float(pending_buy["進出場價格"])
                    pnl_pct = (latest_price - buy_price) / buy_price * 100 if buy_price else 0
                    journal_trades.append({
                        "買入日期": pending_buy["交易日期"], "買入手法": pending_buy["進出手法"],
                        "買入張數": _journal_shares(pending_buy), "買入價": buy_price,
                        "賣出日期": "-", "賣出手法": "-", "賣出張數": None, "賣出價": None,
                        "狀態": "未平倉", "報酬率(%)": round(pnl_pct, 2),
                    })
                pending_buy = jr
            elif jr["動作"] == "賣出" and pending_buy is not None:
                buy_price = float(pending_buy["進出場價格"])
                sell_price = float(jr["進出場價格"])
                pnl_pct = (sell_price - buy_price) / buy_price * 100 if buy_price else 0
                journal_trades.append({
                    "買入日期": pending_buy["交易日期"], "買入手法": pending_buy["進出手法"],
                    "買入張數": _journal_shares(pending_buy), "買入價": buy_price,
                    "賣出日期": jr["交易日期"], "賣出手法": jr["進出手法"],
                    "賣出張數": _journal_shares(jr), "賣出價": sell_price,
                    "狀態": "已平倉", "報酬率(%)": round(pnl_pct, 2),
                })
                pending_buy = None

        # 迴圈跑完後，若還有尚未配對到賣出的買入(通常是最後一筆)，同樣視為未平倉納入
        if pending_buy is not None and latest_price is not None:
            buy_price = float(pending_buy["進出場價格"])
            pnl_pct = (latest_price - buy_price) / buy_price * 100 if buy_price else 0
            journal_trades.append({
                "買入日期": pending_buy["交易日期"], "買入手法": pending_buy["進出手法"],
                "買入張數": _journal_shares(pending_buy), "買入價": buy_price,
                "賣出日期": "-", "賣出手法": "-", "賣出張數": None, "賣出價": None,
                "狀態": "未平倉", "報酬率(%)": round(pnl_pct, 2),
            })

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
