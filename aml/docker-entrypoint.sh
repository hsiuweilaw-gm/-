#!/bin/sh
# 先套用資料庫遷移，再啟動應用程式。
#
# 遷移在此執行而非應用程式內部：多個 worker 同時啟動時各自跑遷移會互相競爭。
# 本容器為單一實例，順序執行是安全的；若日後水平擴充，應改為獨立的遷移工作。
set -e

echo "套用資料庫遷移…"
alembic upgrade head

echo "啟動應用程式…"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
