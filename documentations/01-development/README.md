# 🛠️ Local Development Guide

This guide details how to set up, run, and develop the **PAYG VPN Core Backend Services** in local hot-reload development mode.

---

## 📋 Table of Contents

- [System Prerequisites](#-system-prerequisites)
- [Step 1: Environment Setup](#-step-1-environment-setup)
- [Step 2: Generate Asymmetric RSA Keys](#-step-2-generate-asymmetric-rsa-keys)
- [Step 3: Launching the Backend Stack](#-step-3-launching-the-backend-stack)
- [Step 4: Launching the Web Portal](#-step-4-launching-the-web-portal)
- [Step 5: Verifying Health Checks](#-step-5-verifying-health-checks)
- [Hot-Reload & Volume Mounting](#-hot-reload--volume-mounting)
- [Stopping & Resetting Services](#-stopping--resetting-services)

---

## ⚙️ System Prerequisites

Ensure you have the following installed on your host machine:

| Dependency | Minimum Version | Purpose |
| :--- | :--- | :--- |
| **Docker Engine** | `v24.0+` | Container runtime engine |
| **Docker Compose** | `v2.20+` | Multi-container orchestration |
| **OpenSSL** | `v1.1+` / `v3.0+` | RSA keypair generation |
| **Node.js & npm** | `Node 20+` / `npm 10+` | (Optional) For running the Web Portal outside Docker |
| **Python** | `3.11` | (Optional) For running tests locally |

---

## 🛠️ Step 1: Environment Setup

1. In the project root, duplicate the environment template:
   ```bash
   cp .env.example .env
   ```

2. Review the settings inside `.env`. Default development settings are pre-configured:
   ```env
   ENVIRONMENT=development
   DB_USER=admin
   DB_PASSWORD=super_secret_db_pass
   RMQ_USER=admin
   RMQ_PASSWORD=super_secret_rmq_pass
   ```

---

## 🔐 Step 2: Generate Asymmetric RSA Keys

> ⚠️ **CRITICAL PREREQUISITE:** The backend services strictly require valid RSA-2048 keys. If these keys are not generated before running Docker Compose, `iam-service` and all other services will exit immediately upon boot.

Run these commands in your project root:

```bash
# 1. Create keys directory
mkdir -p keys

# 2. Generate RSA Private Key (2048-bit)
openssl genrsa -out keys/jwt_private.pem 2048

# 3. Extract the RSA Public Key
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem

# 4. Set appropriate file permissions
chmod 600 keys/jwt_private.pem
chmod 644 keys/jwt_public.pem
```

---

## 🚀 Step 3: Launching the Backend Stack

Run Docker Compose with the `development` profile:

```bash
# Start all 7 databases, RabbitMQ, Redis, API Gateway, and 8 microservices in background
docker compose -f docker-compose.all.yml --profile development up --build -d
```

To watch the startup process in real time:
```bash
docker compose -f docker-compose.all.yml --profile development logs -f
```

---

## 💻 Step 4: Launching the Web Portal

You can run the React frontend using either of these two methods:

### Option A: Local Node / Vite (Recommended for Rapid UI Development)
```bash
# In the web-portal repository / frontend directory:
npm install
npm run dev
```
*Access Web Portal at: `http://localhost:3000`*

### Option B: Docker Development Container
```bash
docker compose --profile development up --build -d
```
*Access Web Portal at: `http://localhost:3000`*

---

## 🩺 Step 5: Verifying Health Checks

Once the containers are running, verify that each service reports healthy:

| Component | Target URL | Expected Response |
| :--- | :--- | :--- |
| **API Gateway** | `http://localhost:8000/health` | `{"status": "healthy", "gateway": "online"}` |
| **IAM Service** | `http://localhost:8001/health` | `{"service": "iam-service", "status": "healthy"}` |
| **Node Service** | `http://localhost:8002/health` | `{"service": "node-service", "status": "operational"}` |
| **Billing Service** | `http://localhost:8003/health` | `{"service": "billing-service", "status": "operational", "db_connected": true}` |
| **Support Service** | `http://localhost:8004/health` | `{"service": "support-service", "status": "healthy", "db_connected": true}` |
| **Notification Service** | `http://localhost:8005/health` | `{"service": "notification-service", "status": "operational"}` |
| **Config Service** | `http://localhost:8006/health` | `{"service": "config-service", "status": "operational"}` |
| **Traffic Service** | `http://localhost:8007/health` | `{"service": "traffic-service", "status": "operational", "db_connected": true}` |
| **RabbitMQ Management** | `http://localhost:15672` | RabbitMQ Web Dashboard (Login: `admin` / `super_secret_rmq_pass`) |

---

## 🔄 Hot-Reload & Volume Mounting

In development mode, each FastAPI microservice mounts its local source code folder into the container:

```yaml
volumes:
  - ./billing-service/app:/app/app
  - ./shared-lib/vpn_shared:/shared-lib/vpn_shared
  - ./keys:/app/keys:ro
```

* **Instant Hot-Reloading:** Editing any file inside `<service-name>/app/` triggers an immediate Uvicorn reload inside the container without needing a rebuild.
* **Shared Library Sync:** Editing any file in `shared-lib/vpn_shared/` propagates immediately to all running backend containers.

---

## 🛑 Stopping & Resetting Services

```bash
# Stop dev environment and preserve database data volumes
docker compose -f docker-compose.all.yml --profile development down

# Stop dev environment and completely wipe all containers, networks, and data volumes
docker compose -f docker-compose.all.yml --profile development down -v
```