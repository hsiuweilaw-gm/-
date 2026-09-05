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
