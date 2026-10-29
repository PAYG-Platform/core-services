# 💾 Backup, Archival & Disaster Recovery Runbook

This guide specifies the operational backup procedures, offsite replication, incremental file sync, encryption of sensitive secrets, and end-to-end restore drills for the **PAYG MNG Platform**.

---

## 📑 Table of Contents

- [1. Disaster Recovery Objectives (RPO & RTO)](#1-disaster-recovery-objectives-rpo--rto)
- [2. Production Data Inventory](#2-production-data-inventory)
- [3. PostgreSQL Database Backup & Restore](#3-postgresql-database-backup--restore)
  - [3.1 Automated Multi-Database Backup Script](#31-automated-multi-database-backup-script)
  - [3.2 Single-Database Disaster Recovery (Restore)](#32-single-database-disaster-recovery-restore)
- [4. Media, Receipts & Uploads Archival](#4-media-receipts--uploads-archival)
  - [4.1 Storage Locations & Media Types](#41-storage-locations--media-types)
  - [4.2 Local Archival & Compression](#42-local-archival--compression)
  - [4.3 Offsite Sync to Cloud Object Storage (S3 / B2 / MinIO via Rclone)](#43-offsite-sync-to-cloud-object-storage-s3--b2--minio-via-rclone)
- [5. Cryptographic Keys & Secrets Vault Backup](#5-cryptographic-keys--secrets-vault-backup)
- [6. State Snapshotting: Redis & RabbitMQ](#6-state-snapshotting-redis--rabbitmq)
- [7. Master Unified Backup Script (`backup_all.sh`)](#7-master-unified-backup-script-backup_allsh)
- [8. Automated Scheduling & Retention Policies](#8-automated-scheduling--retention-policies)
- [9. Complete Clean-Slate Restoration Drill](#9-complete-clean-slate-restoration-drill)
- [10. Backup Integrity & Verification Checklist](#10-backup-integrity--verification-checklist)

---

## 1. Disaster Recovery Objectives (RPO & RTO)

| Metric | Target Objective | Implementation Strategy |
| :--- | :---: | :--- |
| **Recovery Point Objective (RPO)** | **< 24 Hours** *(or < 1 Hr with WAL)* | Daily automated database dumps + hourly incremental sync of financial records. |
| **Recovery Time Objective (RTO)** | **< 30 Minutes** | Pre-scripted restore jobs, automated Docker volume re-mounting, and container health checks. |

---

## 2. Production Data Inventory

The platform state is distributed across databases, local filesystems, state caches, and cryptographic key stores:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            PRODUCTION DATA ASSETS                           │
├──────────────────────┬────────────────────────────────┬─────────────────────┤
│ Component            │ Storage Location / Container   │ Sensitivity Level   │
├──────────────────────┼────────────────────────────────┼─────────────────────┤
│ 7 PostgreSQL DBs     │ Docker Volumes / Host Data Dir │ Critical (Stateful) │
│ Payment Receipts     │ billing-service/uploads        │ High (Financial)    │
│ Ticket Attachments   │ support-service/uploads        │ High (User Data)    │
│ RSA Keypair (RS256)  │ keys/jwt_private.pem           │ Maximum (Secret)    │
│ Platform Environment │ .env                           │ Maximum (Secret)    │
│ Redis Persistence    │ data/redis (dump.rdb, AOF)     │ Medium (State/FSM)  │
│ RabbitMQ Definitions │ data/rabbitmq (Mnesia)         │ Medium (Queues)     │
└──────────────────────┴────────────────────────────────┴─────────────────────┘
```

---

## 3. PostgreSQL Database Backup & Restore

Each microservice isolates its domain in a dedicated PostgreSQL 17 database.

### 3.1 Automated Multi-Database Backup Script

Save this script on your database server as `/opt/payg-mng/scripts/backup_postgres.sh`:

```bash
#!/bin/bash
set -eo pipefail

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_DIR="/opt/payg-mng/backups/database/${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

DB_USER="${DB_USER:-vpn_prod_admin}"
DB_CONTAINER_PREFIX="${DB_CONTAINER_PREFIX:-mng-postgres}"

# Databases mapped to their respective container names
declare -A DBS=(
  ["iam_prod_db"]="mng-postgres-iam-prod"
  ["node_prod_db"]="mng-postgres-node-prod"
  ["billing_prod_db"]="mng-postgres-billing-prod"
  ["support_prod_db"]="mng-postgres-support-prod"
  ["notification_prod_db"]="mng-postgres-notification-prod"
  ["config_prod_db"]="mng-postgres-config-prod"
  ["traffic_prod_db"]="mng-postgres-traffic-prod"
)

echo "======================================================="
echo "🗄️ Starting PostgreSQL Multi-DB Backup: ${TIMESTAMP}"
echo "======================================================="

for DB_NAME in "${!DBS[@]}"; do
  CONTAINER="${DBS[$DB_NAME]}"
  DUMP_FILE="${BACKUP_DIR}/${DB_NAME}.sql.gz"

  echo "⏳ Dumping ${DB_NAME} from container [${CONTAINER}]..."

  # Verify container is running
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "⚠️ WARNING: Container ${CONTAINER} is not running! Skipping ${DB_NAME}."
    continue
  fi

  # Pipe pg_dump directly into gzip compression
  docker exec -i "$CONTAINER" pg_dump \
    -U "$DB_USER" \
    --format=custom \
    --no-owner \
    --no-acl \
    "$DB_NAME" | gzip -c > "$DUMP_FILE"

  # Validate file was created and is non-empty
  if [ -s "$DUMP_FILE" ]; then
    SIZE=$(du -h "$DUMP_FILE" | cut -f1)
    echo "✅ Successfully backed up ${DB_NAME} (${SIZE})"
  else
    echo "❌ ERROR: Dump for ${DB_NAME} failed or resulted in 0 bytes!"
    exit 1
  fi
done

echo "🎉 All databases backed up successfully to: ${BACKUP_DIR}"
```

---

### 3.2 Single-Database Disaster Recovery (Restore)

To restore a database after corruption or hardware failure:

```bash
# Example: Restoring billing_prod_db
BACKUP_FILE="/opt/payg-mng/backups/database/2026-09-26_030000/billing_prod_db.sql.gz"
CONTAINER="mng-postgres-billing-prod"
DB_NAME="billing_prod_db"
DB_USER="vpn_prod_admin"

echo "🛑 Terminating active connections to ${DB_NAME}..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();"

echo "🗑️ Recreating clean database..."
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};"
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d postgres -c "CREATE DATABASE ${DB_NAME};"

echo "📥 Restoring from ${BACKUP_FILE}..."
gunzip -c "$BACKUP_FILE" | docker exec -i "$CONTAINER" pg_restore \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  --clean \
  --if-exists \
  --no-owner \
  --no-acl

echo "✅ Restoration of ${DB_NAME} completed successfully."
```

---

## 4. Media, Receipts & Uploads Archival

### 4.1 Storage Locations & Media Types

Uploads are strictly compartmentalized by service domain:
1. **`billing-service`:** Card payment transfer screenshots, banking receipts (`/app/uploads/receipt_*.jpg`).
2. **`support-service`:** Client bug screenshots, traceroute logs, screen recordings (`/app/uploads/*.png`, `*.mp4`).

Whether services are grouped on one machine or scattered across different VPS nodes, each service stores its media inside its local `./uploads` directory on that server.

---

### 4.2 Local Archival & Compression

Create `/opt/payg-mng/scripts/backup_uploads.sh`:

```bash
#!/bin/bash
set -eo pipefail

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_DIR="/opt/payg-mng/backups/media/${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

echo "📦 Archiving uploaded media files..."

# If running single-host monorepo
if [ -d "/opt/payg-mng/core-services/data/prod/uploads" ]; then
  tar -czf "${BACKUP_DIR}/monorepo_uploads_${TIMESTAMP}.tar.gz" \
    -C /opt/payg-mng/core-services/data/prod uploads
  echo "✅ Archived monorepo uploads directory."
fi

# If running standalone service folders
for SVC_DIR in "/opt/payg-mng/billing-service" "/opt/payg-mng/support-service"; do
  if [ -d "${SVC_DIR}/uploads" ]; then
    SVC_NAME=$(basename "$SVC_DIR")
    tar -czf "${BACKUP_DIR}/${SVC_NAME}_uploads_${TIMESTAMP}.tar.gz" \
      -C "$SVC_DIR" uploads
    echo "✅ Archived ${SVC_NAME} uploads."
  fi
done
```

---

### 4.3 Offsite Sync to Cloud Object Storage (S3 / B2 / MinIO via Rclone)

To survive physical data center fires or host termination, replicate backups offsite using **Rclone**.

#### Step 1: Install & Configure Rclone
```bash
sudo apt install -y rclone
rclone config
# Configure a remote named 'remote-backup' (AWS S3, Backblaze B2, Cloudflare R2, or Wasabi)
```

#### Step 2: Offsite Synchronization Command
```bash
# Sync local backups to an encrypted S3 bucket
rclone sync /opt/payg-mng/backups/ remote-backup:payg-mng-backups/$(hostname)/ \
  --transfers 4 \
  --checkers 8 \
  --fast-list \
  --log-file=/var/log/rclone_backup.log \
  --log-level INFO
```

---

## 5. Cryptographic Keys & Secrets Vault Backup

> ⚠️ **SECURITY WARNING:** Never upload `jwt_private.pem` or `.env` files to unencrypted storage or git repositories!

Encrypt secrets using **OpenSSL AES-256-CBC** with a master passphrase before saving or transmitting them offsite.

### Automated Encrypted Secrets Backup Script (`backup_secrets.sh`)
```bash
#!/bin/bash
set -eo pipefail

TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_DIR="/opt/payg-mng/backups/secrets/${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

VAULT_PASSWORD="${VAULT_PASSWORD:-ChangeThisMasterPassphraseInProduction!}"

echo "🔐 Creating encrypted archive of cryptographic keys and .env..."

# Create a temporary staging archive
TEMP_TAR=$(mktemp)
tar -czf "$TEMP_TAR" \
  -C /opt/payg-mng/core-services keys .env 2>/dev/null || \
tar -czf "$TEMP_TAR" \
  -C /opt/payg-mng keys .env 2>/dev/null

# Encrypt the tarball using AES-256 with PBKDF2 key derivation
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
  -in "$TEMP_TAR" \
  -out "${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc" \
  -pass pass:"$VAULT_PASSWORD"

rm -f "$TEMP_TAR"
echo "✅ Encrypted secrets vault created at: ${BACKUP_DIR}/secrets_${TIMESTAMP}.tar.gz.enc"
```

### Decrypting the Secrets Archive (Restore)
```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
  -in secrets_2026-09-26_030000.tar.gz.enc \
  -out secrets_restored.tar.gz \
  -pass pass:"ChangeThisMasterPassphraseInProduction!"

tar -xzf secrets_restored.tar.gz
```

---

## 6. State Snapshotting: Redis & RabbitMQ

### Redis Persistence Snapshot
Redis retains sliding-window rate limit counters and Telegram Bot FSM conversation state:
```bash
# Force background snapshot to disk (creates /data/dump.rdb)
docker exec mng-redis redis-cli BGSAVE

# Backup the RDB dump file
cp /opt/payg-mng/core-services/data/redis/dump.rdb \
   /opt/payg-mng/backups/redis_dump_$(date +%Y-%m-%d).rdb
```

### RabbitMQ Schema & Definitions Export
Export users, vhosts, queues, exchanges, and bindings:
```bash
docker exec mng-rabbitmq-prod rabbitmqctl export_definitions \
  /var/lib/rabbitmq/rabbitmq_definitions_$(date +%Y-%m-%d).json

cp /opt/payg-mng/core-services/data/prod/rabbitmq/rabbitmq_definitions_*.json \
   /opt/payg-mng/backups/
```

---

## 7. Master Unified Backup Script (`backup_all.sh`)

Consolidate all routines into a single idempotent script `/opt/payg-mng/scripts/backup_all.sh`:

```bash
#!/bin/bash
set -eo pipefail

START_TIME=$(date +%s)
DATE_TAG=$(date +%Y-%m-%d_%H%M%S)
BASE_BACKUP_DIR="/opt/payg-mng/backups"
DAILY_DIR="${BASE_BACKUP_DIR}/${DATE_TAG}"

mkdir -p "$DAILY_DIR/databases" "$DAILY_DIR/media" "$DAILY_DIR/secrets"

echo "🚀 [$(date)] Initiating Full PAYG MNG Backup Suite..."

# 1. Back up all PostgreSQL databases
DB_USER="${DB_USER:-vpn_prod_admin}"
for CONTAINER in $(docker ps --format '{{.Names}}' | grep "postgres"); do
  DB_NAME=$(docker exec "$CONTAINER" psql -U "$DB_USER" -t -c "SELECT current_database();" | xargs)
  if [ -n "$DB_NAME" ] && [ "$DB_NAME" != "postgres" ]; then
    echo "  -> Dumping $DB_NAME from $CONTAINER..."
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" --format=custom "$DB_NAME" | gzip > "${DAILY_DIR}/databases/${DB_NAME}.sql.gz"
  fi
done

# 2. Back up media and receipts
if [ -d "/opt/payg-mng/core-services/data/prod/uploads" ]; then
  echo "  -> Archiving uploads..."
  tar -czf "${DAILY_DIR}/media/uploads.tar.gz" -C /opt/payg-mng/core-services/data/prod uploads
fi

# 3. Encrypt and back up keys & environment
if [ -d "/opt/payg-mng/core-services/keys" ]; then
  echo "  -> Encrypting secrets vault..."
  tar -czf - -C /opt/payg-mng/core-services keys .env 2>/dev/null | \
    openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 \
    -out "${DAILY_DIR}/secrets/vault.tar.gz.enc" \
    -pass pass:"${VAULT_PASSWORD:-DefaultSecretKeyDoNotUseInProduction}"
fi

# 4. Offsite replication via Rclone (if configured)
if command -v rclone &> /dev/null && rclone listremotes | grep -q "remote-backup:"; then
  echo "  -> Syncing archive to cloud object storage..."
  rclone copy "$DAILY_DIR" "remote-backup:payg-mng-backups/${DATE_TAG}/" --transfers 4
fi

# 5. Retention Pruning: Remove local backups older than 14 days
echo "  -> Pruning local backups older than 14 days..."
find "$BASE_BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d -ctime +14 -exec rm -rf {} +

ELAPSED=$(( $(date +%s) - START_TIME ))
echo "✅ [$(date)] Complete backup finished successfully in ${ELAPSED} seconds."
```

---

## 8. Automated Scheduling & Retention Policies

Install a cron job running every night at **03:00 AM server time**:

```bash
chmod +x /opt/payg-mng/scripts/backup_all.sh

# Add to system crontab
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/payg-mng/scripts/backup_all.sh >> /var/log/payg_backup.log 2>&1") | crontab -
```

### Recommended Retention Schedule (Grandfather-Father-Son)
* **Daily Local Backups:** Kept on host for **14 days**.
* **Weekly Remote Backups:** Kept on Object Storage for **8 weeks**.
* **Monthly Archives:** Retained for **12 months** for financial audit compliance.

---

## 9. Complete Clean-Slate Restoration Drill

In a disaster recovery event (e.g., destroyed host, corrupted disk):

### Step 1: Provision Clean Server & Install Runtime
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 openssl rclone
mkdir -p /opt/payg-mng
cd /opt/payg-mng
```

### Step 2: Download Backups & Restore Vault
```bash
# Pull latest backup folder from remote cloud storage
rclone copy remote-backup:payg-mng-backups/latest /opt/payg-mng/restore/

# Decrypt configuration vault
openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
  -in /opt/payg-mng/restore/secrets/vault.tar.gz.enc \
  -out vault.tar.gz \
  -pass pass:"YourProductionMasterPassphrase"

tar -xzf vault.tar.gz -C /opt/payg-mng/
```

### Step 3: Clone Codebase & Restore Media
```bash
git clone https://github.com/payg-platform/core-services.git /opt/payg-mng/core-services
cp -r /opt/payg-mng/keys /opt/payg-mng/core-services/
cp /opt/payg-mng/.env /opt/payg-mng/core-services/.env

# Restore uploads
mkdir -p /opt/payg-mng/core-services/data/prod/
tar -xzf /opt/payg-mng/restore/media/uploads.tar.gz -C /opt/payg-mng/core-services/data/prod/
```

### Step 4: Boot Infrastructure & Restore Databases
```bash
cd /opt/payg-mng/core-services

# Start databases and message broker first
docker compose -f docker-compose.all.yml --profile production up -d \
  postgres-iam-prod postgres-node-prod postgres-billing-prod \
  postgres-support-prod postgres-notification-prod postgres-config-prod \
  postgres-traffic-prod rabbitmq-prod redis

echo "Waiting 15 seconds for databases to initialize schemas..."
sleep 15

# Restore each database
for DUMP in /opt/payg-mng/restore/databases/*.sql.gz; do
  DB_NAME=$(basename "$DUMP" .sql.gz)
  CONTAINER="mng-${DB_NAME//_/-}"  # Maps db name to container
  CONTAINER="${CONTAINER%-db}-prod"
  
  echo "Restoring $DB_NAME into $CONTAINER..."
  gunzip -c "$DUMP" | docker exec -i "$CONTAINER" pg_restore \
    -U vpn_prod_admin -d "$DB_NAME" --clean --if-exists --no-owner --no-acl || true
done

# Boot remaining microservices and gateway
docker compose -f docker-compose.all.yml --profile production up -d
```

---

## 10. Backup Integrity & Verification Checklist

- [ ] **Automated Cron Verified:** Check `/var/log/payg_backup.log` to confirm nightly runs execute without non-zero exit codes.
- [ ] **Gzip Validation:** Periodically verify dump integrity using `gzip -t /path/to/backup.sql.gz`.
- [ ] **Quarterly Restoration Drill:** Perform a dry-run restoration on a staging VPS once every 90 days to verify that dump archives restore without relational constraint conflicts.
- [ ] **Offsite Sync Confirmation:** Verify the object storage bucket contains updated timestamps matching the nightly host schedule.
