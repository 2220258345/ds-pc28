FROM python:3.10-slim

WORKDIR /app

# 安装依赖 (SQLAlchemy 存储抽象 + PostgreSQL/MySQL 驱动 + 采集器依赖)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用源码和静态资源
COPY app/ ./app/
COPY static/ ./static/

# 数据库连接完全由 docker-compose.yml 的 environment 注入 (PostgreSQL)。
# 不在镜像内固化任何 DB 默认值, 避免误连 SQLite 产生空库。
EXPOSE 8000

# -u 关闭 stdout 缓冲, 使日志实时可见
CMD ["python", "-u", "app/server.py", "--port", "8000"]
