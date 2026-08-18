"""
benchmark_utils.py
====================
大盤（發行量加權股價指數 / TAIEX）歷史與每日資料的抓取、存取工具。

用途：讓掃描器可以計算「個股 vs 大盤」的比較欄位——大盤MA位置、大盤MA排列、
RS超額報酬%、RS Rating、RS Line 創新高（見 signals/context.py）。

儲存方式：比照個股一樣，寫入 twse_ohlcv.db 的 ohlcv_data 表（跟個股共用同一張表）：
    SecurityCode = "^TWII"，SecurityName = "加權指數"，Market = "大盤"
用獨立的 Market 值把大盤資料跟「上市/上櫃」個股區隔開，
避免被 symbol_to_db_market() 這類「WHERE Market IN ('上市','上櫃')」的個股查詢誤撈到。
下游只需要 Close 欄位就能算 MA5/10/20/60 與 RS 加權報酬%，
若某天只抓得到收盤指數、抓不到真正的開高低，就用 Close 補上 Open/High/Low/Volume=0，
不影響任何下游計算（跟個股 OHLC 完全無關的欄位）。

資料來源優先順序（2026-08-16 五輪確認方向）：
  1. 證交所官方 MI_INDEX 端點（跟 update_db.py / pages/5_💾_Database Editor.py 的
     fetch_twse_daily() 用同一支 API），從回傳的「大盤統計資訊」表格解析
     「發行量加權股價指數」當日收盤值 —— 每日增量更新只需要「這一天」的收盤值。
  2. 官方端點抓不到（該日欄位對不上 / 無交易資料 / 連線失敗）時，改用 yfinance 抓 ^TWII 補值。
  3. 初次建置歷史資料（backfill，需要一次補齊 60~250+ 個交易日才夠算 MA60/RS）
     一律直接用 yfinance 一次抓一大段，比逐日呼叫官方 API 好幾十次有效率很多；
     yfinance 沒裝或抓不到時，才退回「逐日呼叫官方 API」這條路（較慢，僅作最後備援）。
"""
import sqlite3
from datetime import datetime, date, timedelta
from typing import Any, Optional

import pandas as pd
import requests

BENCHMARK_CODE = "^TWII"
BENCHMARK_NAME = "加權指數"
BENCHMARK_MARKET = "大盤"

# 至少要有這麼多筆資料才夠算 MA60 + 4週RS加權報酬（20個交易日) + RS Line 60日新高判斷
MIN_HISTORY_ROWS = 90

TWSE_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}

# 大盤統計資訊表格的欄位名稱，官方偶爾會微調措辭，多列幾種常見寫法做容錯
_INDEX_NAME_COL_CANDIDATES = ["指數", "指數名稱"]
_INDEX_CLOSE_COL_CANDIDATES = ["收盤指數", "指數值", "收盤"]
_TARGET_INDEX_NAME = "發行量加權股價指數"


def _number(value: Any) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def extract_taiex_close_from_mi_index_payload(payload: dict) -> Optional[float]:
    """
    從 MI_INDEX (type=ALLBUT0999) 回傳的 payload 裡，找出「大盤統計資訊」表格中
    「發行量加權股價指數」當日收盤值。找不到就回傳 None，呼叫端會 fallback 改用 yfinance。
    若呼叫端已經因為抓個股資料而打過同一支 API，可以直接把同一份 payload 傳進來，
    不用為了大盤再多打一次官方 API。
    """
    if not payload:
        return None
    for table in payload.get("tables", []):
        fields = [str(f).strip() for f in table.get("fields", [])]
        name_col = next((c for c in _INDEX_NAME_COL_CANDIDATES if c in fields), None)
        close_col = next((c for c in _INDEX_CLOSE_COL_CANDIDATES if c in fields), None)
        if not name_col or not close_col:
            continue
        name_idx = fields.index(name_col)
        close_idx = fields.index(close_col)
        for row in table.get("data", []):
            if len(row) <= max(name_idx, close_idx):
                continue
            if _TARGET_INDEX_NAME in str(row[name_idx]):
                close_val = _number(row[close_idx])
                if close_val > 0:
                    return close_val
    return None


def fetch_taiex_close_from_twse(report_date: str) -> Optional[float]:
    """單日打一次官方 API 取得大盤收盤指數 (report_date 格式 YYYYMMDD)。抓不到回傳 None。"""
    try:
        resp = requests.get(
            TWSE_MI_INDEX_URL,
            params={"date": report_date, "type": "ALLBUT0999", "response": "json"},
            headers=HEADERS, timeout=30, verify=False,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None
    return extract_taiex_close_from_mi_index_payload(payload)


def fetch_taiex_history_yfinance(period: str = "2y") -> pd.DataFrame:
    """
    用 yfinance 抓 ^TWII 歷史日K，回傳欄位 Date/Open/High/Low/Close/Volume。
    只有在「官方端點抓不到」或「初次建置歷史資料 backfill」時才會呼叫。
    yfinance 未安裝或抓取失敗時，回傳空 DataFrame（呼叫端需自行判斷）。
    """
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()
    try:
        df = yf.download(BENCHMARK_CODE, period=period, interval="1d", auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        df["Date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col not in df.columns:
                df[col] = df.get("Close")
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Date", "Close"]).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def save_benchmark_to_db(db_path: str, df: pd.DataFrame) -> int:
    """
    把大盤指數資料寫入 twse_ohlcv.db 的 ohlcv_data 表（跟個股共用同一張表，
    比照 6_Stock simulator.py save_to_database() 的「先刪同一批日期舊資料再寫入」寫法，
    避免重複寫入同一天資料）。回傳實際寫入筆數。
    """
    if df is None or df.empty:
        return 0
    work = df.copy()
    work["Date"] = pd.to_datetime(work["Date"]).dt.date.astype(str)
    for col in ["Open", "High", "Low", "Volume"]:
        if col not in work.columns:
            work[col] = work["Close"]
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(work["Close"])
    work["Close"] = pd.to_numeric(work["Close"], errors="coerce")
    work = work.dropna(subset=["Close"])
    work["Market"] = BENCHMARK_MARKET
    work["SecurityCode"] = BENCHMARK_CODE
    work["SecurityName"] = BENCHMARK_NAME
    work = work[["Date", "Market", "SecurityCode", "SecurityName", "Open", "High", "Low", "Close", "Volume"]]
    work = work.drop_duplicates(subset=["Date"], keep="last")
    if work.empty:
        return 0

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        dates = work["Date"].unique().tolist()
        CHUNK = 500
        for i in range(0, len(dates), CHUNK):
            chunk = dates[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM ohlcv_data WHERE SecurityCode = ? AND Date IN ({placeholders})",
                [BENCHMARK_CODE, *chunk],
            )
        work.to_sql("ohlcv_data", conn, if_exists="append", index=False)
        conn.commit()
    return len(work)


def get_benchmark_ohlcv(db_path: str, end_date_str: str = None) -> pd.DataFrame:
    """
    取得目前資料庫內的大盤歷史 OHLCV（Date 由舊到新排序，Date 欄位為 date 物件）。
    end_date_str (YYYY-MM-DD) 可選，指定時只取這天(不含)以前的資料，比照個股
    download_stock_data_db_history() 的「歷史資料不含今天」慣例。
    """
    import os
    if not os.path.exists(db_path):
        return pd.DataFrame()
    with sqlite3.connect(db_path) as conn:
        q = "SELECT Date, Open, High, Low, Close, Volume FROM ohlcv_data WHERE SecurityCode = ?"
        params = [BENCHMARK_CODE]
        if end_date_str:
            q += " AND Date < ?"
            params.append(end_date_str)
        q += " ORDER BY Date"
        try:
            df = pd.read_sql(q, conn, params=params)
        except Exception:
            return pd.DataFrame()
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    return df.dropna(subset=["Date"]).reset_index(drop=True)


def get_benchmark_row_count(db_path: str) -> int:
    import os
    if not os.path.exists(db_path):
        return 0
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM ohlcv_data WHERE SecurityCode = ?", (BENCHMARK_CODE,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def backfill_benchmark_history(db_path: str, period: str = "2y") -> int:
    """初次建置/補齊大盤歷史資料：直接用 yfinance 一次抓一大段寫入。回傳寫入筆數（0 代表失敗）。"""
    hist = fetch_taiex_history_yfinance(period=period)
    return save_benchmark_to_db(db_path, hist)


def update_benchmark_daily(db_path: str, report_date: str, mi_index_payload: dict = None) -> bool:
    """
    每日增量更新用：report_date 格式 YYYYMMDD。
    若呼叫端已經打過官方 MI_INDEX API（例如 update_db.py 的 fetch_twse_daily 內部），
    可以把同一份 payload 傳進來 (mi_index_payload)，不用為了大盤再多打一次官方 API；
    否則這裡會自己另外打一次。官方管道都拿不到收盤值時，改抓 yfinance 當天那一筆補值。
    回傳是否成功寫入。
    """
    close_val = None
    if mi_index_payload is not None:
        close_val = extract_taiex_close_from_mi_index_payload(mi_index_payload)
    if close_val is None:
        close_val = fetch_taiex_close_from_twse(report_date)

    try:
        target_date = datetime.strptime(report_date, "%Y%m%d").date()
    except ValueError:
        return False

    if close_val is not None:
        row_df = pd.DataFrame([{"Date": target_date, "Close": close_val}])
        return save_benchmark_to_db(db_path, row_df) > 0

    # 官方管道拿不到（例如假日、格式異動），改用 yfinance 補這一天
    yf_hist = fetch_taiex_history_yfinance(period="5d")
    if yf_hist.empty:
        return False
    day_row = yf_hist[yf_hist["Date"] == target_date]
    if day_row.empty:
        return False
    return save_benchmark_to_db(db_path, day_row) > 0


def fetch_taiex_today_yfinance(today_str: str) -> pd.DataFrame:
    """
    2026-08-16 新增：取得「今天」這一筆大盤即時/最新值，用法跟
    common_fubon.py 的 download_stock_data_yfinance_today() 完全對應——
    個股掃描時會另外即時抓「今天」這一筆(不等每日排程更新資料庫)，
    大盤原本只從資料庫讀（等於只有到「昨天」），這裡補上同一種「今天」即時抓法，
    讓「個股 vs 大盤」比較在盤中也能反映當天最新的大盤位置，不會一直停在昨收。
    today_str 格式 YYYY-MM-DD。抓不到（yfinance 未安裝/尚未收盤沒有今天這筆/連線失敗）回傳空 DataFrame。
    """
    hist = fetch_taiex_history_yfinance(period="5d")
    if hist.empty:
        return hist
    try:
        today = pd.to_datetime(today_str).date()
    except Exception:
        return pd.DataFrame()
    return hist[hist["Date"] == today].reset_index(drop=True)


def combine_benchmark_history_and_today(history_df: pd.DataFrame, today_df: pd.DataFrame) -> pd.DataFrame:
    """
    把「資料庫裡的大盤歷史(不含今天)」跟「即時抓到的今天這一筆」合併成掃描器可以直接用的
    完整序列，邏輯跟 common_fubon.py download_stock_data_by_source() 內的 _combine() 一致：
    同一天以「今天即時抓到的」為準（如果有的話），按日期排序後回傳。
    """
    frames = [d for d in [history_df, today_df] if d is not None and not d.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    if "Date" in combined.columns:
        combined = combined.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
    return combined


def ensure_benchmark_history(db_path: str, report_date: str = None) -> bool:
    """
    自我修復用：確保資料庫內的大盤資料「足夠新、足夠多」，供掃描器隨時可以直接使用。
      - 資料筆數不足 MIN_HISTORY_ROWS（例如全新資料庫、從未抓過大盤）→ 用 yfinance 一次補齊 2 年份。
      - 資料已存在，但最新一筆日期比今天早超過 5 天（例如剛部署、還沒等到下一次排程更新）
        → 嘗試用官方 API 補上最近一天，抓不到就用 yfinance 補近 5 天。
    設計給「掃描器啟動時」與「update_db.py 排程」共用，避免兩邊各寫一份判斷邏輯。
    回傳是否有實際寫入新資料。
    """
    row_count = get_benchmark_row_count(db_path)
    if row_count < MIN_HISTORY_ROWS:
        return backfill_benchmark_history(db_path, period="2y") > 0

    existing = get_benchmark_ohlcv(db_path)
    if existing.empty:
        return backfill_benchmark_history(db_path, period="2y") > 0

    latest_date = existing["Date"].max()
    today = date.today() if report_date is None else datetime.strptime(report_date, "%Y%m%d").date()
    if (today - latest_date).days <= 5:
        return False  # 資料已經夠新，不用特地補

    # 資料存在但偏舊：先試官方 API 補當天，抓不到再用 yfinance 補近 5 天
    if report_date is not None:
        ok = update_benchmark_daily(db_path, report_date)
        if ok:
            return True
    recent = fetch_taiex_history_yfinance(period="5d")
    return save_benchmark_to_db(db_path, recent) > 0
