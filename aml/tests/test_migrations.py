"""資料庫遷移測試。

上線後資料須保存五年（範本第六點），schema 只能以遷移演進，不能重建。
這裡把兩件事鎖住：
  1. 從零套用全部遷移，結果必須與模型定義完全一致
  2. 改了模型卻沒寫遷移時，測試要失敗——否則正式環境會出現「程式要的欄位資料庫沒有」
  3. 每個遷移都要能降級，出事時才回得去
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

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


def test_migrations_can_be_downgraded_to_base(fresh_db):
    """出事時要回得去。降級失敗代表遷移只能單向，正式環境沒有退路。"""
    assert _run(["upgrade", "head"], fresh_db).returncode == 0
    result = _run(["downgrade", "base"], fresh_db)
    assert result.returncode == 0, result.stderr

    engine = create_engine(fresh_db)
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert not remaining, f"降級後仍殘留資料表：{remaining}"


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
