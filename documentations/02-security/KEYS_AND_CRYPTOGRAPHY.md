# 🔐 Asymmetric Cryptography & Security Architecture (RS256)

This document specifies the cryptographic foundations, key lifecycle procedures, token verification pipelines, and identity-hardening standards implemented across the **PAYG VPN Platform**.

---

## 📑 Table of Contents

- [1. Security Philosophy & Zero-Trust Model](#1-security-philosophy--zero-trust-model)
- [2. Cryptographic Specifications & Algorithms](#2-cryptographic-specifications--algorithms)
- [3. Keypair Generation & Verification Runbook](#3-keypair-generation--verification-runbook)
- [4. Multi-Server Key Distribution Matrix](#4-multi-server-key-distribution-matrix)
- [5. Container & Volume Mount Integration](#5-container--volume-mount-integration)
- [6. Token Issuance & Stateless Verification Pipeline](#6-token-issuance--stateless-verification-pipeline)
- [7. Ephemeral Delegation Tokens (Telegram Service)](#7-ephemeral-delegation-tokens-telegram-service)
- [8. Identity Hardening & Credentials Security](#8-identity-hardening--credentials-security)
  - [8.1 Bcrypt Password Hashing](#81-bcrypt-password-hashing)
  - [8.2 Password Complexity & Dictionary Gate](#82-password-complexity--dictionary-gate)
  - [8.3 12 Single-Use Cryptographic Recovery Keys](#83-12-single-use-cryptographic-recovery-keys)
- [9. Threat Modeling & Attack Mitigations](#9-threat-modeling--attack-mitigations)
- [10. Operational Security Checklist](#10-operational-security-checklist)

---

## 1. Security Philosophy & Zero-Trust Model

The PAYG VPN platform operates under a **Stateless Asymmetric Token Delegation Architecture**:

1. **Centralized Signing Authority:** Only the **`iam-service`** possesses the cryptographic capability to mint and sign access tokens.
2. **Stateless Edge Verification:** Downstream microservices (`node-service`, `billing-service`, `support-service`, `notification-service`, `traffic-service`, and `config-service`) verify incoming user claims in-memory using an RSA Public Key without making synchronous HTTP calls or database lookups to `iam-service`.
3. **Compartmentalized Blast Radius:** If an attacker compromises a peripheral microservice (e.g., `traffic-service` or `support-service`), they obtain only the read-only Public Key (`jwt_public.pem`). They **cannot** forge tokens, escalate roles, or mint identity claims.

```
                    ┌──────────────────────────────────────────────┐
                    │                 iam-service                  │
                    │   Holds: jwt_private.pem + jwt_public.pem    │
                    │         Signs Tokens via RS256               │
                    └──────────────────────┬───────────────────────┘
                                           │
                           Issues Signed JWT Access Token
                                           │
                                           ▼
              ┌─────────────────────────────────────────────────────────┐
              │           Downstream Services / API Gateway             │
              │              Holds ONLY: jwt_public.pem                 │
              │                                                         │
              │  • node-service        • billing-service                │
              │  • traffic-service     • support-service                │
              │  • config-service      • notification-service           │
              │                                                         │
              │        Verifies RS256 Signatures Statelessly            │
              └─────────────────────────────────────────────────────────┘
```

---

## 2. Cryptographic Specifications & Algorithms

| Parameter | Specification | Implementation Detail |
| :--- | :--- | :--- |
| **Token Signing Algorithm** | `RS256` | RSA Signature with SHA-256 (RFC 7518) |
| **Key Type** | `RSA` | PKCS#8 Private Key, SubjectPublicKeyInfo Public Key |
| **Key Size** | `2048-bit` | Standard enterprise security strength |
| **Public Key Format** | `X.509 PEM` | `-----BEGIN PUBLIC KEY-----` |
| **Private Key Format** | `PKCS#8 PEM` | `-----BEGIN PRIVATE KEY-----` |
| **Token Expiration (`exp`)** | `7 Days` | `ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7` |
| **Clock Skew Tolerance** | `60 Seconds` | `jwt.decode(..., leeway=60)` |
| **Password Hashing** | `bcrypt` | Blowfish cipher with cryptographic salt |
| **Password Truncation** | `72 Bytes` | Truncated to avoid silent bcrypt overflow vulnerability |
| **Recovery Keys** | `12 x 12 chars` | Cryptographically random `A-Z, 0-9` via CSPRNG |

### Token Claims Structure (JWT Payload)
```json
{
  "sub": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "username": "arash_vpn",
  "email": "arash@example.com",
  "role": "Client",
  "billing_currency": "IRT",
  "language": "en",
  "parent_id": null,
  "iat": 1727260800,
  "exp": 1727865600
}
```

---

## 3. Keypair Generation & Verification Runbook

### Step 1: Generate Keypair via OpenSSL
Execute the following commands from the root directory of your project:

```bash
# 1. Create a dedicated storage directory
mkdir -p keys

# 2. Generate a 2048-bit RSA Private Key
openssl genrsa -out keys/jwt_private.pem 2048

# 3. Extract the corresponding Public Key from the private key
openssl rsa -in keys/jwt_private.pem -pubout -out keys/jwt_public.pem
```

### Step 2: Enforce Strict POSIX File Permissions
Protect the private key against unauthorized local user access:

```bash
# Private key: readable and writable ONLY by the owner
chmod 600 keys/jwt_private.pem

# Public key: readable by all container runtimes
chmod 644 keys/jwt_public.pem
```

### Step 3: Cryptographic Integrity Verification
Verify that both files form a mathematically valid keypair by comparing their MD5 modulus hashes:

```bash
openssl rsa -modulus -noout -in keys/jwt_private.pem | openssl md5
openssl rsa -pubin -modulus -noout -in keys/jwt_public.pem | openssl md5
```
*Output Verification:* **Both hashes must be 100% identical.** If the hashes differ, tokens signed by the private key will fail validation across all microservices.

---

## 4. Multi-Server Key Distribution Matrix

When deploying microservices across separate physical servers or virtual private clouds (VPCs):

| Host / Machine Role | Target Services | Required Keys | Environment Configuration |
| :--- | :--- | :---: | :--- |
| **Authentication Authority Host** | `iam-service` | `jwt_private.pem`<br>`jwt_public.pem` | `JWT_PRIVATE_KEY_PATH=/keys/jwt_private.pem`<br>`JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Fleet & Gateway Hosts** | `node-service`<br>`api-gateway` | `jwt_public.pem` **ONLY** | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Ledger & Settlement Hosts**| `billing-service` | `jwt_public.pem` **ONLY** | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Support & Messaging Hosts**| `support-service`<br>`notification-service` | `jwt_public.pem` **ONLY** | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Telemetry & Metrics Hosts** | `traffic-service` | `jwt_public.pem` **ONLY** | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Configuration Host** | `config-service` | `jwt_public.pem` **ONLY** | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |
| **Telegram Bot Worker Host** | `telegram-service` | `jwt_public.pem` *(+ private if delegation used)* | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` |

> 🚨 **CRITICAL RULE:** Never copy `jwt_private.pem` to `node-service`, `billing-service`, or any other peripheral container. Only `iam-service` requires the private key.

---

## 5. Container & Volume Mount Integration

In `docker-compose.all.yml`, the key directory is mounted as a read-only volume (`:ro`) into each service container:

```yaml
services:
  # IAM Service requires BOTH keys
  iam-service-dev:
    volumes:
      - ./keys:/app/keys:ro
    environment:
      - JWT_PRIVATE_KEY_PATH=/app/keys/jwt_private.pem
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem

  # Downstream services require ONLY the public key
  node-service-dev:
    volumes:
      - ./keys:/app/keys:ro
    environment:
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem

  billing-service-dev:
    volumes:
      - ./keys:/app/keys:ro
    environment:
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem

  traffic-service-dev:
    volumes:
      - ./keys:/app/keys:ro
    environment:
      - JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem
```

### Strict Startup Panic Gate
Hardcoded fallback keys have been removed from the platform. If key files are missing on container boot, the application terminates immediately via `os._exit(1)` with a diagnostic log:

```text
================================================================================
[FATAL ERROR] MISSING RSA PUBLIC KEY (MICROSERVICE VERIFICATION)
================================================================================
• WHAT IS MISSING:
  The RSA Public Key file (jwt_public.pem) could not be found.
  - Configured Path in JWT_PUBLIC_KEY_PATH: '/keys/jwt_public.pem'
• WHY THIS ERROR OCCURRED:
  Hardcoded fallback keys have been disabled. All microservices require
  the authentic OpenSSL-generated RSA Public Key to verify JWT tokens.
================================================================================
```

---

## 6. Token Issuance & Stateless Verification Pipeline

### 6.1 Token Issuance (`iam-service`)
When a user logs in via `/api/iam/auth/login` or registers via `/api/iam/auth/register`, the token is signed with the RSA private key:

```python
# iam-service/app/core/security.py
def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    private_key = get_jwt_private_key()
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"iat": now, "exp": expire})
    return jwt.encode(to_encode, private_key, algorithm="RS256")
```

### 6.2 Dual-Transport Extraction (`vpn_shared`)
Every incoming HTTP request is evaluated for authentication via two channels:
1. **HTTP-Only Cookie (`access_token`):** Primary transport for browsers and Web Portal SPAs (mitigating XSS extraction attacks).
2. **Authorization Header (`Bearer <token>`):** Transport for programmatic API consumers, mobile clients, and testing suites.

```python
# shared-lib/vpn_shared/jwt_auth.py
def extract_token_from_request(request: Request, cookie_name: str = "access_token") -> Optional[str]:
    # 1. Inspect Authorization Header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        token = parse_bearer_header(auth_header)
        if token:
            return token

    # 2. Fallback to HttpOnly Cookie
    cookie_token = request.cookies.get(cookie_name)
    if cookie_token:
        return parse_bearer_header(cookie_token)

    return None
```

### 6.3 Stateless Verification Dependency
Endpoints declare authentication by injecting the `get_current_user_payload` dependency:

```python
# Example in billing-service / node-service endpoints
@router.get("/client/balance")
def get_balance(
    db: Session = Depends(get_db),
    user_payload: dict = Depends(get_current_user_payload)
):
    user_id = uuid.UUID(str(user_payload["sub"]))
    user_currency = user_payload.get("billing_currency", "IRT")
    ...
```

---

## 7. Ephemeral Delegation Tokens (Telegram Service)

The **`telegram-service`** worker operates outside the browser request-response cycle. When an authenticated Telegram user interacts with an interactive bot wizard (e.g., requesting a top-up slip submission or querying active node configurations), the bot client mints an **ephemeral delegation token**:

* **Lifetime:** Strictly limited to **60 seconds** (`exp = now + 60s`).
* **Identity Injection:** Encodes `sub = user_id`, `role = Client`, and `username`.
* **Execution:** Sent via the internal API Gateway to execute operations directly on behalf of the user with full RBAC preservation.

```python
# telegram-service/app/services/api_client.py
def create_delegated_token(user_id: str, role: str = "Client", username: str = "") -> str:
    private_key = _load_private_key()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username or f"tg_{str(user_id)[:8]}",
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=60)
    }
    return jwt.encode(payload, private_key, algorithm="RS256")
```

---

## 8. Identity Hardening & Credentials Security

### 8.1 Bcrypt Password Hashing
Password hashes are computed using `bcrypt` with automated salt generation.
* **72-Byte Truncation Guard:** Standard bcrypt ignores characters beyond the 72nd byte. The codebase explicitly enforces UTF-8 byte truncation `password.encode('utf-8')[:72]` to prevent truncation attacks or denial-of-service vulnerabilities through excessively large inputs:

```python
# iam-service/app/core/security.py
def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')
```

### 8.2 Password Complexity & Dictionary Gate
The user registration and password reset endpoints enforce strict complexity validation via Pydantic validators (`UserCreate` schema):
* Minimum length: **8 characters**.
* Must contain at least **one uppercase letter** (`A-Z`).
* Must contain at least **one lowercase letter** (`a-z`).
* Must contain at least **one number** (`0-9`).
* Must contain at least **one special character** (`[!@#$%^&*(),.?":{}|<>\-_=+\\/]`).
* **Dictionary Rejection:** Evaluated against `COMMON_WEAK_PASSWORDS_SET` (`iam-service/app/core/passwords.py`), rejecting over 100 common passwords, repetitive characters (`11111111`, `aaaaaaaa`), and simple keyboard walks (`qwerty123`).

### 8.3 12 Single-Use Cryptographic Recovery Keys
Upon registration (or manual regeneration), every user is assigned **12 alphanumeric recovery keys**.
* **Entropy:** Generated using Python's cryptographically secure pseudo-random number generator (`secrets.choice` over `string.ascii_uppercase + string.digits`).
* **Format:** 12 uppercase characters (e.g., `A9K3P2M8L1W7`).
* **Single-Use Consumption:** Stored in the `recovery_keys` table. Once used to reset a password, the key is permanently marked `is_used = True` and rejected on subsequent attempts.
* **Regeneration Audit:** Regenerating keys immediately deletes all prior keys from `iam_db` and emits a `USER_RECOVERY_KEYS_REGENERATED` security event over RabbitMQ to notify the user across Web and Telegram.

```python
# iam-service/app/core/security.py
def generate_recovery_keys(count: int = 12, length: int = 12) -> List[str]:
    alphabet = string.ascii_uppercase + string.digits
    return [''.join(secrets.choice(alphabet) for _ in range(length)) for _ in range(count)]
```

---

## 9. Threat Modeling & Attack Mitigations

| Threat | Attack Vector | Architecture Mitigation |
| :--- | :--- | :--- |
| **Worker Compromise** | Attacker compromises `traffic-service` container. | Downstream containers possess only `jwt_public.pem`. An attacker cannot sign forged tokens or elevate privileges. |
| **Token Forgery** | Attacker attempts to forge an admin JWT token. | Without the private key (`jwt_private.pem`), any forged token will fail RS256 signature verification across all services. |
| **Cross-Site Scripting (XSS)** | Malicious script attempts to steal access tokens from browser storage. | Tokens are stored in **HttpOnly, SameSite=Lax** cookies, making them inaccessible to JavaScript document APIs. |
| **Replay & Clock Drift** | Delayed token replay across servers with unsynchronized clocks. | Downstream JWT verifier enforces a maximum 60-second leeway clock skew (`leeway=60`). |
| **Bcrypt Denial of Service** | Attacker submits megabyte-long password string to exhaust CPU cycles. | Passwords are strictly truncated to 72 bytes prior to bcrypt hashing. |
| **Brute-Force Rate Limiting** | Automated credential stuffing attacks against `/auth/login`. | Redis sliding-window middleware throttles login attempts per IP and per account identity (`rate_limits.yml`). |

---

## 10. Operational Security Checklist

- [ ] **Git Exclusion:** Verify that `keys/jwt_private.pem` is listed in `.gitignore` and has never been committed to git history.
- [ ] **Filesystem Hardening:** Verify file permissions:
  * `keys/jwt_private.pem` is set to `600`.
  * `keys/jwt_public.pem` is set to `644`.
- [ ] **Modulus Match:** Verify both keys share identical MD5 checksums before deployment:
  ```bash
  openssl rsa -modulus -noout -in keys/jwt_private.pem | openssl md5
  openssl rsa -pubin -modulus -noout -in keys/jwt_public.pem | openssl md5
  ```
- [ ] **Production Cookie Flags:** Ensure `COOKIE_SECURE=True` is enabled in production `.env` files when serving through HTTPS/TLS termination.
- [ ] **Key Rotation Readiness:** In the event of a private key compromise:
  1. Generate a new keypair in `./keys/`.
  2. Distribute the new `jwt_public.pem` to all servers.
  3. Restart `iam-service` with the new `jwt_private.pem` (this immediately invalidates all active sessions).
