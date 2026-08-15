# -*- coding: utf-8 -*-
"""
pages/2_📝_股票列表編輯器.py
============================
獨立頁面：股票列表編輯器
1. 讀取 Excel (.xlsx) 或 純文字 (.txt) 股票清單，支援一次上傳多個檔案並合併
2. 去重化（依代碼）
3. 60MA 篩選開關：預設關閉，關閉時完全不會打 API；打開後才會逐檔抓 yfinance 近3個月
   日K原始資料、自己算 60MA，篩選出「站上 60MA」或「跌破 60MA」的股票
4. 可編輯表格（勾選要保留的股票），篩選/編輯完成後可下載成 .txt / .xlsx，
   拿去其他頁面（例如「巧妙點掃描」）的「上傳清單檔案」使用

⚠️ 這個頁面刻意設計成「完全獨立、不 import common_fubon.py」，
   這樣不管共用模組更新到哪一版，都不會因為版本沒同步而整頁出錯。
"""

import os
import re
import zipfile
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    import yfinance as yf
except ImportError:
    yf = None

# ===== 頁面基本設定 =====
st.set_page_config(page_title="股票列表編輯器", layout="wide")

CODE_MAP_FILE = "TWstocklistname2.txt"   # 用來把純數字代碼轉換成正確的 .TW / .TWO 後綴
MA60_HISTORY_TTL_SEC = 60 * 60           # 60MA 歷史資料快取 1 小時，避免重複打 API

st.title("📝 股票列表編輯器")
st.caption("上傳 Excel / txt 股票清單 → 合併去重 → （可選）60MA 篩選 → 編輯後匯出，供其他掃描頁面使用。")

tw_now = datetime.now(ZoneInfo("Asia/Taipei"))

# ===== session_state 初始化 =====
if "sle_merged_df" not in st.session_state:
    st.session_state.sle_merged_df = None
if "sle_ma60_applied" not in st.session_state:
    st.session_state.sle_ma60_applied = False


# ===== 代碼後綴解析工具（自成一格，不依賴其他檔案）=====
@st.cache_data(ttl=86400)
def load_code_to_ticker_map(file_path: str = CODE_MAP_FILE) -> dict:
    """從『代碼<TAB/空白>名稱』格式的清單檔載入『純數字代碼 -> 完整代碼(含.TW/.TWO)』對照表。"""
    mapping = {}
    if not os.path.exists(file_path):
        return mapping
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\u3000", "")
            if not line:
                continue
            parts = re.split(r"[\t]+", line) if "\t" in line else line.split(None, 1)
            if not parts:
                continue
            ticker = parts[0].strip().upper()
            if "." in ticker:
                mapping[ticker.split(".")[0]] = ticker
    return mapping

@st.cache_data(ttl=86400)
def load_ticker_to_name_map(file_path: str = CODE_MAP_FILE) -> dict:
    """從清單檔載入『代碼 -> 股票名稱』對照表，用於畫面顯示補齊名稱。"""
    mapping = {}
    if not os.path.exists(file_path):
        return mapping
    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\u3000", " ")
            if not line:
                continue
            parts = re.split(r"[\t ]+", line, maxsplit=1)
            if len(parts) >= 2:
                ticker = parts[0].strip().upper()
                name = parts[1].strip()
                mapping[ticker] = name
                if "." in ticker:
                    mapping[ticker.split(".")[0]] = name
    return mapping

def normalize_symbol_quick(input_text: str):
    s = str(input_text).strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")):
            return f"{s}.TWO"
        return f"{s}.TW"
    return s

def resolve_ticker_suffix(raw_code, code_map: dict = None) -> str:
    """已經帶明確後綴就直接使用；純數字則優先查對照表，查不到才退回猜測 .TW/.TWO。"""
    code_map = code_map or {}
    raw = str(raw_code).strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    if raw in code_map:
        return code_map[raw]
    return normalize_symbol_quick(raw) or raw


# ===== 檔案解析工具 =====
def parse_excel_file(file_obj, code_map: dict, name_map: dict) -> pd.DataFrame:
    """解析 Excel 清單，嘗試辨識常見欄位名稱（代碼／商品／名稱...）"""
    df = pd.read_excel(file_obj)
    df.columns = [str(c).strip() for c in df.columns]

    code_col = next((c for c in ["代碼", "股票代碼", "代號", "Code", "code"] if c in df.columns), None)
    name_col = next((c for c in ["商品", "股票名稱", "名稱", "商品名稱", "Name", "name"] if c in df.columns), None)
    if code_col is None:
        raise ValueError("Excel 檔案裡找不到「代碼」欄位（支援：代碼／股票代碼／代號／Code）")

    out_rows = []
    for _, r in df.iterrows():
        raw_code = str(r[code_col]).strip()
        if raw_code.endswith(".0"):  # pandas 把整數欄讀成 float 常見的尾巴
            raw_code = raw_code[:-2]
        if not raw_code or raw_code.lower() == "nan":
            continue
        full_ticker = resolve_ticker_suffix(raw_code, code_map)
        
        # 查表補齊股票名稱
        name = str(r[name_col]).strip() if name_col else ""
        if not name:
            name = name_map.get(full_ticker, name_map.get(str(raw_code).split(".")[0], ""))
            
        extra = {}
        for c in ["成交", "漲幅%", "總量", "昨收"]:
            if c in df.columns and pd.notna(r.get(c)):
                extra[c] = r[c]
        out_rows.append({"代碼": full_ticker, "股票名稱": name, **extra})
    return pd.DataFrame(out_rows)


def parse_txt_file(file_obj, code_map: dict, name_map: dict) -> pd.DataFrame:
    content = file_obj.read().decode("utf-8-sig", errors="ignore")
    out_rows = []
    for raw_line in content.splitlines():
        line = raw_line.strip().replace("\u3000", "")
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        raw_code = parts[0].strip()
        full_ticker = resolve_ticker_suffix(raw_code, code_map)
        
        # 查表補齊股票名稱
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            name = name_map.get(full_ticker, name_map.get(str(raw_code).split(".")[0], ""))
            
        out_rows.append({"代碼": full_ticker, "股票名稱": name})
    return pd.DataFrame(out_rows)


# ===== zip 資料夾展開工具 =====
class ZipMemberFile(BytesIO):
    """把從 zip 內解出來的檔案內容，包成跟 st.file_uploader 回傳的 UploadedFile 一樣
    有 .name 屬性的檔案物件。直接繼承 BytesIO，所以 read/seek/tell 等 pandas、openpyxl
    需要的檔案介面全部原生具備，不用另外補方法。"""
    def __init__(self, name: str, data: bytes):
        super().__init__(data)
        self.name = name


def expand_uploaded_files(files):
    """如果上傳的是 .zip（例如整個資料夾壓縮打包），自動解壓縮並取出裡面所有
    .xlsx / .xls / .txt（不管在幾層子資料夾內都會抓到）；非 zip 的檔案原樣保留。"""
    expanded = []
    zip_errors = []
    for f in files:
        if f.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(f) as zf:
                    for member in zf.namelist():
                        if member.endswith("/"):
                            continue  # 資料夾本身，跳過
                        base_name = member.rsplit("/", 1)[-1]
                        if not base_name or base_name.startswith("__MACOSX") or base_name.startswith("."):
                            continue
                        if base_name.lower().endswith((".xlsx", ".xls", ".txt")):
                            expanded.append(ZipMemberFile(base_name, zf.read(member)))
            except Exception as e:
                zip_errors.append(f"{f.name}: {e}")
        else:
            expanded.append(f)
    for err in zip_errors:
        st.error(f"解壓縮失敗：{err}")
    return expanded


# ===== 上傳區 =====
uploaded_files_raw = st.file_uploader(
    "上傳股票清單檔案（可一次選多個 .xlsx / .txt，或把整個資料夾壓縮成 .zip 一次上傳）",
    type=["xlsx", "xls", "txt", "zip"],
    accept_multiple_files=True,
)
uploaded_files = expand_uploaded_files(uploaded_files_raw) if uploaded_files_raw else []

col_load, col_dedupe = st.columns([1, 1])
with col_load:
    load_btn = st.button("📥 讀取並合併清單", use_container_width=True, disabled=not uploaded_files_raw)
with col_dedupe:
    auto_dedupe = st.checkbox("自動去重化（依代碼，保留第一次出現的資料）", value=True)

if load_btn and uploaded_files:
    code_map = load_code_to_ticker_map(CODE_MAP_FILE)
    name_map = load_ticker_to_name_map(CODE_MAP_FILE)
    frames = []
    parse_errors = []
    for f in uploaded_files:
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                part = parse_excel_file(f, code_map, name_map)
            else:
                part = parse_txt_file(f, code_map, name_map)
            part["來源檔案"] = f.name
            frames.append(part)
        except Exception as e:
            parse_errors.append(f"{f.name}: {e}")

    if parse_errors:
        for err in parse_errors:
            st.error(f"讀取失敗：{err}")

    if frames:
        merged = pd.concat(frames, ignore_index=True)
        merged = merged[merged["代碼"].astype(str).str.strip() != ""]
        total_before = len(merged)

        if auto_dedupe:
            merged = merged.drop_duplicates(subset=["代碼"], keep="first").reset_index(drop=True)
        dup_removed = total_before - len(merged)

        merged.insert(0, "保留", True)
        st.session_state.sle_merged_df = merged
        st.session_state.sle_ma60_applied = False
        st.success(f"已合併 {len(frames)} 個檔案（含 zip 內解出的檔案），共 {total_before} 筆，去重後剩 {len(merged)} 筆（移除 {dup_removed} 筆重複）。")

if st.session_state.sle_merged_df is None:
    st.info("請先上傳清單檔案並按「讀取並合併清單」。")
    st.stop()

# ===== 60MA 篩選（開關預設關閉，關閉時完全不打 API）=====
with st.expander("📈 60MA 篩選（開關預設關閉，開啟才會抓取資料）", expanded=True):
    enable_ma60_filter = st.toggle(
        "啟用 60日均線（60MA）篩選",
        value=False,
        key="sle_enable_ma60",
        help="關閉時完全不會呼叫 yfinance，只做清單合併/去重/手動編輯。開啟後才會抓歷史資料計算 60MA。",
    )
    ma60_direction = st.radio(
        "篩選方向",
        options=["站上 60MA（現價 ≥ 60MA）", "跌破 60MA（現價 ＜ 60MA）"],
        index=0,
        disabled=not enable_ma60_filter,
        key="sle_ma60_direction",
    )
    apply_ma60_btn = st.button(
        "🔄 抓取資料並套用 60MA 篩選",
        disabled=not enable_ma60_filter,
        use_container_width=True,
    )


def normalize_yf_history_df(raw_df):
    """把 yf.download() 回傳的單檔 DataFrame 整理成統一的 Date/Open/High/Low/Close/Volume 格式。"""
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    df = raw_df.copy()
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


@st.cache_data(ttl=MA60_HISTORY_TTL_SEC)
def fetch_3mo_history(symbol: str):
    """單檔抓近 3 個月日K，自己算 60MA 用。回傳 (DataFrame, 錯誤訊息或None)。
    刻意逐檔抓（不用整批 group_by 多檔請求），比較不會被 Yahoo 判定成異常流量而整批擋掉，
    而且可以每一檔各自快取、互不拖累。"""
    if yf is None:
        return pd.DataFrame(), "yfinance 未安裝"
    try:
        raw = yf.download(symbol, period="3mo", interval="1d", auto_adjust=False, progress=False, threads=False)
        df = normalize_yf_history_df(raw)
        if df.empty:
            return df, "回傳資料為空"
        return df, None
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {e}"


if apply_ma60_btn and enable_ma60_filter:
    if yf is None:
        st.warning("⚠️ 尚未安裝 yfinance 套件：pip install yfinance")
    else:
        symbols = tuple(sorted(set(st.session_state.sle_merged_df["代碼"].astype(str))))
        progress_placeholder = st.empty()
        progress_bar = progress_placeholder.progress(0, text=f"抓取近3個月資料中：0/{len(symbols)}")

        results = []
        fetch_errors = {}
        for idx, symbol in enumerate(symbols, start=1):
            progress_bar.progress(idx / len(symbols), text=f"抓取近3個月資料中：{idx}/{len(symbols)}（{symbol}）")
            df, err = fetch_3mo_history(symbol)
            if err:
                fetch_errors[symbol] = err
            if df.empty or len(df) < 60:
                results.append({
                    "代碼": symbol, "現價": None, "60MA": None, "現價/60MA%": None,
                    "60MA狀態": "資料不足" if not err else "抓取失敗",
                })
                continue
            close = pd.to_numeric(df["Close"], errors="coerce")
            ma60 = float(close.tail(60).mean())
            price = float(close.iloc[-1])
            if ma60 <= 0 or pd.isna(ma60):
                results.append({"代碼": symbol, "現價": price, "60MA": None, "現價/60MA%": None, "60MA狀態": "資料異常"})
                continue
            status = "站上60MA" if price >= ma60 else "跌破60MA"
            results.append({
                "代碼": symbol, "現價": round(price, 2), "60MA": round(ma60, 2),
                "現價/60MA%": round(price / ma60 * 100, 1), "60MA狀態": status,
            })
        progress_placeholder.empty()

        fail_count = sum(1 for r in results if r["60MA狀態"] in ("資料不足", "抓取失敗", "資料異常"))
        if fail_count:
            st.warning(f"⚠️ {fail_count} / {len(symbols)} 檔抓取失敗或資料不足（近3個月交易日不到60天，或 Yahoo 暫時限流）。")
        if fetch_errors:
            with st.expander(f"🔧 除錯資訊：{len(fetch_errors)} 檔抓取時發生錯誤（回報問題時可附上）", expanded=(len(fetch_errors) == len(symbols))):
                st.json(dict(list(fetch_errors.items())[:30]))
                if len(fetch_errors) > 30:
                    st.caption(f"...另外還有 {len(fetch_errors) - 30} 檔錯誤未顯示")

        ma60_df = pd.DataFrame(results)
        base_df = st.session_state.sle_merged_df.drop(
            columns=[c for c in ["現價", "60MA", "現價/60MA%", "60MA狀態"] if c in st.session_state.sle_merged_df.columns]
        )
        merged_with_ma60 = base_df.merge(ma60_df, on="代碼", how="left")

        want_above = ma60_direction.startswith("站上")
        target_status = "站上60MA" if want_above else "跌破60MA"
        merged_with_ma60["保留"] = merged_with_ma60["60MA狀態"] == target_status

        st.session_state.sle_merged_df = merged_with_ma60
        st.session_state.sle_ma60_applied = True
        hit_count = int((merged_with_ma60["60MA狀態"] == target_status).sum())
        st.success(f"60MA 篩選完成：符合「{target_status}」的有 {hit_count} 檔（已自動勾選「保留」，可再手動調整）。")

# ===== 可編輯表格 =====
st.markdown("### ✏️ 編輯清單（可勾選「保留」，或直接刪除整列）")
edited_df = st.data_editor(
    st.session_state.sle_merged_df,
    use_container_width=True,
    num_rows="dynamic",
    key="sle_data_editor",
    column_config={
        "保留": st.column_config.CheckboxColumn("保留", help="取消勾選 = 匯出時排除此檔"),
        "代碼": st.column_config.TextColumn("代碼", disabled=True),
    },
)
st.session_state.sle_merged_df = edited_df

final_df = edited_df[edited_df.get("保留", True) == True].drop(columns=["保留"], errors="ignore").reset_index(drop=True)

m1, m2, m3 = st.columns(3)
m1.metric("清單總筆數", len(edited_df))
m2.metric("目前保留（將匯出）", len(final_df))
m3.metric("60MA 篩選狀態", "已套用" if st.session_state.sle_ma60_applied else "未啟用")

st.markdown("### 📋 匯出後清單預覽")
st.dataframe(final_df, use_container_width=True)


# ===== Excel 字型工具（自成一格）=====
def _contains_cjk(text) -> bool:
    if text is None:
        return False
    s = str(text)
    return any(
        ("\u4e00" <= ch <= "\u9fff") or ("\u3400" <= ch <= "\u4dbf") or ("\uf900" <= ch <= "\ufaff")
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
                elif _contains_cjk(cell.value):
                    cell.font = Font(name=chinese_font_name)
                else:
                    cell.font = Font(name=english_font_name)


# ===== 匯出（txt / xlsx）=====
def build_export_txt(df: pd.DataFrame) -> bytes:
    code_map = load_code_to_ticker_map(CODE_MAP_FILE)
    lines = []
    for _, r in df.iterrows():
        raw_code = str(r["代碼"]).strip()
        if not raw_code:
            continue
            
        # 1. 提取純數字部分
        numeric_match = re.search(r'\d+', raw_code)
        if not numeric_match:
            continue # 如果沒有數字就跳過
        num_code = numeric_match.group(0)
        
        # 2. 透過 mapping 加上 .TW 或 .TWO
        full_ticker = resolve_ticker_suffix(num_code, code_map)
        
        lines.append(full_ticker)
        
    # 去重
    unique_lines = []
    seen = set()
    for line in lines:
        if line not in seen:
            unique_lines.append(line)
            seen.add(line)
            
    return "\n".join(unique_lines).encode("utf-8-sig")


def build_export_xlsx(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="股票清單", index=False)
        apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()


export_col1, export_col2 = st.columns(2)
with export_col1:
    st.download_button(
        "下載 .txt（可用於其他頁面「上傳清單檔案」）",
        data=build_export_txt(final_df),
        file_name=f"stocklist_{tw_now.strftime('%Y%m%d_%H%M')}.txt",
        mime="text/plain",
        use_container_width=True,
        disabled=final_df.empty,
    )
with export_col2:
    st.download_button(
        "下載 .xlsx",
        data=build_export_xlsx(final_df),
        file_name=f"stocklist_{tw_now.strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=final_df.empty,
    )

with st.expander("ℹ️ 使用說明"):
    st.markdown(
        f"""
- **多檔合併**：可一次上傳多個 Excel / txt，會自動合併成一份清單。
- **去重化**：依「代碼」去重，預設保留第一次出現的資料，可關閉。
- **60MA 篩選開關**：預設關閉，關閉時完全不會呼叫 yfinance；打開後按「抓取資料並套用」才會逐檔抓取近 3 個月日K原始資料、自己算 60 日均線，
  篩選「站上 60MA」或「跌破 60MA」的股票（近3個月交易日不到 60 天的股票會標記「資料不足」，不列入篩選結果）。
- **手動編輯**：下方表格可直接勾選/取消「保留」，或用列尾的刪除鈕整列刪除、最下方新增空白列手動輸入。
- **匯出**：完成篩選/編輯後，用「代碼<TAB>股票名稱」的格式匯出 .txt，可直接拿到「巧妙點掃描」等頁面的「上傳清單檔案」使用；也可下載 .xlsx 備份。
- 代碼對照表（純數字轉 .TW/.TWO）讀取自 `{CODE_MAP_FILE}`，找不到時才會用猜測（數字開頭 3/6/8 猜 .TWO，其餘猜 .TW）。
- 這個頁面**不依賴 common_fubon.py**，獨立運作，避免共用模組版本沒同步時整頁出錯。
        """
    )
