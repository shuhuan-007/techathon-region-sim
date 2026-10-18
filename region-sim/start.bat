@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 区域发展模拟 · Region Sim 一键启动

rem ============================================================
rem  Region Sim · Techathon 2026 · 一键启动（建设特区）
rem  真实默认端口 = 8000（server/app.py 的 argparse default）
rem  换端口：改下面的 PORT（如 8211），浏览器地址会跟着变
rem  需要 Python 3.10+，纯标准库，无需 pip install
rem
rem  ⚠ 不能只用 where python：Windows 商店版 Python 是个 0 字节桩，
rem    会「静默成功但不干活」。所以下面逐个试跑 print(1) 验真。
rem ============================================================
set "PORT=8000"
set "HOST=127.0.0.1"

rem 想纯离线演示（不调模型，省掉等叙事的几十秒）：去掉下一行开头的 rem
rem set "OFFLINE=--offline"

rem ── 找能真正跑起来的 Python ──
set "PY="
rem 本机已知可用的解释器优先（路径无空格，后面 cmd /k 不再加引号）
if exist "D:\dsh\resources\runtime\primary-runtime\dependencies\python\python.exe" set "PY=D:\dsh\resources\runtime\primary-runtime\dependencies\python\python.exe"
if not defined PY for /f "delims=" %%v in ('py -3 -c "print(1)" 2^>nul') do set "PY=py -3"
if not defined PY for /f "delims=" %%v in ('python -c "print(1)" 2^>nul') do set "PY=python"

if not defined PY (
  echo [错误] 没找到可用的 Python。装 Python 3.10+，或把 python.exe 目录加进 PATH。
  echo        注意：Windows 商店那个 python 桩不算。
  echo.
  pause
  endlocal
  exit /b 1
)

echo [0/3] 使用解释器：%PY%

if exist ".env" (
  echo [1/3] 检测到 .env：按在线模式启动（AI 叙事 / 建议会真调模型）
) else (
  echo [1/3] 未检测到 .env：数值与界面照常，AI 叙事 / 建议会不可用
  echo       想要纯离线演示，可在本文件里打开 OFFLINE 开关
)

echo [2/3] 启动服务：%PY% server\app.py --port %PORT%
rem 注意：%PY% 不加引号（这些路径都不含空格），否则 cmd /k 里嵌套引号会解析失败
start "Region Sim 服务 - 关闭此窗口即停止" cmd /k %PY% server\app.py --port %PORT% %OFFLINE%

rem 等 3 秒，服务就绪后再开浏览器（避免刚打开时连接被拒）
timeout /t 3 /nobreak >nul

echo [3/3] 打开浏览器：http://%HOST%:%PORT%/
start "" http://%HOST%:%PORT%/

echo.
echo 服务在另一个「Region Sim 服务」窗口里运行，关掉它即停止服务。
echo 若浏览器提示无法访问：等 2 秒刷新；仍不行就确认端口 %PORT% 没被占用。
echo.
pause
endlocal
