"""
signals/chart.py
=================
通用 K線 + 訊號標記圖表，取代舊版僅限「趨勢突破」使用的 plot_trend_breakout_chart。
任何訊號的 marks（日期清單）都可以用同一份函式畫出來，供「股票名稱」詳情查看器使用。
"""
import pandas as pd
import streamlit as st

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    go = None
    make_subplots = None


def build_signal_chart_figure(symbol: str, name: str, chart_df: pd.DataFrame, marks=None, lookback_days: int = 90):
    """建立 K線 + 成交量 + 訊號標記(藍色虛線+★) 的 Plotly Figure。"""
    if go is None or make_subplots is None:
        return None
    if chart_df is None or chart_df.empty:
        return None

    work = chart_df.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    else:
        work = work.reset_index().rename(columns={work.reset_index().columns[0]: "Date"})
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    work = work.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)

    required = {"Open", "High", "Low", "Close"}
    if not required.issubset(work.columns):
        return None

    if lookback_days and len(work) > lookback_days:
        work = work.tail(lookback_days).reset_index(drop=True)

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

    marks = marks or []
    if marks:
        mark_dates = pd.to_datetime(pd.Series(marks), errors="coerce").dropna()
        for mdate in mark_dates:
            matched = work[work["Date"] == mdate]
            if matched.empty:
                continue
            mrow = matched.iloc[0]
            fig.add_annotation(
                x=mrow["Date"], y=mrow["High"], text="★", showarrow=True,
                arrowhead=2, arrowcolor="blue", ax=0, ay=-30, row=1, col=1,
            )
            fig.add_vline(x=mrow["Date"], line_width=1, line_dash="dot", line_color="blue", row=1, col=1)

    if has_volume:
        vol_colors = ["red" if c >= o else "green" for o, c in zip(work["Open"], work["Close"])]
        fig.add_trace(go.Bar(x=work["Date"], y=work["Volume"], marker_color=vol_colors, name="成交量"), row=2, col=1)

    fig.update_layout(
        title=f"{symbol} {name}｜訊號K線圖（★ = 訊號標記日）",
        xaxis_rangeslider_visible=False,
        height=480 if has_volume else 380,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
    )
    return fig


def render_signal_detail_panel(symbol: str, name: str, chart_df: pd.DataFrame, signal_details: dict, signal_marks: dict, key_suffix: str = ""):
    """顯示「訊號說明 + K線圖」，給主程式在使用者選取股票時呼叫（滿足『股票名稱要能顯示訊號說明&K線』需求）。

    key_suffix: 同一檔股票可能會在「全部訊號」與多個分頁的詳情查看器中重複出現，
    plotly_chart 需要全站唯一的 key，因此呼叫端(通常是每個分頁/區塊)應傳入不同的 key_suffix
    (例如分頁名稱)，避免 StreamlitDuplicateElementKey 錯誤。
    """
    if signal_details:
        st.markdown("**📋 訊號說明：**")
        for label, detail in signal_details.items():
            st.markdown(f"- **{label}**：{detail}")
    else:
        st.caption("此股票目前沒有觸發任何訊號說明。")

    all_marks = sorted({d for marks in (signal_marks or {}).values() for d in marks})
    fig = build_signal_chart_figure(symbol, name, chart_df, marks=all_marks)
    if fig is not None:
        chart_key = f"signal_chart_{symbol}_{key_suffix}" if key_suffix else f"signal_chart_{symbol}"
        st.plotly_chart(fig, use_container_width=True, key=chart_key)
    else:
        st.caption("尚未安裝 plotly 或暫無圖表資料，無法繪製K線圖。")
