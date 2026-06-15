
@echo off

REM 啟動 streamlit（在背景執行）
start "" cmd /k streamlit run app.py

REM 等待 5 秒
timeout /t 5 /nobreak >nul

REM 用 Chrome 開啟
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" http://localhost:8501
