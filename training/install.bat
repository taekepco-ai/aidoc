@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   Install
echo ================================
echo.
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
pip install transformers peft datasets accelerate bitsandbytes optimum onnx onnxruntime
echo.
echo Done\!
pause
