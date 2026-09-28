# 📚 PAYG VPN Platform — Documentation Suite

Welcome to the central technical documentation suite for the **PAYG VPN Core Backend Services**. This directory contains all architecture specifications, development runbooks, database schemas, cryptographic specs, and production deployment guides.

---

## 🗂️ Documentation Navigation

### [01. Local Development (`01-development/`)](./01-development/)
* [📘 **Development Guide (`README.md`)**](./01-development/README.md): Step-by-step instructions for booting the stack in hot-reload mode, configuring environment variables, and managing local workflows.
* [🗄️ **Databases & Port Allocations (`DATABASES_AND_PORTS.md`)**](./01-development/DATABASES_AND_PORTS.md): Complete connection matrix for all 7 PostgreSQL databases, Redis, RabbitMQ, and host port mappings.
* [📋 **Logging & Debugging (`LOGGING_AND_DEBUGGING.md`)**](./01-development/LOGGING_AND_DEBUGGING.md): Live log streaming commands, individual service inspection, health check probes, and common error resolution.

---

### [02. Security & Cryptography (`02-security/`)](./02-security/KEYS_AND_CRYPTOGRAPHY.md)
* [🔐 **Asymmetric Keys & Cryptography (`KEYS_AND_CRYPTOGRAPHY.md`)**](./02-security/KEYS_AND_CRYPTOGRAPHY.md): RSA-2048 keypair generation, permissions, zero-trust token delegation, and credential hardening specifications.

---

### [03. Architecture & Specifications (`03-architecture/`)](./03-architecture/)
* [🏛️ **Microservices Specification (`MICROSERVICES_SPEC.md`)**](./03-architecture/MICROSERVICES_SPEC.md): Domain boundaries, roles, database associations, and responsibilities for all 8 microservices and the API Gateway.
* [🔄 **Event Bus & Messaging Topology (`EVENT_TOPOLOGY.md`)**](./03-architecture/EVENT_TOPOLOGY.md): Comprehensive RabbitMQ AMQP message schemas, fanout exchange topologies, and 2-phase capacity hold pipelines.
* [⚡ **3X-UI Fleet Engine & Protocol Specs (`3XUI_INTEGRATION_SPEC.md`)**](./03-architecture/3XUI_INTEGRATION_SPEC.md): Panel discovery, collision-proof config naming (`{username}-{region}-{short_id}-PAYG`), batch metering, and self-healing route replacement.

---

### [04. Production & Deployment (`04-production/`)](./04-production/)
* [🏭 **Production Deployment Runbook (`PRODUCTION_DEPLOYMENT.md`)**](./04-production/PRODUCTION_DEPLOYMENT.md): Container-first production setup, multi-container orchestration, Let's Encrypt TLS reverse proxy termination, rolling updates, and system tuning.
* [🌍 **Multi-Server & WireGuard Mesh Runbook (`MULTI_SERVER_RUNBOOK.md`)**](./04-production/MULTI_SERVER_RUNBOOK.md): Dynamic multi-host infrastructure topology, WireGuard private mesh setup, zero-trust cryptographic separation, and GitHub Actions CI/CD multi-server deployments.
* [💾 **Backup & Disaster Recovery Runbook (`BACKUP_AND_DISASTER_RECOVERY.md`)**](./04-production/BACKUP_AND_DISASTER_RECOVERY.md): Comprehensive database dumping, media upload archiving, encrypted secret vaults, cloud object storage sync via Rclone, and clean-slate restore drills.
