"""
signals 套件（2026 改版）
=========================
訊號判斷邏輯已全部移交給 signal_module/ 下的可編輯訊號模組：
  漲幅達標、KD高腳、周1K、三白兵、布林縮窄突破、3K反轉、巧妙點、
  雙跳空、單跳空、漲停、雙漲停、跌停、移動停利、
  廣義上升三法、廣義下降三法、島狀反轉、反向島狀。
（原本的 跳空 / 黃金交叉 / 即將黃金交叉 / 週黃金交叉 / 週即將黃金交叉 / MACD翻正 / 趨勢突破 已移除）

這個套件保留給主程式(app)呼叫的固定入口，讓主程式不用管訊號實作細節：
  - compute_indicators(df, price, symbol, name, rise_threshold) -> dict
  - get_signal_registry() -> 目前註冊的訊號清單 (可在「🛠️ 訊號編輯」頁面新增/修改後即時反映)

（2026 改版：拿掉了 K線圖繪製功能，因為對每檔命中訊號的股票即時畫 Plotly 圖
CPU 負擔過大、拖慢掃描與畫面渲染速度。現在看K線請直接點主表格「代碼」欄位的
Yahoo 股市連結，不再需要在 Streamlit 裡自己畫圖。）

（2026-08-11 修補：compute_indicators() 回傳的 data dict 原本缺少
MA10 / MA20 / VolRatioYesterday 三個欄位，導致 scoring.py 5.5節
「漲停」專屬濾網加分（MA10>MA20 且 VolRatioYesterday<=0.95 時 +15分）
data.get(...) 永遠拿到 None，這段加分從未真正生效過。
這裡從 df_ind 最後一列補上這三個欄位，直接對應 indicators.py
add_indicators() 的原始輸出欄位，不做任何分類轉換。）
"""
import pandas as pd

from signal_module import module_loader
from signal_module.base import SIGNAL_REGISTRY, SignalContext as ModuleSignalContext, SignalResult
from signal_module.indicators import add_indicators

from .context import build_base_context, build_relative_strength_fields, calc_rs_line_new_high

# 啟動時（本 process 第一次 import 這個套件時）載入一次預設訊號模組。
# 之後若在「🛠️ 訊號編輯」頁面存檔，會直接呼叫 module_loader.load_default_signal_modules()
# 重新載入 —— 因為 SIGNAL_REGISTRY 是同一個 dict 物件，全站都會立即看到最新版本。
if not SIGNAL_REGISTRY:
    module_loader.load_default_signal_modules()


def get_signal_registry():
    """回傳目前已註冊的訊號清單：{key: {"label","description","kind","func"}}"""
    return SIGNAL_REGISTRY


def _prepare_indicator_df(df: pd.DataFrame, price: float) -> pd.DataFrame:
    """
    把主程式抓回來的 df (含 Date 欄位 + OHLCV) 轉成 signal_module 需要的格式：
    index=Date字串、由舊到新排序，並附上 K/D/MA/Bias/BBand 等技術指標欄位。
    最後一筆收盤價用即時價覆蓋，貼近盤中即時狀態 (與原本 context.py 邏輯一致)。
    """
    work = df.copy()
    work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
    work = work.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    if work.empty:
        raise ValueError("下載資料為空")

    work.loc[work.index[-1], "Close"] = float(price)

    work = work.set_index(work["Date"].dt.strftime("%Y-%m-%d"))[["Open", "High", "Low", "Close", "Volume"]]
    work.index.name = "Date"
    work = add_indicators(work)
    return work


def _safe_round_or_none(value, ndigits):
    """value 是 NaN/None 時回傳 None，否則回傳四捨五入後的 float。"""
    if value is None or pd.isna(value):
        return None
    return round(float(value), ndigits)


def run_signal_registry(symbol: str, name: str, df_ind: pd.DataFrame, scan_date: str, rise_threshold: float = 5.0) -> dict:
    """對單一股票已含指標的 df 跑過全部已註冊訊號，回傳 {key: SignalResult}"""
    ctx = ModuleSignalContext(
        code=symbol, name=name, df=df_ind, scan_date=scan_date,
        params={"rise_threshold": rise_threshold},
    )
    results = {}
    for key, cfg in SIGNAL_REGISTRY.items():
        try:
            results[key] = cfg["func"](ctx)
        except Exception as e:
            results[key] = SignalResult(hit=False, detail=f"訊號執行發生錯誤：{e}")
    return results


def compute_indicators(df, price, symbol="", name="", rise_threshold=5.0, benchmark_ctx=None, benchmark_df=None, rs_line_lookback=60):
    """
    主流程呼叫入口：
    1. 計算表格/評分共用的基礎數值 (價格/漲跌/MA位置/成交量/波動率/RS)
    2. 補上今日即時價並計算 K/D/MA/Bias/BBand 等技術指標
    3. 跑過訊號登記表，收集所有命中的訊號
    4.（2026-08-16 新增）個股 vs 大盤比較：大盤MA位置/大盤MA排列/RS超額報酬%/RS Line創新高。
       benchmark_ctx / benchmark_df 由主程式在單次掃描開始前算好一次、全部股票共用傳入；
       兩者皆為 None（例如大盤資料抓取失敗）時，比較欄位一律回傳 "-"，不影響其餘既有邏輯。
       RS Rating（全市場百分位排名）需要整批掃描結果才能算，不在這裡處理，由主程式在
       全部股票掃描完後統一計算。
    """
    base = build_base_context(df, price)
    df_ind = _prepare_indicator_df(df, price)
    scan_date = df_ind.index[-1]
    latest_row = df_ind.iloc[-1]

    signal_results = run_signal_registry(symbol, name, df_ind, scan_date, rise_threshold)

    hit_keys = [key for key, res in signal_results.items() if res.hit]
    signal_types = [SIGNAL_REGISTRY[key]["label"] for key in hit_keys]
    signal_kinds = {SIGNAL_REGISTRY[key]["label"]: SIGNAL_REGISTRY[key].get("kind", "buy") for key in hit_keys}
    signal_details = {SIGNAL_REGISTRY[key]["label"]: signal_results[key].detail for key in hit_keys}
    signal_marks = {SIGNAL_REGISTRY[key]["label"]: signal_results[key].marks for key in hit_keys}
    # sub_label: 選用欄位，讓「同一個訊號、但這次觸發的細分類會變動」的訊號
    # (例如下降趨勢線突破依觸發當下區分短期/中短期/中長期) 能動態附加顯示文字，
    # 不用為了顯示細分類而拆成好幾個獨立註冊的訊號。
    # 這裡刻意跟 signal_types(比對用的固定 label) 分開存放，
    # 只用來組「訊號類型」的顯示文字，不影響 Setting 勾選/分頁分類等既有比對邏輯。
    signal_sublabels = {
        SIGNAL_REGISTRY[key]["label"]: (signal_results[key].sub_label or "")
        for key in hit_keys
    }

    rs_fields = build_relative_strength_fields(base, benchmark_ctx)
    rs_line_new_high = None
    if benchmark_df is not None and not benchmark_df.empty:
        try:
            rs_line_new_high = calc_rs_line_new_high(df, base["price"], benchmark_df, lookback=rs_line_lookback)
        except Exception:
            rs_line_new_high = None

    return {
        "price": round(base["price"], 2),
        "pct": round(base["pct"], 2),
        "ma_range": base["ma_range"],
        "ma_trend": base["ma_trend"],
        "volume": int(base["latest_volume"]),
        "volume_lots": round(base["volume_lots"], 1),
        "volatility_pct": round(base["volatility_pct"], 2) if base["volatility_pct"] is not None else "-",
        "rs_raw": round(base["rs_raw"], 2) if base["rs_raw"] is not None else "-",
        # --- 2026-08-11 新增：對應 scoring.py 5.5節「漲停」專屬濾網加分 ---
        # 直接取 df_ind 最後一列 (今日/掃描日) 的原始數值，不做分類轉換。
        # 資料筆數不足(例如第一天 VolRatioYesterday 會是 NaN)時回傳 None，
        # scoring.py 端已用 is not None 判斷會自動跳過這段加分，不影響其他邏輯。
        "MA10": _safe_round_or_none(latest_row.get("MA10"), 2),
        "MA20": _safe_round_or_none(latest_row.get("MA20"), 2),
        "VolRatioYesterday": _safe_round_or_none(latest_row.get("VolRatioYesterday"), 4),
        # ------------------------------------------------------------------
        # --- 2026-08-16 新增：個股 vs 大盤比較（詳見 signals/context.py 說明）---
        "benchmark_ma_range": rs_fields["benchmark_ma_range"],
        "benchmark_ma_trend": rs_fields["benchmark_ma_trend"],
        "rs_excess": round(rs_fields["rs_excess"], 2) if rs_fields["rs_excess"] is not None else "-",
        "rs_line_new_high": rs_line_new_high,  # True / False / None(資料不足，無法判斷)
        # ------------------------------------------------------------------
        "signal_types": signal_types,
        "signal_kinds": signal_kinds,
        "signal_details": signal_details,
        "signal_marks": signal_marks,
        "signal_sublabels": signal_sublabels,
        "signal_results": signal_results,
        "scan_date": scan_date,
    }
