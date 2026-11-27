"""pytest 全局配置：让持久化账簿在每条用例使用独立的临时 SQLite 库。

必须在导入 app.* 之前设置 LEDGER_PATH，因此本文件顶部直接写环境变量。
"""
from __future__ import annotations

import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="ledger_test_")
os.environ["LEDGER_PATH"] = os.path.join(_TMP_DIR, "test_ledger.sqlite3")
os.environ.setdefault("LEDGER_AUTO_MIGRATE", "1")

import pytest  # noqa: E402

from app.ledger import ledger  # noqa: E402
from app.seed import SEED_ROWS  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def reset_ledger_and_store():
    """每条用例前把账簿与内存仓库都还原到 seed 初始态。"""
    ledger.reset()
    store._idempotent_log.clear()
    store._resource_locks.clear()
    for name in list(store._tables.keys()):
        if name in SEED_ROWS:
            store._tables[name] = [dict(row) for row in SEED_ROWS[name]]
        else:
            store._tables[name] = []
    yield
