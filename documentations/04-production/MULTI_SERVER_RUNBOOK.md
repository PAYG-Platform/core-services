# 🌍 Multi-Server & Distributed Topology Runbook (Modular & Cloud-Ready)

This runbook specifies the operational architecture and configuration patterns for deploying the **PAYG VPN Platform** across any arbitrary infrastructure: from dedicated per-service virtual servers and shared multi-tenant VPS instances to managed cloud backends (CloudAMQP, Redis Cloud, Managed PostgreSQL).

---

## 📑 Table of Contents

- [1. Dynamic Topology Paradigms](#1-dynamic-topology-paradigms)
  - [1.1 Pattern A: Fully Granular (1 Service per VPS)](#11-pattern-a-fully-granular-1-service-per-vps)
  - [1.2 Pattern B: Grouped / Hybrid Multi-Tenant VPS](#12-pattern-b-grouped--hybrid-multi-tenant-vps)
  - [1.3 Pattern C: Cloud-Managed Infrastructure (DBaaS & SaaS Broker)](#13-pattern-c-cloud-managed-infrastructure-dbaas--saas-broker)
- [2. Universal Inter-Server Networking](#2-universal-inter-server-networking)
  - [2.1 Option 1: WireGuard Private Mesh (`10.10.0.0/24`)](#21-option-1-wireguard-private-mesh-10100024)
  - [2.2 Option 2: Cloud VPC Private Subnets](#22-option-2-cloud-vpc-private-subnets)
- [3. Cryptographic Key Distribution Matrix](#3-cryptographic-key-distribution-matrix)
- [4. Decoupled Shared Infrastructure Deployment](#4-decoupled-shared-infrastructure-deployment)
  - [4.1 Dedicated RabbitMQ (Self-Hosted Standalone vs CloudAMQP)](#41-dedicated-rabbitmq-self-hosted-standalone-vs-cloudamqp)
  - [4.2 Dedicated Redis (Self-Hosted Standalone vs Redis Cloud / Upstash)](#42-dedicated-redis-self-hosted-standalone-vs-redis-cloud--upstash)
  - [4.3 Database Strategy (Per-Service Container vs Managed PostgreSQL)](#43-database-strategy-per-service-container-vs-managed-postgresql)
- [5. Standalone Service Deployment Pattern](#5-standalone-service-deployment-pattern)
  - [5.1 Directory Layout for Independent Nodes](#51-directory-layout-for-independent-nodes)
  - [5.2 Local Uploads & Media Storage Isolation](#52-local-uploads--media-storage-isolation)
  - [5.3 Boilerplate: Standalone Microservice Docker Compose](#53-boilerplate-standalone-microservice-docker-compose)
- [6. API Gateway: Dynamic Upstream Configuration](#6-api-gateway-dynamic-upstream-configuration)
- [7. Fleet Integration: Remote 3X-UI Server Panels](#7-fleet-integration-remote-3x-ui-server-panels)
- [8. Automated CI/CD Multi-Node Deployment](#8-automated-cicd-multi-node-deployment)
- [9. Incident Response & Node Isolation Protocol](#9-incident-response--node-isolation-protocol)

---

## 1. Dynamic Topology Paradigms

The PAYG VPN platform is completely decoupled. Every service relies solely on environment variables (`DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`, `JWT_PUBLIC_KEY_PATH`). It does not matter whether a service shares a physical server or runs 5,000 miles away on a standalone node.

```
                           ┌───────────────────────────────┐
                           │      Public Client / Web      │
                           └──────────────┬────────────────┘
                                          │ HTTPS :443
                                          ▼
                           ┌───────────────────────────────┐
                           │       api-gateway Node        │
                           └──────────────┬────────────────┘
                                          │ Private Mesh / Cloud VPC
       ┌──────────────────┬───────────────┼───────────────┬──────────────────┐
       ▼                  ▼               ▼               ▼                  ▼
┌──────────────┐   ┌──────────────┐┌──────────────┐┌──────────────┐   ┌──────────────┐
│  iam-service │   │ node-service ││billing-serv. ││support-serv. │...│traffic-serv. │
│(Server/VPS A)│   │(Server/VPS B)││(Server/VPS C)││(Server/VPS D)│   │(Server/VPS E)│
└──────┬───────┘   └──────┬───────┘└──────┬───────┘└──────┬───────┘   └──────┬───────┘
       │                  │               │               │                  │
       └──────────────────┴───────┬───────┴───────────────┴──────────────────┘
                                  ▼
        ┌───────────────────────────────────────────────────────────┐
        │  Decoupled State & Messaging Tier (Standalone or Cloud)   │
        │   • RabbitMQ: Dedicated VPS Stack OR CloudAMQP / AWS MQ   │
        │   • Redis:    Dedicated VPS Stack OR Redis Cloud / Upstash│
        │   • Postgres: Container alongside service OR Cloud DBaaS  │
        └───────────────────────────────────────────────────────────┘
```

### 1.1 Pattern A: Fully Granular (1 Service per VPS)
* Each microservice runs on its own minimal VPS (e.g., 1-2 vCPU, 2GB RAM).
* Databases run either as a co-located local container or connect to a central database server/managed DBaaS.
* Uploads reside locally on the host executing `billing-service` (payment receipts) and `support-service` (ticket media).

### 1.2 Pattern B: Grouped / Hybrid Multi-Tenant VPS
* A single high-resource VPS runs two or more services completely isolated from each other in separate directories:
  * Directory `/opt/payg-vpn/billing-service` has its own `docker-compose.yml` and its own `.env`.
  * Directory `/opt/payg-vpn/node-service` runs side-by-side with its own independent lifecycle.
* Services never share internal networks unless needed; they route via localhost or the private mesh.

### 1.3 Pattern C: Cloud-Managed Infrastructure (DBaaS & SaaS Broker)
* **Message Broker:** RabbitMQ provisioned via **CloudAMQP**, **Aiven**, or **Amazon MQ**.
* **Cache / FSM:** Redis provisioned via **Redis Cloud**, **Upstash**, or **AWS ElastiCache**.
* **Databases:** PostgreSQL instances hosted on **Neon**, **Supabase**, **DigitalOcean Managed DB**, or self-hosted PostgreSQL clusters.
* **Advantage:** Eliminates database and message queue maintenance from VPS hosts entirely.

---

## 2. Universal Inter-Server Networking

When services reside on different servers or providers, all communication must remain encrypted and firewalled from the public internet.

### 2.1 Option 1: WireGuard Private Mesh (`10.10.0.0/24`)

WireGuard establishes a lightweight, kernel-level mesh network across any Linux hosts, regardless of whether they are on Hetzner, DigitalOcean, AWS, or local bare metal.

#### Mesh IP Assignment Convention
Create an internal IP addressing scheme for your cluster:

| Assigned Node Identity | WireGuard Mesh IP | Public Ports Allowed |
| :--- | :---: | :---: |
| **Central RabbitMQ Node** | `10.10.0.10` | None (Only WG UDP `51820`) |
| **Central Redis Node** | `10.10.0.11` | None (Only WG UDP `51820`) |
| **API Gateway (Edge)** | `10.10.0.20` | `80`, `443` |
| **IAM Service Node** | `10.10.0.31` | None (Only WG UDP `51820`) |
| **Node Service Node** | `10.10.0.32` | None (Only WG UDP `51820`) |
| **Billing Service Node** | `10.10.0.33` | None (Only WG UDP `51820`) |
| **Support Service Node** | `10.10.0.34` | None (Only WG UDP `51820`) |
| **Notification Service Node** | `10.10.0.35` | None (Only WG UDP `51820`) |
| **Config Service Node** | `10.10.0.36` | None (Only WG UDP `51820`) |
| **Traffic Service Node** | `10.10.0.37` | None (Only WG UDP `51820`) |
| **Telegram Bot Node** | `10.10.0.38` | None (Only WG UDP `51820`) |

#### Setting Up a New Node in the Mesh
On any new host:
```bash
sudo apt update && sudo apt install -y wireguard
wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
chmod 600 /etc/wireguard/privatekey
```

Add peer definitions to `/etc/wireguard/wg0.conf`:
```ini
[Interface]
Address = 10.10.0.X/24
ListenPort = 51820
PrivateKey = <THIS_SERVER_PRIVATE_KEY>

# Peer with another node in the mesh
[Peer]
PublicKey = <TARGET_NODE_PUBLIC_KEY>
Endpoint = <TARGET_PUBLIC_IP>:51820
AllowedIPs = 10.10.0.Y/32
PersistentKeepalive = 25
```

Enable and start:
```bash
sudo systemctl enable --now wg-quick@wg0
```

### 2.2 Option 2: Cloud VPC Private Subnets
If deploying all nodes inside a single cloud provider (AWS, DigitalOcean, Hetzner Cloud, GCP), use their native **Private VPC / Virtual Network**:
* Bind services to internal private IPs (e.g., `10.0.0.0/16`).
* Eliminate WireGuard overhead while maintaining strict security group firewalls.

---

## 3. Cryptographic Key Distribution Matrix

The platform strictly uses **Asymmetric RSA (RS256)** for JWT token signing and verification. This decouples trust across servers:

```
[ IAM Server ONLY ]                       [ ALL Other Target Servers ]
      │                                                │
      ├── Holds: jwt_private.pem (Signs Tokens)        └── Holds ONLY: jwt_public.pem
      └── Holds: jwt_public.pem (Verifies Self)            (Verifies Tokens In-Memory)
```

### Key Distribution Rules:

1. **`iam-service` Host:**
   * Requires **BOTH** `jwt_private.pem` (mode `600`) and `jwt_public.pem` (mode `644`).
   * Never commit, export, or transmit `jwt_private.pem` outside of this specific server.

2. **All Other Hosts (Node, Billing, Support, Traffic, Notifications, Config, Telegram):**
   * Require **ONLY** `jwt_public.pem` (mode `644`).
   * Place `jwt_public.pem` inside that service's directory (e.g., `/opt/payg-vpn/<service-name>/keys/jwt_public.pem`).
   * If a peripheral server is compromised, attackers hold only the public verification key. **They cannot mint valid tokens, forge identities, or elevate user roles.**

---

## 4. Decoupled Shared Infrastructure Deployment

### 4.1 Dedicated RabbitMQ (Self-Hosted Standalone vs CloudAMQP)

RabbitMQ can run on its own dedicated server or via a managed SaaS broker.

#### Option A: Standalone Docker Compose (`docker-compose.rabbitmq.yml`)
Run this on your designated RabbitMQ VPS (e.g., WireGuard IP `10.10.0.10`):

```yaml
version: '3.8'

services:
  rabbitmq:
    image: rabbitmq:3-management-alpine
    container_name: vpn-rabbitmq-dedicated
    restart: always
    environment:
      RABBITMQ_DEFAULT_USER: ${RMQ_USER:-admin}
      RABBITMQ_DEFAULT_PASS: ${RMQ_PASSWORD:-super_secret_rmq_pass}
    ports:
      # Bind AMQP to WireGuard mesh IP or localhost only
      - "10.10.0.10:5672:5672"
      # Bind Management Web UI to localhost (access via SSH tunnel)
      - "127.0.0.1:15672:15672"
    volumes:
      - ./data/rabbitmq:/var/lib/rabbitmq
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "5"
```

#### Option B: CloudAMQP / Managed Cloud Broker
Sign up on [CloudAMQP](https://www.cloudamqp.com/) or Amazon MQ:
* Copy the supplied AMQP TLS connection string:
  `amqps://username:password@b-xxxxxx.mq.eu-west-1.amazonaws.com:5671/`
* In every service `.env` file across all servers, simply set:
  ```env
  RABBITMQ_URL=amqps://username:password@b-xxxxxx.mq.eu-west-1.amazonaws.com:5671/
  ```

---

### 4.2 Dedicated Redis (Self-Hosted Standalone vs Redis Cloud / Upstash)

Redis handles sliding-window rate limit counters and Telegram Bot FSM conversation states.

#### Option A: Standalone Docker Compose (`docker-compose.redis.yml`)
Run this on your designated Redis VPS (e.g., WireGuard IP `10.10.0.11`):

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    container_name: vpn-redis-dedicated
    restart: always
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD} --bind 127.0.0.1 10.10.0.11
    ports:
      - "10.10.0.11:6379:6379"
    volumes:
      - ./data/redis:/data
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "5"
```

#### Option B: Redis Cloud / Upstash
Provision a managed Redis instance:
* Copy the connection string:
  `rediss://default:password@eu-central-1.upstash.io:6379/0`
* Update your microservices `.env`:
  ```env
  REDIS_URL=rediss://default:password@eu-central-1.upstash.io:6379/0
  ```

---

### 4.3 Database Strategy (Per-Service Container vs Managed PostgreSQL)

Every microservice in this monorepo has an isolated database schema (`iam_db`, `node_db`, `billing_db`, `support_db`, `notification_db`, `config_db`, `traffic_db`).

#### Strategy 1: Isolated Container Beside Each Service (Recommended for Self-Hosting)
When deploying a service to a VPS, run its dedicated database right next to it in that service's `docker-compose.yml`:
* High performance (inter-container network speed, zero network latency).
* Absolute isolation: crashing or restarting one service's DB has zero impact on any other service.

#### Strategy 2: Managed Cloud Database (DBaaS)
If using Supabase, Neon, AWS Aurora, or DigitalOcean Managed PostgreSQL:
* Create a dedicated database for each microservice (`iam_prod_db`, `billing_prod_db`, etc.).
* Inject the cloud pooler URL directly into each service `.env`:
  ```env
  DATABASE_URL=postgresql://user:password@ep-cool-fog-12345.eu-central-1.neon.tech/billing_prod_db?sslmode=require
  ```

---

## 5. Standalone Service Deployment Pattern

When running services independently across different servers or directories, each service directory operates as a self-contained unit.

### 5.1 Directory Layout for Independent Nodes

On any target server, organize the deployment inside `/opt/payg-vpn/<service-name>`:

```text
/opt/payg-vpn/billing-service/
├── Dockerfile                  # Copied or built from monorepo
├── docker-compose.yml          # Standalone service composition
├── .env                        # Local environment variables
├── requirements.txt            # Python dependencies
├── app/                        # Service source code
├── config/                     # Rate limits YAML
├── shared-lib/                 # Standard vpn_shared package
├── keys/
│   └── jwt_public.pem          # Verification public key
├── uploads/                    # Local storage for receipts (isolated to this host)
└── logs/                       # Local rotating log files
```

### 5.2 Local Uploads & Media Storage Isolation

Uploads are strictly tied to the service that handles them:
* **Payment Receipts:** Handled exclusively by `billing-service` $\longrightarrow$ stored in `/opt/payg-vpn/billing-service/uploads/`.
* **Ticket Media Attachments:** Handled exclusively by `support-service` $\longrightarrow$ stored in `/opt/payg-vpn/support-service/uploads/`.

By binding `./uploads:/app/uploads` locally, you do not need NFS, S3, or complex cross-server distributed storage. The service serves its own uploaded files locally via Nginx or FastAPI static mounts.

---

### 5.3 Boilerplate: Standalone Microservice Docker Compose

Use this template to deploy **any service independently** on its own server.

Here is an example for **`billing-service`** on a dedicated server:

```yaml
version: '3.8'

networks:
  service_net:
    driver: bridge

services:
  # Isolated database running locally on this node
  postgres-billing:
    image: postgres:17-alpine
    container_name: billing-postgres
    restart: always
    environment:
      POSTGRES_USER: ${DB_USER:-admin}
      POSTGRES_PASSWORD: ${DB_PASSWORD:-secret}
      POSTGRES_DB: ${BILLING_DB_NAME:-billing_db}
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    networks:
      - service_net
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "5"

  # The application container
  billing-service:
    build:
      context: .
      dockerfile: ./Dockerfile
    container_name: billing-service-app
    restart: always
    env_file:
      - .env
    ports:
      # Bind port to WireGuard mesh IP or localhost
      - "${BIND_IP:-127.0.0.1}:8003:8000"
    environment:
      - DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@postgres-billing:5432/${BILLING_DB_NAME}
      - RABBITMQ_URL=${RABBITMQ_URL}
      - REDIS_URL=${REDIS_URL}
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem
      - ENVIRONMENT=production
      - LOG_DIR=/app/logs
    volumes:
      # Isolated local storage for receipts
      - ./uploads:/app/uploads
      # Isolated local rotating log directory
      - ./logs:/app/logs
      # Asymmetric RSA Public Key
      - ./keys/jwt_public.pem:/keys/jwt_public.pem:ro
    depends_on:
      - postgres-billing
    networks:
      - service_net
    logging:
      driver: "json-file"
      options:
        max-size: "20m"
        max-file: "5"
```

---

## 6. API Gateway: Dynamic Upstream Configuration

The `api-gateway` operates as the single front door to your cluster. When services are located on arbitrary servers, update **`api-gateway/nginx.conf`** upstream blocks with their respective WireGuard mesh or private IPs:

```nginx
# Map upstreams dynamically to wherever each service lives
upstream iam_backend {
    # If IAM is on Server A:
    server 10.10.0.31:8001;
}

upstream node_backend {
    # If Node Service is on Server B:
    server 10.10.0.32:8002;
}

upstream billing_backend {
    # If Billing Service is on Server C:
    server 10.10.0.33:8003;
}

upstream support_backend {
    # If Support Service is on Server D:
    server 10.10.0.34:8004;
}

upstream notification_backend {
    # If Notification Service is on Server E:
    server 10.10.0.35:8005;
}

upstream config_backend {
    # If Config Service is on Server F:
    server 10.10.0.36:8006;
}

upstream traffic_backend {
    # If Traffic Service is on Server G:
    server 10.10.0.37:8007;
}

server {
    listen 80;
    server_name localhost;
    client_max_body_size 25M;

    # Ingress endpoints
    location /api/iam {
        rewrite ^/api/iam/?(.*)$ /$1 break;
        proxy_pass http://iam_backend;
        proxy_set_header Host $host;
        proxy_pass_header Set-Cookie;
    }

    location /api/node {
        rewrite ^/api/node/?(.*)$ /$1 break;
        proxy_pass http://node_backend;
    }

    # Universal Subscription Endpoint (v2rayNG, Sing-box)
    location /sub/ {
        proxy_pass http://node_backend;
    }

    location /api/billing {
        rewrite ^/api/billing/?(.*)$ /$1 break;
        proxy_pass http://billing_backend;
    }

    # Direct uploads route routed to the specific server hosting Support
    location /uploads/ {
        proxy_pass http://support_backend;
    }

    location /api/support {
        rewrite ^/api/support/?(.*)$ /$1 break;
        proxy_pass http://support_backend;
    }

    # Real-Time SSE Stream with buffer disabling
    location /api/notifications {
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 12h;
        rewrite ^/api/notifications/(.*)$ /notifications/$1 break;
        proxy_pass http://notification_backend;
    }

    location /api/config {
        rewrite ^/api/config/?(.*)$ /$1 break;
        proxy_pass http://config_backend;
    }

    location /api/traffic {
        rewrite ^/api/traffic/?(.*)$ /$1 break;
        proxy_pass http://traffic_backend;
    }
}
```

---

## 7. Fleet Integration: Remote 3X-UI Server Panels

`node-service` manages configurations across any number of remote 3X-UI servers spread worldwide (Germany, US, Singapore, Netherlands, etc.).

```
[ node-service ]
       │
       ├── HTTPS TLS (Direct Connection) ──────► [ German 3X-UI Panel :2053 ]
       │
       └── via Proxy (USE_3XUI_PROXY=true) ────► [ Restricted Region 3X-UI Panel :2053 ]
```

### Connectivity Modes in `node-service` `.env`:
* **Direct Connection (Standard):**
  ```env
  USE_3XUI_PROXY=false
  XUI_REQUEST_TIMEOUT=15.0
  ```
* **Outbound HTTP Proxy Tunneling:**
  Used when `node-service` is hosted in an IP range blocked by target panel firewalls:
  ```env
  USE_3XUI_PROXY=true
  XUI_HTTP_PROXY=http://proxy-server.internal:10808
  XUI_REQUEST_TIMEOUT=20.0
  ```

---

## 8. Automated CI/CD Multi-Node Deployment

When using GitHub Actions (`.github/workflows/ci-cd.yml`), deployments are triggered per-service based on path-filtering:

1. **GitHub Secrets Configuration:** Define target host IPs and SSH keys independently:
   * `IAM_HOST`, `IAM_USER`, `IAM_KEY` (Points to IAM server)
   * `BILLING_HOST`, `BILLING_USER`, `BILLING_KEY` (Points to Billing server)
   * `NODE_HOST`, `NODE_USER`, `NODE_KEY` (Points to Node server)
   * *(If two services share a VPS, simply set their `_HOST` secrets to the same IP)*

2. **Automated Deployment Script Executed on Each Host:**
   ```bash
   set -e
   cd /opt/payg-vpn/${SERVICE_NAME}

   # Pull new hardened image tag from GHCR
   docker compose pull ${SERVICE_NAME}

   # Zero-downtime rolling restart of this service
   docker compose up -d --no-deps ${SERVICE_NAME}

   # Verify local health check gate
   sleep 4
   curl -f http://localhost:${LOCAL_PORT}/health || exit 1
   ```

---

## 9. Incident Response & Node Isolation Protocol

Because services and credentials are decoupled, security breaches are compartmentalized:

### Scenario 1: A Peripheral Microservice Host is Compromised
* **Risk:** Attacker has root access to `billing-service` or `support-service` host.
* **Impact Assessment:**
  * Attacker possesses **only** `jwt_public.pem`. **They CANNOT forge JWT tokens** or create Admin sessions.
  * Attacker cannot access other databases (`iam_db` remains completely isolated on its own server).
* **Remediation:**
  1. Sever the compromised server's WireGuard mesh peer:
     ```bash
     sudo wg set wg0 peer <COMPROMISED_PUBLIC_KEY> remove
     ```
  2. Rotate database passwords for that specific service.
  3. Re-deploy the compromised service to a clean VPS.

### Scenario 2: The Primary IAM Host is Compromised
* **Risk:** Attacker gains access to `jwt_private.pem`.
* **Remediation:**
  1. Shut down the compromised IAM node immediately.
  2. Generate a fresh 2048-bit keypair on a clean server:
     ```bash
     openssl genrsa -out keys/jwt_private.pem 2048
     openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
     ```
  3. Copy `jwt_public.pem` to all other servers (`node`, `billing`, `support`, etc.).
  4. Restart all services. **Every previous token is immediately invalidated globally.**
