@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   1. Training
echo ================================
echo.
python train.py
if %errorlevel% neq 0 (
    echo.
    echo ERROR: install.bat first
    pause
    exit /b
)
echo.
echo ================================
echo   2. ONNX Export
echo ================================
echo.
python export_onnx.py
echo.
pause
