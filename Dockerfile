FROM python:3.12-alpine

LABEL org.opencontainers.image.title="notice-hub" \
      org.opencontainers.image.description="重要通知中心 - 企微/Outlook 通知归档与检索"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8091 \
    NOTICE_DATA_DIR=/app/data

WORKDIR /app

COPY server.py /app/server.py
COPY public /app/public

RUN mkdir -p /app/data

EXPOSE 8091

# 健康检查用 busybox wget，不用 python -c：
# 每 30 秒拉起一个 Python 解释器要 ~0.47s CPU，一天下来白烧 20 多分钟 CPU；
# wget 来自 alpine 自带的 busybox，耗时几乎为 0。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8091/api/health || exit 1

CMD ["python", "/app/server.py"]
