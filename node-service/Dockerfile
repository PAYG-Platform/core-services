# Base image: Official lightweight Python 3.11 slim image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Set Python Path so 'app' imports resolve correctly
ENV PYTHONPATH=/app

# Install system dependencies required for PostgreSQL (psycopg2 C adapter)
RUN apt-get update && \
    apt-get install -y libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Copy and install shared library from root build context (context: .)
COPY shared-lib /shared-lib
RUN pip install -e /shared-lib

# Copy service-specific requirements file from root context
COPY node-service/requirements.txt .

# Install Python dependencies without caching to minimize image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source code from root context into container /app/app
COPY node-service/app ./app
COPY node-service/config ./config

# Expose FastAPI default application port
EXPOSE 8000

# Start FastAPI application using Uvicorn web server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]