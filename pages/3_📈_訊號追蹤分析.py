"""
pages/1_📈_訊號追蹤分析.py
================================
Streamlit Pages UI for reading and analyzing all Database/signal_tracking*.csv files
from GitHub, updating post-signal performance, and plotting 60-bar candlestick charts
with MA / trendline / horizontal resistance / KD.

Place this file at:
    pages/3_📈_訊號追蹤分析.py

Required Streamlit secrets:
    GITHUB_TOKEN = "github_pat_xxx"
    GITHUB_OWNER = "henglunlin"
    GITHUB_REPO = "stock-scanner-FUBAN"
    GITHUB_BRANCH = "main"
    GITHUB_DATABASE_DIR = "Database"
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except Exception:  # pragma: no cover
    go = None
    make_subplots = None

st.set_page_config(page_title="訊號追蹤分析", layout="wide")

DEFAULT_OWNER = "henglunlin"
DEFAULT_REPO = "stock-scanner-FUBAN"
DEFAULT_BRANCH = "main"
DEFAULT_DATABASE_DIR = "Database"
TRACKING_PREFIX = "signal_tracking"


def tw_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


def get_secret(key: str, default: str = "") -> str:
    try:
        val = st.secrets.get(key, default)
        return str(val) if val not in [None, ""] else default
    except Exception:
        return default


def github_config() -> Dict[str, str]:
    return {
        "token": get_secret("GITHUB_TOKEN", ""),
        "owner": get_secret("GITHUB_OWNER", DEFAULT_OWNER),
        "repo": get_secret("GITHUB_REPO", DEFAULT_REPO),
        "branch": get_secret("GITHUB_BRANCH", DEFAULT_BRANCH),
        "database_dir": get_secret("GITHUB_DATABASE_DIR", DEFAULT_DATABASE_DIR).strip("/"),
    }


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in ["-", "", None]:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def normalize_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not s:
        return s
    if "." in s:
        return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")):
            return f"{s}.TWO"
        return f"{s}.TW"
    return s


def github_headers(token: str) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path.strip('/')}"


@st.cache_data(ttl=180)
def list_database_files(owner: str, repo: str, branch: str, database_dir: str, token: str) -> List[Dict[str, Any]]:
    url = github_contents_url(owner, repo, database_dir)
    res = requests.get(url, headers=github_headers(token), params={"ref": branch}, timeout=25)
    if res.status_code != 200:
        raise RuntimeError(f"讀取 GitHub Database 失敗：{res.status_code} {res.text}")
    data = res.json()
    if not isinstance(data, list):
        raise RuntimeError("GitHub Database 回傳格式不是資料夾清單。")
    return data


@st.cache_data(ttl=180)
def download_github_file(owner: str, repo: str, branch: str, path: str, token: str) -> bytes:
    url = github_contents_url(owner, repo, path)
    res = requests.get(url, headers=github_headers(token), params={"ref": branch}, timeout=25)
    if res.status_code != 200:
        raise RuntimeError(f"下載 GitHub 檔案失敗：{path}｜{res.status_code} {res.text}")
    payload = res.json()
    content = payload.get("content", "")
    if not content:
        raise RuntimeError(f"GitHub 檔案內容為空：{path}")
    return base64.b64decode(content)


def upload_file_to_github(file_bytes: bytes, github_path: str, commit_message: str) -> bool:
    cfg = github_config()
    if not cfg["token"]:
        st.error("GITHUB_TOKEN 尚未設定，無法上傳。")
        return False
    url = github_contents_url(cfg["owner"], cfg["repo"], github_path)
    headers = github_headers(cfg["token"])

    sha = None
    get_res = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=25)
    if get_res.status_code == 200:
        sha = get_res.json().get("sha")
    elif get_res.status_code != 404:
        st.error(f"讀取既有 GitHub 檔案失敗：{get_res.status_code} {get_res.text}")
        return False

    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(file_bytes).decode("utf-8"),
        "branch": cfg["branch"],
    }
    if sha:
        payload["sha"] = sha

    put_res = requests.put(url, headers=headers, json=payload, timeout=35)
    if put_res.status_code not in [200, 201]:
        st.error(f"上傳 GitHub 失敗：{put_res.status_code} {put_res.text}")
        return False
    html_url = put_res.json().get("content", {}).get("html_url", "")
    st.success(f"已上傳：{html_url}")
    return True


def is_tracking_csv(filename: str) -> bool:
    lower = filename.lower()
    return lower.startswith(TRACKING_PREFIX) and lower.endswith(".csv")


def read_tracking_csv(file_bytes: bytes, source_name: str) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8-sig")
    df["source_file"] = source_name
    return df


def normalize_tracking_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in [
        "scan_date", "代碼", "股票名稱", "entry_price", "訊號類型", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列", "K值", "D值", "週K值", "週D值",
        "MACD柱", "趨勢突破", "量能倍數", "status",
    ]:
        if col not in out.columns:
            out[col] = ""
    out["scan_date"] = pd.to_datetime(out["scan_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["代碼"] = out["代碼"].apply(normalize_symbol)
    out["entry_price"] = out["entry_price"].apply(lambda x: safe_float(x, None))
    out["訊號分數"] = out["訊號分數"].apply(lambda x: safe_float(x, None))
    out = out.sort_values(["scan_date", "代碼", "source_file"], na_position="last")
    out = out.drop_duplicates(subset=["scan_date", "代碼"], keep="last").reset_index(drop=True)
    return out


def load_all_tracking_from_github() -> Tuple[pd.DataFrame, List[str]]:
    cfg = github_config()
    files = list_database_files(cfg["owner"], cfg["repo"], cfg["branch"], cfg["database_dir"], cfg["token"])
    tracking_files = sorted(
        [f for f in files if f.get("type") == "file" and is_tracking_csv(f.get("name", ""))],
        key=lambda x: x.get("name", ""),
    )
    dfs = []
    loaded_names = []
    for f in tracking_files:
        name = f.get("name", "")
        path = f.get("path") or f"{cfg['database_dir']}/{name}"
        try:
            data = download_github_file(cfg["owner"], cfg["repo"], cfg["branch"], path, cfg["token"])
            dfs.append(read_tracking_csv(data, name))
            loaded_names.append(name)
        except Exception as e:
            st.warning(f"讀取 {name} 失敗：{e}")
    if not dfs:
        return pd.DataFrame(), loaded_names
    return normalize_tracking_df(pd.concat(dfs, ignore_index=True)), loaded_names


@st.cache_data(ttl=3600, show_spinner=False)
def download_price_history(symbol: str, period: str = "1y") -> pd.DataFrame:
    hist = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if hist.empty:
        return hist
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)
    hist = hist.copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    return hist


def calc_return_after_days(closes: pd.Series, entry_price: float, days: int) -> Optional[float]:
    if len(closes) < days:
        return None
    return round((float(closes.iloc[days - 1]) / entry_price - 1) * 100, 2)


def calc_max_gain(highs: pd.Series, entry_price: float) -> Optional[float]:
    if highs.empty:
        return None
    return round((float(highs.max()) / entry_price - 1) * 100, 2)


def calc_max_drawdown(lows: pd.Series, entry_price: float) -> Optional[float]:
    if lows.empty:
        return None
    return round((float(lows.min()) / entry_price - 1) * 100, 2)


def classify_success(max_gain: float, max_drawdown: float, close_return: float) -> int:
    return int(max_gain >= 5 and max_drawdown > -5 and close_return >= 2)


def update_forward_performance(df: pd.DataFrame, max_days: int = 20, period: str = "6mo") -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    result_rows = []
    symbols = sorted(out["代碼"].dropna().unique().tolist())
    progress = st.progress(0, text="準備下載股價資料...")
    price_map: Dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols, start=1):
        progress.progress(i / max(len(symbols), 1), text=f"下載股價：{symbol} ({i}/{len(symbols)})")
        try:
            price_map[symbol] = download_price_history(symbol, period=period)
        except Exception as e:
            st.warning(f"{symbol} 股價下載失敗：{e}")
    progress.empty()

    for _, r in out.iterrows():
        symbol = r.get("代碼", "")
        scan_date = pd.to_datetime(r.get("scan_date"), errors="coerce")
        entry_price = safe_float(r.get("entry_price"), default=0)
        hist = price_map.get(symbol)
        if pd.isna(scan_date) or hist is None or hist.empty or entry_price <= 0:
            result_rows.append(r)
            continue
        future = hist[hist.index > scan_date].head(max_days)
        if future.empty:
            result_rows.append(r)
            continue
        closes, highs, lows = future["Close"], future["High"], future["Low"]
        r["days_tracked"] = len(future)
        for d in [1, 3, 5, 10, 20]:
            if d <= max_days:
                r[f"return_{d}d%"] = calc_return_after_days(closes, entry_price, d)
        r["max_gain_5d%"] = calc_max_gain(highs.head(5), entry_price)
        r["max_drawdown_5d%"] = calc_max_drawdown(lows.head(5), entry_price)
        r["max_gain_10d%"] = calc_max_gain(highs.head(10), entry_price)
        r["max_drawdown_10d%"] = calc_max_drawdown(lows.head(10), entry_price)
        r["max_gain_20d%"] = calc_max_gain(highs.head(20), entry_price)
        r["max_drawdown_20d%"] = calc_max_drawdown(lows.head(20), entry_price)
        r["is_success_5d"] = classify_success(
            safe_float(r.get("max_gain_5d%", 0)), safe_float(r.get("max_drawdown_5d%", 0)), safe_float(r.get("return_5d%", 0))
        )
        r["status"] = "done" if len(future) >= 10 else "tracking"
        result_rows.append(r)
    return pd.DataFrame(result_rows)


def calc_kd(df: pd.DataFrame, n: int = 9) -> pd.DataFrame:
    out = df.copy()
    low_n = out["Low"].rolling(n, min_periods=1).min()
    high_n = out["High"].rolling(n, min_periods=1).max()
    denom = (high_n - low_n).replace(0, pd.NA)
    out["RSV"] = ((out["Close"] - low_n) / denom * 100).fillna(50)
    out["K"] = out["RSV"].ewm(alpha=1/3, adjust=False).mean()
    out["D"] = out["K"].ewm(alpha=1/3, adjust=False).mean()
    return out


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for n in [5, 10, 20, 60]:
        out[f"MA{n}"] = out["Close"].rolling(n, min_periods=1).mean()
    return out


def build_60bar_signal_window(symbol: str, scan_date: str, bars: int = 60, period: str = "1y") -> pd.DataFrame:
    hist = download_price_history(symbol, period=period)
    if hist.empty:
        return pd.DataFrame()
    sd = pd.to_datetime(scan_date)
    hist = hist.sort_index().copy()
    pos_candidates = [i for i, dt in enumerate(hist.index) if dt >= sd]
    sig_pos = pos_candidates[0] if pos_candidates else len(hist) - 1
    before_target = 20
    after_target = bars - before_target - 1
    start_pos = max(0, sig_pos - before_target)
    end_pos = min(len(hist), sig_pos + after_target + 1)
    shortage = bars - (end_pos - start_pos)
    if shortage > 0:
        extra_start = max(0, start_pos - shortage)
        shortage -= start_pos - extra_start
        start_pos = extra_start
    if shortage > 0:
        end_pos = min(len(hist), end_pos + shortage)
    window = hist.iloc[start_pos:end_pos].copy()
    window = add_moving_averages(window)
    window = calc_kd(window)
    window["relative_bar"] = range(len(window))
    return window


def find_swing_highs(df: pd.DataFrame, left: int = 2, right: int = 2) -> List[Tuple[int, pd.Timestamp, float]]:
    """找出局部高點，回傳：(位置, 日期, High)。"""
    highs: List[Tuple[int, pd.Timestamp, float]] = []
    if df.empty:
        return highs
    for i in range(left, len(df) - right):
        val = float(df["High"].iloc[i])
        if val >= float(df["High"].iloc[i-left:i].max()) and val >= float(df["High"].iloc[i+1:i+right+1].max()):
            highs.append((i, df.index[i], val))
    return highs


def _dedupe_points(points: List[Tuple[int, pd.Timestamp, float]]) -> List[Tuple[int, pd.Timestamp, float]]:
    """同一個 K 棒只保留一筆，並依位置排序。"""
    best_by_pos: Dict[int, Tuple[int, pd.Timestamp, float]] = {}
    for p in points:
        old = best_by_pos.get(p[0])
        if old is None or p[2] > old[2]:
            best_by_pos[p[0]] = p
    return sorted(best_by_pos.values(), key=lambda x: x[0])


def _upper_convex_hull(points: List[Tuple[int, float]]) -> List[Tuple[int, float]]:
    """計算「上凸包」(upper convex hull)。

    傳入 (位置, 高點價格) 點集，回傳的頂點序列具有數學保證：原始點集中
    每一個點的價格都不會高於這條凸包折線本身。也就是說，只要趨勢線的
    P1、P2 取自同一組上凸包，這條線在該範圍內絕對不會被任何一根K棒的
    最高價「穿越」。這是畫壓力／下降趨勢線最穩固的作法。
    """
    if len(points) < 2:
        return list(points)

    # 同一個 x（位置）只保留最高價，並依位置排序。
    dedup: Dict[int, float] = {}
    for x, y in points:
        if x not in dedup or y > dedup[x]:
            dedup[x] = y
    seq = sorted(dedup.items(), key=lambda p: p[0])
    if len(seq) < 2:
        return seq

    hull: List[Tuple[int, float]] = []
    for p in seq:
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0])
            if cross >= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    return hull


def _fit_hull_trendline(
    df: pd.DataFrame,
    start_pos: int,
    end_pos_exclusive: int,
    min_span: int = 3,
) -> Optional[Dict[str, Any]]:
    """在 [start_pos, end_pos_exclusive) 範圍內（此範圍刻意不含「今日」那一根）
    用上凸包找出下降趨勢線的兩個高點 P1、P2。

    重要修正：上凸包是「向上鼓起」的形狀，P1 與凸包「最後一個」頂點的直線，
    中間的凸包頂點反而會凸出在這條線之上（等於被穿越）。所以 P2 不能只取
    最後一個頂點，而是要把 P1 之後每一個凸包頂點都當作候選 P2，各自算出
    「從 P1 延伸出去、沿整段可見K線（含今日）逐根檢查後最遠能畫到哪裡」，
    再挑「畫線範圍最長、且完全不被任何K棒穿越」的那一組，這樣線才會像
    人工畫的趨勢線一樣，盡量貼著多個高點延伸到最新的K棒。
    """
    if end_pos_exclusive - start_pos < min_span:
        return None

    seg = df.iloc[start_pos:end_pos_exclusive]
    pts = [(start_pos + i, float(h)) for i, h in enumerate(seg["High"].tolist())]
    hull = _upper_convex_hull(pts)
    if len(hull) < 2:
        return None

    # P1：這段區間上凸包裡的全域最高點（同價取位置較左者，代表壓力起點）。
    p1 = max(hull, key=lambda p: (p[1], -p[0]))
    hull_after_p1 = [p for p in hull if p[0] > p1[0] and p[1] < p1[1]]
    if not hull_after_p1:
        return None

    full_len = len(df)
    tol = max((float(df["High"].max()) - float(df["Low"].min())) * 0.001, 0.001)

    best: Optional[Dict[str, Any]] = None
    for p2 in hull_after_p1:
        slope = (p2[1] - p1[1]) / max(p2[0] - p1[0], 1)
        if slope >= 0:
            continue

        # 沿整段可見K線（含今日）逐根檢查，找到第一次被穿越的位置，畫線只延伸到那之前。
        draw_end = full_len - 1
        for x in range(p1[0], full_len):
            line_val = p1[1] + slope * (x - p1[0])
            high_val = float(df["High"].iloc[x])
            if high_val > line_val + tol:
                draw_end = x - 1
                break
        if draw_end <= p1[0]:
            continue

        # 評分：畫線能延伸得越遠越好（最重要），其次是 P1、P2 兩點間距越大代表越具代表性。
        span = p2[0] - p1[0]
        score = draw_end * 1000 + span
        if best is None or score > best["score"]:
            best = {"p1": p1, "p2": p2, "slope": slope, "draw_end": draw_end, "score": score}

    if best is None:
        return None

    p1, p2, slope, draw_end = best["p1"], best["p2"], best["slope"], best["draw_end"]

    y_start = p1[1]
    y_end = p1[1] + slope * (draw_end - p1[0])

    return {
        "x": [df.index[p1[0]], df.index[draw_end]],
        "y": [y_start, y_end],
        "p1_date": df.index[p1[0]],
        "p1_val": p1[1],
        "p2_date": df.index[p2[0]],
        "p2_val": p2[1],
        "slope": slope,
    }


def detect_descending_trendlines(df: pd.DataFrame, short_term_bars: int = 25) -> Dict[str, Optional[Dict[str, Any]]]:
    """偵測長期／短期兩條下降趨勢線。

    重點規則：
    1. 高點（P1、P2）判斷一律排除「今日」那一根K棒（也就是視窗最後一根），
       避免當天盤中拉高的影線被誤判成趨勢高點。
    2. 同時找「長期」（用整個可見視窗）與「短期」（只用最近一段區間）兩條線。
    3. 兩條線都用上凸包（upper convex hull）計算，數學上保證延伸出去的線段
       不會被任何一根K棒的最高價穿越；若後續走高突破，畫線會自動停在突破前
       一根，不會硬拉一條被打穿的線。
    """
    result: Dict[str, Optional[Dict[str, Any]]] = {"long": None, "short": None}
    if df.empty or len(df) < 8:
        return result

    full_len = len(df)
    today_pos = full_len - 1  # 視窗最後一根＝今日，高點判斷不可使用這一根

    # 長期線：用「今日」以前的整段可見資料找高點。
    result["long"] = _fit_hull_trendline(df, 0, today_pos, min_span=max(4, full_len // 12))

    # 短期線：只用最近一段（今日之前）資料找高點，反映近期較陡的下降壓力。
    short_start = max(0, today_pos - short_term_bars)
    result["short"] = _fit_hull_trendline(df, short_start, today_pos, min_span=3)

    # 若短期線與長期線的兩個高點幾乎相同，代表是同一條線，短期線就不重複顯示。
    if result["long"] and result["short"]:
        same_p1 = result["long"]["p1_date"] == result["short"]["p1_date"]
        same_p2 = result["long"]["p2_date"] == result["short"]["p2_date"]
        if same_p1 and same_p2:
            result["short"] = None

    return result


def detect_horizontal_resistance(df: pd.DataFrame, scan_date: str) -> Optional[Dict[str, Any]]:
    if df.empty:
        return None
    sd = pd.to_datetime(scan_date)
    base = df[df.index <= sd]
    if base.empty:
        base = df
    idx = base["High"].idxmax()
    return {"level": float(base.loc[idx, "High"]), "date": idx}


def show_price_chart(row: pd.Series) -> None:
    symbol = row.get("代碼", "")
    scan_date = row.get("scan_date", "")
    entry_price = safe_float(row.get("entry_price", 0))
    stock_name = row.get("股票名稱", "")

    st.markdown("#### 圖表設定")
    ma_cols = st.columns(4)
    with ma_cols[0]:
        show_ma5 = st.checkbox("5MA", value=False, key=f"ma5_{symbol}_{scan_date}")
    with ma_cols[1]:
        show_ma10 = st.checkbox("10MA", value=False, key=f"ma10_{symbol}_{scan_date}")
    with ma_cols[2]:
        show_ma20 = st.checkbox("20MA", value=False, key=f"ma20_{symbol}_{scan_date}")
    with ma_cols[3]:
        show_ma60 = st.checkbox("60MA", value=True, key=f"ma60_{symbol}_{scan_date}")

    trend = build_60bar_signal_window(symbol, scan_date, bars=60, period="1y")
    if trend.empty:
        st.info("此股票目前沒有足夠股價資料可畫圖。")
        return
    if go is None or make_subplots is None:
        st.line_chart(trend[["Close", "MA60"]])
        st.line_chart(trend[["K", "D"]])
        return

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.72, 0.28],
        subplot_titles=(f"{symbol} {stock_name}｜60個交易日K線 / 均線 / 趨勢線", "KD 指標"),
    )
    fig.add_trace(
        go.Candlestick(
            x=trend.index, open=trend["Open"], high=trend["High"], low=trend["Low"], close=trend["Close"], name="K線",
            increasing=dict(line=dict(color="#ef4444", width=1.4), fillcolor="rgba(239,68,68,0.55)"),
            decreasing=dict(line=dict(color="#10b981", width=1.4), fillcolor="rgba(16,185,129,0.55)"),
        ), row=1, col=1,
    )
    ma_config = [("MA5", show_ma5, "#f97316"), ("MA10", show_ma10, "#8b5cf6"), ("MA20", show_ma20, "#0ea5e9"), ("MA60", show_ma60, "#111827")]
    for ma_name, enabled, color in ma_config:
        if enabled and ma_name in trend.columns:
            fig.add_trace(go.Scatter(x=trend.index, y=trend[ma_name], mode="lines", name=ma_name, line=dict(color=color, width=1.8)), row=1, col=1)
    if entry_price > 0:
        fig.add_hline(y=entry_price, line_dash="dot", line_color="#6366f1", annotation_text=f"訊號價 {entry_price}", row=1, col=1)
    fig.add_vline(x=pd.to_datetime(scan_date), line_dash="dash", line_color="#ef4444", annotation_text="訊號日", row=1, col=1)

    trendlines = detect_descending_trendlines(trend)
    trend_long = trendlines.get("long")
    trend_short = trendlines.get("short")
    # 兩條下降趨勢線統一用「深橘色虛線」；長期線較粗、短期線較細，方便區分但格式一致。
    if trend_long:
        fig.add_trace(go.Scatter(x=trend_long["x"], y=trend_long["y"], mode="lines", name="長期下降趨勢線", line=dict(color="#c2410c", width=2.6, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=[trend_long["p1_date"], trend_long["p2_date"]], y=[trend_long["p1_val"], trend_long["p2_val"]], mode="markers", name="長期趨勢高點", marker=dict(color="#c2410c", size=8)), row=1, col=1)
    if trend_short:
        fig.add_trace(go.Scatter(x=trend_short["x"], y=trend_short["y"], mode="lines", name="短期下降趨勢線", line=dict(color="#c2410c", width=1.6, dash="dash")), row=1, col=1)
        fig.add_trace(go.Scatter(x=[trend_short["p1_date"], trend_short["p2_date"]], y=[trend_short["p1_val"], trend_short["p2_val"]], mode="markers", name="短期趨勢高點", marker=dict(color="#c2410c", size=6, symbol="diamond")), row=1, col=1)
    horizontal = detect_horizontal_resistance(trend, scan_date)
    if horizontal:
        fig.add_hline(y=horizontal["level"], line_dash="dot", line_color="#2563eb", annotation_text=f"水平壓力 {horizontal['level']:.2f}", row=1, col=1)

    fig.add_trace(go.Scatter(x=trend.index, y=trend["K"], mode="lines", name="K", line=dict(color="#f59e0b", width=1.8)), row=2, col=1)
    fig.add_trace(go.Scatter(x=trend.index, y=trend["D"], mode="lines", name="D", line=dict(color="#3b82f6", width=1.8)), row=2, col=1)
    fig.add_hline(y=80, line_dash="dot", line_color="#94a3b8", row=2, col=1)
    fig.add_hline(y=20, line_dash="dot", line_color="#94a3b8", row=2, col=1)
    fig.update_layout(title=f"{symbol} {stock_name}｜訊號後股價走勢", height=780, margin=dict(l=20, r=20, t=75, b=25), xaxis_rangeslider_visible=False, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), hovermode="x unified")
    fig.update_yaxes(title_text="價格", row=1, col=1)
    fig.update_yaxes(title_text="KD", range=[0, 100], row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    info_cols = st.columns(3)
    with info_cols[0]:
        st.caption(f"顯示K線數：{len(trend)} 個交易日")
    with info_cols[1]:
        if trend_long:
            st.caption(f"長期下降趨勢線：{trend_long['p1_date'].strftime('%Y-%m-%d')} {trend_long['p1_val']:.2f} → {trend_long['p2_date'].strftime('%Y-%m-%d')} {trend_long['p2_val']:.2f}")
        else:
            st.caption("長期下降趨勢線：目前找不到明確的兩個下降高點")
        if trend_short:
            st.caption(f"短期下降趨勢線：{trend_short['p1_date'].strftime('%Y-%m-%d')} {trend_short['p1_val']:.2f} → {trend_short['p2_date'].strftime('%Y-%m-%d')} {trend_short['p2_val']:.2f}")
    with info_cols[2]:
        if horizontal:
            st.caption(f"水平壓力：{horizontal['level']:.2f}（{horizontal['date'].strftime('%Y-%m-%d')} 高點）")


def run_update_signal_tracking_script() -> None:
    script_path = Path("update_signal_tracking.py")
    if not script_path.exists():
        st.error("找不到 repo 根目錄的 update_signal_tracking.py。請確認檔案已放在根目錄。")
        return
    env = os.environ.copy()
    for key in ["GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO", "GITHUB_BRANCH", "GITHUB_DATABASE_DIR", "LOCAL_DATABASE_DIR"]:
        val = get_secret(key, "")
        if val:
            env[key] = val
    cmd = [sys.executable, str(script_path), "--download-github", "--upload-github"]
    with st.spinner("執行 update_signal_tracking.py 中..."):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=420, env=env)
        except Exception as e:
            st.error(f"執行 update_signal_tracking.py 發生錯誤：{e}")
            return
    if result.returncode == 0:
        st.success("update_signal_tracking.py 執行完成。")
        st.code(result.stdout or "完成，但沒有 stdout。", language="bash")
    else:
        st.error("update_signal_tracking.py 執行失敗。")
        st.code(result.stderr or result.stdout, language="bash")


st.title("📈 訊號追蹤分析")
st.caption("分析 GitHub Database 內所有 signal_tracking*.csv，更新訊號後 1/3/5/10/20 日走勢與績效。")

cfg = github_config()
with st.sidebar:
    st.header("GitHub 設定")
    st.write(f"Repo：`{cfg['owner']}/{cfg['repo']}`")
    st.write(f"Branch：`{cfg['branch']}`")
    st.write(f"Database：`{cfg['database_dir']}`")
    st.write(f"Token：{'✅ 已設定' if cfg['token'] else '⚠️ 未設定'}")
    st.divider()
    st.header("分析設定")
    period = st.selectbox("下載股價期間", ["3mo", "6mo", "1y", "2y"], index=1)
    max_days = st.selectbox("追蹤天數", [5, 10, 20], index=2)

col_a, col_b, col_c = st.columns([1.1, 1.2, 1.2])
with col_a:
    load_btn = st.button("📥 讀取 GitHub 全部追蹤檔", use_container_width=True)
with col_b:
    update_btn = st.button("🔄 讀取並更新股價績效", use_container_width=True)
with col_c:
    run_script_btn = st.button("▶️ 執行 update_signal_tracking.py", use_container_width=True)

if run_script_btn:
    run_update_signal_tracking_script()
if load_btn or update_btn or "tracking_df" not in st.session_state:
    try:
        with st.spinner("讀取 GitHub Database 中的 signal_tracking*.csv..."):
            tracking_df, loaded_files = load_all_tracking_from_github()
        st.session_state.tracking_df = tracking_df
        st.session_state.loaded_tracking_files = loaded_files
        if tracking_df.empty:
            st.warning("GitHub Database 內尚未找到 signal_tracking*.csv。")
        else:
            st.success(f"已讀取 {len(loaded_files)} 個追蹤檔，合併去重後 {len(tracking_df)} 筆訊號。")
    except Exception as e:
        st.error(f"讀取失敗：{e}")
if update_btn and "tracking_df" in st.session_state and not st.session_state.tracking_df.empty:
    with st.spinner("更新訊號後股價績效..."):
        st.session_state.tracking_df = update_forward_performance(st.session_state.tracking_df, max_days=max_days, period=period)
    st.success("股價績效更新完成。")

tracking_df = st.session_state.get("tracking_df", pd.DataFrame())
loaded_files = st.session_state.get("loaded_tracking_files", [])
if tracking_df.empty:
    st.stop()

with st.expander("已讀取檔案", expanded=False):
    st.write(loaded_files)

st.subheader("篩選條件")
f1, f2, f3, f4 = st.columns(4)
with f1:
    min_score = st.number_input("最低訊號分數", min_value=0.0, max_value=100.0, value=0.0, step=5.0)
with f2:
    grades = sorted([x for x in tracking_df.get("追蹤等級", pd.Series(dtype=str)).dropna().unique().tolist() if str(x)])
    selected_grades = st.multiselect("追蹤等級", grades, default=grades)
with f3:
    signal_text = st.text_input("訊號類型包含", value="")
with f4:
    symbols_text = st.text_input("指定代碼，多檔逗號分隔", value="")

filtered = tracking_df.copy()
if "訊號分數" in filtered.columns:
    filtered = filtered[filtered["訊號分數"].fillna(0).astype(float) >= float(min_score)]
if selected_grades and "追蹤等級" in filtered.columns:
    filtered = filtered[filtered["追蹤等級"].isin(selected_grades)]
if signal_text.strip() and "訊號類型" in filtered.columns:
    filtered = filtered[filtered["訊號類型"].astype(str).str.contains(signal_text.strip(), na=False)]
if symbols_text.strip():
    symbols = [normalize_symbol(x) for x in symbols_text.replace("，", ",").split(",") if x.strip()]
    filtered = filtered[filtered["代碼"].isin(symbols)]

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("訊號筆數", f"{len(filtered):,}")
k2.metric("股票數", f"{filtered['代碼'].nunique():,}" if "代碼" in filtered.columns else "0")
k3.metric("5D平均報酬", f"{filtered['return_5d%'].dropna().mean():.2f}%" if "return_5d%" in filtered.columns and filtered["return_5d%"].notna().any() else "未更新")
k4.metric("5D平均最高漲幅", f"{filtered['max_gain_5d%'].dropna().mean():.2f}%" if "max_gain_5d%" in filtered.columns and filtered["max_gain_5d%"].notna().any() else "未更新")
k5.metric("5D成功率", f"{filtered['is_success_5d'].mean():.2%}" if "is_success_5d" in filtered.columns and filtered["is_success_5d"].notna().any() else "未更新")

st.subheader("績效摘要")
tab1, tab2, tab3, tab4 = st.tabs(["依追蹤等級", "依訊號類型", "依MA排列", "明細資料"])

def summary_group(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    if group_col not in df.columns:
        return pd.DataFrame()
    agg_dict: Dict[str, Tuple[str, str]] = {"筆數": ("代碼", "count")}
    for col in ["return_3d%", "return_5d%", "return_10d%", "max_gain_5d%", "max_drawdown_5d%", "is_success_5d"]:
        if col in df.columns:
            agg_dict[col] = (col, "mean")
    return df.groupby(group_col, dropna=False).agg(**agg_dict).reset_index().sort_values("筆數", ascending=False)

with tab1:
    st.dataframe(summary_group(filtered, "追蹤等級"), use_container_width=True)
with tab2:
    if "訊號類型" in filtered.columns:
        exploded = filtered.copy()
        exploded["single_signal"] = exploded["訊號類型"].astype(str).str.split("、")
        exploded = exploded.explode("single_signal")
        exploded["single_signal"] = exploded["single_signal"].str.strip()
        st.dataframe(summary_group(exploded, "single_signal"), use_container_width=True)
with tab3:
    st.dataframe(summary_group(filtered, "MA排列"), use_container_width=True)
with tab4:
    show_cols = [c for c in ["scan_date", "代碼", "股票名稱", "entry_price", "訊號分數", "追蹤等級", "訊號類型", "return_1d%", "return_3d%", "return_5d%", "return_10d%", "return_20d%", "max_gain_5d%", "max_drawdown_5d%", "MA位置", "MA排列", "RS加權報酬%", "source_file"] if c in filtered.columns]
    st.dataframe(filtered[show_cols].sort_values(["scan_date", "訊號分數"], ascending=[False, False]), use_container_width=True)

st.subheader("匯出 / 回寫")
out_csv = filtered.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
col_d1, col_d2 = st.columns(2)
with col_d1:
    st.download_button("下載目前篩選結果 CSV", data=out_csv, file_name=f"tracking_analysis_{tw_now().strftime('%Y%m%d_%H%M%S')}.csv", mime="text/csv", use_container_width=True)
with col_d2:
    if st.button("上傳目前更新結果到 GitHub 今日日期檔", use_container_width=True):
        github_path = f"{cfg['database_dir']}/signal_tracking_{tw_now().strftime('%Y%m%d')}.csv"
        upload_file_to_github(out_csv, github_path, f"Update tracking analysis {tw_now().strftime('%Y-%m-%d %H:%M:%S')}")

st.subheader("個股訊號後走勢")
if filtered.empty:
    st.info("目前篩選條件下沒有資料。")
else:
    search_col1, search_col2 = st.columns([1.2, 3.8])
    with search_col1:
        chart_symbol_query = st.text_input("股票代碼搜尋", value="", placeholder="例如 2610 或 2610.TW")
    chart_candidates = filtered.copy()
    if chart_symbol_query.strip():
        query_symbol = normalize_symbol(chart_symbol_query)
        query_raw = chart_symbol_query.strip().upper()
        chart_candidates = chart_candidates[
            chart_candidates["代碼"].astype(str).str.upper().str.contains(query_symbol, na=False)
            | chart_candidates["代碼"].astype(str).str.upper().str.contains(query_raw, na=False)
            | chart_candidates.get("股票名稱", pd.Series("", index=chart_candidates.index)).astype(str).str.contains(chart_symbol_query.strip(), na=False)
        ]
    with search_col2:
        st.caption(f"符合圖表搜尋條件：{len(chart_candidates)} 筆")
    if chart_candidates.empty:
        st.warning("找不到符合股票代碼 / 名稱的訊號，請換一個代碼搜尋。")
    else:
        chart_candidates = chart_candidates.copy()
        chart_candidates["label"] = chart_candidates.apply(lambda r: f"{r.get('scan_date', '')}｜{r.get('代碼', '')}｜{r.get('股票名稱', '')}｜分數 {r.get('訊號分數', '')}｜{r.get('訊號類型', '')}", axis=1)
        selected_label = st.selectbox("選擇一筆訊號", chart_candidates["label"].tolist())
        selected_row = chart_candidates[chart_candidates["label"] == selected_label].iloc[0]
        show_price_chart(selected_row)
