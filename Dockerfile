FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py sheets_client.py ./

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/credentials \
    && chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/data"]

CMD ["python", "bot.py"]
