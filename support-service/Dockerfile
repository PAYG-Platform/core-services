FROM python:3.11-slim

WORKDIR /app

# Set PYTHONPATH so Python can locate the 'app' module
ENV PYTHONPATH=/app

# Install system dependencies required for PostgreSQL adapter
RUN apt-get update && \
    apt-get install -y libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy and install shared library from root context
COPY shared-lib /shared-lib
RUN pip install -e /shared-lib

# Copy requirements and install
COPY support-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY support-service/app ./app
COPY support-service/config ./config

EXPOSE 8000

# Start FastAPI application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]