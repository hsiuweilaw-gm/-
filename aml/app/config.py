"""系統組態。所有機密一律由環境變數注入，不得寫入原始碼或版控。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AML_", env_file=".env", extra="ignore")

    # 資料庫。正式環境用 PostgreSQL；測試預設 SQLite。
    database_url: str = "sqlite:///./aml.db"

    # 會話簽章金鑰。正式環境必須覆寫。
    secret_key: str = "dev-only-secret-change-me"

    # 個資加密主金鑰（32 bytes，base64url 編碼）。正式環境必須覆寫。
    # 產生方式：
    #   python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    pii_key: str = "ZGV2LW9ubHktcGlpLWtleS0zMi1ieXRlcy1ub3Qtc2FmZQ=="

    session_max_age_seconds: int = 8 * 60 * 60  # 上班日 8 小時後強制重新登入

    # 公司名稱。用於報表表頭與驗證器 App 顯示的發行者名稱。
    company_name: str = "看見保險經紀人股份有限公司"

    # 雙因素驗證。系統存有客戶身分證字號，對外開放時僅靠帳密不足；
    # 設為 false 僅供封閉內網之教育訓練環境使用。
    totp_required: bool = True
    # 應用程式前方有幾層反向代理。
    #
    # X-Forwarded-For 的左半段完全由客戶端控制：Nginx 的 $proxy_add_x_forwarded_for
    # 是把真實來源「接在後面」，客戶端自己先塞一個值進去，最左邊那段就是偽造的。
    # 取值必須從右邊往回數，數幾層由此設定決定，不能猜。
    #
    #   0 = 直接對外（無代理），一律以連線對端為準，完全忽略此標頭
    #   1 = 前方一層反向代理（README 所述之標準部署）
    #   2 = 例如 CDN 之後再接自架反向代理
    trusted_proxy_hops: int = 1

    # 洗防專責、內部稽核、系統管理者可存取的來源位址（以逗號分隔的 IP 或 CIDR）。
    #
    # 這三種角色看得到客戶個資明文、可匯出全公司報表、可管理帳號，是風險最高的
    # 帳號；而他們的工作型態本來就在總公司，不需要行動力。第一線業務員與單位
    # 主管不受此限。
    #
    # 留空 = 不啟用（試辦階段或尚未確定公司對外位址時）。啟用前務必確認
    # trusted_proxy_hops 設定正確，否則比對到的會是錯的位址。
    privileged_ip_allowlist: str = ""

    # 同一來源位址在時間窗內允許的登入嘗試次數（含密碼與一次性密碼）。
    # 帳號鎖定只擋單一帳號，擋不住換帳號輪流嘗試。
    login_attempts_per_ip: int = 30
    login_attempt_window_seconds: int = 15 * 60
    cookie_secure: bool = False  # 正式環境走 HTTPS 時設為 true

    # 高風險門檻可由主管機關函令調整，故設為組態而非常數。
    # 預設沿用現行紙本檢核表：30 分以上（含）為高風險。
    high_risk_threshold: int = 30

    # 年度報表各構面「平均風險分數 → 風險等級」門檻（依內部風險胃納訂定，可調）。
    aggregate_high_min: float = 3.0
    aggregate_medium_min: float = 2.0

    # 定期審查週期（月）。高風險客戶須較頻繁複核（問答集 Q8：「特別是高風險客戶」）。
    review_months_high: int = 12
    review_months_general: int = 36

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""  # 留空則不自動建立管理者


@lru_cache
def get_settings() -> Settings:
    return Settings()
