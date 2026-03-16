FROM python:3.11-slim

WORKDIR /app

# Install system deps for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright + Chromium
RUN playwright install --with-deps chromium

# Copy app
COPY . .

# Railway injects PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
