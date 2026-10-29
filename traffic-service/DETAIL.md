# 📈 Traffic Metering & ISP Attribution Service

This microservice manages timeseries bandwidth usage aggregation (`DailyTrafficUsage`), pre-compiled in-memory CIDR client IP matching against Iranian and international telecoms (`config/isp_subnets.json`), multi-device usage attribution (`user_isp_traffic_usage`), and background 3X-UI traffic delta synchronization.

---

## 🚀 How to Run Docker Compose in Different Scenarios

### ⚙️ Prerequisites
Before starting, create host directories, initialize your environment, and copy your public RSA verification key:
```bash
# 1. Create required persistent host directories
mkdir -p keys logs

# 2. Place your public RSA verification key into ./keys/
# (Traffic Service only verifies tokens and NEVER requires the private key)
cp /path/to/jwt_public.pem ./keys/jwt_public.pem
chmod 644 ./keys/jwt_public.pem

# 3. Create your local environment configuration
cp .env.example .env
```

---

### Scenario 1: Standalone Server (Service + Local PostgreSQL) — *Current Default*
Run both Traffic Service and its isolated database container on the same server, connecting to remote RabbitMQ and Redis:

```bash
# 1. In .env, COMPOSE_PROFILES=local-db is active by default:
#    COMPOSE_PROFILES=local-db
#    DATABASE_URL=postgresql://admin:super_secret_db_pass@postgres-traffic:5432/traffic_db

# 2. Launch the stack (Docker Compose automatically starts both DB and Service):
docker compose up -d
```

To build locally from source code instead of pulling pre-built images:
```bash
docker compose up -d --build
```

---

### Scenario 2: Connecting to Cloud/External Database (Neon, Supabase, AWS RDS)
When using managed PostgreSQL, the local database container is bypassed entirely:

```bash
# 1. In .env, disable the local DB profile and provide your cloud connection string:
#    COMPOSE_PROFILES=""
#    DATABASE_URL=postgresql://user:password@ep-cool-fog-12345.eu-central-1.neon.tech/traffic_db?sslmode=require

# 2. Start the service alone (only mng-traffic-service will be launched):
docker compose up -d
```

---

### Scenario 3: Automated CI/CD Production Deployment (GHCR)
When deploying via the GitHub Actions CI/CD pipeline:

#### ⚠️ First-Time Server Boot (Run Once Manually)
On a brand-new VPS, run this command **once** before triggering automated CI/CD:
```bash
docker compose up -d
```
*(This initializes the `postgres-traffic` volume and schema so subsequent rolling updates with `--no-deps` work without dropping the database).*

#### Ongoing Rolling Updates (Automated via CI/CD)
The deployment pipeline automatically executes:
```bash
export IMAGE_TAG="7c9e667"
docker compose pull traffic-service
docker compose up -d --no-deps traffic-service
```

---

### Scenario 4: Manual Deployment from Source (`git clone`)
For developers or operators cloning the monorepo to a fresh VPS:

```bash
# 1. Clone repository
git clone https://github.com/payg-platform/core-services.git
cd core-services/traffic-service

# 2. Setup environment and keys
cp .env.example .env
nano .env # Set your remote RabbitMQ, Redis, and DB credentials

# Copy your public verification key
mkdir -p keys logs
cp ../keys/jwt_public.pem ./keys/jwt_public.pem

# 3. Build and launch (Build context uses '..' to access shared-lib)
sudo docker compose up -d --build
```

---

## 🩺 Verification & Health Checks

```bash
# Check container status
docker compose ps

# Test service health endpoint on host port 8007
curl -s http://127.0.0.1:8007/health
# Expected Output: {"service": "traffic-service", "status": "operational", "db_connected": true}

# Inspect isolated rotating log file on host
tail -f logs/traffic-service.log

# Stream container standard output (Docker stream)
docker logs -f mng-traffic-service
```

---

## 🛑 Stopping & Maintenance

```bash
# Stop containers while preserving database and log volumes
docker compose down

# Stop and wipe database data volume (Destructive / Factory Reset)
docker compose down -v
```