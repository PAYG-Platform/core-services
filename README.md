<p align="center">
  <img src="documentations/assets/banner.png" alt="PAYG VPN - Pay For What You Use" width="750" />
</p>

# PAYG VPN — Core Backend Services

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11" />
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/PostgreSQL-17_Alpine-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL 17" />
  <img src="https://img.shields.io/badge/RabbitMQ-3.13_Alpine-FF6600?style=flat-square&logo=rabbitmq&logoColor=white" alt="RabbitMQ" />
  <img src="https://img.shields.io/badge/Redis-7_Alpine-DC382D?style=flat-square&logo=redis&logoColor=white" alt="Redis" />
  <img src="https://img.shields.io/badge/Cryptography-RS256-4EBA6F?style=flat-square" alt="RS256" />
  <img src="https://img.shields.io/badge/License-Proprietary-red?style=flat-square" alt="License" />
</p>

The **`core-services`** monorepo is the central distributed backend engine powering the **PAYG VPN Platform**. Architected around an **Asynchronous Event-Driven Choreography** and **Stateless Asymmetric Token Delegation (RS256)**, the system manages dynamic 3X-UI server fleets, multi-bucket financial ledgers (supporting dual-currency Toman and USD), real-time timeseries traffic metering, and CIDR-based subnet ISP attribution.

---

## 📑 Table of Contents

- [Architectural Overview](#-architectural-overview)
- [Microservice Inventory](#-microservice-inventory)
- [Key Engineering Highlights](#-key-engineering-highlights)
- [Repository Structure](#-repository-structure)
- [Security & Asymmetric Cryptography](#-security--asymmetric-cryptography)
- [Quick Start: Local Development](#-quick-start-local-development)
- [Event Bus & Choreography Topology](#-event-bus--choreography-topology)
- [Testing & Quality Gates](#-testing--quality-gates)
- [Documentation Directory](#-documentation-directory)

---

## 🏛️ Architectural Overview

The backend uses RabbitMQ as a centralized AMQP message broker (orchestrated via FastStream) and delegative JWT verification using an RSA-2048 keypair:

```
                  [ Web Portal (React) / Client Apps ]
                                    │
                                    │ HTTP / REST / SSE / Subscriptions
                                    ▼
                     [ API Gateway (Nginx) :8000 ]
                                    │
    ┌──────────┬──────────┬─────────┼──────────┬──────────┬──────────┐
    ▼          ▼          ▼         ▼          ▼          ▼          ▼
 [ IAM ]    [ Node ]  [ Billing ] [Support]  [Notif.]  [Config]  [Traffic]
  :8001      :8002      :8003     :8004      :8005      :8006     :8007
 (iam_db)   (node_db) (billing_db)(support_db)(notif_db)(config_db)(traffic_db)
    │          │          │         │          │          │          │
    │          │     [ Shared Redis :6379 ]    │          │          │
    │          │     (Rate Limits & FSM State) │          │          │
    │          │          │         │          │          │          │
    ▲          ▲          ▲         ▲          ▲          ▲          ▲
    │          │          │         │          │          │          │
    └──────────┴──────────┴────┬────┴──────────┴──────────┴──────────┘
                               │
                               ▼
                [ RabbitMQ Event Bus :5672 ]
                               ▲
                               │ Events (Push / Alerts)
                               ▼
                   [ Telegram Bot Service ]
                      (MTProto Worker)
```

```
  ══════════════════════════ INGRESS / HTTP PLANE ═════════════════════════

                 [ Web Portal / Client VPN Applications ]
                                    │
                                    │ Port 8000 (HTTPS/WSS)
                                    ▼
                     [ API Gateway (Nginx) :8000 ]
                                    │
       ┌───────────┬────────────┬───┴────────┬────────────┬───────────┐
       │ /api/iam  │ /api/node  │/api/billing│/api/support│ /sub/{tk} │ /api/traffic
       ▼           ▼            ▼            ▼            ▼           ▼
   [  IAM  ]   [  Node  ]   [ Billing ]  [ Support ]  [  Node  ]  [ Traffic ]
     :8001       :8002        :8003        :8004        :8002       :8007
       │           │            │            │                        │
       │           │            │            │ /uploads               │
       │           │            │            └──────────┐             │
       │           │            │                       ▼             │
   ( iam_db )  ( node_db )  (billing_db)           (support_db)  (traffic_db)
       │           │            │                       │             │
  ═════╪═══════════╪════════ EVENT CHOREOGRAPHY PLANE ══╪═════════════╪═══════
       │           │            │                       │             │
       ▼           ▼            ▼                       ▼             ▼
   ═════════════════════════════════════════════════════════════════════════
                       [ RabbitMQ Message Broker :5672 ]
             Fanouts: user_created | traffic_consumed | config_settings
   ═════════════════════════════════════════════════════════════════════════
          ▲                     ▲                     ▲            ▲
          │                     │                     │            │
          ▼                     ▼                     │            │
     [ Config ]          [ Notification ]             │            │
       :8006                 :8005                    │            │
    (config_db)           (notif_db)                  │            │
          │                     │                     │            │
          │                     │ /notifications/stream (SSE)      │
          │                     ▼                     │            │
          │             [ Browser Client ]            │            │
          │                                           ▼            │
          │                               [ Telegram Bot Worker ]  │
          │                                  (Telethon MTProto)    │
          │                                           │            │
          └─────────────────┬─────────────────────────┴────────────┘
                            ▼
                  [ Shared Redis :6379 ]
                 (FSM State & Rate Limits)
```
---

## 📦 Microservice Inventory

| Service | Primary Responsibility | Database | Host Port | Internal Container Port |
| :--- | :--- | :--- | :---: | :---: |
| **`api-gateway`** | Centralized Nginx routing, path rewriting, dynamic CORS matching, and SSE keep-alive optimization. | — | `8000` | `80` |
| **`iam-service`** | Identity authority, password hashing (`bcrypt`), 12 recovery keys, RS256 token signing authority. | `iam_db` | `8001` | `8000` |
| **`node-service`** | 3X-UI server fleet orchestration, capacity holds, dynamic DB rates, v2rayNG subscription links (`/sub/{token}`). | `node_db` | `8002` | `8000` |
| **`billing-service`** | Multi-bucket PAYG wallet ledger (`GIFT`, `FIAT`, `CRYPTO`), waterfall deductions, AML refund cooldowns. | `billing_db` | `8003` | `8000` |
| **`support-service`** | Helpdesk ticketing, threaded discussions, media/diagnostic log uploads (`/uploads`), read receipt tracking. | `support_db` | `8004` | `8000` |
| **`notification-service`** | In-app notification store, custom low-balance thresholds, Server-Sent Events (SSE) streaming (`/notifications/stream`). | `notification_db` | `8005` | `8000` |
| **`config-service`** | Platform branding, maintenance mode lockdown, dynamic PAYG sync intervals, route replacement policies. | `config_db` | `8006` | `8000` |
| **`traffic-service`** | Timeseries bandwidth aggregation and CIDR-based subnet ISP attribution (MCI, Irancell, Rightel, Shatel, etc.). | `traffic_db` | `8007` | `8000` |
| **`telegram-service`** | Asynchronous Telethon MTProto bot, interactive FSM wizards (Top-up, Refund, Support), instant push alerts. | Redis State | — | — |
| **`shared-lib`** | Standardized `vpn_shared` library containing `EventKeys`, Pydantic models, RS256 token verifier, and Redis rate limiters. | — | — | — |

---

## ⚙️ Key Engineering Highlights

### 1. Waterfall Deduction & Multi-Bucket Ledger
Billing implements a strict sequential drain order to protect client capital and eliminate unexpected debt:
$$\text{GIFT Bucket (Promotional)} \longrightarrow \text{FIAT Bucket (Shetab Bank)} \longrightarrow \text{CRYPTO Bucket (USDT/TON)}$$
* Promotional gift credits never create negative debt upon overshoot.
* Paid credit overages are hard-capped in the database at `-max_negative_balance`.
* Fully supports dual-currency accounting: Toman (IRT) and USD ($).

### 2. 3X-UI Fleet Management & Collision-Proof Configs
* Configurations are named using unique identifiers: `{username}-{region}-{short_id}-PAYG` (with multi-config indexing: `{username}-{region}-{index}-{short_id}-PAYG`).
* Eliminates duplicate email collisions in 3X-UI when clients re-provision or connect from multiple devices.
* Dynamic timeout configuration (`xui_request_timeout`) replaces hardcoded timeouts across all panel operations.

### 3. Edge-Triggered Low-Balance Warning Engine
* Alerts fire **exactly once** when remaining usable credit drops below the user's custom threshold.
* Re-arms automatically when the wallet is refilled (`WALLET_FUNDED`) or balance recovers above threshold.

### 4. CIDR Subnet ISP Attribution
* Pre-compiled memory matching of IPv4/IPv6 client subnets against Iranian and international telecoms (`config/isp_subnets.json`).
* Real-time distribution breakdown visualized on web dashboards and Telegram bot telemetry.

---

## 📁 Repository Structure

```text
core-services/
├── api-gateway/            # Nginx proxy configuration & Dockerfile
├── iam-service/            # Authentication, identity & recovery keys
├── node-service/           # 3X-UI server management & subscription engine
├── billing-service/        # Multi-bucket ledger & PAYG financial engine
├── support-service/        # Helpdesk ticketing & message attachments
├── notification-service/   # Notification inbox, preferences & SSE stream
├── config-service/         # Platform branding, sync intervals & policies
├── traffic-service/        # Telemetry aggregation & ISP CIDR matcher
├── telegram-service/       # Telethon MTProto Telegram Bot worker
├── shared-lib/             # vpn_shared package (installed in all containers)
├── keys/                   # Asymmetric RS256 RSA Key storage (gitignored)
├── documentations/         # Comprehensive documentation suite
│   ├── 01-development/     # Local dev runbooks, DB matrix, logging & debugging
│   ├── 02-security/        # RS256 asymmetric keys, generation & crypto specs
│   ├── 03-architecture/    # Microservices specs, event topology & 3X-UI integration
│   ├── 04-production/      # Production deployment & multi-server WireGuard runbook
│   ├── assets/             # Documentation media & banner images
│   └── README.md           # Documentation suite master index
├── docker-compose.all.yml  # Master local multi-container composition
└── .env.example            # Master environment variable template
```

---

## 🔐 Security & Asymmetric Cryptography

The platform strictly uses **Asymmetric RSA (RS256)** for JWT token signing and verification:
* **Private Key (`jwt_private.pem`):** Kept **strictly** inside `iam-service`. Used exclusively to sign access tokens upon login/registration.
* **Public Key (`jwt_public.pem`):** Distributed to all other microservices to statelessly verify token authenticity.

### Generate RSA Keys (Required Prerequisite)
Run in your project root before launching containers:
```bash
mkdir -p keys
openssl genrsa -out keys/jwt_private.pem 2048
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
chmod 600 keys/jwt_private.pem
chmod 644 keys/jwt_public.pem
```

---

## 🚀 Quick Start: Local Development

### Prerequisites
* **Docker Engine:** `v24.0+`
* **Docker Compose:** `v2.20+`
* **OpenSSL:** (for generating development RSA keys)

### 1. Setup Environment and Keys
```bash
# Clone the repository
git clone https://github.com/payg-vpn-platform/core-services.git
cd core-services

# Copy environment variables template
cp .env.example .env

# Generate required RSA keys
mkdir -p keys
openssl genrsa -out keys/jwt_private.pem 2048
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
```

### 2. Start Backend Stack (Hot-Reload Mode)
```bash
docker compose -f docker-compose.all.yml --profile development up --build -d
```

### 3. Verify Health Checks
* **API Gateway:** `http://localhost:8000/health`
* **IAM Service:** `http://localhost:8001/health`
* **Node Service:** `http://localhost:8002/health`
* **Billing Service:** `http://localhost:8003/health`
* **Support Service:** `http://localhost:8004/health`
* **Notification Service:** `http://localhost:8005/health`
* **Config Service:** `http://localhost:8006/health`
* **Traffic Service:** `http://localhost:8007/health`
* **RabbitMQ Management:** `http://localhost:15672` *(User: `admin`, Pass: `super_secret_rmq_pass`)*

---

## 🔄 Event Bus & Choreography Topology

The platform coordinates inter-service workflows asynchronously over RabbitMQ (`vpn_shared.events.EventKeys`) using Fanout Exchanges and direct queue bindings. The table below outlines the end-to-end choreography implemented across all microservices:

| Event Key / Exchange | Publisher | Primary Consumers | Purpose & Downstream Impact |
| :--- | :--- | :--- | :--- |
| **`user_created_fanout`** *(Exchange)* | `iam-service` | `billing`, `node`, `notification` | Initializes wallet ledger, creates V2Ray subscription token, and sets initial welcome gift credit. |
| **`traffic_consumed_fanout`** *(Exchange)* | `node-service` | `billing`, `traffic` | Simultaneously drains wallet ledger buckets (PAYG) and aggregates timeseries bandwidth usage. |
| **`vpn.events.user_suspended`** | `iam-service` | `node`, `notification` | Revokes client credentials, purges configs from 3X-UI panels, and dispatches security alerts. |
| **`vpn.events.user_activated`** | `iam-service` | `node`, `billing`, `notification` | Re-evaluates balance eligibility and re-provisions active server configurations. |
| **`vpn.events.wallet_low`** | `billing-service` | `notification` | Edge-detected threshold check firing in-app alert and triggering push delivery via `notification_created`. |
| **`vpn.events.wallet_empty`** | `billing-service` | `node`, `notification`, `telegram` | Disables client credentials on 3X-UI, sends push alert, and begins the regional grace period countdown. |
| **`vpn.events.wallet_funded`** | `billing-service` | `node`, `notification` | Re-enables credentials on 3X-UI, auto-provisions missing locations, and re-arms low balance warning latches. |
| **`vpn.events.grace_period_warning`** | `billing-service` | `notification`, `telegram` | Dispatches daily countdown notices (Day 1, 2, 3) to users before configurations are permanently deleted. |
| **`vpn.events.config_released`** | `billing-service` | `node`, `notification` | Permanently deletes expired client configs from 3X-UI panels and database, freeing capacity slots. |
| **`vpn.events.payment_processed`** | `billing-service` | `notification`, `telegram` | Alerts Admins on pending top-ups and notifies Clients on payment approval/rejection outcomes. |
| **`vpn.events.config_reservation_*`** *(Pipeline)* | `billing` / `node` | `node`, `billing` | 2-Phase atomic capacity hold pipeline reserving slots during top-up verification to eliminate overselling. |
| **`vpn.events.server_offline`** | `node-service` | `notification` | Sends technical failure diagnostics to Admins and sanitized outage notices to affected Clients. |
| **`vpn.events.server_recovered`** | `node-service` | `notification` | Informs Admins and affected Clients that a server node is reachable and operational. |
| **`vpn.events.server_changed`** | `node-service` | `notification`, `telegram` | Prompts clients to refresh/update their subscription link following node modifications. |
| **`vpn.events.server_replaced`** | `node-service` | `notification`, `telegram` | Emits emergency migration notices requiring mandatory client checkbox acknowledgment. |
| **`vpn.events.auto_support_ticket_requested`** | `node-service` | `support` | Automatically files an urgent NOC ticket with client ISP details when route replacement has no alternate SNI. |
| **`vpn.events.ticket_message_created`** | `support-service` | `notification`, `telegram` | Pushes real-time chat replies via Server-Sent Events (SSE) and Telegram alerts. |
| **`config_settings_fanout`** *(Exchange)* | `config-service` | `iam`, `node`, `traffic` | Distributes 3X-UI timeouts, client toggle cooldowns, and route replacement policies without HTTP coupling. |
| **`vpn.events.config_sync_interval_changed`** | `config-service` | `node-service` | Dynamically updates the background 3X-UI cron sweep polling interval. |
| **`vpn.events.system_broadcast`** | `config-service` | `notification` | Dispatches platform-wide maintenance announcements to all active users. |
| **`vpn.events.notification_created`** | `notification-service` | `telegram-service` | Universal event trigger consumed by the Telethon worker for instant push delivery to linked chats. |

---

## 🧪 Quality Gates & Validation

The codebase enforces automated syntax verification and application compilation gates. Full unit and integration test suites using `pytest` and `httpx` are actively on the development roadmap.

### 1. Current Sanity & Compilation Checks
You can verify that all models, schemas, and FastAPI dependencies compile without syntax or import errors:

```bash
# Verify shared library imports
python -c "import vpn_shared; print('✓ Shared library compiled successfully')"

# Verify specific microservice models and FastAPI application
cd billing-service
python -c "import app.models; import app.main; print('✓ Billing service compiled without errors')"
```

### 2. CI/CD Static Analysis Gates
Every push and pull request executes automated security and quality gates via GitHub Actions:
* **Secret Detection:** GitGuardian (`ggshield`) scans commits for leaked private keys and API tokens.
* **Syntax & PEP-8 Linting:** `flake8` checks for undefined variables, syntax errors, and complexity.
* **Import Verification:** Compiles `app.models` and `app.main` against headless development environments.

### 3. Planned Testing Suite *(Roadmap)*
* [ ] Isolated unit tests for multi-bucket waterfall ledger deductions (`billing-service`).
* [ ] Mocked 3X-UI panel REST API response tests (`node-service` / `XUIClient`).
* [ ] Asymmetric token verification and expired signature tests (`vpn_shared.jwt_auth`).
* [ ] End-to-end event choreography integration tests with temporary RabbitMQ test containers.

---

## 📚 Documentation Directory

Detailed technical guides, local database tables, and runbooks are organized in the **`/documentations`** folder:

* [📘 **Documentation Index**](./documentations/README.md)
* [💻 **Local Development Guide**](./documentations/01-development/README.md)
* [🗄️ **Databases & Ports Matrix**](./documentations/01-development/DATABASES_AND_PORTS.md)
* [📋 **Logging & Debugging Runbook**](./documentations/01-development/LOGGING_AND_DEBUGGING.md)
* [🔐 **Keys & Asymmetric Security Guide**](./documentations/KEYS_GENERATION_GUIDE.md)
