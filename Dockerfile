FROM python:3.12-slim

# tesseract for OCR (繁體中文)
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-tra && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ 目錄放在容器內作為預設；Railway 可掛載持久磁碟覆蓋此路徑
RUN mkdir -p data

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
