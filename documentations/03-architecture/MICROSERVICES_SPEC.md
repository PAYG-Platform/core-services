# 🏛️ Microservices Specification & Domain Boundaries

This document provides a comprehensive technical specification for the 8 core backend microservices, the API Gateway, and the shared library comprising the **PAYG MNG Platform**.

---

## 📑 Table of Contents

- [1. System Topology Overview](#1-system-topology-overview)
- [2. API Gateway (`api-gateway`)](#2-api-gateway-api-gateway)
- [3. IAM Service (`iam-service`)](#3-iam-service-iam-service)
- [4. Node Fleet Service (`node-service`)](#4-node-fleet-service-node-service)
- [5. Billing & Financial Ledger Service (`billing-service`)](#5-billing--financial-ledger-service-billing-service)
- [6. Support & Helpdesk Service (`support-service`)](#6-support--helpdesk-service-support-service)
- [7. Notification & SSE Service (`notification-service`)](#7-notification--sse-service-notification-service)
- [8. System Config & Policy Service (`config-service`)](#8-system-config--policy-service-config-service)
- [9. Traffic Metering & Attribution Service (`traffic-service`)](#9-traffic-metering--attribution-service-traffic-service)
- [10. Telegram Bot MTProto Worker (`telegram-service`)](#10-telegram-bot-mtproto-worker-telegram-service)
- [11. Shared Framework Library (`shared-lib` / `mng_shared`)](#11-shared-framework-library-shared-lib--mng_shared)

---

## 1. System Topology Overview

```
                                  [ Web Client / Mobile Apps ]
                                                │
                                                ▼
                                    [ API Gateway :8000 ]
                                                │
       ┌──────────────┬──────────────┬──────────┴───┬──────────────┬──────────────┐
       ▼              ▼              ▼              ▼              ▼              ▼
[ iam-service ] [ node-service ] [ billing-service ] [ support ] [ notif-service ] [ config ]
   Port 8001      Port 8002       Port 8003        Port 8004      Port 8005      Port 8006
   (iam_db)       (node_db)       (billing_db)    (support_db)  (notif_db)      (config_db)
       │              │              │              │              │              │
       └──────────────┴──────────────┴──────┬───────┴──────────────┴──────────────┘
                                            ▼
                              [ RabbitMQ Event Bus :5672 ]
                                            │
                                  ┌─────────┴─────────┐
                                  ▼                   ▼
                        [ traffic-service ]  [ telegram-service ]
                            Port 8007          (Telethon Worker)
                           (traffic_db)          (Redis State)
```

---

## 2. API Gateway (`api-gateway`)

* **Technology:** Nginx (Alpine Linux)
* **Host Port:** `8000` | **Container Port:** `80`
* **Upstream Targets:** Internal Docker network `vpn_net`

### Responsibilities:
1. **Centralized Routing & Path Rewriting:** Translates ingress `/api/<service>/...` routes to internal microservice root paths.
2. **Specialized Ingress Routes:**
   * `/sub/{token}`: Routes directly to `node-service:8000/sub/{token}` for v2rayNG/Sing-box subscription pulls.
   * `/uploads/{filename}`: Routes directly to `support-service:8000/uploads/{filename}` for ticket screenshots.
3. **CORS Normalization:** Dynamically matches frontend origins (e.g., `localhost:3000`, `127.0.0.1:3000`) and injects required CORS headers (`Access-Control-Allow-Credentials: true`).
4. **SSE & Streaming Optimization:** Disables proxy buffering (`proxy_buffering off`) and chunked transfer encoding for `/api/notifications/stream` with a 12-hour read timeout.
5. **Cookie Preservation:** Employs `proxy_pass_header Set-Cookie;` to guarantee seamless HttpOnly JWT delivery across microservices.

---

## 3. IAM Service (`iam-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`iam_db`), bcrypt, PyJWT (RS256)
* **Host Port:** `8001` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Cryptographic Identity Authority:** Holds `jwt_private.pem` and mints RS256-signed tokens with 7-day expiration.
2. **Dual-Identifier Authentication:** Allows login via email or username.
3. **Password Security:** Enforces bcrypt hashing (72-byte safe truncation), complexity validation, and common weak password dictionary rejection.
4. **Emergency Recovery System:** Generates 12 cryptographically random alphanumeric single-use recovery keys (`secrets.choice` across `A-Z, 0-9`).
5. **Account Lifecycle & Governance:**
   * Admin-initiated account suspension/activation (emits `USER_SUSPENDED` / `USER_ACTIVATED`).
   * Client self-service traffic pause/resume with dynamic cooldown timers.
6. **Telegram Account Association:** Issues single-use start tokens (`tk_...`) and verifies deep-link callbacks.

---

## 4. Node Fleet Service (`node-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`node_db`), HTTPX, APScheduler
* **Host Port:** `8002` | **Container Port:** `8000`

### Domain Responsibilities:
1. **3X-UI Fleet Orchestration:** Discovers inbounds, provisions clients, updates enable/disable states, and deletes configurations across both v2.x and v3.x OpenAPI 3X-UI panels.
2. **Collision-Proof Configuration Naming:** Formats client identifiers as `{username}-{region}-{short_id}-PAYG` (with index support for multi-configs: `{username}-{region}-{index}-{short_id}-PAYG`).
3. **Atomic 2-Phase Capacity Holds:** Implements `ConfigReservation` with time-to-live (TTL) to hold inbound capacity during fiat payment audits, preventing overselling.
4. **Self-Healing Route Replacement:** Replaces blocked routes using alternate SNI/Hosts or dispatches `AUTO_SUPPORT_TICKET_REQUESTED` if no capacity exists.
5. **Universal V2Ray Subscription Engine:** Delivers Base64-encoded subscriptions via `/sub/{token}`.
6. **Automated Sweep & Reconciliation:** Background loop that checks server reachability, polls traffic deltas, enforces credit states on 3X-UI panels, and recovers failed nodes.

---

## 5. Billing & Financial Ledger Service (`billing-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`billing_db`), FastStream
* **Host Port:** `8003` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Waterfall Deduction Engine:** Sequentially drains balances in strict order:
   $$\text{GIFT (Promotional)} \longrightarrow \text{FIAT (Shetab Bank)} \longrightarrow \text{CRYPTO (USDT/TON)}$$
2. **Overdraft Protection:** Prevents gift credits from creating negative balances; caps paid credit debt at `-max_negative_balance`.
3. **Safety Cutoff Protection:** Disconnects clients immediately when usable credit drops below `safety_cutoff_threshold` (e.g., 50 Toman or $0.01 USD).
4. **Dual-Currency Financial Ledgering:** Independently balances Toman (IRT) and USD accounts.
5. **AML Refund Cooldown Engine:** Restricts refund submissions to once every $N$ days (anchored to the latest approved deposit or refund payout).
6. **Grace Period Expiration & Capacity Reclamation:** Runs an evaluation loop every 60 seconds that tracks `empty_since`, issues daily reminders (`GRACE_PERIOD_WARNING`), and permanently frees expired configs (`CONFIG_RELEASED`).

---

## 6. Support & Helpdesk Service (`support-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`support_db`), python-multipart
* **Host Port:** `8004` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Role-Based Threaded Helpdesk:** Segregates discussion threads between Clients and Administrators.
2. **Diagnostic Media Attachments:** Handles multipart uploads (`/uploads`) for screenshots, traceroute logs, and screen recordings (up to 25MB).
3. **Read-Receipt Tracking:** Manages unread message counters (`is_seen_by_admin`, `is_seen_by_client`) and sends green dot indicators to client dashboards.
4. **Automated NOC Escalation:** Consumes `AUTO_SUPPORT_TICKET_REQUESTED` to automatically open priority tickets with client ISP metadata when route replacement cannot find an alternative SNI.

---

## 7. Notification & SSE Service (`notification-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`notification_db`), Server-Sent Events (SSE)
* **Host Port:** `8005` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Persistent Real-Time SSE Stream:** Maintains `/notifications/stream` for live in-browser updates.
2. **In-App Notification Inbox:** Persists historical alerts with categorized labels (`billing`, `ticket`, `server`, `security`).
3. **Edge-Triggered Low-Balance Warning:** Employs an `is_low_balance_warned` latch to fire low-balance warnings **exactly once** when remaining usable credit drops below the user's custom threshold.
4. **Mandatory Acknowledgment Notices:** Displays blocking modals for critical system alerts (e.g., emergency server migrations) requiring explicit user confirmation before dismissal.
5. **Universal Push Gateway:** Emits `mng.events.notification_created` to dispatch alerts to the Telegram bot worker.

---

## 8. System Config & Policy Service (`config-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`config_db`), FastStream
* **Host Port:** `8006` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Platform Branding & Localization:** Serves dynamic brand names, currency defaults, and timezone rules via `/config/public`.
2. **Maintenance Lockdown:** Controls global platform maintenance locks and broadcasts system announcements (`SYSTEM_BROADCAST`).
3. **Event-Carried State Transfer:** Publishes `config_settings_fanout` to update timeouts, client cooldowns, and route policies across all microservices without synchronous HTTP calls.
4. **Dynamic Sync Intervals:** Broadcasts `CONFIG_SYNC_INTERVAL_CHANGED` to instantly update background cron frequencies across the platform.

---

## 9. Traffic Metering & Attribution Service (`traffic-service`)

* **Technology:** FastAPI, SQLAlchemy, PostgreSQL 17 (`traffic_db`), APScheduler
* **Host Port:** `8007` | **Container Port:** `8000`

### Domain Responsibilities:
1. **Timeseries Bandwidth Aggregation:** Accumulates consumption deltas from `traffic_consumed_fanout` into daily usage records (`DailyTrafficUsage`).
2. **In-Memory CIDR Subnet ISP Matcher (`ISPMatcher`):** Matches client IPv4 and IPv6 subnets against `config/isp_subnets.json` (MCI, Irancell, Rightel, Shatel, TCI, Rostelecom, MTS, etc.) with pre-configured UI hex colors.
3. **Multi-Device Attribution:** Splits and logs usage across simultaneously connected device IPs under `user_isp_traffic_usage`.
4. **Analytics Telemetry Endpoints:** Exposes daily usage curves and ISP distribution percentages for web dashboards and Telegram bot charts.

---

## 10. Telegram Bot MTProto Worker (`telegram-service`)

* **Technology:** Python 3.11, Telethon (MTProto), Redis 7 (FSM & State), qrcode
* **Exposed Ports:** *None (Outbound Worker)*

### Domain Responsibilities:
1. **MTProto Client:** Runs an asynchronous Telegram client in bot mode via long-polling.
2. **Interactive Multi-Step FSM Wizards:**
   * **Top-Up Checkout:** Validates amounts against user thresholds and collects payment receipt screenshots.
   * **Refund Wizard:** Validates refundable balances, enforces AML cooldowns, and captures Shetab card numbers.
   * **Ticket Creator:** Captures issue details and optional image attachments.
3. **Instant Push Alert Consumer:** Subscribes to RabbitMQ events (`PAYMENT_PROCESSED`, `WALLET_EMPTY`, `SERVER_REPLACED`, `NOTIFICATION_CREATED`) for real-time Telegram delivery.
4. **Delegation Token Minter:** Generates ephemeral 60-second RS256 JWTs to query API Gateway endpoints on behalf of authenticated Telegram users.

---

## 11. Shared Framework Library (`shared-lib` / `mng_shared`)

* **Packaging:** Python package (`pip install -e /shared-lib`) installed across all containers.

### Core Modules:
1. **`mng_shared.events`:** Authoritative registry of all queue and exchange names (`EventKeys`).
2. **`mng_shared.jwt_auth`:** RS256 token verification, dual-transport extraction (HttpOnly cookie and Authorization header), and FastAPI dependency helpers (`get_current_user_payload`).
3. **`mng_shared.logger`:** Centralized enterprise dual-channel logging engine (`setup_logging`). Outputs unbuffered logs to `stdout` for Docker streaming while writing to auto-rotating UTF-8 host files (`RotatingFileHandler` with 15MB cap and 7 backups). Intercepts and formats Uvicorn, FastStream, Telethon, and APScheduler logs uniformly.
4. **`mng_shared.rate_limiter`:** Sliding-window rate limiter powered by Redis Lua scripts, configured per-service via `config/rate_limits.yml`.
5. **`mng_shared.schemas`:** System-wide Pydantic models for AMQP event payloads.
6. **`mng_shared.utils`:** Unit converters (Bytes $\longleftrightarrow$ GB).