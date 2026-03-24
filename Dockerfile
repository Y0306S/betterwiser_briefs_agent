FROM python:3.12-slim

# Install system dependencies for weasyprint and playwright
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    libjpeg-dev \
    libxml2-dev \
    libxslt1-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install playwright browsers for crawl4ai fallback
RUN python -m playwright install chromium --with-deps

COPY . .

# Create runs directory
RUN mkdir -p runs

ENTRYPOINT ["python", "-m", "src.orchestrator"]
CMD ["--help"]
