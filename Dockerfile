FROM python:3.11-slim

WORKDIR /app

# Build deps for Pillow / rapidfuzz wheels fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Icons are committed in static/icons/ — no build-time generation needed
RUN mkdir -p /data

ENV SEED_ON_START=false
ENV DATABASE_URL=sqlite:////data/postpilot.db

EXPOSE 8000

RUN chmod +x start.sh

CMD ["./start.sh"]
