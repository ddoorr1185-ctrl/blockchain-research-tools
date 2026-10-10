"""Points the whole test session at an isolated SQLite file so tests never
touch a real local radar.db. Must set the env var before any app module
that reads app.config.DB_PATH is imported.
"""
import os
import tempfile

_tmp_db = tempfile.NamedTemporaryFile(prefix="csr_test_", suffix=".db", delete=False)
os.environ["CSR_DB_PATH"] = _tmp_db.name

import pytest

from app.db import init_db, get_conn

init_db()


@pytest.fixture(autouse=True)
def clean_tables():
    with get_conn() as conn:
        for table in [
            "alerts", "events", "raw_items", "account_monitor_state",
            "official_accounts", "coin_rank_history", "coins",
            "source_health", "scheduler_state",
        ]:
            conn.execute(f"DELETE FROM {table}")
    yield
