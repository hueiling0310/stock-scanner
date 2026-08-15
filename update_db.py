import pandas as pd
import requests
import sqlite3
import time
import urllib3
import os
from datetime import datetime, timezone, timedelta
from typing import Any

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}
DB_NAME = "twse_ohlcv.db"

# ======== 新增 Telegram 推播函數 ========
def send_telegram_message(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("未設定 Telegram 變數，略過推播。")
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram 傳送失敗: {e}")
# ========================================

def number(value: Any) -> float:
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "---", "----", "None", "nan", "X"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0

def get_json(url: str, params: dict[str, str] | None = None) -> Any:
    response = requests.get(url, params=params, headers=HEADERS, timeout=30, verify=False)
    response.raise_for_status()
    return response.json()

def check_status(payload: dict[str, Any], source: str) -> bool:
    status = str(payload.get("stat", ""))
    if status in {"", "OK"}: return True
    if "沒有符合條件的資料" in status: return False
    raise RuntimeError(f"{source} status: {status}")

def unique_columns(fields: list[Any]) -> list[str]:
    seen, result = {}, []
    for field in fields:
        base = str(field).strip()
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result

def fetch_twse_daily(report_date: str) -> pd.DataFrame:
    try:
        payload = get_json(TWSE_URL, {"date": report_date, "type": "ALLBUT0999", "response": "json"})
        if not check_status(payload, "prices"): return pd.DataFrame()
        
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
                return df[df["Close"] > 0].drop_duplicates("SecurityCode")
        return pd.DataFrame()
    except Exception as e:
        print(f"上市資料解析失敗 ({report_date}): {e}")
        return pd.DataFrame()

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
        print(f"上櫃資料解析失敗 ({report_date}): {e}")
        return pd.DataFrame()

def save_to_database(df: pd.DataFrame):
    if df.empty: return
    with sqlite3.connect(DB_NAME) as conn:
        dates = df["Date"].unique()
        markets = df["Market"].unique()
        for d in dates:
            for m in markets:
                conn.execute("DELETE FROM ohlcv_data WHERE Date = ? AND Market = ?", (str(d), m))
        df.to_sql("ohlcv_data", conn, if_exists="append", index=False)

if __name__ == "__main__":
    # 設定台灣時區
    tw_tz = timezone(timedelta(hours=8))
    tw_now = datetime.now(tw_tz)
    
    # 建立要抓取的日期清單 (昨天與今天)
    dates_to_fetch = [
        (tw_now - timedelta(days=1)).strftime("%Y%m%d"),
        tw_now.strftime("%Y%m%d")
    ]
    
    summary_lines = []
    has_valid_data = False
    
    for date_str in dates_to_fetch:
        print(f"開始執行自動抓取: {date_str}")
        twse_df = fetch_twse_daily(date_str)
        time.sleep(2) # 稍微等待，避免請求過於頻繁
        tpex_df = fetch_tpex_daily(date_str)
        
        daily_df = pd.concat([twse_df, tpex_df], ignore_index=True)
        if not daily_df.empty:
            save_to_database(daily_df)
            msg = f"📅 {date_str}: 上市 {len(twse_df)} 檔 / 上櫃 {len(tpex_df)} 檔"
            print(f"✅ {msg}")
            summary_lines.append(msg)
            has_valid_data = True
        else:
            msg = f"⏸️ {date_str}: 無交易資料 (可能為假日)"
            print(msg)
            summary_lines.append(msg)
            
        time.sleep(2) # 迴圈之間保護性暫停
        
    # 如果這兩天之中至少有一天有資料，就發送 Telegram 推播
    if has_valid_data:
        success_msg = (
            f"✅ <b>自動資料庫更新成功</b>\n" +
            "\n".join(summary_lines) +
            f"\n🤖 Github Actions 已將資料推回 Repo。"
        )
        send_telegram_message(success_msg)
    else:
        print("兩日皆無交易資料，不發送推播。")
