FROM python:3.10-slim

WORKDIR /app

# 安装依赖 (pc89.net 采集器需要 pycryptodome 解密 AES 接口)
RUN pip install --no-cache-dir pycryptodome

# 复制应用源码和静态资源
COPY app/ ./app/
COPY static/ ./static/

# 数据持久化目录 (SQLite 数据库存放于此)
RUN mkdir -p /app/data
ENV DB_DIR=/app/data

EXPOSE 8000
VOLUME ["/app/data"]

# -u 关闭 stdout 缓冲, 使日志实时可见
CMD ["python", "-u", "app/server.py", "--port", "8000"]
