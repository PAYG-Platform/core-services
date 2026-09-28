# 🤖 Telegram Bot MTProto Worker Service

The **Telegram Service** is an asynchronous Telethon MTProto background worker. It provides an interactive client self-service interface (interactive menus, balance queries, top-up receipt uploads, refund requests, support ticket chat, and scannable QR codes) and consumes push notification events directly from RabbitMQ.

---

## 🚀 How to Run Docker Compose in Different Scenarios

### ⚙️ Prerequisites
Before starting, create host directories and copy your RSA keys:
```bash
# 1. Create required persistent host directories
mkdir -p keys logs

# 2. Place your keys into ./keys/
# - jwt_public.pem (Required to verify tokens)
# - jwt_private.pem (Optional: allows the bot to mint 60s short-lived delegation tokens)
cp /path/to/jwt_public.pem ./keys/jwt_public.pem
chmod 644 ./keys/jwt_public.pem

# If minting delegation tokens:
cp /path/to/jwt_private.pem ./keys/jwt_private.pem
chmod 600 ./keys/jwt_private.pem

# 3. Create your local environment configuration
cp .env.example .env
```

---

### Scenario 1: Standalone Server Deployment (Connected to Remote Redis & RabbitMQ)
Because `telegram-service` does not require a local PostgreSQL database, running it is simple:

```bash
# 1. In .env, ensure your TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_BOT_TOKEN are set.
# 2. Set the remote RABBITMQ_URL and REDIS_URL.

# 3. Launch the worker container:
docker compose up -d
```

To build locally from source code instead of pulling pre-built images:
```bash
docker compose up -d --build
```

---

### Scenario 2: Automated CI/CD Production Deployment (GHCR)
When deploying via the GitHub Actions CI/CD pipeline:

```bash
# CI/CD automatically manages the image pull and restart:
export IMAGE_TAG="7c9e667"
docker compose pull telegram-service
docker compose up -d --no-deps telegram-service
```

---

### Scenario 3: Manual Deployment from Source (`git clone`)
For developers or operators cloning the monorepo to a fresh VPS:

```bash
# 1. Clone repository
git clone https://github.com/payg-vpn-platform/core-services.git
cd core-services/telegram-service

# 2. Setup environment and keys
cp .env.example .env
nano .env # Set your Telegram credentials, remote RabbitMQ, and Redis URLs

mkdir -p keys logs
cp ../keys/jwt_public.pem ./keys/jwt_public.pem
chmod 644 ./keys/jwt_public.pem

# 3. Build and launch (Build context uses '..' to access shared-lib)
sudo docker compose up -d --build
```

---

## 🩺 Verification & Health Checks

Because `telegram-service` is an outbound worker rather than an HTTP server, verify its status via container processes and logs:

```bash
# 1. Check container is running
docker compose ps

# 2. Inspect isolated rotating log file on host
tail -f logs/telegram-service.log

# 3. Stream container standard output (Look for successful authentication)
docker logs -f vpn-telegram-service
# Expected output: "Telethon Bot successfully authenticated as @PAYG_VPN_Bot"

# 4. Interactive Test:
# Open Telegram and send /start to your bot handle.
```

---

## 🛑 Stopping & Maintenance

```bash
# Stop the bot container
docker compose down
```