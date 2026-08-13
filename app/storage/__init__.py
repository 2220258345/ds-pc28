# -*- coding: utf-8 -*-
"""存储层包入口：统一工厂与缓存实例。"""
from __future__ import annotations

from .base import COLUMNS, SUM_ODDS, Storage, VerifyResult
from .store import SQLAlchemyStorage


_storage: Storage | None = None


def get_storage(config=None) -> Storage:
    """返回进程内共享的存储实例 (按需创建)。"""
    global _storage
    if _storage is None:
        if config is None:
            from config import get_db_config

            config = get_db_config()
        _storage = SQLAlchemyStorage(config)
    return _storage


def reset_storage() -> None:
    """释放当前实例，供测试或切换后端时使用。"""
    global _storage
    if _storage is not None:
        _storage.engine.dispose()
    _storage = None


__all__ = [
    "COLUMNS",
    "SUM_ODDS",
    "Storage",
    "VerifyResult",
    "get_storage",
    "reset_storage",
]
