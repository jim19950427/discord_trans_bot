FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py translator.py config.py ./

# channel_config.json is stored on the mounted volume
ENV CONFIG_FILE=/data/channel_config.json

VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
