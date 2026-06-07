FROM python:3.11-slim

WORKDIR /app

# Tesseract OCR（含主要語言包）+ fontconfig/Noto 字型（供圖片翻譯覆蓋繪製文字使用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-tra tesseract-ocr-chi-sim tesseract-ocr-jpn tesseract-ocr-kor \
    tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa tesseract-ocr-vie \
    tesseract-ocr-tha tesseract-ocr-ind tesseract-ocr-rus tesseract-ocr-ara tesseract-ocr-pol \
    fontconfig fonts-noto-cjk fonts-noto-core fonts-noto-extra \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 原始碼透過 docker-compose volumes 掛載，不打包進 image
# 只有 requirements.txt 變更時才需要重新建置 image

ENV CONFIG_FILE=/data/channel_config.json

VOLUME ["/data"]

CMD ["python", "-u", "bot.py"]
