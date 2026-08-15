# -*- coding: utf-8 -*-
"""
pages/1_🎯_巧妙點掃描.py
========================
獨立頁面：巧妙點 掃描條件
- 條件1：K線型態符合十字線家族 (實體極小，依據上下影線比例區分 十字/T字/倒T字)
- 條件2：今日成交量 < N日均量 × 門檻%（預設 10日、100%，可調整）
- 使用「獨立」的股票掃描清單檔案（不吃主頁面的 TWstocklistname2.txt / 分組清單）
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import common_fubon as cf

# ===== 輔助函式：自動判別 .TW 或 .TWO =====
@st.cache_data(ttl=86400)
def load_code_to_ticker_map(filepath="TWstocklistname2.txt"):
    mapping = {}
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split()
                if parts:
                    ticker = parts[0].strip().upper()
                    if "." in ticker:
                        code = ticker.split(".")[0]
                        mapping[code] = ticker
    return mapping

def symbol_to_code(symbol: str) -> str:
    return str(symbol).split(".")[0]

def normalize_symbol_quick(input_text: str):
    s = str(input_text).strip().upper()
    if not s: return None
    if "." in s: return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")): return f"{s}.TWO"
        return f"{s}.TW"
    return s

def build_yfinance_candidates(symbol: str, code_map: dict = None):
    code_map = code_map or {}
    raw = str(symbol).strip().upper()
    code = symbol_to_code(raw)

    if "." in raw: return [raw]
    if code in code_map: return [code_map[code]]

    candidates = []
    normalized = normalize_symbol_quick(raw)
    if normalized: candidates.append(normalized)
    if code:
        for suffix in (".TW", ".TWO"):
            cand = f"{code}{suffix}"
            if cand not in candidates:
                candidates.append(cand)

    result, seen = [], set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result

def parse_raw_symbols(text: str) -> list:
    symbols = []
    if not text: return symbols
    text = text.replace("\ufeff", "").replace("\u3000", " ").replace(",", "\n")
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        raw_code = line.split()[0].upper()
        if raw_code and raw_code not in symbols:
            symbols.append(raw_code)
    return symbols

# ===== 防阻擋的安全分批下載函數 =====
def safe_batch_yfinance_download(candidates: tuple, today_str: str, period="4mo"):
    """
    將大量代碼拆分成每批 40 檔進行下載，避免因猜測後綴產生太多無效代碼
    而導致 Yahoo Finance 判定為異常流量並阻擋整批請求。
    """
    if cf.yf is None or not candidates:
        return {}
    
    result_map = {}
    chunk_size = 40
    cand_list = list(candidates)
    today = pd.to_datetime(today_str).date()
    
    for i in range(0, len(cand_list), chunk_size):
        chunk = cand_list[i:i+chunk_size]
        try:
            raw = cf.yf.download(
                tickers=chunk, period=period, interval="1d",
                auto_adjust=False, group_by="ticker", threads=True, progress=False
            )
            if raw is None or raw.empty:
                continue
                
            is_multi = isinstance(raw.columns, pd.MultiIndex)
            for sym in chunk:
                try:
                    if is_multi:
                        if sym not in raw.columns.get_level_values(0): continue
                        df = raw[sym].copy()
                    else:
                        if len(chunk) > 1: continue
                        df = raw.copy()
                    
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                    df = df.reset_index()
                    date_col = "Date" if "Date" in df.columns else df.columns[0]
                    df["Date"] = pd.to_datetime(df[date_col], errors="coerce").dt.date
                    for col in ["Open", "High", "Low", "Close", "Volume"]:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                    df = df.dropna(subset=["Date", "Open", "High", "Low", "Close"]).reset_index(drop=True)
                    
                    if df.empty: continue
                        
                    if period == "4mo":
                        result_map[sym] = df[df["Date"] < today].reset_index(drop=True)
                    else:
                        result_map[sym] = df[df["Date"] >= today].reset_index(drop=True)
                except Exception:
                    continue
        except Exception:
            continue
            
    return result_map
# ==========================================

# ===== 巧妙點專用資料獲取包裝器 =====
def get_qmd_stock_data_safe(symbol, _sdk, source, today_str, history_map, yf_today_map, min_rows):
    """
    巧妙點專屬資料獲取邏輯：
    1. 完全阻斷單檔 Yfinance 請求：歷史資料僅從批次下載的 map 讀取，避免觸發限流。
    2. 強化備援邏輯：當「資料筆數不足(min_rows)」時，強制觸發富邦 90 天完整歷史備援。
    """
    history_df = history_map.get(symbol, pd.DataFrame())
    
    if source == "Yfinance":
        today_df = yf_today_map.get(symbol, pd.DataFrame())
    else:
        # WebSocket 盤中模式：只跟富邦要當天單日 K 線
        today_df = cf.download_stock_data_fubon_today(symbol, _sdk, today_str) if _sdk is not None else pd.DataFrame()
        
    frames = [d for d in [history_df, today_df] if d is not None and not d.empty]
    if frames:
        df = pd.concat(frames, ignore_index=True)
        if "Date" in df.columns:
            df = df.drop_duplicates(subset=["Date"], keep="last").sort_values("Date").reset_index(drop=True)
    else:
        df = pd.DataFrame()
        
    df = cf.normalize_ohlc(df)
    
    # 🔥 關鍵備援：如果筆數不夠（包含空資料或中途停牌導致天數不足），直接退回富邦 90 天歷史
    if len(df) < min_rows and _sdk is not None:
        fubon_df = cf.download_stock_data(symbol, _sdk)
        fubon_df = cf.normalize_ohlc(fubon_df)
        if len(fubon_df) > len(df):
            df = fubon_df
            
    return df
# ==========================================

# ===== 頁面基本設定 =====
st.set_page_config(page_title="巧妙點掃描", layout="wide")

QIAOMIAO_STOCK_FILE = "TWstocklistname_QiaoMiaoDian.txt"

cf.ensure_fubon_session_state()
if "qmd_scan_enabled" not in st.session_state: st.session_state.qmd_scan_enabled = False
if "qmd_scan_requested" not in st.session_state: st.session_state.qmd_scan_requested = False
if "qmd_last_scan_result" not in st.session_state: st.session_state.qmd_last_scan_result = None
if "qmd_manual_symbols_text" not in st.session_state: st.session_state.qmd_manual_symbols_text = ""

st.title("🎯 巧妙點掃描")
st.caption("條件１：實體佔全距(高-低)比例極小，且具有長影線特徵。條件２：今日成交量 < N日均量 × 門檻%。兩條件同時成立才算命中。")

# ===== 側邊欄：富邦連線 / 資料來源 =====
cf.render_fubon_login_sidebar()
tw_now = datetime.now(ZoneInfo("Asia/Taipei"))
active_price_source = cf.render_price_source_selector_sidebar(tw_now)

# ===== 側邊欄：巧妙點掃描條件 =====
with st.sidebar.expander("⚙️ 巧妙點掃描條件", expanded=False):
    body_threshold = st.number_input(
        "條件１：實體佔全距上限 (%)",
        min_value=0.0, max_value=90.0, value=60.0, step=5.0, format="%.1f",
        help="實體(|收-開|) 佔 全日振幅(高-低) 的百分比上限。預設 < 10% 視為十字線家族。",
    )
    shadow_threshold = st.number_input(
        "條件１：長影線最低門檻 (%)",
        min_value=0.0, max_value=100.0, value=60.0, step=5.0, format="%.1f",
        help="單邊影線長度需佔全日振幅大於此門檻，才符合 T字線 或 倒T字線 特徵。",
    )
    vol_ma_days = st.number_input(
        "均量天數 N（日）", min_value=2, max_value=60, value=10, step=1,
    )
    vol_ratio_threshold = st.number_input(
        f"條件２：成交量 / {int(vol_ma_days)}日均量 上限 (%)",
        min_value=0.0, max_value=500.0, value=100.0, step=5.0, format="%.1f",
    )
    vol_ma_include_today = st.checkbox(
        f"{int(vol_ma_days)}日均量計算是否包含當日", value=False,
    )
    min_volume_lots = st.number_input(
        "最低成交量下限 (張)，過濾冷門股", min_value=0, value=900, step=50,
    )

# ===== 側邊欄：獨立股票清單來源 =====
with st.sidebar.expander("📋 巧妙點股票清單", expanded=True):
    list_source_mode = st.radio(
        "清單來源", options=["預設清單檔案", "上傳清單檔案", "手動輸入代碼"], index=0, key="qmd_list_source_mode",
    )
    uploaded_symbols = None
    if list_source_mode == "預設清單檔案":
        st.caption(f"讀取檔案：`{QIAOMIAO_STOCK_FILE}`")
        if not os.path.exists(QIAOMIAO_STOCK_FILE):
            st.warning(f"尚未找到 {QIAOMIAO_STOCK_FILE}，請改用「上傳清單檔案」或「手動輸入代碼」。")
    elif list_source_mode == "上傳清單檔案":
        upload_file = st.file_uploader("上傳巧妙點股票清單 (.txt)", type=["txt"], key="qmd_upload_file")
        if upload_file is not None:
            content = upload_file.read().decode("utf-8-sig", errors="ignore")
            uploaded_symbols = parse_raw_symbols(content)
            st.success(f"已解析 {len(uploaded_symbols)} 檔股票代碼")
    else:
        st.session_state.qmd_manual_symbols_text = st.text_area(
            "手動輸入代碼（每行一檔，或用逗號分隔）", value=st.session_state.qmd_manual_symbols_text, height=140,
        )

# ===== 解析出這次掃描要用的股票清單 =====
if list_source_mode == "預設清單檔案":
    if os.path.exists(QIAOMIAO_STOCK_FILE):
        with open(QIAOMIAO_STOCK_FILE, "r", encoding="utf-8-sig", errors="ignore") as f:
            scan_symbols = parse_raw_symbols(f.read())
    else: scan_symbols = []
elif list_source_mode == "上傳清單檔案": scan_symbols = uploaded_symbols or []
else: scan_symbols = parse_raw_symbols(st.session_state.qmd_manual_symbols_text)

st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}｜價格來源：{active_price_source}｜清單來源：{list_source_mode}｜清單股票數：{len(scan_symbols)}")

# ===== 掃描按鈕 =====
btn_col1, btn_col2, toggle_col, spacer_col = st.columns([0.9, 0.9, 1.4, 3.8])
with btn_col1:
    if st.button("▶️ 開始掃描", use_container_width=True, disabled=st.session_state.qmd_scan_enabled, key="qmd_start_btn"):
        st.session_state.qmd_scan_enabled = True
        st.session_state.qmd_scan_requested = True
        st.cache_data.clear()
        st.rerun()
with btn_col2:
    if st.button("⏹️ 停止掃描", use_container_width=True, disabled=not st.session_state.qmd_scan_enabled, key="qmd_stop_btn"):
        st.session_state.qmd_scan_enabled = False
        st.session_state.qmd_scan_requested = False
        st.rerun()
with toggle_col:
    show_only_hits = st.toggle("只顯示命中巧妙點的股票", value=True, key="qmd_show_only_hits")

if st.session_state.qmd_scan_enabled: st.caption("🟢 掃描狀態：執行中")
elif st.session_state.qmd_last_scan_result: st.caption(f"✅ 掃描狀態：已完成，上次完成時間：{st.session_state.qmd_last_scan_result.get('scan_completed_at', '-')}")
else: st.caption("⚪ 掃描狀態：已停止，按「開始掃描」才會抓取資料。")

# ===== 前置檢查 =====
if not scan_symbols:
    st.info("目前清單內沒有股票代碼，請先設定「巧妙點股票清單」。")
    st.stop()

should_run_scan = bool(st.session_state.pop("qmd_scan_requested", False))
has_last_result = st.session_state.qmd_last_scan_result is not None

progress_placeholder = st.empty()

if not should_run_scan and not has_last_result:
    st.info("請按「開始掃描」開始抓取股票資料。")
    st.stop()

# ===== 掃描主邏輯 =====
if should_run_scan:
    scan_today_str = tw_now.strftime("%Y-%m-%d")
    unique_raw_symbols = tuple(sorted(set(scan_symbols)))
    code_map = load_code_to_ticker_map("TWstocklistname2.txt")

    all_candidates = []
    for original_symbol in unique_raw_symbols:
        all_candidates.extend(build_yfinance_candidates(original_symbol, code_map))
    all_unique_candidates = tuple(sorted(set(all_candidates)))

    # 🚀 使用安全的分批下載，避免 YF 阻擋整批資料
    if cf.yf is not None:
        yf_history_map = safe_batch_yfinance_download(all_unique_candidates, scan_today_str, "4mo")
        if active_price_source == "Yfinance":
            yf_today_map = safe_batch_yfinance_download(all_unique_candidates, scan_today_str, "5d")
        else:
            yf_today_map = {}
    else:
        yf_history_map = {}
        yf_today_map = {}

    total_count = len(unique_raw_symbols)
    progress_bar = progress_placeholder.progress(0, text=f"掃描進度：0.0%（準備掃描 {total_count} 檔股票）")

    all_rows = []
    hit_rows = []
    error_count = 0
    need_days = int(vol_ma_days) + (0 if vol_ma_include_today else 1)

    for idx, original_symbol in enumerate(unique_raw_symbols, start=1):
        if not st.session_state.qmd_scan_enabled:
            progress_placeholder.empty()
            st.warning("掃描已停止。")
            st.stop()

        progress_value = min(idx / total_count, 1.0) if total_count else 1.0
        progress_bar.progress(progress_value, text=f"掃描進度：{progress_value*100:.1f}%（{idx}/{total_count}：{original_symbol}）")

        try:
            candidates = build_yfinance_candidates(original_symbol, code_map)
            df = pd.DataFrame()
            valid_symbol = original_symbol
            
            for candidate in candidates:
                try:
                    # 改用自訂的安全獲取包裝器，並傳入 min_rows 參數觸發歷史備援
                    temp_df = get_qmd_stock_data_safe(
                        candidate, st.session_state.fubon_sdk, active_price_source, scan_today_str,
                        history_map=yf_history_map, yf_today_map=yf_today_map, min_rows=need_days + 1
                    )
                    if not temp_df.empty and len(temp_df) >= need_days + 1:
                        df = temp_df
                        valid_symbol = candidate
                        break  
                except Exception: continue

            if df.empty or len(df) < need_days + 1: raise ValueError("歷史資料不足，無法計算均量")

            open_price = df["Open"].iloc[-1]
            if pd.isna(open_price) or open_price == 0: raise ValueError("今日尚無有效開盤資料")
            open_price = float(open_price)

            price = cf.get_last_price_by_source(valid_symbol, df, st.session_state.fubon_sdk, active_price_source)
            high_price = float(df["High"].iloc[-1])
            low_price = float(df["Low"].iloc[-1])
            stock_name = cf.get_stock_name(valid_symbol, st.session_state.fubon_sdk)

            volume_series = pd.to_numeric(df["Volume"], errors="coerce")
            today_volume = float(volume_series.iloc[-1]) if pd.notna(volume_series.iloc[-1]) else 0.0

            if vol_ma_include_today: vol_ma_window = volume_series.tail(int(vol_ma_days))
            else: vol_ma_window = volume_series.iloc[-(int(vol_ma_days) + 1):-1]

            if vol_ma_window.empty or vol_ma_window.isna().all(): raise ValueError("均量資料不足")
            vol_ma = float(vol_ma_window.mean())
            if vol_ma <= 0: raise ValueError("均量資料異常")
                
            vol_ratio_pct = today_volume / vol_ma * 100
            today_volume_lots = today_volume / 1000
            vol_ma_lots = vol_ma / 1000

            k_range = high_price - low_price
            body_size = abs(price - open_price)
            upper_shadow = high_price - max(open_price, price)
            lower_shadow = min(open_price, price) - low_price

            if k_range > 0:
                body_ratio_pct = (body_size / k_range) * 100
                upper_shadow_pct = (upper_shadow / k_range) * 100
                lower_shadow_pct = (lower_shadow / k_range) * 100
            else:
                body_ratio_pct = upper_shadow_pct = lower_shadow_pct = 0.0

            k_pattern = "-"
            if body_ratio_pct <= body_threshold:
                if lower_shadow_pct >= shadow_threshold and upper_shadow_pct <= body_threshold: k_pattern = "T字線"
                elif upper_shadow_pct >= shadow_threshold and lower_shadow_pct <= body_threshold: k_pattern = "倒T字線"
                else: k_pattern = "十字線"

            passes_k_pattern = (k_pattern != "-")
            passes_vol = (vol_ratio_pct < vol_ratio_threshold)
            passes_min_volume = (today_volume_lots >= float(min_volume_lots))
            
            is_hit = passes_k_pattern and passes_vol and passes_min_volume

            row = {
                "代碼": valid_symbol, "代碼網址": cf.yahoo_quote_url(valid_symbol), "股票名稱": stock_name,
                "開盤": round(open_price, 2), "現價": round(price, 2), "型態": k_pattern,
                "實體佔比%": round(body_ratio_pct, 1), "上影佔比%": round(upper_shadow_pct, 1), "下影佔比%": round(lower_shadow_pct, 1),
                "成交量(張)": round(today_volume_lots, 1), f"{int(vol_ma_days)}日均量(張)": round(vol_ma_lots, 1), "量比%": round(vol_ratio_pct, 1),
                "是否命中巧妙點": "✅ 是" if is_hit else "否", "來源": active_price_source,
            }
            
            all_rows.append(row)
            if is_hit: hit_rows.append(row)
                
        except Exception as e:
            error_count += 1
            all_rows.append({
                "代碼": original_symbol, "代碼網址": "", "股票名稱": cf.get_stock_name(original_symbol, st.session_state.fubon_sdk),
                "開盤": "-", "現價": "錯誤", "型態": "-", "實體佔比%": "-", "上影佔比%": "-", "下影佔比%": "-", 
                "成交量(張)": "-", f"{int(vol_ma_days)}日均量(張)": "-", "量比%": "-",
                "是否命中巧妙點": f"抓取失敗: {str(e)}", "來源": active_price_source,
            })

    progress_placeholder.empty()
    st.session_state.qmd_scan_enabled = False
    st.session_state.qmd_last_scan_result = {
        "all_rows": all_rows,
        "hit_rows": hit_rows,
        "error_count": error_count,
        "scan_completed_at": tw_now.strftime("%Y-%m-%d %H:%M:%S"),
        "body_threshold": body_threshold,
        "shadow_threshold": shadow_threshold,
        "vol_ratio_threshold": vol_ratio_threshold,
        "vol_ma_days": int(vol_ma_days),
        "excel_filename": f"巧妙點_scan_{tw_now.strftime('%Y%m%d_%H%M')}.xlsx",
    }

last_result = st.session_state.qmd_last_scan_result or {}
all_rows = last_result.get("all_rows", [])
hit_rows = last_result.get("hit_rows", [])
error_count = last_result.get("error_count", 0)

# ===== 結果輸出（Excel / Telegram / TXT）=====
def build_qiaomiao_excel_bytes(hit_rows_local):
    from io import BytesIO
    columns = ["代碼", "股票名稱", "開盤", "現價", "型態", "實體佔比%", "上影佔比%", "下影佔比%", "成交量(張)", "量比%", "是否命中巧妙點", "來源"]
    df = pd.DataFrame(hit_rows_local)
    if df.empty: df = pd.DataFrame(columns=columns)
    else:
        keep_cols = [c for c in df.columns if c not in ("代碼網址",) and (c in columns or c.endswith("日均量(張)"))]
        df = df[keep_cols]
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="巧妙點命中清單", index=False)
        cf.apply_excel_fonts(writer.book)
    output.seek(0)
    return output.getvalue()

def build_export_txt(df_rows) -> bytes:
    df = pd.DataFrame(df_rows)
    if df.empty or "代碼" not in df.columns: return b""
    # 修改為只匯出股票代碼，不匯出股票名稱
    lines = df["代碼"].astype(str).str.strip().tolist()
    return "\n".join(lines).encode("utf-8-sig")

excel_bytes = build_qiaomiao_excel_bytes(hit_rows)
txt_bytes = build_export_txt(hit_rows)
excel_filename = last_result.get("excel_filename", f"QiaoMiaoDian_scan_{tw_now.strftime('%Y%m%d_%H%M%S')}.xlsx")
txt_filename = excel_filename.replace(".xlsx", ".txt")

action_col1, action_col2, action_col3 = st.columns(3)
with action_col1:
    st.download_button("下載命中清單 Excel", data=excel_bytes, file_name=excel_filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, key="qmd_download_excel_btn")
with action_col2:
    st.download_button("下載命中清單 txt", data=txt_bytes, file_name=txt_filename, mime="text/plain", use_container_width=True, key="qmd_download_txt_btn")
with action_col3:
    if st.button("推送命中清單到 Telegram", use_container_width=True, key="qmd_push_tg_btn"):
        ok = cf.send_telegram_document(excel_bytes, excel_filename, caption=f"巧妙點掃描結果｜實體佔比<{last_result.get('body_threshold', body_threshold)}%｜量比<{last_result.get('vol_ratio_threshold', vol_ratio_threshold)}%｜{tw_now.strftime('%Y-%m-%d %H:%M:%S')}")
        if ok: st.success("已將 Excel 推送到 Telegram。")

st.markdown("### 🔎 掃描結果")
m1, m2, m3 = st.columns(3)
m1.metric("命中巧妙點檔數", len(hit_rows))
m2.metric("清單股票總數", len(scan_symbols))
m3.metric("抓取失敗檔數", error_count)

# 🔥 新增：抓取失敗股票清單下拉視窗
if error_count > 0:
    with st.expander(f"⚠️ 檢視 {error_count} 檔抓取失敗清單", expanded=False):
        # 篩選出 all_rows 中，"現價" 為 "錯誤" 的資料，並擷取代碼與錯誤訊息
        error_records = [
            {"代碼": r.get("代碼", ""), "錯誤訊息": r.get("是否命中巧妙點", "")} 
            for r in all_rows if r.get("現價") == "錯誤"
        ]
        if error_records:
            st.dataframe(pd.DataFrame(error_records), use_container_width=True)

# 🔥 核心防呆：如果 0 命中，且有錯誤，強制顯示所有清單以供排查！
if show_only_hits and len(hit_rows) == 0 and error_count > 0:
    st.warning(f"⚠️ 掃描完畢，但沒有任何股票命中巧妙點，且有 **{error_count}** 檔抓取失敗。已為您自動展開所有清單，請查看上方下拉視窗或表格中的『是否命中巧妙點』欄位了解錯誤原因：")
    display_rows = all_rows
else:
    display_rows = hit_rows if show_only_hits else all_rows

display_columns = ["代碼", "股票名稱", "開盤", "現價", "型態", "實體佔比%", "上影佔比%", "下影佔比%", "成交量(張)", f"{last_result.get('vol_ma_days', int(vol_ma_days))}日均量(張)", "量比%", "是否命中巧妙點", "來源"]

if display_rows:
    display_df = pd.DataFrame(display_rows)
    if "代碼網址" in display_df.columns:
        display_df["代碼"] = display_df["代碼網址"].where(display_df["代碼網址"] != "", display_df["代碼"])
    for col in display_columns:
        if col not in display_df.columns: display_df[col] = "-"
    if "成交量(張)" in display_df.columns: display_df["成交量(張)"] = display_df["成交量(張)"].apply(cf.format_volume)
    
    vol_ma_col = f"{last_result.get('vol_ma_days', int(vol_ma_days))}日均量(張)"
    if vol_ma_col in display_df.columns: display_df[vol_ma_col] = display_df[vol_ma_col].apply(cf.format_volume)

    st.dataframe(
        display_df[display_columns], use_container_width=True,
        column_config={
            "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
        },
    )
else:
    st.info("目前沒有符合條件的股票（或清單抓取皆失敗）。")

with st.expander("ℹ️ 巧妙點條件說明"):
    st.markdown(
        f"""
- **條件１（價格與型態）**：
    - 計算全日振幅 `(最高價 - 最低價)`。
    - 實體佔比：`|收盤價 - 開盤價| ÷ 振幅 × 100%` 需小於 **{last_result.get('body_threshold', body_threshold)}%**。
    - 上/下影線佔比依據長短判斷為 **十字線**、**T字線**（下影線大於 {last_result.get('shadow_threshold', shadow_threshold)}%）、**倒T字線**（上影線大於 {last_result.get('shadow_threshold', shadow_threshold)}%）。
- **條件２（量能）**：今日成交量 `÷ {last_result.get('vol_ma_days', int(vol_ma_days))}日均量 × 100%` 需小於 **{last_result.get('vol_ratio_threshold', vol_ratio_threshold)}%**。
- 兩條件需同時成立才算「命中巧妙點」。
- 股票清單為**獨立清單**（`{QIAOMIAO_STOCK_FILE}` 或使用者自行上傳/輸入），不會套用主頁面的分組或全市場清單。
        """
    )
