# 🏭 Production Deployment Guide & Hardening Runbook

This guide specifies the operational procedures, environment configurations, container orchestration, SSL/TLS reverse proxy setups, and automated backup strategies required to deploy the **PAYG VPN Core Backend Services** into production.

---

## 📑 Table of Contents

- [1. Production Architecture Overview](#1-production-architecture-overview)
- [2. Production Readiness Checklist](#2-production-readiness-checklist)
- [3. Environment Configuration (`.env`)](#3-environment-configuration-env)
- [4. Multi-Stage Container Orchestration](#4-multi-stage-container-orchestration)
- [5. Host Nginx & TLS Termination (Let's Encrypt)](#5-host-nginx--tls-termination-lets-encrypt)
- [6. Zero-Downtime Rolling Deployments](#6-zero-downtime-rolling-deployments)
- [7. Automated Database Backups & State Persistence](#7-automated-database-backups--state-persistence)
- [8. Resource Allocation & Concurrency Tuning](#8-resource-allocation--concurrency-tuning)

---

## 1. Production Architecture Overview

In a single-host or clustered production environment, the backend uses container isolation with minimal host exposure:

```
[ Public Clients / Web Portal / Mobile Apps ]
                    │ HTTPS :443 (TLS Termination)
                    ▼
    [ Host Nginx (Let's Encrypt SSL) ]
                    │ Reverse Proxy HTTP
                    ▼
[ API Gateway (vpn-api-gateway-prod) : 127.0.0.1:8000 ]
                    │
         Docker Bridge Network (vpn_net)
                    │
  ┌─────────────────┼─────────────────┬─────────────────┐
  ▼                 ▼                 ▼                 ▼
[ IAM ]          [ Node ]        [ Billing ]       [ Support ]
 Port 8001        Port 8002       Port 8003         Port 8004
(iam_prod_db)   (node_prod_db)  (billing_prod_db) (support_prod_db)
  │                 │                 │                 │
  └─────────────────┼─────────────────┴─────────────────┘
                    ▼
     [ RabbitMQ Message Broker (rabbitmq-prod) :5672 ]
                    ▲
  ┌─────────────────┼─────────────────┬─────────────────┐
  ▼                 ▼                 ▼                 ▼
[ Notif. ]       [ Config ]       [ Traffic ]      [ Telegram Bot ]
 Port 8005        Port 8006        Port 8007       (MTProto Worker)
(notif_prod_db) (config_prod_db) (traffic_prod_db)   (Redis State)
```

* **Network Isolation:** Microservices and databases communicate exclusively over the internal `vpn_net` Docker network.
* **Host Binding:** `api-gateway-prod` binds to `127.0.0.1:8000`, preventing direct public access that bypasses host firewalls.
* **Database Isolation:** PostgreSQL production instances do not expose ports to the public host; services connect via Docker DNS (e.g., `postgres-iam-prod:5432`).
* **State Management:** Redis (Port 6379) handles sliding-window rate limiting counters and Telegram conversation state.

---

## 2. Production Readiness Checklist

Before launching the production stack:

- [ ] **Real RSA Keys:** Generated genuine 2048-bit RSA keys (`openssl genrsa`) in `./keys/`.
- [ ] **Key Permissions:** Verified that `keys/jwt_private.pem` is `chmod 600` and `keys/jwt_public.pem` is `chmod 644`.
- [ ] **Strong Passwords:** Replaced all default credentials (`supersecret`, `super_secret_rmq_pass`) in `.env` with random 32+ character secrets.
- [ ] **HTTPS Enforced:** Set `COOKIE_SECURE=True` and `ENVIRONMENT=production` in `.env`.
- [ ] **UFW Firewall Active:** Allowed only ports `22` (SSH), `80` (HTTP), and `443` (HTTPS) on the host.
- [ ] **Persistent Storage Dirs:** Verified write permissions on `./data/prod/uploads` and `./data/prod/*`.

---

## 3. Environment Configuration (`.env`)

Create your production configuration file at the repository root:

```bash
cp .env.example .env
nano .env
```

Apply the following production parameters:

```env
# ==============================================================================
# GLOBAL RUNTIME & SECURITY CONFIGURATION
# ==============================================================================
ENVIRONMENT=production
COOKIE_SECURE=True
COOKIE_HTTPONLY=True

# Asymmetric RS256 Key Storage
JWT_PRIVATE_KEY_PATH=/keys/jwt_private.pem
JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem

# ==============================================================================
# POSTGRESQL PRODUCTION CREDENTIALS
# ==============================================================================
DB_USER=vpn_prod_admin
DB_PASSWORD=a9F8k2LmP4qR8vW1xZ5bC7dE3gH6jK9m
DB_PORT=5432

# Production Microservice Databases
IAM_DB_HOST=postgres-iam-prod
IAM_DB_NAME=iam_prod_db
IAM_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${IAM_DB_HOST}:${DB_PORT}/${IAM_DB_NAME}

NODE_DB_HOST=postgres-node-prod
NODE_DB_NAME=node_prod_db
NODE_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${NODE_DB_HOST}:${DB_PORT}/${NODE_DB_NAME}

BILLING_DB_HOST=postgres-billing-prod
BILLING_DB_NAME=billing_prod_db
BILLING_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${BILLING_DB_HOST}:${DB_PORT}/${BILLING_DB_NAME}

SUPPORT_DB_HOST=postgres-support-prod
SUPPORT_DB_NAME=support_prod_db
SUPPORT_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${SUPPORT_DB_HOST}:${DB_PORT}/${SUPPORT_DB_NAME}

NOTIFICATION_DB_HOST=postgres-notification-prod
NOTIFICATION_DB_NAME=notification_prod_db
NOTIFICATION_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${NOTIFICATION_DB_HOST}:${DB_PORT}/${NOTIFICATION_DB_NAME}

CONFIG_DB_HOST=postgres-config-prod
CONFIG_DB_NAME=config_prod_db
CONFIG_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${CONFIG_DB_HOST}:${DB_PORT}/${CONFIG_DB_NAME}

TRAFFIC_DB_HOST=postgres-traffic-prod
TRAFFIC_DB_NAME=traffic_prod_db
TRAFFIC_DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@${TRAFFIC_DB_HOST}:${DB_PORT}/${TRAFFIC_DB_NAME}

# ==============================================================================
# RABBITMQ PRODUCTION BROKER
# ==============================================================================
RMQ_HOST=rabbitmq-prod
RMQ_PORT=5672
RMQ_USER=rmq_prod_admin
RMQ_PASSWORD=e3B7v9X1z4L8k2P6qR0mW5nC8jF4gH7k
RABBITMQ_URL=amqp://${RMQ_USER}:${RMQ_PASSWORD}@${RMQ_HOST}:${RMQ_PORT}/

# ==============================================================================
# SHARED REDIS (RATE LIMITING & FSM STATE)
# ==============================================================================
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_URL=redis://${REDIS_HOST}:${REDIS_PORT}/${REDIS_DB}

# ==============================================================================
# PLATFORM SETTINGS
# ==============================================================================
SYNC_INTERVAL_MINUTES=5
TELEGRAM_BOT_USERNAME=PAYG_VPN_Official_Bot
UPLOAD_DIR=/app/uploads

# 3X-UI Connectivity Settings
USE_3XUI_PROXY=false
XUI_REQUEST_TIMEOUT=15.0
```

---

## 4. Multi-Stage Container Orchestration

### Step 1: Initialize Required Directories and Keys
```bash
# Create host-level persistent directories
mkdir -p data/prod/postgres-{iam,node,billing,support,notification,config,traffic}
mkdir -p data/prod/rabbitmq
mkdir -p data/prod/uploads
mkdir -p data/redis
mkdir -p keys

# Generate production RS256 keypair if not already present
openssl genrsa -out keys/jwt_private.pem 2048
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
chmod 600 keys/jwt_private.pem
chmod 644 keys/jwt_public.pem
```

### Step 2: Build and Launch Production Profile
Execute Docker Compose using the `production` profile:

```bash
docker compose -f docker-compose.all.yml --profile production up --build -d
```

### Step 3: Verify Running Services
```bash
docker compose -f docker-compose.all.yml --profile production ps
```

All containers should report `Up` with restart policy `always`:
* `vpn-api-gateway-prod` (bound to `127.0.0.1:8000:80`)
* `vpn-iam-service-prod`, `vpn-node-service-prod`, `vpn-billing-service-prod`, etc.
* `vpn-rabbitmq-prod`
* `vpn-postgres-*-prod` (7 isolated databases)

---

## 5. Host Nginx & TLS Termination (Let's Encrypt)

Do not expose Docker directly to port `443`. Terminate TLS using a host-level Nginx instance to manage certificates cleanly.

### Step 1: Install Nginx & Certbot
```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### Step 2: Configure Host Nginx
Create `/etc/nginx/sites-available/api.yourdomain.com`:

```nginx
server {
    server_name api.yourdomain.com;

    client_max_body_size 25M;

    # Gzip compression for performance
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # Standard proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket & Long-Polling Support
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Cookie Preservation for Authentication
        proxy_pass_header Set-Cookie;

        # Server-Sent Events (SSE) stream tuning
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 12h;
    }

    listen 80;
}
```

### Step 3: Enable Site & Issue SSL Certificate
```bash
sudo ln -s /etc/nginx/sites-available/api.yourdomain.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Issue Let's Encrypt SSL certificate
sudo certbot --nginx -d api.yourdomain.com --non-interactive --agree-tos -m admin@yourdomain.com --redirect
```

---

## 6. Zero-Downtime Rolling Deployments

When pulling code updates or deploying new container images, deploy microservices individually using `--no-deps` to preserve live user connections:

```bash
#!/bin/bash
set -e

SERVICE=$1

if [ -z "$SERVICE" ]; then
    echo "Usage: ./deploy.sh <service-name> (e.g., billing-service)"
    exit 1
fi

echo "🚀 Starting zero-downtime rolling update for ${SERVICE}..."

# 1. Pull latest image or build context
docker compose -f docker-compose.all.yml build ${SERVICE}-prod

# 2. Re-create container without dropping adjacent microservices
docker compose -f docker-compose.all.yml up -d --no-deps ${SERVICE}-prod

# 3. Post-deploy health gate verification
echo "🩺 Verifying container health status..."
sleep 4

HEALTHY=0
for i in {1..10}; do
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health || true)
    if [ "$HTTP_STATUS" = "200" ]; then
        echo "✅ Health check passed (HTTP 200) on attempt $i."
        HEALTHY=1
        break
    fi
    echo "⏳ Attempt $i/10: Received HTTP $HTTP_STATUS. Retrying in 2s..."
    sleep 2
done

if [ "$HEALTHY" -ne 1 ]; then
    echo "❌ Deployment verification failed! Check logs: docker logs vpn-${SERVICE}-prod"
    exit 1
fi

echo "✨ ${SERVICE} deployed and verified successfully."
```

---

## 7. Automated Database Backups & State Persistence

To protect user financial ledgers, subscription tokens, and 3X-UI server mappings, configure an automated daily backup cron job.

### Backup Script (`/opt/payg-vpn/scripts/backup_dbs.sh`)
```bash
#!/bin/bash
set -e

BACKUP_DIR="/opt/payg-vpn/backups/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

DATABASES=("iam_prod_db" "node_prod_db" "billing_prod_db" "support_prod_db" "notification_prod_db" "config_prod_db" "traffic_prod_db")
CONTAINERS=("vpn-postgres-iam-prod" "vpn-postgres-node-prod" "vpn-postgres-billing-prod" "vpn-postgres-support-prod" "vpn-postgres-notification-prod" "vpn-postgres-config-prod" "vpn-postgres-traffic-prod")

echo "📦 Starting automated database backup at $(date)..."

for i in "${!DATABASES[@]}"; do
    DB_NAME="${DATABASES[$i]}"
    CONTAINER="${CONTAINERS[$i]}"
    BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}.sql.gz"
    
    echo "Dumping ${DB_NAME} from ${CONTAINER}..."
    docker exec "$CONTAINER" pg_dump -U vpn_prod_admin "$DB_NAME" | gzip > "$BACKUP_FILE"
done

# Backup Uploaded Media Receipts & Attachments
echo "Backing up upload attachments..."
tar -czf "${BACKUP_DIR}/uploads.tar.gz" -C /opt/payg-vpn/core-services/data/prod uploads

# Prune backups older than 14 days
find /opt/payg-vpn/backups/* -type d -ctime +14 -exec rm -rf {} +

echo "✅ Backup completed successfully at $BACKUP_DIR"
```

### Install Daily Cron Task
```bash
chmod +x /opt/payg-vpn/scripts/backup_dbs.sh
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/payg-vpn/scripts/backup_dbs.sh >> /var/log/payg_backup.log 2>&1") | crontab -
```

---

## 8. Resource Allocation & Concurrency Tuning

Production microservices run using Uvicorn. To maximize request throughput under high traffic:

### 1. Database Connection Pool Settings
The database engines in `app/database.py` are optimized for production concurrency:
```python
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,      # Discards stale or severed connections
    pool_size=20,            # High concurrent worker limit
    max_overflow=20,         # Burst connection capacity
    pool_timeout=15,         # Prevents request hang on pool exhaustion
    pool_recycle=1800        # Recycles connections every 30 minutes
)
```

### 2. Linux System Limits (`/etc/security/limits.conf`)
High-concurrency proxying and WebSocket SSE streams require elevated file descriptor limits on the host:
```text
* soft nofile 65535
* hard nofile 65535
```
Apply via:
```bash
sudo sysctl -w fs.file-max=2097152
sudo sysctl -p
```