# 📋 Logging & Debugging Runbook

This guide specifies the operational tools, live log streaming methods, file inspection workflows, and diagnostic resolutions used to debug the **PAYG VPN Core Backend Services** in development mode.

---

## 📑 Table of Contents

- [1. Dual-Channel Logging Architecture](#1-dual-channel-logging-architecture)
- [2. Separated Host Log Files](#2-separated-host-log-files)
- [3. Live Container Log Streaming](#3-live-container-log-streaming)
  - [3.1 Stream All Core Services Simultaneously](#31-stream-all-core-services-simultaneously)
  - [3.2 Single-Service Container Logs (`docker logs`)](#32-single-service-container-logs-docker-logs)
- [4. Host File Tailing & Search (`tail` & `grep`)](#4-host-file-tailing--search-tail--grep)
- [5. Container Health Check Verification](#5-container-health-check-verification)
- [6. Common Troubleshooting & Resolutions](#6-common-troubleshooting--resolutions)
  - [6.1 Fatal Missing RSA Key Error](#61-fatal-missing-rsa-key-error)
  - [6.2 Database Connection Timeouts / Retries](#62-database-connection-timeouts--retries)
  - [6.3 RabbitMQ Exchange Binding Mismatches](#63-rabbitmq-exchange-binding-mismatches)
  - [6.4 Redis Connection Issues on Local Host](#64-redis-connection-issues-on-local-host)
  - [6.5 Hot-Reloading & Single-Service Rebuilds](#65-hot-reloading--single-service-rebuilds)

---

## 1. Dual-Channel Logging Architecture

The platform adheres to **The Twelve-Factor App (Factor XI: Logs as event streams)** while providing local persistence through a dual-channel pipeline:

1. **Channel 1 — Standard Stream (`stdout` / `stderr`):**
   * Output unbuffered to container streams.
   * Captured by Docker’s `json-file` log driver with automated rotation caps (`20m` max file size, `5` historical rotations) to prevent host disk exhaustion.
2. **Channel 2 — Isolated Host Files (`./logs/<service-name>/`):**
   * Written inside each container to `/app/logs/<service-name>.log` and mounted directly to the host filesystem.
   * Rotated via Python’s `RotatingFileHandler` with a strict **15 MB cap and 7 backup rotations** per service (maximum ~105 MB footprint per service).

---

## 2. Separated Host Log Files

Each service writes to its own isolated subfolder within `./logs/`. You can open and inspect these files directly in VS Code or tail them from your host terminal:

| Microservice | Dedicated Host Log File | Primary Events Captured |
| :--- | :--- | :--- |
| **API Gateway** | `./logs/api-gateway/access.log`<br>`./logs/api-gateway/error.log` | Ingress HTTP routing, upstream status codes, SSE keep-alive events. |
| **IAM Service** | `./logs/iam-service/iam-service.log` | User auth, login, registration, recovery key audits, RS256 token signing. |
| **Node Service** | `./logs/node-service/node-service.log` | 3X-UI sync sweeps, client provisioning, route replacement, subscriptions. |
| **Billing Service** | `./logs/billing-service/billing-service.log` | PAYG waterfall deductions, top-ups, refunds, grace period evaluations. |
| **Support Service** | `./logs/support-service/support-service.log` | Ticket creation, chat replies, diagnostic attachments, read-receipts. |
| **Notification Service**| `./logs/notification-service/notification-service.log` | In-app alerts, SSE streams, edge-triggered low-balance alerts. |
| **Config Service** | `./logs/config-service/config-service.log` | Branding updates, maintenance mode, policy fanouts, cron sync resets. |
| **Traffic Service** | `./logs/traffic-service/traffic-service.log` | Timeseries bandwidth usage, background cron jobs, CIDR ISP matching. |
| **Telegram Bot Worker** | `./logs/telegram-service/telegram-service.log` | Telethon client events, FSM wizard state transitions, push alerts. |

---

## 3. Live Container Log Streaming

### 3.1 Stream All Core Services Simultaneously
To view live output from all 8 application microservices without noise from databases or RabbitMQ:

```bash
docker compose -f docker-compose.all.yml logs -f \
  iam-service-dev \
  node-service-dev \
  billing-service-dev \
  support-service-dev \
  notification-service-dev \
  config-service-dev \
  traffic-service-dev \
  telegram-service-dev
```

---

### 3.2 Single-Service Container Logs (`docker logs`)

Target an individual service container to isolate logs during active development:

```bash
# IAM Service (Auth, Registration, RS256 Token Issuance)
docker logs -f vpn-iam-service-dev

# Node Service (3X-UI Fleet Engine, V2Ray Subscriptions)
docker logs -f vpn-node-service-dev

# Billing Service (Ledger, Waterfall Deductions, Top-ups, Refunds)
docker logs -f vpn-billing-service-dev

# Support Service (Tickets, Messages, Media Uploads)
docker logs -f vpn-support-service-dev

# Notification Service (SSE Stream, Edge Warnings)
docker logs -f vpn-notification-service-dev

# Config Service (Branding, Policies, Sync Broadcasts)
docker logs -f vpn-config-service-dev

# Traffic Service (Cron Metering, ISP Subnet Matching)
docker logs -f vpn-traffic-service-dev

# Telegram Bot Service (Telethon Worker, FSM State)
docker logs -f vpn-telegram-service-dev

# API Gateway (Nginx Request Proxying & Rewrites)
docker logs -f vpn-api-gateway-dev

# RabbitMQ Message Broker
docker logs -f vpn-rabbitmq-dev
```

---

## 4. Host File Tailing & Search (`tail` & `grep`)

Because log files are mounted directly into `./logs/`, you can use standard terminal utilities without attaching to Docker containers:

### Tail Specific Host Log Files:
```bash
# Follow Billing ledger deductions and top-up approvals
tail -f logs/billing-service/billing-service.log

# Follow 3X-UI fleet synchronization and route replacements
tail -f logs/node-service/node-service.log

# Follow incoming Nginx ingress requests
tail -f logs/api-gateway/access.log
```

### Search Across Logs with Grep:
```bash
# Find all errors across all microservice log files
grep -rn "ERROR" logs/

# Trace 3X-UI connection timeouts in node-service logs
grep -i "timeout" logs/node-service/node-service.log

# Search for a specific user UUID across all services
grep -rn "3fa85f64-5717-4562-b3fc-2c963f66afa6" logs/
```

---

## 5. Container Health Check Verification

Ensure all microservice HTTP endpoints are operational by testing their `/health` routes:

```bash
# API Gateway
curl -s http://localhost:8000/health

# Microservices (Mapped Host Ports)
curl -s http://localhost:8001/health  # IAM
curl -s http://localhost:8002/health  # Node
curl -s http://localhost:8003/health  # Billing
curl -s http://localhost:8004/health  # Support
curl -s http://localhost:8005/health  # Notification
curl -s http://localhost:8006/health  # Config
curl -s http://localhost:8007/health  # Traffic
```

---

## 6. Common Troubleshooting & Resolutions

### 6.1 Fatal Missing RSA Key Error
* **Symptom:** `iam-service-dev` exits with code 1 and outputs:
  ```text
  [FATAL ERROR] MISSING RSA PRIVATE_KEY FILE (IAM SERVICE)
  ```
* **Cause:** Hardcoded fallback keys have been disabled. The service cannot sign JWT access tokens without genuine RSA keys.
* **Resolution:** Generate the required keypair in the project root:
  ```bash
  mkdir -p keys
  openssl genrsa -out keys/jwt_private.pem 2048
  openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
  chmod 600 keys/jwt_private.pem
  chmod 644 keys/jwt_public.pem
  docker compose -f docker-compose.all.yml restart iam-service-dev
  ```

---

### 6.2 Database Connection Timeouts / Retries
* **Symptom:** Logs show `OperationalError: could not connect to server` or `connection attempt 1/10 failed`.
* **Cause:** Microservices booted while their respective PostgreSQL container was still running internal startup scripts.
* **Resolution:** All microservices include a built-in 10-attempt retry loop (retrying every 3 seconds). If the database still fails after 30 seconds, verify container health:
  ```bash
  docker compose -f docker-compose.all.yml --profile development ps
  ```

---

### 6.3 RabbitMQ Exchange Binding Mismatches
* **Symptom:** Events published by `iam-service` or `node-service` are not processed by downstream consumers (`billing-service`, `traffic-service`).
* **Cause:** The Fanout Exchange was not declared prior to consumer queue binding.
* **Resolution:**
  1. Open the RabbitMQ Management UI at `http://localhost:15672` (User: `admin`, Pass: `super_secret_rmq_pass`).
  2. Navigate to **Exchanges** and verify the following fanout exchanges exist:
     * `user_created_fanout`
     * `traffic_consumed_fanout`
     * `config_settings_fanout`
  3. If missing, restart the publishing service to re-register the exchange topologies:
     ```bash
     docker compose -f docker-compose.all.yml restart iam-service-dev node-service-dev config-service-dev
     ```

---

### 6.4 Redis Connection Issues on Local Host
* **Symptom:** Local scripts, test suites, or host CLI tools fail with `Connection refused` when connecting to `localhost:6379`.
* **Cause:** The shared `redis` service in `docker-compose.all.yml` must explicitly expose port `6379:6379` to the host machine.
* **Resolution:** Ensure the `redis` service in `docker-compose.all.yml` includes the host port mapping:
  ```yaml
  redis:
    image: redis:7-alpine
    container_name: vpn-redis
    restart: unless-stopped
    command: redis-server --appendonly yes
    ports:
      - "6379:6379"
    volumes:
      - ./data/redis:/data
    networks:
      - vpn_net
  ```
  Apply changes:
  ```bash
  docker compose -f docker-compose.all.yml up -d redis
  ```

---

### 6.5 Hot-Reloading & Single-Service Rebuilds
In development mode, source code is mounted directly into containers (`/app/app`), allowing Uvicorn to hot-reload automatically upon saving files.

* **Restart a service without rebuilding:**
  ```bash
  docker compose -f docker-compose.all.yml restart billing-service-dev
  ```

* **Rebuild a service after modifying `requirements.txt`:**
  ```bash
  docker compose -f docker-compose.all.yml up -d --build billing-service-dev
  ```