# 🗄️ Database & Port Allocations Matrix (Development Mode)

In development mode, each microservice connects to an isolated PostgreSQL 17 database instance. Ports are explicitly mapped to unique host ports to prevent collisions and allow inspection with database GUI clients (e.g. pgAdmin, DBeaver, TablePlus, VS Code Database Tools).

---

## 📊 Infrastructure Ports Matrix

| Service / Database | Container Name | Host Binding | Database Name | Username | Password (from `.env`) |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **IAM Database** | `mng-postgres-iam-dev` | `localhost:5432` | `iam_db` | `admin` | `super_secret_db_pass` |
| **Node Database** | `mng-postgres-node-dev` | `localhost:5431` | `node_db` | `admin` | `super_secret_db_pass` |
| **Billing Database** | `mng-postgres-billing-dev` | `localhost:5434` | `billing_db` | `admin` | `super_secret_db_pass` |
| **Support Database** | `mng-postgres-support-dev` | `localhost:5435` | `support_db` | `admin` | `super_secret_db_pass` |
| **Notification Database** | `mng-postgres-notification-dev` | `localhost:5436` | `notification_db` | `admin` | `super_secret_db_pass` |
| **Config Database** | `mng-postgres-config-dev` | `localhost:5437` | `config_db` | `admin` | `super_secret_db_pass` |
| **Traffic Database** | `mng-postgres-traffic-dev` | `localhost:5438` | `traffic_db` | `admin` | `super_secret_db_pass` |
| **Shared Redis** | `mng-redis` | `localhost:6379` | `DB: 0` | — | *(No Auth in Dev)* |
| **RabbitMQ AMQP** | `mng-rabbitmq-dev` | `localhost:5672` | vhost: `/` | `admin` | `super_secret_rmq_pass` |
| **RabbitMQ Web UI** | `mng-rabbitmq-dev` | `localhost:15672` | Management | `admin` | `super_secret_rmq_pass` |

---

## 🌐 Application & Microservice HTTP Ports

| Application / Service | Container Name | Host URL | Description |
| :--- | :--- | :--- | :--- |
| **API Gateway (Nginx)** | `mng-api-gateway-dev` | `http://localhost:8000` | Central routing gateway & CORS proxy |
| **IAM Service** | `mng-iam-service-dev` | `http://localhost:8001` | Auth, Identity, RS256 token issuance |
| **Node Service** | `mng-node-service-dev` | `http://localhost:8002` | 3X-UI Fleet Manager, Subscriptions |
| **Billing Service** | `mng-billing-service-dev` | `http://localhost:8003` | Multi-bucket ledger, Top-ups, Refunds |
| **Support Service** | `mng-support-service-dev` | `http://localhost:8004` | Ticketing, attachments, chat threads |
| **Notification Service** | `mng-notification-service-dev` | `http://localhost:8005` | Notification inbox, SSE streaming |
| **Config Service** | `mng-config-service-dev` | `http://localhost:8006` | Branding, policies, maintenance mode |
| **Traffic Service** | `mng-traffic-service-dev` | `http://localhost:8007` | Bandwidth metering, ISP matcher |
| **Telegram Bot Worker** | `mng-telegram-service-dev` | — | MTProto Telethon background worker |
| **Web Portal (Frontend)** | `mng-frontend-dev` | `http://localhost:3000` | React 19 / Vite single-page application |

---

## 🛠️ CLI Quick Connect Commands

### PostgreSQL Inspection via Terminal
```bash
# Connect to IAM Database
psql -h localhost -p 5432 -U admin -d iam_db

# Connect to Node Database
psql -h localhost -p 5431 -U admin -d node_db

# Connect to Billing Database
psql -h localhost -p 5434 -U admin -d billing_db

# Connect to Support Database
psql -h localhost -p 5435 -U admin -d support_db

# Connect to Notification Database
psql -h localhost -p 5436 -U admin -d notification_db

# Connect to Config Database
psql -h localhost -p 5437 -U admin -d config_db

# Connect to Traffic Database
psql -h localhost -p 5438 -U admin -d traffic_db
```

### Redis CLI
```bash
# Interactive Redis shell
redis-cli -h localhost -p 6379

# Test Redis connection
redis-cli -h localhost -p 6379 ping
```