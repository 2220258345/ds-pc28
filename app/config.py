# -*- coding: utf-8 -*-
"""统一运行配置。

数据库连接支持三种后端，优先级如下：
  1. DB_URI          完整连接串，例如:
       postgresql+psycopg2://user:pass@127.0.0.1:5432/pc28
       mysql+pymysql://user:pass@127.0.0.1:3306/pc28?charset=utf8mb4
       sqlite:///C:/path/to/pc28_history.db
  2. DB_BACKEND      显式选择后端 (sqlite | postgres | mysql)，配合以下变量:
       sqlite:  DB_DIR           数据库目录 (缺省为项目根目录)
       postgres: DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD [DB_DRIVER=psycopg2]
       mysql:    DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD [DB_DRIVER=pymysql]

本模块只负责读取配置并生成 SQLAlchemy URL，不做任何连接。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SQLITE_PATH = os.path.join(BASE_DIR, "pc28_history.db")


@dataclass
class DBConfig:
    backend: str                 # sqlite | postgres | mysql
    path: str | None = None      # sqlite 专用
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    driver: str | None = None    # postgres 默认 psycopg2, mysql 默认 pymysql

    def sqlalchemy_url(self):
        """返回 sqlalchemy.engine.URL 对象 (create_engine 可直接接受)。"""
        from sqlalchemy.engine import URL

        if self.backend == "sqlite":
            path = os.path.abspath(self.path or DEFAULT_SQLITE_PATH)
            return URL.create("sqlite", database=path)

        if self.backend == "postgres":
            drivername = f"postgresql+{self.driver or 'psycopg2'}"
            return URL.create(
                drivername,
                username=self.username,
                password=self.password,
                host=self.host or "127.0.0.1",
                port=self.port or 5432,
                database=self.database or "pc28",
            )

        if self.backend == "mysql":
            drivername = f"mysql+{self.driver or 'pymysql'}"
            return URL.create(
                drivername,
                username=self.username,
                password=self.password,
                host=self.host or "127.0.0.1",
                port=self.port or 3306,
                database=self.database or "pc28",
                query={"charset": "utf8mb4"},
            )

        raise ValueError(f"不支持的数据库后端: {self.backend}")


def _sqlite_path_from_uri(uri: str) -> str:
    """从 sqlite:// URI 中提取文件路径 (兼容 Windows 盘符)。"""
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        prefix = "sqlite://"
    path = uri[len(prefix):]
    path = unquote(path)
    # sqlite:///C:/x -> C:/x ; sqlite:////C:/x -> /C:/x
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if not path:
        path = DEFAULT_SQLITE_PATH
    return path


def _from_uri(uri: str) -> DBConfig:
    p = urlsplit(uri)
    scheme = (p.scheme or "").lower()

    if scheme.startswith("sqlite"):
        return DBConfig(backend="sqlite", path=_sqlite_path_from_uri(uri))

    if scheme.startswith("postgres") or scheme.startswith("postgresql"):
        driver = scheme.split("+", 1)[1] if "+" in scheme else None
        return DBConfig(
            backend="postgres",
            host=p.hostname,
            port=p.port,
            database=(p.path or "").lstrip("/") or None,
            username=unquote(p.username) if p.username else None,
            password=unquote(p.password) if p.password else None,
            driver=driver,
        )

    if scheme.startswith("mysql"):
        driver = scheme.split("+", 1)[1] if "+" in scheme else None
        return DBConfig(
            backend="mysql",
            host=p.hostname,
            port=p.port,
            database=(p.path or "").lstrip("/") or None,
            username=unquote(p.username) if p.username else None,
            password=unquote(p.password) if p.password else None,
            driver=driver,
        )

    raise ValueError(f"无法识别的 DB_URI scheme: {scheme}")


def get_db_config() -> DBConfig:
    """读取数据库配置，返回 DBConfig。"""
    uri = os.environ.get("DB_URI")
    if uri:
        return _from_uri(uri)

    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    if backend not in ("sqlite", "postgres", "mysql"):
        raise ValueError(f"DB_BACKEND 仅支持 sqlite/postgres/mysql: {backend}")

    if backend == "sqlite":
        db_dir = os.environ.get("DB_DIR")
        path = os.path.join(db_dir, "pc28_history.db") if db_dir else DEFAULT_SQLITE_PATH
        return DBConfig(backend="sqlite", path=path)

    return DBConfig(
        backend=backend,
        host=os.environ.get("DB_HOST"),
        port=int(os.environ["DB_PORT"]) if os.environ.get("DB_PORT") else None,
        database=os.environ.get("DB_NAME"),
        username=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        driver=os.environ.get("DB_DRIVER"),
    )
