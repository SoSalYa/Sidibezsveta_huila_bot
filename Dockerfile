FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates curl \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libx11-xcb1 libxcomposite1 libxrandr2 libasound2 \
    libgbm1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ✅ Ось ЦЕ ВАЖЛИВО
RUN pip install playwright && playwright install --with-deps chromium

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["bash", "start.sh"]