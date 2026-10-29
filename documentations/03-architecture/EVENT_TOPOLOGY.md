# 🔄 Event Bus Topology & AMQP Message Contracts

This document specifies the RabbitMQ messaging topology, exchange configurations, queue bindings, and strict payload schemas implemented across the **PAYG MNG Platform**.

---

## 📑 Table of Contents

- [1. Broker Architecture & Exchange Types](#1-broker-architecture--exchange-types)
- [2. Exchange & Queue Topology Diagram](#2-exchange--queue-topology-diagram)
- [3. Complete Event Payload Contracts](#3-complete-event-payload-contracts)
  - [3.1 IAM & Account Lifecycle](#31-iam--account-lifecycle)
  - [3.2 3X-UI Fleet & Node Management](#32-3x-ui-fleet--node-management)
  - [3.3 Financial Ledger & Grace Period](#33-financial-ledger--grace-period)
  - [3.4 Atomic Capacity Hold Pipeline](#34-atomic-capacity-hold-pipeline)
  - [3.5 System Policies & Notifications](#35-system-policies--notifications)
- [4. Dead Lettering & Error Handling](#4-dead-lettering--error-handling)

---

## 1. Broker Architecture & Exchange Types

The platform uses **RabbitMQ 3.13 (Alpine)** on default AMQP port `5672` (Management UI: `15672`).

1. **Fanout Exchanges (`fanout`):** Used when an event must trigger parallel actions across multiple decoupled domains. Messages are broadcast to all bound queues without routing keys.
2. **Direct Queues (`direct`):** Used for point-to-point worker tasks, state confirmations, and 2-phase capacity holds.

---

## 2. Exchange & Queue Topology Diagram

```
[ iam-service ]
      │
      └──► ( user_created_fanout ) ───┬──► [ billing_user_created_queue ]
                                      ├──► [ node_user_created_queue ]
                                      └──► [ notification_user_created_queue ]

[ node-service ]
      │
      └──► ( traffic_consumed_fanout ) ──┬──► [ billing_traffic_consumed_queue ]
                                         └──► [ traffic_metering_consumed_queue ]

[ config-service ]
      │
      └──► ( config_settings_fanout ) ───┬──► [ iam_config_settings_queue ]
                                         ├──► [ node_config_settings_queue ]
                                         └──► [ traffic_config_settings_queue ]

[ Point-to-Point Direct Queues ]
  • mng.events.wallet_low             ──► [ notification-service ]
  • mng.events.wallet_empty           ──► [ node-service ], [ notification-service ], [ telegram-service ]
  • mng.events.wallet_funded          ──► [ node-service ], [ notification-service ]
  • mng.events.payment_processed      ──► [ notification-service ], [ telegram-service ]
  • mng.events.grace_period_warning   ──► [ notification-service ], [ telegram-service ]
  • mng.events.config_released        ──► [ node-service ], [ notification-service ]
  • mng.events.config_reservation_*   ──► [ node-service ] ◄──► [ billing-service ]
  • mng.events.ticket_message_created ──► [ notification-service ], [ telegram-service ]
  • mng.events.notification_created   ──► [ telegram-service ]
```

---

## 3. Complete Event Payload Contracts

All event payloads are serialized as JSON and validated using Pydantic schemas.

### 3.1 IAM & Account Lifecycle

#### `user_created_fanout` *(Fanout Exchange)*
* **Publisher:** `iam-service` | **Consumers:** `billing-service`, `node-service`, `notification-service`
* **Purpose:** Replicates user identity, initializes wallet ledger, sets up subscription token, and grants initial signup gift credit.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "username": "arash_vpn",
  "email": "arash@example.com",
  "role": "Client",
  "parent_id": null,
  "gift_balance": 20000.0,
  "billing_currency": "IRT"
}
```

#### `mng.events.user_suspended`
* **Publisher:** `iam-service` | **Consumers:** `node-service`, `notification-service`
* **Purpose:** Revokes client credentials, purges configs from 3X-UI panels, and sends security alerts.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "is_active": false
}
```

#### `mng.events.user_activated`
* **Publisher:** `iam-service` | **Consumers:** `node-service`, `billing-service`, `notification-service`
* **Purpose:** Restores login access, evaluates wallet balance, and provisions active server nodes.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "is_active": true
}
```

#### `mng.events.user_vpn_paused` / `mng.events.user_vpn_resumed`
* **Publisher:** `iam-service` | **Consumer:** `node-service`
* **Purpose:** Sets 3X-UI credentials `enable=false` (paused) or `enable=true` (resumed) for client self-service toggles without deleting database configs.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

---

### 3.2 3X-UI Fleet & Node Management

#### `traffic_consumed_fanout` *(Fanout Exchange)*
* **Publisher:** `node-service` | **Consumers:** `billing-service`, `traffic-service`
* **Purpose:** Broadcasts bandwidth consumption deltas for financial deduction and timeseries logging.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "server_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "used_bytes": 104857600,
  "price_per_gb": 1000.0,
  "price_per_gb_usd": 0.05,
  "client_email": "arash_vpn-Germany-a1b2c3d4-PAYG",
  "client_ips": ["5.112.45.12", "5.112.45.14"],
  "client_ip": "5.112.45.12"
}
```

#### `mng.events.server_offline`
* **Publisher:** `node-service` | **Consumer:** `notification-service`
* **Purpose:** Sends technical failure logs to Admins and sanitized outage notices to affected Clients.
```json
{
  "server_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "server_name": "Frankfurt-01 Fast Node",
  "api_url": "https://de.mng-node.com:2053",
  "error_message": "Connection timed out after 15.0s",
  "location_name": "Germany",
  "affected_user_ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"]
}
```

#### `mng.events.server_recovered`
* **Publisher:** `node-service` | **Consumer:** `notification-service`
* **Purpose:** Notifies Admins and affected Clients that an offline node is back online.
```json
{
  "server_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "server_name": "Frankfurt-01 Fast Node",
  "api_url": "https://de.mng-node.com:2053",
  "location_name": "Germany",
  "affected_user_ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"]
}
```

#### `mng.events.auto_support_ticket_requested`
* **Publisher:** `node-service` | **Consumer:** `support-service`
* **Purpose:** Opens an urgent support ticket when route replacement has no alternative SNI available.
```json
{
  "creator_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "creator_role": "Client",
  "category": "Server Issue",
  "subject": "[Auto-Report] Blocked Route Replacement: Germany (Irancell)",
  "content": "Client requested replacement for a blocked route. No alternative inbounds with free capacity exist in Germany.",
  "region": "Germany",
  "isp": "Irancell (MTN)"
}
```

---

### 3.3 Financial Ledger & Grace Period

#### `mng.events.wallet_low`
* **Publisher:** `billing-service` | **Consumer:** `notification-service`
* **Purpose:** Edge-detected alert firing when usable credit drops below user's custom threshold.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "balance": 4500.0,
  "threshold": 10000.0,
  "currency": "IRT"
}
```

#### `mng.events.wallet_empty`
* **Publisher:** `billing-service` | **Consumers:** `node-service`, `notification-service`, `telegram-service`
* **Purpose:** Disables client credentials on 3X-UI panels and begins grace period countdown.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": null,
  "balance": -150.0,
  "currency": "IRT",
  "grace_days": 3
}
```

#### `mng.events.wallet_funded`
* **Publisher:** `billing-service` | **Consumers:** `node-service`, `notification-service`
* **Purpose:** Re-enables credentials on 3X-UI panels, provisions missing locations, and re-arms warning latches.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": null
}
```

#### `mng.events.grace_period_warning`
* **Publisher:** `billing-service` | **Consumers:** `notification-service`, `telegram-service`
* **Purpose:** Sends daily countdown reminders (Day 1, 2, 3) before permanent configuration deletion.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "grace_day": 2,
  "grace_days_total": 3,
  "days_remaining": 1,
  "currency": "IRT"
}
```

#### `mng.events.config_released`
* **Publisher:** `billing-service` | **Consumers:** `node-service`, `notification-service`
* **Purpose:** Permanently deletes expired client configs from 3X-UI and database when grace period ends.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": null
}
```

#### `mng.events.payment_processed`
* **Publisher:** `billing-service` | **Consumers:** `notification-service`, `telegram-service`
* **Purpose:** Informs Admins of pending submissions and notifies Clients of payment approval/rejection outcomes.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "tx_type": "Topup",
  "status": "Approved",
  "amount": 50000.0,
  "reference_id": "TOP-A1B2C3D4",
  "admin_notes": "Verified via bank SMS receipt",
  "currency": "IRT",
  "action": "Credit",
  "tracking_ref": ""
}
```

---

### 3.4 Atomic Capacity Hold Pipeline

This pipeline implements a 2-phase reservation protocol between `billing-service` and `node-service` to prevent capacity overselling during payment verification.

```
[ billing-service ]                         [ node-service ]
        │                                           │
        ├─── CONFIG_RESERVATION_REQUESTED ─────────►│ (Holds slot with TTL)
        │                                           │
        │◄── CONFIG_RESERVATION_RESERVED ──────────┤
        │    (or NOT_REQUIRED / REJECTED)           │
        │                                           │
   [ Admin Reviews ]                                │
        │                                           │
        ├─── CONFIG_RESERVATION_CONSUME_REQUESTED ─►│ (Provisions on 3X-UI)
        │                                           │
        │◄── CONFIG_RESERVATION_CONSUMED ──────────┤
```

#### 1. `mng.events.config_reservation_requested`
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": "8d3e1234-5678-90ab-cdef-1234567890ab",
  "topup_transaction_id": "9b1e2233-4455-6677-8899-aabbccddeeff",
  "ttl_minutes": 30
}
```

#### 2. `mng.events.config_reservation_reserved`
```json
{
  "reservation_id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": "8d3e1234-5678-90ab-cdef-1234567890ab",
  "topup_transaction_id": "9b1e2233-4455-6677-8899-aabbccddeeff",
  "server_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "inbound_id": 1,
  "protocol": "vless_reality",
  "expires_at": "2026-09-25T14:30:00Z"
}
```

#### 3. `mng.events.config_reservation_consume_requested`
```json
{
  "reservation_id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": "8d3e1234-5678-90ab-cdef-1234567890ab",
  "topup_transaction_id": "9b1e2233-4455-6677-8899-aabbccddeeff"
}
```

#### 4. `mng.events.config_reservation_consumed`
```json
{
  "reservation_id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": "8d3e1234-5678-90ab-cdef-1234567890ab",
  "topup_transaction_id": "9b1e2233-4455-6677-8899-aabbccddeeff",
  "config_id": "4e5f6a7b-8c9d-0e1f-2a3b-4c5d6e7f8a9b",
  "server_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "inbound_id": 1,
  "protocol": "vless_reality"
}
```

#### 5. `mng.events.config_reservation_release_requested`
```json
{
  "reservation_id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "location_id": "8d3e1234-5678-90ab-cdef-1234567890ab",
  "topup_transaction_id": "9b1e2233-4455-6677-8899-aabbccddeeff"
}
```

---

### 3.5 System Policies & Notifications

#### `config_settings_fanout` *(Fanout Exchange)*
* **Publisher:** `config-service` | **Consumers:** `iam-service`, `node-service`, `traffic-service`
* **Purpose:** Distributes policy settings to all services without HTTP coupling.
```json
{
  "client_status_cooldown_minutes": 60,
  "route_replace_cooldown_days": 2,
  "disable_client_route_replacement": false,
  "xui_request_timeout": 15.0
}
```

#### `mng.events.config_sync_interval_changed`
* **Publisher:** `config-service` | **Consumer:** `node-service`
* **Purpose:** Dynamically reschedules the background 3X-UI traffic polling cron loop.
```json
{
  "billing_sync_interval": 5
}
```

#### `mng.events.notification_created`
* **Publisher:** `notification-service` | **Consumer:** `telegram-service`
* **Purpose:** Universal trigger consumed by the Telethon worker for instant push delivery.
```json
{
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "notification_id": "5f6e7d8c-9b0a-1a2b-3c4d-5e6f7a8b9c0d",
  "title": "🛑 Connections Disabled - Balance Exhausted",
  "message": "Your usable wallet balance has reached 0. Configurations have been disabled."
}
```

---

## 4. Dead Lettering & Error Handling

1. **Backoff Retries:** Database and message broker listeners include 10-attempt connection loops with 3-second intervals during cold boots.
2. **Reconciliation Self-Healing:** If an event is missed (e.g., during network partitioning), the background sweep in `node-service` (`run_server_health_and_provisioning_sweep`) resolves differences between `billing-service` balances and 3X-UI panel configurations.
3. **Queue Durability:** All RabbitMQ queues are created with `durable=True`, ensuring message preservation during broker restarts.