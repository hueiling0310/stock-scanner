"""
signals/macd.py
================
掃描條件：MACD 柱狀圖翻正 / 翻負。
"""

from .context import SignalContext


def check_macd_signal(ctx: SignalContext, params: dict) -> dict:
    """MACD 柱狀圖翻正 / 翻負"""
    hist_t, hist_y = ctx.macd_hist_t, ctx.macd_hist_y
    if hist_y <= 0 and hist_t > 0:
        label = "MACD翻正"
    elif hist_y >= 0 and hist_t < 0:
        label = "MACD翻負"
    else:
        label = "-"
    triggered = label == "MACD翻正"
    fields = {"MACD柱": round(hist_t, 4), "MACD訊號": label}
    return {"labels": [label] if triggered else [], "fields": fields, "extra": None}


