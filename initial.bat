@echo off
chcp 65001 >nul


echo =========================
echo 開始安裝初始套件
echo =========================

py -m pip install --upgrade pip

py -m pip install ^
requests ^
pandas ^
streamlit ^
openpyxl ^
yfinance

echo.
echo ✅ 全部安裝完成！
pause