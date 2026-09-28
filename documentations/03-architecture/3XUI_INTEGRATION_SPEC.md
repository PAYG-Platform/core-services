# ⚡ 3X-UI API Integration & Fleet Orchestration Specification

This document provides technical specifications for integrating, managing, polling, and self-healing **3X-UI server fleets** (supporting both legacy v2.x and OpenAPI v3.5.0 panels) within the **PAYG VPN Platform**.

---

## 📑 Table of Contents

- [1. Supported 3X-UI Panel Architectures](#1-supported-3x-ui-panel-architectures)
- [2. Inbound Discovery & Auto-Detection Algorithm](#2-inbound-discovery--auto-detection-algorithm)
- [3. Collision-Proof Client Provisioning](#3-collision-proof-client-provisioning)
- [4. Traffic Metering & Polling Architecture](#4-traffic-metering--polling-architecture)
- [5. IP Extraction & Multi-Device Attribution](#5-ip-extraction--multi-device-attribution)
- [6. Self-Healing Route Replacement Workflow](#6-self-healing-route-replacement-workflow)
- [7. Fleet Sanitation & Orphaned Config Purging](#7-fleet-sanitation--orphaned-config-purging)
- [8. Connection Resilience & Proxy Routing](#8-connection-resilience--proxy-routing)

---

## 1. Supported 3X-UI Panel Architectures

The platform dynamically detects and interfaces with two main variants of the 3X-UI panel:

| Feature / Endpoint | 3X-UI v3.x OpenAPI (`v3.5.0+`) | 3X-UI v2.x Legacy (`MHSanaei / v2.4.x`) |
| :--- | :--- | :--- |
| **Add Client** | `POST /panel/api/clients/add` | `POST /panel/api/inbounds/addClient` |
| **Update Enable State** | `POST /panel/api/clients/bulkEnable`<br>`POST /panel/api/clients/bulkDisable` | `POST /panel/api/inbounds/updateClient/{uuid}` |
| **Delete Client** | `POST /panel/api/clients/del/{email}?keepTraffic=0` | `POST /panel/api/inbounds/{id}/delClient/{uuid}` |
| **Single Traffic Query**| `GET /panel/api/clients/traffic/{email}` | `GET /panel/api/inbounds/getClientTraffics/{email}` |
| **Batch Traffic Query** | `GET /panel/api/inbounds/list` | `GET /panel/api/inbounds/list` |
| **Client IP Discovery** | `POST /panel/api/clients/ips/{email}` | `POST /panel/api/inbounds/clientIps/{email}` |

### Panel Version Detection
The `XUIClient` handles version-specific endpoints via `is_v3_version(version_str)`:
```python
def is_v3_version(version_str: Optional[str]) -> bool:
    if not version_str:
        return False
    clean = version_str.lower().strip()
    return clean.startswith("v3") or "3." in clean or "v3.5" in clean
```

---

## 2. Inbound Discovery & Auto-Detection Algorithm

When an admin clicks **Detect from 3X-UI** in the Admin Portal, `node-service` executes `XUIClient.fetch_inbounds_list`:

```
[ Admin Portal ] ──► POST /api/node/servers/detect-inbounds
                             │
                             ▼
                    GET /panel/api/inbounds/list
                             │
        ┌────────────────────┴────────────────────┐
        ▼                                         ▼
Parse streamSettings                      Extract Transport
• security: "reality" ──► vless_reality   • network: "xhttp" ──► path & host
• security: "tls"     ──► serverName      • network: "ws"    ──► path & host
• dest: "yahoo.com"   ──► fallback SNI    • network: "grpc"  ──► serviceName
```

### Auto-Detection Parser Logic:
1. **Protocol Classification:**
   * `security == "reality"` $\longrightarrow$ `vless_reality` (Flow: `xtls-rprx-vision`)
   * `network in ["xhttp", "splithttp"]` $\longrightarrow$ `xhttp` (VLESS XHTTP)
   * `network == "ws"` $\longrightarrow$ `vless_ws` / `vmess`
   * `protocol == "trojan"` $\longrightarrow$ `trojan`
2. **Host & SNI Extraction:** Safely extracts domain headers from nested JSON objects (`xhttpSettings.host`, `wsSettings.headers.Host`, `tlsSettings.serverName`, or `realitySettings.serverNames[0]`).
3. **Path Extraction:** Extracts routing paths (e.g., `/xhttp`, `/ws`) to construct connection strings.

---

## 3. Collision-Proof Client Provisioning

### The Duplicate Email Collision Problem
In high-concurrency environments, if multiple users connect or a user re-provisions after an outage, 3X-UI panels return an error if an email already exists in an inbound:
`{"success": false, "msg": "Duplicate email"}`

### Standardized Naming Convention
Configurations use a collision-proof format across all panels:
* **Single Configuration:** `{username}-{region}-{short_id}-PAYG`
* **Multi-Configuration:** `{username}-{region}-{index}-{short_id}-PAYG`

*(Spaces are sanitized to underscores `_`)*

### Auto-Healing Collision Retries
If 3X-UI encounters an email collision, `XUIClient.add_client` auto-heals by regenerating a unique nonce:

```python
# node-service/app/xui_client.py
for attempt in range(max_attempts):
    # Submit client payload to 3X-UI panel
    response = await client.post(url, headers=headers, json=payload, timeout=XUIClient.TIMEOUT)
    result = response.json() if response.status_code == 200 else {}
    success = response.status_code == 200 and result.get("success", False)

    # Detect duplicate email error
    error_str = f"{result.get('msg', '')} {response.text}".lower()
    if not success and "duplicate" in error_str and attempt < max_attempts - 1:
        nonce = secrets.token_hex(2)
        current_uuid = str(uuid.uuid4())
        current_sub_id = current_uuid.replace("-", "")[:16]
        current_email = f"{email}_{nonce}"
        logger.warning(f"Auto-resolving duplicate email on 3X-UI: retrying with '{current_email}'...")
        continue
```

---

## 4. Traffic Metering & Polling Architecture

Instead of querying 3X-UI for each user sequentially ($O(N)$ HTTP requests), `node-service` performs a **Single Batch Query per Server** ($O(1)$ HTTP request per panel) by fetching `/panel/api/inbounds/list`.

```
[ Scheduled Sweep ] ──► GET /panel/api/inbounds/list (Single Call)
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
       Parse inb.clientStats           Parse inb.settings.clients
       (Extracts up + down bytes)      (Discovers zero-traffic users)
                 │                               │
                 └───────────────┬───────────────┘
                                 ▼
                     Compute Delta Algorithm
                 delta = current_bytes - last_bytes
                                 │
           ┌─────────────────────┴─────────────────────┐
           ▼                                           ▼
      delta > 0                                delta < 0 (Reboot)
  Publish TRAFFIC_CONSUMED               Xray core restarted:
                                         delta = current_bytes
```

---

## 5. IP Extraction & Multi-Device Attribution

3X-UI stores connected client IP addresses in varied formats across versions (JSON dictionaries, arrays, strings with timestamps, bracketed IPv6 addresses).

`XUIClient.get_client_ips` handles parsing and validation:
1. Strips port numbers (`1.2.3.4:54321` $\to$ `1.2.3.4`).
2. Strips timestamps (`1.2.3.4 (2026-09-25 10:00:00)` $\to$ `1.2.3.4`).
3. Strips bracketed IPv6 addresses (`[2001:db8::1]:443` $\to$ `2001:db8::1`).
4. Validates IP syntax via `ipaddress.ip_address()`.
5. Uses regex fallback for stringified payload residues.

---

## 6. Self-Healing Route Replacement Workflow

When a client reports a blocked connection, the platform runs an automated replacement process:

```
[ Client clicks Replace ] ──► GET /configs/{id}/replace-status
                                      │
            ┌─────────────────────────┴─────────────────────────┐
            ▼                                                   ▼
  Policy Check Passed?                                Policy Check Blocked?
  (Cooldown met & unblocked route exists)             (In cooldown or region full)
            │                                                   │
            ▼                                                   ▼
  1. Record current route in blacklist history        Prompt user for carrier ISP
  2. Select alternative server with DIFFERENT SNI               │
  3. Provision client on new server                             ▼
  4. Revoke old client from old 3X-UI panel           Dispatch AUTO_SUPPORT_TICKET
  5. Purge old configs from region                    (Keep current route active!)
```

### Replacement Rules:
* **Blacklist Memory:** The user's blocked server/inbound pair is stored in `UserBlockedRouteHistory`.
* **Different SNI Guarantee:** The replacement engine skips any candidate on the same server that shares the blocked Host/SNI.
* **Non-Destructive Escalation:** If no alternative SNI has capacity, the user's existing config **remains active and enabled**, while an urgent NOC support ticket is opened with the client's ISP details.

---

## 7. Fleet Sanitation & Orphaned Config Purging

To prevent 3X-UI panels from accumulating stale clients:

1. **User-Scoped Cleanups (`purge_user_old_configs`):**
   * Triggered immediately after route replacement.
   * Compares clients against the active configuration and removes previously blocked credentials for that user.
2. **Server-Level Sweeps (`sweep_and_remove_all_payg_clients`):**
   * Triggered when an admin deletes a server node or region.
   * Matches and removes all credentials matching the platform pattern (`*-PAYG`, `usr_*`).

---

## 8. Connection Resilience & Proxy Routing

3X-UI panels hosted in restricted network zones may be unreachable directly from the backend cluster.

The platform provides configurable routing options:
* **Proxy Routing (`USE_3XUI_PROXY=true`):** Routes panel API calls through an external proxy specified in `XUI_HTTP_PROXY`.
* **Direct Routing (`USE_3XUI_PROXY=false`):** Bypasses all system proxy variables (`trust_env=False`) to connect directly.
* **Configurable Timeouts:** Replaces hardcoded timeouts with `XUIClient.get_timeout()`, driven dynamically by the `xui_request_timeout` setting.