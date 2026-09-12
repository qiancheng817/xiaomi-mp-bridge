FROM python:3.12-alpine

LABEL org.opencontainers.image.title="xiaomi-mp-bridge"
LABEL org.opencontainers.image.description="小爱音箱 -> SongLoft Webhook -> MoviePilot v2 订阅桥接服务"
LABEL org.opencontainers.image.source="https://github.com/qiancheng817/xiaomi-mp-bridge"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=9080 \
    TZ=Asia/Shanghai

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 9080

CMD ["python", "-u", "app.py"]
