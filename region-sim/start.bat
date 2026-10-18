@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  Region Sim · Techathon 2026 · 一键启动（建设特区）
rem  用法：双击本文件 → 自动开浏览器 → http://127.0.0.1:8000/
rem  需要 Python 3.10+（仅标准库，无需 pip install）
rem ============================================================

set PORT=8000

echo [Region Sim] 启动本地服务，端口 %PORT% ...
echo [Region Sim] 若端口被占用，请改本文件里的 PORT 后重试。
echo.

start "" "http://127.0.0.1:%PORT%/"
python "server\app.py" --port %PORT%

if errorlevel 1 (
  echo.
  echo [Region Sim] 启动失败。常见原因：
  echo   1^) 没装 Python 或不叫 python —— 试试 py -3 "server\app.py" --port %PORT%
  echo   2^) 端口被占用 —— 改本文件的 PORT
  echo   3^) 缺 .env —— 不影响运行，AI 叙事会自动降级
  echo.
)

pause
endlocal
