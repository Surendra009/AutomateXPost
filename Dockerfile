FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime JPEG lib only — Pillow/rapidfuzz/pypdf use prebuilt wheels (skip gcc/apt build chain)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --default-timeout=120 --retries=10 -r requirements.txt

COPY . .

RUN mkdir -p /data && chmod +x start.sh

ENV SEED_ON_START=false
ENV DATABASE_URL=sqlite:////data/postpilot.db

EXPOSE 8000

CMD ["./start.sh"]
