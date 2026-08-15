# stock-scanner-FUBAN
stock-scanner-FUBAN
台股掃描器/
├── app.py                    # 主程式：UI + 主迴圈
├── signals/
│   ├── __init__.py            # SIGNAL_REGISTRY + compute_indicators + run_signal_registry
│   ├── context.py             # SignalContext + build_signal_context()（含KD/週KD/MACD/RS/波動率計算）
│   ├── gap.py                 # check_gap_signal
│   ├── kd.py                  # check_kd_golden_cross_signal / check_week_kd_signal
│   ├── macd.py                # check_macd_signal
│   ├── rise_threshold.py      # check_rise_threshold_signal
│   └── trend_breakout.py      # detect_downtrend_breakout + check_trend_breakout_signal + plot_trend_breakout_chart
└── common_fubon.py            # 富邦/yfinance 資料源模組
