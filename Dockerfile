FROM python:3.10-slim

WORKDIR /app

# 复制应用源码和静态资源 (全部使用 Python 标准库, 无需 pip 安装)
COPY app/ ./app/
COPY static/ ./static/

# 数据持久化目录 (SQLite 数据库存放于此)
RUN mkdir -p /app/data
ENV DB_DIR=/app/data

EXPOSE 8000
VOLUME ["/app/data"]

# -u 关闭 stdout 缓冲, 使日志实时可见
CMD ["python", "-u", "app/server.py", "--port", "8000"]
