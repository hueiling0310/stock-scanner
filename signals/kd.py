"""
signals/kd.py
==============
掃描條件：日KD / 週KD 黃金交叉、即將黃金交叉。
"""

from .context import SignalContext


def check_kd_golden_cross_signal(ctx: SignalContext, params: dict) -> dict:
    """日KD 黃金交叉 / 即將黃金交叉"""
    label = ctx.kd_signal
    triggered = label in ("黃金交叉", "即將黃金交叉")
    return {"labels": [label] if triggered else [], "fields": {}, "extra": None}


def check_week_kd_signal(ctx: SignalContext, params: dict) -> dict:
    """週KD 黃金交叉 / 即將黃金交叉（資料不足時 week_kd_signal 為 "-"，不觸發）"""
    label = ctx.week_kd_signal
    triggered = label in ("黃金交叉", "即將黃金交叉")
    return {"labels": [f"週{label}"] if triggered else [], "fields": {}, "extra": None}


