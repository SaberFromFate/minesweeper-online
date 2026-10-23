@echo off
rem Запуск игры без консольного окна: pyw/pythonw, аргументы пробрасываются.
rem Для сервера (--server) оставляем консоль: там видно лог и Ctrl+C.
setlocal
cd /d "%~dp0"

echo %* | find "--server" >nul
if not errorlevel 1 (
    py -3.13 "%~dp0main.py" %*
    if errorlevel 1 pause
    goto :eof
)

where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3.13 "%~dp0main.py" %*
    goto :eof
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py" %*
    goto :eof
)

rem pyw не нашёлся — запускаем как есть, окно консоли останется.
py -3.13 "%~dp0main.py" %*
if errorlevel 1 pause
