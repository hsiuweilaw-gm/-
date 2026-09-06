@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo ============================================================
echo   洗錢防制客戶風險評估系統 - 安裝（僅供試用與評估）
echo ============================================================
echo.

set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if (3,11)<=sys.version_info<(3,14) else 1)" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo [找不到可用的 Python]
    echo.
    echo 本系統需要 Python 3.11、3.12 或 3.13。
    echo 請注意：最新的 3.14 不適用，套件尚無對應的安裝檔。
    echo.
    echo 1. 前往 https://www.python.org/downloads/windows/
    echo 2. 找到 "Python 3.13.x" 那一區，下載 Windows installer ^(64-bit^)
    echo 3. 安裝時務必勾選 "Add python.exe to PATH"
    echo 4. 安裝完成後關閉此視窗，重新點兩下本檔案
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%A in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%A"
echo 使用 Python !PYVER!
echo.

echo [1/5] 建立獨立環境...
if exist ".venv" (
    echo       已存在，沿用。
) else (
    %PY% -m venv .venv
    if errorlevel 1 goto :failed
)

echo [2/5] 安裝套件（第一次約需 1 到 3 分鐘）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

echo [3/5] 建立設定檔與加密金鑰...
if exist ".env" (
    echo       已存在，保留原有金鑰。
) else (
    rem 批次檔會吃掉單一的百分比符號，故此處不得以百分比符號做字串格式化，改用字串相接。
    ".venv\Scripts\python.exe" -c "import os,base64,secrets;open('.env','w',encoding='utf-8').write('AML_DATABASE_URL=sqlite:///./dev.db\nAML_SECRET_KEY=' + secrets.token_urlsafe(48) + '\nAML_PII_KEY=' + base64.urlsafe_b64encode(os.urandom(32)).decode() + '\n')"
    if errorlevel 1 goto :failed
)

echo [4/5] 建立資料庫...
".venv\Scripts\python.exe" -m alembic upgrade head
if errorlevel 1 goto :failed

echo [5/5] 建立示範資料...
".venv\Scripts\python.exe" -m scripts.seed_demo

echo.
echo ============================================================
echo   安裝完成
echo ------------------------------------------------------------
echo   接著請點兩下同一資料夾中的「啟動.bat」
echo.
echo   示範帳號密碼皆為 demo-password-1234
echo     agent01  業務員（看不到分數）
echo     sup_tc   台中通訊處經理
echo     aml01    洗防專責
echo     admin    系統管理者
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo [安裝未完成] 請把上方的錯誤訊息整段複製回報。
echo.
pause
exit /b 1
