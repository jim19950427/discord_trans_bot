FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 原始碼透過 docker-compose volumes 掛載，不打包進 image
# 只有 requirements.txt 變更時才需要重新建置 image

ENV CONFIG_FILE=/data/channel_config.json

VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
