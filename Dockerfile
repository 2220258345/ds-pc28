FROM python:3.10-slim

WORKDIR /app

# 仅复制服务运行所需文件 (全部使用 Python 标准库, 无需 pip 安装)
COPY server.py collector.py backtest_e9.py backtest_chart.html ./

# 数据持久化目录 (SQLite 数据库存放于此)
RUN mkdir -p /app/data
ENV DB_DIR=/app/data

EXPOSE 8000
VOLUME ["/app/data"]

# -u 关闭 stdout 缓冲, 使日志实时可见
CMD ["python", "-u", "server.py", "--port", "8000"]
