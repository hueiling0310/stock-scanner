"""
signals/rise_threshold.py
==========================
掃描條件：漲幅達標。
"""

from .context import SignalContext


def check_rise_threshold_signal(ctx: SignalContext, params: dict) -> dict:
    """漲幅達標：當日漲幅 >= 門檻值（門檻來自側邊欄「儀表板漲幅達標門檻」）"""
    threshold = params.get("threshold", 5.0)
    triggered = ctx.change_pct >= threshold
    return {"labels": ["漲幅達標"] if triggered else [], "fields": {}, "extra": None}


