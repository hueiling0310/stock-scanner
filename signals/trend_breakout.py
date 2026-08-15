"""
signals/trend_breakout.py
===========================
掃描條件：下降趨勢突破。
- 用「上凸包 (upper convex hull)」找出真正有效的下降壓力線，取代任意兩點窮舉。
- 可選「量能確認」：突破日成交量須放大到 N 日均量的一定倍數以上。
- 另外提供 plot_trend_breakout_chart()：畫出 K線 + 下降趨勢線 + 成交量，方便肉眼複核訊號品質。
"""

import pandas as pd
import streamlit as st

from .context import SignalContext

# ===== plotly（用於繪製 K 線 + 趨勢線圖表）=====
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    go = None
    make_subplots = None


# ===== 下降趨勢線偵測參數 =====
TREND_LOOKBACK_DAYS = 70              
TREND_SWING_LEFT = 2                  
TREND_SWING_RIGHT = 2                 
TREND_MIN_PEAK_GAP = 5                
TREND_MIN_DROP_PCT = 3.0              
TREND_BREAKOUT_BUFFER_PCT = 0.0       
TREND_RESIST_TOL_PCT = 1.2            
TREND_TOUCH_TOL_PCT = 2.0             
TREND_MAX_VIOLATIONS = 1              
TREND_REQUIRE_MA60_UP = False         

# ===== 量能確認參數（突破日成交量須放大到 N 日均量的幾倍）=====
TREND_VOL_MA_PERIOD = 5
TREND_VOL_RATIO_MIN = 1.3

def _format_date_for_table(value):
    if value is None or value == "-" or pd.isna(value):
        return "-"
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def find_swing_highs(values, left=2, right=2):
    s = pd.Series(values, dtype="float64").reset_index(drop=True)
    peaks = []
    if len(s) < left + right + 1:
        return peaks

    for i in range(left, len(s) - right):
        center = s.iloc[i]
        if pd.isna(center):
            continue
        window = s.iloc[i-left:i+right+1]
        if window.isna().any():
            continue
        if center >= window.max():
            if peaks and abs(s.iloc[peaks[-1]] - center) < 1e-9 and i - peaks[-1] <= right:
                peaks[-1] = i
            else:
                peaks.append(i)
    return peaks


def _hull_cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def upper_hull_edges(peak_positions, high_series):
    """
    對 swing high 點集合取「上凸包 (upper convex hull)」，只回傳其中呈下降的邊。
    上凸包保證：這條線段之間沒有任何一個高點超出線的上方（零違反），
    比任意兩點窮舉連線更貼近圖表上真正有效的壓力線／下降趨勢線。
    回傳依 x1 (較早的高點) 排序的 (x1, x2, y1, y2) list。
    """
    pts = sorted({(int(p), float(high_series.iloc[p])) for p in peak_positions})
    if len(pts) < 2:
        return []

    hull = []
    for p in pts:
        while len(hull) >= 2 and _hull_cross(hull[-2], hull[-1], p) >= 0:
            hull.pop()
        hull.append(p)

    edges = []
    for i in range(len(hull) - 1):
        (x1, y1), (x2, y2) = hull[i], hull[i + 1]
        if y2 < y1:  # 只保留下降邊，才是下降趨勢線的候選
            edges.append((x1, x2, y1, y2))
    return edges


def detect_downtrend_breakout(
    df,
    price_val,
    lookback=TREND_LOOKBACK_DAYS,
    swing_left=TREND_SWING_LEFT,
    swing_right=TREND_SWING_RIGHT,
    min_peak_gap=TREND_MIN_PEAK_GAP,
    min_drop_pct=TREND_MIN_DROP_PCT,
    breakout_buffer_pct=TREND_BREAKOUT_BUFFER_PCT,
    resist_tol_pct=TREND_RESIST_TOL_PCT,
    touch_tol_pct=TREND_TOUCH_TOL_PCT,
    max_violations=TREND_MAX_VIOLATIONS,
    vol_ma_period=TREND_VOL_MA_PERIOD,
    vol_ratio_min=TREND_VOL_RATIO_MIN,
    require_volume_confirm=False,
):
    if df is None or df.empty or not {"High", "Close"}.issubset(df.columns):
        return {"signal": "-"}

    work = df.copy().reset_index(drop=True)
    work["High"] = pd.to_numeric(work["High"], errors="coerce")
    work["Close"] = pd.to_numeric(work["Close"], errors="coerce")
    if "Volume" in work.columns:
        work["Volume"] = pd.to_numeric(work["Volume"], errors="coerce")
    if "Date" not in work.columns:
        work["Date"] = work.index.astype(str)

    work = work.dropna(subset=["High", "Close"]).reset_index(drop=True)
    if len(work) < max(25, min_peak_gap + swing_left + swing_right + 5):
        return {"signal": "-"}

    lookback = min(int(lookback), len(work))
    work = work.iloc[-lookback:].reset_index(drop=True)
    high = work["High"].reset_index(drop=True)
    close = work["Close"].reset_index(drop=True)
    n = len(work)
    x_today = n - 1
    x_yesterday = n - 2

    # ===== 量能確認：突破日成交量 / N日均量 =====
    vol_ratio = None
    if "Volume" in work.columns:
        vol = work["Volume"].reset_index(drop=True)
        vol_ma = vol.rolling(window=vol_ma_period, min_periods=1).mean()
        # 用「昨日」均量當基準，避免今日爆量把自己均量拉高、稀釋比率
        base_vol_ma = float(vol_ma.iloc[x_yesterday]) if x_yesterday >= 0 else None
        today_vol = float(vol.iloc[x_today]) if pd.notna(vol.iloc[x_today]) else None
        if base_vol_ma and base_vol_ma > 0 and today_vol is not None:
            vol_ratio = today_vol / base_vol_ma

    peaks = find_swing_highs(high.values, left=swing_left, right=swing_right)
    if len(peaks) < 2 and swing_right > 1:
        peaks = find_swing_highs(high.values, left=swing_left, right=1)

    peaks = [p for p in peaks if p <= x_yesterday]
    if len(peaks) < 2:
        return {"signal": "-"}

    # ===== 用上凸包取代 O(n^2) 窮舉，候選邊數大幅減少且每條邊天生零違反(對峰值點而言) =====
    hull_edges = upper_hull_edges(peaks, high)

    best = None
    for p1_pos, p2_pos, p1_v, p2_v in hull_edges:
        if p1_pos > x_today - min_peak_gap:
            continue
        if p2_pos <= p1_pos + min_peak_gap:
            continue
        if p2_pos >= x_today:
            continue

        drop_pct = (p1_v - p2_v) / p1_v * 100
        if drop_pct < min_drop_pct:
            continue

        slope = (p2_v - p1_v) / (p2_pos - p1_pos)
        if slope >= 0:
            continue

        xs = pd.Series(range(n), dtype="float64")
        line = p1_v + slope * (xs - p1_pos)

        # 仍對「每日高點」(而非僅峰值點)做違反檢查，避免兩峰值之間的盤中假突破被漏掉
        check_start = p1_pos + 1
        check_end = x_yesterday
        if check_start <= check_end:
            segment_high = high.iloc[check_start:check_end+1].reset_index(drop=True)
            segment_line = line.iloc[check_start:check_end+1].reset_index(drop=True)
            above = segment_high > segment_line * (1 + resist_tol_pct / 100)
            p2_segment_idx = p2_pos - check_start
            if 0 <= p2_segment_idx < len(above):
                above.iloc[p2_segment_idx] = False
            violations = int(above.sum())
        else:
            violations = 0

        if violations > max_violations:
            continue

        touch_count = 0
        for p in peaks:
            if p < p1_pos or p > x_yesterday:
                continue
            line_p = float(line.iloc[p])
            if line_p <= 0:
                continue
            dist_pct = abs(float(high.iloc[p]) - line_p) / line_p * 100
            if dist_pct <= touch_tol_pct:
                touch_count += 1

        trendline_today = float(line.iloc[x_today])
        trendline_yesterday = float(line.iloc[x_yesterday])
        today_close = float(price_val)
        yesterday_close = float(close.iloc[-2])
        buffer = 1 + breakout_buffer_pct / 100

        is_breakout = (
            today_close > trendline_today * buffer
            and yesterday_close <= trendline_yesterday * buffer
        )
        if not is_breakout:
            continue

        if require_volume_confirm and (vol_ratio is None or vol_ratio < vol_ratio_min):
            continue

        score = (
            (max_violations - violations) * 100000
            + touch_count * 10000
            + p2_pos * 100
            + drop_pct
        )

        candidate = {
            "signal": "趨勢突破",
            "p1_pos": int(p1_pos),
            "p2_pos": int(p2_pos),
            "p1_date": _format_date_for_table(work.loc[p1_pos, "Date"]),
            "p2_date": _format_date_for_table(work.loc[p2_pos, "Date"]),
            "p1_val": p1_v,
            "p2_val": p2_v,
            "slope": slope,
            "slope_pct": drop_pct,
            "trendline_now": trendline_today,
            "touch_count": touch_count,
            "violations": violations,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "score": score,
            # 保留繪圖用資料（K線+趨勢線所需的完整片段）
            "chart_df": work,
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    return best if best else {"signal": "-"}


def build_trend_breakout_chart_figure(symbol: str, name: str, chart_info: dict):
    """
    建立 K線 + 下降趨勢線(藍色) + 成交量的 Plotly Figure。
    這個 helper 讓 UI 顯示與「一鍵下載所有圖表」共用同一份繪圖邏輯。
    """
    if go is None or make_subplots is None:
        return None

    work = chart_info.get("df")
    p1_pos = chart_info.get("p1_pos")
    p2_pos = chart_info.get("p2_pos")
    p1_val = chart_info.get("p1_val")
    slope = chart_info.get("slope")
    if work is None or work.empty or p1_pos is None or slope is None or p1_val is None:
        return None

    work = work.reset_index(drop=True)
    n = len(work)
    has_volume = "Volume" in work.columns

    fig = make_subplots(
        rows=2 if has_volume else 1, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25] if has_volume else [1.0],
        vertical_spacing=0.03,
    )

    fig.add_trace(go.Candlestick(
        x=work["Date"], open=work["Open"], high=work["High"],
        low=work["Low"], close=work["Close"],
        increasing_line_color="red", decreasing_line_color="green",
        name="K線",
    ), row=1, col=1)

    # 下降趨勢線：從 p1 延伸畫到最後一天（今天）
    trend_x = [work.loc[p1_pos, "Date"], work.loc[n - 1, "Date"]]
    trend_y = [p1_val, p1_val + slope * (n - 1 - p1_pos)]
    fig.add_trace(go.Scatter(
        x=trend_x, y=trend_y, mode="lines",
        line=dict(color="blue", width=2.5), name="下降趨勢線",
    ), row=1, col=1)

    # 標記 P1 / P2 兩個高點
    for pos, label in ((p1_pos, "P1"), (p2_pos, "P2")):
        if pos is not None and 0 <= pos < n:
            fig.add_annotation(
                x=work.loc[pos, "Date"], y=work.loc[pos, "High"],
                text=label, showarrow=True, arrowhead=2, arrowcolor="blue",
                ax=0, ay=-30, row=1, col=1,
            )

    # 標記今天的突破點
    fig.add_annotation(
        x=work.loc[n - 1, "Date"], y=work.loc[n - 1, "High"],
        text="突破", showarrow=True, arrowhead=2, arrowcolor="red",
        ax=0, ay=-40, row=1, col=1,
    )

    if has_volume:
        vol_colors = [
            "red" if c >= o else "green"
            for o, c in zip(work["Open"], work["Close"])
        ]
        fig.add_trace(go.Bar(
            x=work["Date"], y=work["Volume"], marker_color=vol_colors, name="成交量",
        ), row=2, col=1)

    fig.update_layout(
        title=f"{symbol} {name}｜下降趨勢突破",
        xaxis_rangeslider_visible=False,
        height=520 if has_volume else 420,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
    )
    return fig


def plot_trend_breakout_chart(symbol: str, name: str, chart_info: dict):
    """
    繪製 K線 + 下降趨勢線(藍色) + 成交量 圖表，用來視覺化「趨勢突破」訊號。
    chart_info 需包含：df（含 Date/Open/High/Low/Close/Volume 的片段）、p1_pos、p2_pos、p1_val、slope
    """
    if go is None or make_subplots is None:
        st.caption("尚未安裝 plotly，無法繪製圖表（請在 requirements.txt 加入 plotly）。")
        return

    fig = build_trend_breakout_chart_figure(symbol, name, chart_info)
    if fig is None:
        st.caption("此檔股票暫無圖表資料。")
        return

    st.plotly_chart(fig, use_container_width=True, key=f"trend_chart_{symbol}")


def check_trend_breakout_signal(ctx: SignalContext, params: dict) -> dict:
    """下降趨勢突破（上凸包演算法找壓力線 + 可選量能確認），沿用 detect_downtrend_breakout()"""
    min_len = max(61, TREND_MIN_PEAK_GAP + TREND_SWING_LEFT + TREND_SWING_RIGHT + 5)
    empty_fields = {
        "P1日期": "-", "區高P1": "-", "P2日期": "-", "近高P2": "-",
        "坡度%": "-", "趨勢價": "-", "貼線數": "-", "穿線數": "-", "量能倍數": "-",
    }
    if len(ctx.df) < min_len:
        return {"labels": [], "fields": empty_fields, "extra": None}

    if TREND_REQUIRE_MA60_UP:
        ma60 = ctx.close.rolling(window=60).mean()
        ma60_today, ma60_yesterday = float(ma60.iloc[-1]), float(ma60.iloc[-2])
        ma60_ok = pd.notna(ma60_today) and pd.notna(ma60_yesterday) and ma60_today > ma60_yesterday
        if not ma60_ok:
            return {"labels": [], "fields": empty_fields, "extra": None}

    result = detect_downtrend_breakout(
        df=ctx.df, price_val=ctx.price,
        require_volume_confirm=params.get("require_volume_confirm", False),
    )
    triggered = result.get("signal") == "趨勢突破"
    if not triggered:
        return {"labels": [], "fields": empty_fields, "extra": None}

    vr = result.get("vol_ratio")
    fields = {
        "P1日期": result.get("p1_date", "-"),
        "區高P1": round(float(result.get("p1_val", 0.0)), 2),
        "P2日期": result.get("p2_date", "-"),
        "近高P2": round(float(result.get("p2_val", 0.0)), 2),
        "坡度%": round(float(result.get("slope_pct", 0.0)), 1),
        "趨勢價": round(float(result.get("trendline_now", 0.0)), 2),
        "貼線數": result.get("touch_count", "-"),
        "穿線數": result.get("violations", "-"),
        "量能倍數": round(vr, 2) if vr is not None else "-",
    }
    extra = {
        "chart_df": result.get("chart_df"),
        "p1_pos": result.get("p1_pos"),
        "p2_pos": result.get("p2_pos"),
        "p1_val": result.get("p1_val"),
        "slope": result.get("slope"),
        "vol_ratio": fields["量能倍數"],
    }
    return {"labels": ["趨勢突破"], "fields": fields, "extra": extra}
