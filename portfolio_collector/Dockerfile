FROM python:3.12-slim

WORKDIR /app

# Build tools needed by some pandas/numpy deps on aarch64
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py .
COPY run.sh /run.sh

# Fix Windows CRLF line endings (file was authored on Windows)
RUN sed -i 's/\r$//' /run.sh && chmod +x /run.sh

CMD ["/run.sh"]
