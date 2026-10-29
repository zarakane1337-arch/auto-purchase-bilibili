@echo off
chcp 65001 >nul
cd /d "%~dp0"
title bilibili 会员购 · 自动抢票

where python >nul 2>nul
if not errorlevel 1 goto :runpy
where py >nul 2>nul
if not errorlevel 1 goto :runlauncher
echo 没有找到 Python。请先安装 Python 3.10 或更高版本，安装时勾选 "Add Python to PATH"。
echo 下载地址：https://www.python.org/downloads/
echo.
echo 服务已退出，按任意键关闭窗口。
pause >nul
exit /b 1

:runpy
python server.py %*
goto :done

:runlauncher
py -3 server.py %*
goto :done

:done
echo.
echo 服务已退出，按任意键关闭窗口。
pause >nul
