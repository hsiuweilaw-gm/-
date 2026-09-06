"""資料庫連線與初始化。"""
from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterator

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger("aml.db")
ROOT = pathlib.Path(__file__).resolve().parent.parent

_settings = get_settings()
if _settings.database_url.startswith("sqlite"):
    _connect_args: dict = {"check_same_thread": False}
else:
    # 關閉 psycopg 的自動預備語句。
    #
    # 預備語句的執行計畫綁定當時的資料表結構。連線是共用且長壽的，一旦
    # 在服務運行中套用遷移（新增或移除欄位），既有連線上的舊計畫就會失效，
    # PostgreSQL 回以「cached plan must not change result type」，直到重啟
    # 才恢復——正好發生在剛升級完、最不希望出事的時刻。
    #
    # 本系統的查詢都很輕，關閉預備語句的代價可忽略；日後若在前方置放
    # PgBouncer（transaction 模式），本來也必須關閉。
    _connect_args = {"prepare_threshold": None}

engine = create_engine(_settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """直接依模型建表。僅供測試與示範資料使用。

    正式環境一律以 Alembic 遷移建立與演進結構——`create_all` 只會建立缺少的資料表，
    不會修改既有資料表，上線後第一次新增欄位就會失效。
    """
    Base.metadata.create_all(engine)


def stamp_head() -> None:
    """把資料庫標記為最新版本，但不執行任何遷移。

    供測試與示範資料使用：結構已由 create_all 依模型建立，
    只需補上版本標記，啟動檢查才不會擋下。正式環境不得使用。
    """
    from alembic import command

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.stamp(config, "head")


def _script_directory() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def schema_revisions() -> tuple[str | None, str | None]:
    """回傳（資料庫目前的版本, 程式碼要求的最新版本）。"""
    head = _script_directory().get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current, head


def assert_schema_current() -> None:
    """資料庫結構落後或未初始化時，拒絕啟動。

    帶著不一致的結構繼續跑，只會在某位業務員存檔時才爆炸——
    那時已經有客戶在等，且錯誤訊息與真正的原因相距甚遠。寧可啟動就失敗。
    """
    current, head = schema_revisions()
    if current == head:
        log.info("資料庫結構為最新版本（%s）", head)
        return
    if current is None:
        raise RuntimeError(
            "資料庫尚未初始化。請先執行：alembic upgrade head"
        )
    raise RuntimeError(
        f"資料庫結構落後：目前 {current}，程式碼要求 {head}。"
        "請先執行：alembic upgrade head"
    )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
