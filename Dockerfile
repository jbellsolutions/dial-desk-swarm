FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose API port
EXPOSE 8080

# Run the API by default; Railway worker services can set DIALDESK_PROCESS=browser.
CMD ["sh", "-c", "if [ \"$DIALDESK_PROCESS\" = \"browser\" ]; then python -m src.browser_operator; else python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port ${PORT:-8080} --http h11; fi"]
