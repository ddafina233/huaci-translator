@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "划词翻译.pyw"
    exit /b 0
)
where pyw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pyw.exe "划词翻译.pyw"
    exit /b 0
)
echo 未找到 pythonw.exe，尝试用 python.exe 启动（会显示错误信息）...
python "划词翻译.pyw"
pause