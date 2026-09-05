"""資料庫遷移測試。

上線後資料須保存五年（範本第六點），schema 只能以遷移演進，不能重建。
這裡把兩件事鎖住：
  1. 從零套用全部遷移，結果必須與模型定義完全一致
  2. 改了模型卻沒寫遷移時，測試要失敗——否則正式環境會出現「程式要的欄位資料庫沒有」
  3. 每個遷移都要能降級，出事時才回得去
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.models import Base

ROOT = pathlib.Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _run(args: list[str], url: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ, AML_DATABASE_URL=url)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


@pytest.fixture
def fresh_db(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'migrate.db'}"


def test_migrations_produce_the_schema_the_models_expect(fresh_db):
    """從零套用遷移後，與模型比對必須沒有任何差異。

    有差異就代表有人改了模型卻沒產生對應的遷移；
    正式環境套用後會出現「程式要的欄位資料庫沒有」而在執行期爆炸。
    """
    result = _run(["upgrade", "head"], fresh_db)
    assert result.returncode == 0, result.stderr

    engine = create_engine(fresh_db)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()

    readable = "\n".join(f"  {d}" for d in diff)
    assert not diff, f"模型與遷移不一致，請執行 alembic revision --autogenerate：\n{readable}"


def test_every_table_in_the_models_is_created(fresh_db):
    result = _run(["upgrade", "head"], fresh_db)
    assert result.returncode == 0, result.stderr
    engine = create_engine(fresh_db)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    missing = set(Base.metadata.tables) - tables
    assert not missing, f"遷移未建立下列資料表：{missing}"


def test_migrations_survive_a_full_downgrade_and_upgrade_cycle(fresh_db):
    """出事時要回得去，而且回去之後還要能再上來。

    只驗證降級不夠：PostgreSQL 的 Enum 是資料庫層級的具名型別，drop_table 不會
    一併移除，降級後重新升級會因型別已存在而失敗——真的要回滾時就回不去了。
    此情形在 SQLite 上測不出來（Enum 存成 VARCHAR），故 CI 另以 PostgreSQL 執行。
    """
    assert _run(["upgrade", "head"], fresh_db).returncode == 0
    result = _run(["downgrade", "base"], fresh_db)
    assert result.returncode == 0, result.stderr

    engine = create_engine(fresh_db)
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert not remaining, f"降級後仍殘留資料表：{remaining}"

    again = _run(["upgrade", "head"], fresh_db)
    assert again.returncode == 0, f"降級後無法重新升級：\n{again.stderr}"


@pytest.fixture
def create_all_db(tmp_path):
    """以 metadata.create_all 建出來的資料庫。

    索引、外鍵等物件的名稱由資料庫自動產生，與遷移自己命名的並不相同。
    測試環境與正式環境重建時走的都是這條路徑，降版腳本若寫死自己命名的
    物件，就只有在這裡才會失敗——偏偏那正是真的要回滾時面對的資料庫。
    """
    configured = os.environ.get("AML_TEST_DATABASE_URL")
    if not configured:
        url = f"sqlite:///{tmp_path / 'create_all.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        yield url
        return

    # 另建一個資料庫，避免降版動作影響其他測試共用的那個。
    base = make_url(configured)
    name = f"{base.database}_migrate"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # 權限不足等
        admin.dispose()
        pytest.skip(f"無法建立測試用資料庫：{exc}")

    url = base.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


def test_downgrade_works_on_a_database_built_by_create_all(create_all_db):
    """降版不得依賴遷移自己命名的物件。

    曾發生：降版腳本以自訂名稱移除外鍵，但該資料庫是 create_all 建的，
    外鍵由 PostgreSQL 自動命名，降版直接失敗。SQLite 的 batch 模式是
    整張表重建，這個錯誤在 SQLite 上測不出來。
    """
    assert _run(["stamp", "head"], create_all_db).returncode == 0

    result = _run(["downgrade", "base"], create_all_db)
    assert result.returncode == 0, f"降版失敗：\n{result.stderr}"

    again = _run(["upgrade", "head"], create_all_db)
    assert again.returncode == 0, f"降版後無法重新升級：\n{again.stderr}"


def test_there_is_exactly_one_head():
    """多個 head 代表分支未合併，upgrade head 會失敗。"""
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    heads = script.get_heads()
    assert len(heads) == 1, f"遷移有多個 head：{heads}，請以 alembic merge 合併"


def _configure_db(url: str):
    """把 app.db 的引擎指向指定資料庫，供啟動檢查測試使用。"""
    import app.db as db_module

    old_engine = db_module.engine
    db_module.engine = create_engine(url)
    return db_module, old_engine


def test_app_refuses_to_start_on_an_uninitialised_database(tmp_path):
    """結構不一致時寧可啟動就失敗。

    帶著不一致的結構繼續跑，只會在某位業務員存檔時才爆炸——
    那時已經有客戶在等，錯誤訊息也與真正的原因相距甚遠。
    """
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    db_module, old = _configure_db(url)
    try:
        with pytest.raises(RuntimeError, match="尚未初始化"):
            db_module.assert_schema_current()
    finally:
        db_module.engine.dispose()
        db_module.engine = old


def test_app_refuses_to_start_when_schema_is_behind(tmp_path, fresh_db):
    import sqlite3

    path = tmp_path / "behind.db"
    url = f"sqlite:///{path}"
    assert _run(["upgrade", "head"], url).returncode == 0
    conn = sqlite3.connect(path)
    conn.execute("update alembic_version set version_num='0000deadbeef'")
    conn.commit()
    conn.close()

    db_module, old = _configure_db(url)
    try:
        with pytest.raises(RuntimeError, match="結構落後"):
            db_module.assert_schema_current()
    finally:
        db_module.engine.dispose()
        db_module.engine = old


def test_app_starts_when_schema_is_current(tmp_path):
    url = f"sqlite:///{tmp_path / 'ok.db'}"
    assert _run(["upgrade", "head"], url).returncode == 0
    db_module, old = _configure_db(url)
    try:
        db_module.assert_schema_current()  # 不應拋出
        current, head = db_module.schema_revisions()
        assert current == head is not None
    finally:
        db_module.engine.dispose()
        db_module.engine = old
