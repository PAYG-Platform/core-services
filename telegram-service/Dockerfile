# Base image: Python 3.11 slim
FROM python:3.11-slim

WORKDIR /app

# Set Python path
ENV PYTHONPATH=/app

# Install system utilities
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install shared-lib from project root
COPY shared-lib /shared-lib
RUN pip install --no-cache-dir -e /shared-lib

# Install service dependencies
COPY telegram-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source code
COPY telegram-service/app ./app

# Start the Telethon Bot process
CMD ["python", "-m", "app.main"]