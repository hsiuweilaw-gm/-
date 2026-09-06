@echo off
chcp 65001 >nul
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo 尚未安裝。請先點兩下同一資料夾中的「安裝.bat」。
    echo.
    pause
    exit /b 1
)

echo 檢查資料庫結構...
".venv\Scripts\python.exe" -m alembic upgrade head
if errorlevel 1 (
    echo.
    echo [資料庫更新失敗] 請把上方錯誤訊息整段複製回報。
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   系統啟動中，稍候會自動開啟瀏覽器
echo ------------------------------------------------------------
echo   網址：http://127.0.0.1:8000
echo.
echo   ★ 這個視窗請保持開著，關掉系統就停了
echo   ★ 要停止系統，在此視窗按 Ctrl + C
echo ============================================================
echo.

start "" http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app
pause
