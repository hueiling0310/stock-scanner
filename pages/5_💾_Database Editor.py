import streamlit as st
import pandas as pd
import requests
import sqlite3
import time
import urllib3
import io
import os
import tempfile
from datetime import datetime, timedelta
from typing import Any

import benchmark_utils

# 停用忽略 SSL 所產生的警告訊息
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# 1. 常數與設定
# ==========================================
TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
# 櫃買中心全市場收盤行情端點
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}

# 欄位名稱設定
SECURITY_CODE = "證券代號"
SECURITY_NAME = "證券名稱"
OPEN_PRICE = "開盤價"
HIGH_PRICE = "最高價"
LOW_PRICE = "最低價"
CLOSE_PRICE = "收盤價"
VOLUME = "成交股數"

DB_NAME = "Data_ohlcv.db"

# ==========================================
# 2. 輔助函數
# ==========================================
def number(value: Any) -> float:
    """清理文字並轉換為浮點數"""
    text = str(value).replace(",", "").strip()
    
    # 把櫃買中心愛用的各種長度減號都納入排除名單
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}:
        return 0.0
        
    # 加上 try-except 作為終極防線，遇到無法轉換的怪字串直接當作 0 處理
    try:
        return float(text)
    except ValueError:
        return 0.0

def get_json(url: str, params: dict[str, str] | None = None) -> Any:
    """發送請求並驗證回傳格式 (忽略 SSL 憑證檢查)"""
    response = requests.get(url, params=params, headers=HEADERS, timeout=30, verify=False)
    response.raise_for_status()
    if "json" not in response.headers.get("Content-Type", "").lower():
        raise RuntimeError(f"Expected JSON from {url}; got {response.text[:160]!r}")
    return response.json()

def check_status(payload: dict[str, Any], source: str) -> bool:
    """檢查 API 回傳狀態，若無資料回傳 False"""
    status = str(payload.get("stat", ""))
    if status in {"", "OK"}:
        return True
    if "沒有符合條件的資料" in status:
        return False
    raise RuntimeError(f"{source} status: {status}")

def unique_columns(fields: list[Any]) -> list[str]:
    """確保欄位名稱不重複"""
    seen: dict[str, int] = {}
    result: list[str] = []
    for field in fields:
        base = str(field).strip()
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result

def table_to_frame(table: dict[str, Any]) -> pd.DataFrame:
    """將 JSON 表格轉換為 DataFrame"""
    columns = unique_columns(table.get("fields", []))
    data = table.get("data", [])
    return pd.DataFrame(data, columns=columns) if columns and data else pd.DataFrame()

def find_table(payload: dict[str, Any], required: set[str], min_columns: int = 0) -> pd.DataFrame:
    """在 JSON 結構中尋找包含指定欄位的表格"""
    for table in payload.get("tables", []):
        frame = table_to_frame(table)
        if required.issubset(frame.columns) and len(frame.columns) >= min_columns:
            return frame
    raise RuntimeError(f"Required table not found: {sorted(required)}")

# ==========================================
# 3. 核心資料處理
# ==========================================
def fetch_twse_daily(report_date: str, return_payload: bool = False):
    """
    抓取單日 [上市] OHLCV 資料。
    return_payload=True 時改回傳 (df, payload) tuple——2026-08-17 新增：
    讓呼叫端（主流程迴圈）可以把這裡已經打過一次的官方 MI_INDEX payload
    直接轉給 benchmark_utils.update_benchmark_daily() 解析大盤指數，
    不用為了大盤又對同一個官方端點多打一次一模一樣的請求，
    降低被限流(429)的風險，也讓大盤同步更不容易失敗。
    """
    payload = None
    try:
        payload = get_json(TWSE_URL, {"date": report_date, "type": "ALLBUT0999", "response": "json"})
        if not check_status(payload, "prices"):
            return (pd.DataFrame(), payload) if return_payload else pd.DataFrame()

        raw_df = find_table(payload, {SECURITY_CODE, CLOSE_PRICE})

        target_cols = [SECURITY_CODE, SECURITY_NAME, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, VOLUME]
        df = raw_df[target_cols].copy()

        df.columns = ["SecurityCode", "SecurityName", "Open", "High", "Low", "Close", "Volume"]
        df["SecurityCode"] = df["SecurityCode"].astype(str).str.strip()
        df["SecurityName"] = df["SecurityName"].astype(str).str.strip()

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = df[col].map(number)

        df.insert(0, "Date", pd.to_datetime(report_date, format="%Y%m%d").date())
        df.insert(1, "Market", "上市")

        result_df = df[df["Close"] > 0].drop_duplicates("SecurityCode")
        return (result_df, payload) if return_payload else result_df
    except Exception as e:
        st.warning(f"上市資料解析失敗 ({report_date}): {e}")
        return (pd.DataFrame(), payload) if return_payload else pd.DataFrame()

def fetch_tpex_daily(report_date: str) -> pd.DataFrame:
    """抓取單日 [上櫃] OHLCV 資料"""
    dt = datetime.strptime(report_date, "%Y%m%d")
    roc_date = f"{dt.year - 1911}/{dt.strftime('%m/%d')}"
    
    try:
        # 使用 se=EW (所有證券-不含權證)
        payload = get_json(TPEX_URL, {"l": "zh-tw", "d": roc_date, "se": "EW", "o": "json"})
        
        # 櫃買中心全市場行情資料主要放在 "tables" 中，相容舊版 "aaData"
        data_list = []
        if "tables" in payload and payload["tables"]:
            for table in payload["tables"]:
                data_list.extend(table.get("data", []))
        elif "aaData" in payload:
            data_list = payload["aaData"]
            
        if not data_list:
            st.warning(f"上櫃資料回傳為空 ({report_date})，可能是假日或尚未結算。")
            return pd.DataFrame()
            
        rows = []
        for row in data_list:
            # 確保欄位數量足夠
            if len(row) >= 8:
                code = str(row[0]).strip()
                if len(code) > 6:  # 排除過長的權證代碼
                    continue
                    
                close_p = number(row[2])
                if close_p > 0:
                    rows.append({
                        "Date": dt.date(),
                        "Market": "上櫃",
                        "SecurityCode": code,
                        "SecurityName": str(row[1]).strip(),
                        "Open": number(row[4]),
                        "High": number(row[5]),
                        "Low": number(row[6]),
                        "Close": close_p,
                        "Volume": number(row[7]) # ✅ 成交股數正確對應 Col_7
                    })
        
        df = pd.DataFrame(rows)
        return df.drop_duplicates("SecurityCode") if not df.empty else df
        
    except Exception as e:
        st.warning(f"上櫃資料解析失敗 ({report_date}): {e}")
        return pd.DataFrame()

def save_to_database(df: pd.DataFrame):
    """將資料存入 SQLite 資料庫，並自動避免重複日期"""
    if df.empty:
        return
    conn = sqlite3.connect(DB_NAME)
    try:
        # 取得這次要寫入的資料中有哪些「日期」和「市場」
        dates = df["Date"].unique()
        markets = df["Market"].unique()
        
        # 如果資料庫已經有這些日期的資料，先將它們刪除，實現自動去重/覆蓋更新
        for d in dates:
            for m in markets:
                try:
                    conn.execute(
                        "DELETE FROM ohlcv_data WHERE Date = ? AND Market = ?", 
                        (str(d), m)
                    )
                except sqlite3.OperationalError:
                    pass  # 表尚未建立，第一次寫入時屬正常情況，直接略過刪除步驟
        conn.commit()
        
        # 將乾淨的新資料寫入，若表格不存在 pandas 會自動建立
        df.to_sql("ohlcv_data", conn, if_exists="append", index=False)
    finally:
        conn.close()

def merge_uploaded_dbs_to_db_bytes(uploaded_files: list) -> tuple[bytes | None, int, int, list[str]]:
    """將多個上傳的 .db 檔案整合、去重後，輸出成一個新的 .db 檔案（位元組資料）"""
    tmp_path = None
    logs: list[str] = []
    files_ok = 0
    total_rows = 0
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)

        conn = sqlite3.connect(tmp_path)
        try:
            for f in uploaded_files:
                try:
                    df = read_ohlcv_table_from_db_bytes(f.getvalue())
                    if df.empty:
                        logs.append(f"⏸️ {f.name}: 資料為空，略過")
                        continue

                    df["Date"] = pd.to_datetime(df["Date"]).dt.date.astype(str)

                    # 依 Date + Market 去重覆蓋（若表尚不存在則略過刪除步驟）
                    dates = df["Date"].unique()
                    markets = df["Market"].unique()
                    for d in dates:
                        for m in markets:
                            try:
                                conn.execute(
                                    "DELETE FROM ohlcv_data WHERE Date = ? AND Market = ?",
                                    (str(d), m),
                                )
                            except sqlite3.OperationalError:
                                pass  # 表尚未建立，第一次寫入時屬正常情況
                    conn.commit()

                    df.to_sql("ohlcv_data", conn, if_exists="append", index=False)
                    files_ok += 1
                    total_rows += len(df)
                    logs.append(f"✅ {f.name}: 已整合 {len(df)} 筆資料")
                except Exception as e:
                    logs.append(f"❌ {f.name}: 整合失敗 - {e}")
        finally:
            conn.close()

        if files_ok == 0:
            return None, files_ok, total_rows, logs

        with open(tmp_path, "rb") as fh:
            db_bytes = fh.read()
        return db_bytes, files_ok, total_rows, logs
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

def dataframe_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "台股歷史資料") -> bytes:
    """將 DataFrame 轉換為 Excel 二進位資料"""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    return output.getvalue()

def read_ohlcv_table_from_db_bytes(db_bytes: bytes) -> pd.DataFrame:
    """將上傳的 .db 檔案位元組資料讀取為 DataFrame (讀取 ohlcv_data 表)"""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp.write(db_bytes)
            tmp_path = tmp.name
        conn = sqlite3.connect(tmp_path)
        try:
            # 先確認資料表是否存在
            check = pd.read_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ohlcv_data'", conn
            )
            if check.empty:
                raise RuntimeError("此 .db 檔案內找不到 ohlcv_data 資料表")
            df = pd.read_sql(
                "SELECT * FROM ohlcv_data ORDER BY Date DESC, Market ASC, SecurityCode ASC", conn
            )
        finally:
            conn.close()  # 一定要在 os.unlink 前關閉連線，否則 Windows 上會出現 WinError 32
        return df
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ==========================================
# 4. Streamlit UI 介面設計
# ==========================================
st.set_page_config(page_title="TWSE & TPEx OHLCV 抓取工具", layout="wide")
st.title("📈 台灣台股 (上市+上櫃) OHLCV 抓取與入庫系統")

st.markdown("""
這個工具會向證交所與櫃買中心 API 請求每日收盤行情，提取**所有個股的開、高、低、收與成交量**，
並自動儲存到本地端的 `data_ohlcv.db` 資料庫中。
""")

with st.sidebar:
    st.header("⚙️ 參數設定")
    start_date = st.date_input("開始日期", datetime.today() - timedelta(days=7))
    end_date = st.date_input("結束日期", datetime.today())
    
    if start_date > end_date:
        st.error("錯誤：開始日期不能大於結束日期")
    
    start_btn = st.button("🚀 開始抓取資料", use_container_width=True)

    st.divider()
    
    # ======== Excel 資料匯出區塊（上傳 db 檔轉出）========
    st.header("📁 資料匯出")
    st.markdown("將上傳的 `.db` 檔案轉出成 Excel 檔案。")

    export_db_file = st.file_uploader(
        "選擇要轉出的 .db 檔案",
        type=["db", "sqlite", "sqlite3"],
        accept_multiple_files=False,
        key="export_db_uploader",
    )

    # 建立兩階段按鈕來處理檔案下載 (避免重複點擊時畫面跳動)
    if st.button("1. 準備 Excel 檔案", use_container_width=True, disabled=export_db_file is None):
        with st.spinner("正在讀取上傳的資料庫並轉換中..."):
            try:
                export_df = read_ohlcv_table_from_db_bytes(export_db_file.getvalue())
                if export_df.empty:
                    st.warning("此 .db 檔案內沒有資料！")
                else:
                    st.session_state["excel_bytes"] = dataframe_to_excel_bytes(export_df)
                    st.session_state["excel_name"] = export_db_file.name
                    st.success(f"轉換完成！共 {len(export_df)} 筆資料，請點擊下方按鈕下載。")
            except Exception as e:
                st.error(f"轉換失敗：{e}")

    if "excel_bytes" in st.session_state:
        export_base_name = os.path.splitext(st.session_state.get("excel_name", "台股歷史資料"))[0]
        st.download_button(
            label="2. ⬇️ 下載 Excel 檔案",
            data=st.session_state["excel_bytes"],
            file_name=f"{export_base_name}_{datetime.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    st.divider()

    # ======== 上傳多個 db 檔，整合去重後輸出一個新的 db 檔 ========
    st.header("🧩 多DB整合去重輸出")
    st.markdown("上傳多個 `.db` 備份檔，依 **日期+市場** 自動去重覆蓋，整合成一個新的 `.db` 檔案供下載（**不會**寫入或影響本地的 `data_ohlcv.db`）。")

    merge_files = st.file_uploader(
        "選擇要整合的 .db 檔案（可多選）",
        type=["db", "sqlite", "sqlite3"],
        accept_multiple_files=True,
        key="merge_db_uploader",
    )

    if st.button("1. 🧩 整合並產生 DB 檔案", use_container_width=True, disabled=not merge_files):
        with st.spinner("正在讀取並整合上傳的資料庫..."):
            merged_db_bytes, files_ok, total_rows, merge_logs = merge_uploaded_dbs_to_db_bytes(merge_files)
        st.session_state["merge_logs"] = merge_logs
        if merged_db_bytes:
            st.session_state["merged_db_bytes"] = merged_db_bytes
            st.success(f"整合完成！成功處理 {files_ok} 個檔案，去重後共 {total_rows} 筆資料，請點擊下方按鈕下載。")
        else:
            st.warning("沒有任何檔案成功整合，請檢查檔案內容。")

    if "merge_logs" in st.session_state:
        with st.expander("查看整合紀錄", expanded=False):
            st.code("\n".join(st.session_state["merge_logs"]))

    if "merged_db_bytes" in st.session_state:
        st.download_button(
            label="2. ⬇️ 下載整合後的 DB 檔案",
            data=st.session_state["merged_db_bytes"],
            file_name=f"data_ohlcv_整合_{datetime.today().strftime('%Y%m%d')}.db",
            mime="application/octet-stream",
            use_container_width=True
        )

# ==========================================
# 5. 主程式執行邏輯
# ==========================================
if start_btn and start_date <= end_date:
    progress_bar = st.progress(0)
    status_text = st.empty()
    log_area = st.empty()
    
    current_date = start_date
    total_days = (end_date - start_date).days + 1
    
    all_data_frames = []
    logs = []
    total_benchmark_days = 0

    for i in range(total_days):
        date_str = current_date.strftime("%Y%m%d")
        status_text.text(f"正在處理: {date_str} ({i+1}/{total_days})")

        try:
            # 分別抓取上市與上櫃資料
            # 2026-08-17 修改：改用 return_payload=True 拿回原始 MI_INDEX payload，
            # 下面同步大盤指數時可以直接複用，不用再多打一次官方 API。
            twse_df, mi_payload = fetch_twse_daily(date_str, return_payload=True)
            time.sleep(1.5) # 在兩個請求之間稍微停頓，避免被當成惡意攻擊
            tpex_df = fetch_tpex_daily(date_str)

            daily_df = pd.concat([twse_df, tpex_df], ignore_index=True)

            if not daily_df.empty:
                save_to_database(daily_df)
                all_data_frames.append(daily_df)
                logs.append(f"✅ {date_str}: 成功取得 {len(twse_df)}筆上市 + {len(tpex_df)}筆上櫃資料")
            else:
                logs.append(f"⏸️ {date_str}: 假日或無交易資料，跳過")

            # 2026-08-16 新增：這裡跟 update_db.py 一樣抓官方 TWSE MI_INDEX 端點，
            # 同步把「大盤（加權指數）」這天的收盤值也存進這個工具自己的 DB_NAME，
            # 失敗不擋主流程，只是這天匯出的資料不會有大盤比較欄位可用。
            # 2026-08-17 修改：優先用上面 fetch_twse_daily 已經抓到的 mi_payload 解析，
            # 官方端點這次沒抓到（mi_payload 為 None 或解析不出來）時，
            # update_benchmark_daily 內部才會自動 fallback 改打一次 yfinance。
            try:
                if benchmark_utils.update_benchmark_daily(DB_NAME, date_str, mi_index_payload=mi_payload):
                    total_benchmark_days += 1
                    logs.append(f"　　↳ 大盤指數同步成功")
                else:
                    logs.append(f"　　↳ ⚠️ 大盤指數這天沒抓到（官方端點與 yfinance 備援皆失敗）")
            except Exception as e:
                logs.append(f"　　↳ ⚠️ 大盤指數同步發生例外：{type(e).__name__}: {e}")

        except Exception as e:
            logs.append(f"❌ {date_str}: 發生整體錯誤 - {e}")

        progress_bar.progress((i + 1) / total_days)
        log_area.code("\n".join(logs))

        current_date += timedelta(days=1)

        # 強制暫停 3 秒保護 IP
        if i < total_days - 1:
            time.sleep(3)

    st.success(f"🎉 抓取任務完成！資料已暫存於雲端的 {DB_NAME}（大盤指數同步 {total_benchmark_days}/{total_days} 天）")
    
    # 🌟 新增：提供下載本地端 DB 檔案的按鈕
    if os.path.exists(DB_NAME):
        with open(DB_NAME, "rb") as file:
            st.download_button(
                label="⬇️ 點此下載最新抓取的資料庫 (.db 檔)",
                data=file,
                file_name=f"data_ohlcv_{datetime.today().strftime('%Y%m%d')}.db",
                mime="application/octet-stream",
                use_container_width=True
            )
            
    if all_data_frames:
        st.subheader("📊 資料庫最新入庫預覽")
        combined_df = pd.concat(all_data_frames, ignore_index=True)
        st.dataframe(combined_df, use_container_width=True)
