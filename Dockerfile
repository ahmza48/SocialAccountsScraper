# ------------------------------------
# Base stage: Playwright + Python
# ------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS base

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium (skip firefox/webkit to save space)
RUN playwright install chromium

# Copy application code
COPY . .

# Create sessions storage directory
RUN mkdir -p sessions/storage/instagram sessions/storage/tiktok sessions/storage/facebook

# Non-root user (playwright image provides "pwuser")
RUN chown -R pwuser:pwuser /app
USER pwuser

# ------------------------------------
# API target
# ------------------------------------
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ------------------------------------
# Worker target
# ------------------------------------
FROM base AS worker
CMD ["python", "-m", "workers.runner"]
