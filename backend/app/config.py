"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_ledger_path() -> str:
    """持久化账簿位置，可用 LEDGER_PATH 覆盖（测试用临时库）。"""
    return os.environ.get(
        "LEDGER_PATH",
        str(Path(__file__).resolve().parent.parent / "data" / "finance_ledger.sqlite3"),
    )


@dataclass(frozen=True)
class Settings:
    app_name: str = "古建筑修缮管理平台"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 带历史版本的持久化账簿（SQLite）路径
    ledger_path: str = field(default_factory=_default_ledger_path)
    # 是否在首次访问财务数据时自动把内存存量迁移进账簿
    ledger_auto_migrate: bool = field(
        default_factory=lambda: os.environ.get("LEDGER_AUTO_MIGRATE", "1") != "0"
    )


settings = Settings()
