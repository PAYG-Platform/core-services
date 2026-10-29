# 📦 `mng_shared` - Shared Microservice Library

This package provides standardized event keys, JWT authentication helpers, and Pydantic event schemas across all microservices.

## Installation in Dockerfile
```dockerfile
COPY shared-lib /shared-lib
RUN pip install -e /shared-lib
```

## Usage Example
```python
from mng_shared import EventKeys, verify_jwt_token, TrafficConsumedEvent
```
```

---

### 2. Python Package Modules (`mng_shared/`)

#### `shared-lib/mng_shared/__init__.py`
*(Exports main utilities for clean single-line imports)*

```python
from mng_shared.events import EventKeys
from mng_shared.jwt_auth import verify_jwt_token, parse_bearer_header
from mng_shared.schemas import (
    UserCreatedEvent,
    TrafficConsumedEvent,
    WalletEmptyEvent,
    WalletFundedEvent,
    ServerOfflineEvent,
    SystemAuditLogEvent
)
from mng_shared.utils import bytes_to_gb, gb_to_bytes

__all__ = [
    "EventKeys",
    "verify_jwt_token",
    "parse_bearer_header",
    "UserCreatedEvent",
    "TrafficConsumedEvent",
    "WalletEmptyEvent",
    "WalletFundedEvent",
    "ServerOfflineEvent",
    "SystemAuditLogEvent",
    "bytes_to_gb",
    "gb_to_bytes"
]
