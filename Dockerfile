FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --chown=appuser:appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

ENV CHROME_BIN=/usr/bin/chromium
ENV PATH="/usr/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
