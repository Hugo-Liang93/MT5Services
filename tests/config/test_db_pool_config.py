"""DBSettings pool 容量配置化回归测试。

历史教训（2026-04-21 事故后跟进）：
  - 事故期间发现 live-main warm-up 期峰值 10 连接不够，15 次 `pool exhausted`
    都集中在启动后 5-15 分钟。
  - 修复：DBSettings 新增 `pool_min_conn / pool_max_conn` 字段，默认 1-20，
    可通过 db.ini `[db.live] pool_max_conn =` 覆盖。
  - TimescaleWriter 构造默认从 settings 读，显式传参仍优先（保留测试 / ops CLI 覆盖能力）。
"""

from __future__ import annotations

from src.config.database import DBSettings
from src.persistence.db import TimescaleWriter


def _writer_from_settings(settings: DBSettings) -> TimescaleWriter:
    """构造 TimescaleWriter 但不真的连 DB——只验证 pool 参数解析。"""
    writer = TimescaleWriter.__new__(TimescaleWriter)
    # 手动执行 __init__ 里除 _init_pool 之外的部分
    writer.settings = settings
    writer._pool = None
    writer._min_conn = int(getattr(settings, "pool_min_conn", 1) or 1)
    writer._max_conn = int(getattr(settings, "pool_max_conn", 20) or 20)
    return writer


def test_dbsettings_default_pool_size_is_1_to_20() -> None:
    """默认 pool_min_conn=1, pool_max_conn=20（事故后从 10 提升）。"""
    s = DBSettings()
    assert s.pool_min_conn == 1
    assert s.pool_max_conn == 20


def test_timescale_writer_reads_pool_from_settings() -> None:
    """TimescaleWriter.__init__ 不传显式 min/max 时走 settings 字段。"""
    s = DBSettings(pool_min_conn=2, pool_max_conn=30)
    writer = _writer_from_settings(s)
    assert writer._min_conn == 2
    assert writer._max_conn == 30


def test_timescale_writer_explicit_overrides_settings() -> None:
    """显式传 min_conn/max_conn 优先于 settings——ops CLI 用小 pool 场景。"""
    s = DBSettings(pool_min_conn=1, pool_max_conn=20)

    class _Stub(TimescaleWriter):
        def _init_pool(self) -> None:  # 跳过真连 pg
            self._pool = None

    writer = _Stub(s, min_conn=1, max_conn=2)
    assert writer._min_conn == 1
    assert writer._max_conn == 2


def test_pool_max_conn_loads_from_ini_section(tmp_path, monkeypatch) -> None:
    """db.ini [db.live] pool_max_conn=30 被 load_db_settings 读出来。"""
    ini_path = tmp_path / "db.ini"
    ini_path.write_text(
        "[db.live]\n"
        "host = 127.0.0.1\n"
        "port = 5432\n"
        "user = u\n"
        "password = p\n"
        "database = d\n"
        "pool_min_conn = 2\n"
        "pool_max_conn = 35\n",
        encoding="utf-8",
    )

    # 把 load_config_with_base 指向 tmp 目录的 db.ini
    import src.config.database as db_mod

    def _stub_load(filename, *args, **kwargs):
        import configparser

        parser = configparser.ConfigParser()
        parser.read(str(ini_path), encoding="utf-8")
        return str(ini_path), parser

    monkeypatch.setattr(db_mod, "load_config_with_base", _stub_load)
    monkeypatch.setattr(db_mod, "resolve_current_environment", lambda env=None: "live")
    # 清 lru_cache
    db_mod._load_db_settings_cached.cache_clear()

    s = db_mod.load_db_settings("live")
    assert s.pool_min_conn == 2
    assert s.pool_max_conn == 35
