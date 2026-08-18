"""
update_signal_tracking.py
=========================
每日更新 Database/signal_tracking.csv 的追蹤績效，並以日期檔名上傳到 GitHub：
Database/signal_tracking_YYYYMMDD.csv

同一天重複執行只會更新同一份檔案；不同天會留下不同日期快照。

Usage:
    python update_signal_tracking.py
    python update_signal_tracking.py --upload-github
    python update_signal_tracking.py --download-github --upload-github
"""

from __future__ import annotations

import argparse
import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

DEFAULT_OWNER = "henglunlin"
DEFAULT_REPO = "stock-scanner-FUBAN"
DEFAULT_BRANCH = "main"
DEFAULT_DATABASE_DIR = "Database"
DEFAULT_TRACKING_FILENAME = "signal_tracking.csv"


def load_toml_secrets() -> Dict[str, Any]:
    possible_paths = [
        Path.cwd() / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for secrets_path in possible_paths:
        if not secrets_path.exists():
            continue
        try:
            try:
                import tomllib
            except ImportError:  # pragma: no cover
                import tomli as tomllib
            with open(secrets_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            return {}
    return {}


SECRETS = load_toml_secrets()


def get_config_value(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value not in [None, ""]:
        return str(value)
    if key in SECRETS and SECRETS[key] not in [None, ""]:
        return str(SECRETS[key])
    return default


def github_config() -> Dict[str, str]:
    return {
        "token": get_config_value("GITHUB_TOKEN", ""),
        "owner": get_config_value("GITHUB_OWNER", DEFAULT_OWNER),
        "repo": get_config_value("GITHUB_REPO", DEFAULT_REPO),
        "branch": get_config_value("GITHUB_BRANCH", DEFAULT_BRANCH),
        "database_dir": get_config_value("GITHUB_DATABASE_DIR", DEFAULT_DATABASE_DIR).strip("/"),
    }


def local_database_dir() -> Path:
    return Path(get_config_value("LOCAL_DATABASE_DIR", DEFAULT_DATABASE_DIR))


def tracking_file_path() -> Path:
    return local_database_dir() / DEFAULT_TRACKING_FILENAME


def today_tw() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


def tracking_github_filename(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = today_tw()
    return f"signal_tracking_{dt.strftime('%Y%m%d')}.csv"


def tracking_github_path(dt: Optional[datetime] = None) -> str:
    cfg = github_config()
    return f"{cfg['database_dir']}/{tracking_github_filename(dt)}"


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in ["-", "", None]:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path.strip('/')}"


def download_file_from_github(github_path: str, local_path: Path) -> bool:
    cfg = github_config()
    token = cfg["token"]
    if not token:
        print("[WARN] GITHUB_TOKEN 未設定，略過 GitHub 下載。")
        return False

    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    res = requests.get(url, headers=github_headers(token), params={"ref": cfg["branch"]}, timeout=20)
    if res.status_code == 404:
        print(f"[INFO] GitHub 找不到檔案：{github_path}")
        return False
    if res.status_code != 200:
        raise RuntimeError(f"GitHub 下載失敗：{res.status_code} {res.text}")

    content = res.json().get("content", "")
    if not content:
        raise RuntimeError("GitHub 回傳內容為空，無法下載追蹤檔。")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(base64.b64decode(content))
    print(f"[OK] 已下載：{github_path} -> {local_path}")
    return True


def download_tracking_from_github() -> bool:
    """優先下載今日日期檔；若今日檔不存在，再退回 signal_tracking.csv。"""
    local_path = tracking_file_path()
    dated_path = tracking_github_path(today_tw())
    if download_file_from_github(dated_path, local_path):
        return True
    cfg = github_config()
    fallback_path = f"{cfg['database_dir']}/{DEFAULT_TRACKING_FILENAME}"
    return download_file_from_github(fallback_path, local_path)


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_config()
    token = cfg["token"]
    if not token:
        print("[WARN] GITHUB_TOKEN 未設定，略過 GitHub 上傳。")
        return False

    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    headers = github_headers(token)
    sha: Optional[str] = None

    get_res = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=20)
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")
    elif get_res.status_code != 404:
        raise RuntimeError(f"讀取 GitHub 既有檔案失敗：{get_res.status_code} {get_res.text}")

    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(file_bytes).decode("utf-8"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, headers=headers, json=payload, timeout=30)
    if put_res.status_code not in [200, 201]:
        raise RuntimeError(f"上傳 GitHub 失敗：{put_res.status_code} {put_res.text}")

    html_url = put_res.json().get("content", {}).get("html_url", "")
    print(f"[OK] 已上傳到 GitHub：{html_url}")
    return True


def upload_tracking_to_github() -> bool:
    path = tracking_file_path()
    if not path.exists():
        print(f"[WARN] 找不到追蹤檔：{path}")
        return False
    dt = today_tw()
    return upload_file_to_github(
        path.read_bytes(),
        tracking_github_path(dt),
        f"Update {tracking_github_filename(dt)}",
    )


def calc_return_after_days(closes: pd.Series, entry_price: float, days: int) -> Optional[float]:
    if len(closes) < days:
        return None
    close_price = float(closes.iloc[days - 1])
    return round((close_price / entry_price - 1) * 100, 2)


def calc_max_gain(highs: pd.Series, entry_price: float) -> Optional[float]:
    if highs.empty:
        return None
    return round((float(highs.max()) / entry_price - 1) * 100, 2)


def calc_max_drawdown(lows: pd.Series, entry_price: float) -> Optional[float]:
    if lows.empty:
        return None
    return round((float(lows.min()) / entry_price - 1) * 100, 2)


def classify_profitable(close_return: Optional[float]) -> Optional[int]:
    """5 日報酬是否為正（不管過程中回撤多少），作為主要、較直覺的『有賺錢』指標。"""
    if close_return is None:
        return None
    return int(close_return > 0)


def classify_clean_win(max_gain: Optional[float], max_drawdown: Optional[float], close_return: Optional[float]) -> Optional[int]:
    """較嚴格的定義：達到滿意漲幅、過程沒有明顯拉回、且收盤仍是正報酬。
    用來衡量『進場後走勢乾不乾淨』，跟 classify_profitable 是互補的兩個指標，不是取代關係。"""
    if max_gain is None or max_drawdown is None or close_return is None:
        return None
    return int(max_gain >= 5 and max_drawdown > -5 and close_return >= 2)


# entry_price 跟 yfinance 實際股價差距超過這個倍數，視為資料異常（除權息斷點、資料源不一致、
# 人工登錄錯誤等），不計算報酬，避免產生像 -90% 這種假的極端值污染統計。
PRICE_SANITY_RATIO = 3.0


def is_price_anomaly(entry_price: float, reference_price: Optional[float]) -> bool:
    if reference_price is None or reference_price <= 0 or entry_price <= 0:
        return False
    ratio = entry_price / reference_price
    return ratio > PRICE_SANITY_RATIO or ratio < (1 / PRICE_SANITY_RATIO)


def normalize_yfinance_columns(hist: pd.DataFrame) -> pd.DataFrame:
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)
    return hist


def update_tracking_result() -> pd.DataFrame:
    path = tracking_file_path()
    if not path.exists():
        raise FileNotFoundError(f"找不到追蹤檔：{path}。請先由 Streamlit 掃描器產生 signal_tracking.csv。")

    df = pd.read_csv(path, encoding="utf-8-sig")
    if df.empty:
        print("[INFO] tracking file is empty")
        return df

    for col in ["scan_date", "代碼", "entry_price"]:
        if col not in df.columns:
            raise ValueError(f"追蹤檔缺少必要欄位：{col}")

    symbols = sorted(df["代碼"].dropna().astype(str).unique().tolist())
    price_map: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        try:
            hist = yf.download(symbol, period="1mo", interval="1d", progress=False, auto_adjust=False)
            if hist.empty:
                print(f"[WARN] {symbol} 無 yfinance 資料")
                continue
            price_map[symbol] = normalize_yfinance_columns(hist)
        except Exception as e:
            print(f"[WARN] {symbol} 下載失敗：{e}")

    result_rows = []
    for _, r in df.iterrows():
        symbol = str(r["代碼"])
        scan_date = pd.to_datetime(r["scan_date"])
        entry_price = safe_float(r["entry_price"])
        hist = price_map.get(symbol)

        if hist is None or hist.empty or entry_price <= 0:
            result_rows.append(r)
            continue

        hist = hist.copy()
        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        future = hist[hist.index > scan_date].head(10)
        if future.empty:
            result_rows.append(r)
            continue

        # 資料防呆：entry_price 應該跟掃描日附近的實際股價同一個量級。
        before = hist[hist.index <= scan_date]
        reference_price = float(before["Close"].iloc[-1]) if not before.empty else float(future["Close"].iloc[0])
        if is_price_anomaly(entry_price, reference_price):
            print(f"[WARN] {symbol} 疑似資料異常：entry_price={entry_price} vs 實際股價≈{reference_price:.2f}，略過報酬計算")
            r["status"] = "資料異常"
            result_rows.append(r)
            continue

        closes = future["Close"]
        highs = future["High"]
        lows = future["Low"]

        r["days_tracked"] = len(future)
        r["return_3d%"] = calc_return_after_days(closes, entry_price, 3)
        r["return_5d%"] = calc_return_after_days(closes, entry_price, 5)
        r["return_10d%"] = calc_return_after_days(closes, entry_price, 10)
        r["max_gain_5d%"] = calc_max_gain(highs.head(5), entry_price)
        r["max_drawdown_5d%"] = calc_max_drawdown(lows.head(5), entry_price)
        r["max_gain_10d%"] = calc_max_gain(highs.head(10), entry_price)
        r["max_drawdown_10d%"] = calc_max_drawdown(lows.head(10), entry_price)
        r["is_profitable_5d"] = classify_profitable(r.get("return_5d%"))
        r["is_clean_win_5d"] = classify_clean_win(
            r.get("max_gain_5d%"), r.get("max_drawdown_5d%"), r.get("return_5d%")
        )
        # 保留舊欄位名，維持既有 CSV schema 與下游相容性。
        r["is_success_5d"] = r["is_clean_win_5d"]
        r["status"] = "done" if len(future) >= 10 else "tracking"
        result_rows.append(r)

    out = pd.DataFrame(result_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[OK] tracking updated：{path}")
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="更新台股掃描器 signal_tracking.csv 追蹤績效")
    parser.add_argument("--download-github", action="store_true", help="更新前先從 GitHub Database 下載今日或 fallback 追蹤 CSV")
    parser.add_argument("--upload-github", action="store_true", help="更新完成後用日期檔名上傳追蹤 CSV 到 GitHub Database")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.download_github:
        download_tracking_from_github()
    update_tracking_result()
    if args.upload_github:
        upload_tracking_to_github()


if __name__ == "__main__":
    main()
