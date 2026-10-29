import os
import time
import re
import yaml
import jwt
from pathlib import Path
from typing import Optional, List, Dict, Any

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import JSONResponse


class RedisRateLimiter:
    """
    Shared Redis-backed rate limiter for FastAPI microservices.

    Rate-limit configuration is loaded from the individual microservice's
    config/rate_limits.yml file.
    """

    _INCR_SCRIPT = """
    local current = redis.call('INCR', KEYS[1])

    if current == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end

    return current
    """

    def __init__(
        self,
        config_path: str,
        service_name: str,
    ):
        self.service_name = service_name

        self.redis_url = os.getenv(
            "REDIS_URL",
            "redis://redis:6379/0",
        )

        try:
            self.redis = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
                health_check_interval=30,
                socket_connect_timeout=5.0,
                socket_timeout=5.0,
                retry_on_timeout=True,
            )
        except Exception:
            self.redis = None

        self.config_path = Path(config_path)
        self.rules: List[Dict[str, Any]] = self._load_config()

    def _load_config(self) -> List[Dict[str, Any]]:
        if not self.config_path.exists():
            return []

        try:
            with self.config_path.open("r", encoding="utf-8") as file:
                config = yaml.safe_load(file) or {}

            rate_limits = config.get("rate_limits")

            if not rate_limits:
                return []

            compiled_rules = []

            for rule in rate_limits:
                method = str(rule["method"]).upper().strip()
                path_template = str(rule["path"]).strip()
                requests = int(rule["requests"])
                window_seconds = int(rule["window_seconds"])

                if requests <= 0 or window_seconds <= 0:
                    continue

                # Convert path templates like /sellers/{seller_id}/regions to regex ^/sellers/[^/]+/regions/?$
                regex_str = re.sub(r"\{[a-zA-Z0-9_]+\}", r"[^/]+", path_template)
                pattern = re.compile(f"^{regex_str}/?$")

                compiled_rules.append({
                    "method": method,
                    "path": path_template,
                    "pattern": pattern,
                    "requests": requests,
                    "window_seconds": window_seconds,
                })

            return compiled_rules
        except Exception:
            return []

    def _get_rule(self, request: Request) -> Optional[Dict[str, Any]]:
        try:
            # Use scope values directly to be completely immune to URL/Route AttributeErrors
            method = request.scope.get("method", "").upper()
            raw_path = request.scope.get("path", "")

            for rule in self.rules:
                if rule["method"] == method and rule["pattern"].match(raw_path):
                    return rule
        except Exception:
            pass

        return None

    def _get_user_id(self, request: Request) -> Optional[str]:
        try:
            token = request.cookies.get("access_token")

            if not token:
                authorization = request.headers.get("Authorization")
                if authorization and authorization.startswith("Bearer "):
                    token = authorization[7:]

            if not token or token in ["null", "undefined", '""', "''"]:
                return None

            token = token.strip().strip('"').strip("'")

            # Extract user_id to apply rate-limiting limits; downstream endpoints perform full RS256 signature verification
            payload = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
            user_id = payload.get("sub")
            return str(user_id) if user_id is not None else None

        except Exception:
            return None

    async def middleware(self, request: Request, call_next):
        rule = self._get_rule(request)

        # Endpoint is not rate limited.
        if rule is None:
            return await call_next(request)

        user_id = self._get_user_id(request)

        # Allow auth dependencies downstream to handle unauthenticated 401s
        if not user_id:
            return await call_next(request)

        requests = rule.get("requests", 10)
        window_seconds = rule.get("window_seconds", 60)
        window_id = int(time.time() // window_seconds)

        redis_key = f"rate_limit:{self.service_name}:{user_id}:{rule.get('method')}:{rule.get('path')}:{window_id}"

        if self.redis is not None:
            try:
                current_count = await self.redis.eval(
                    self._INCR_SCRIPT,
                    1,
                    redis_key,
                    window_seconds,
                )
                
                if current_count > requests:
                    response = JSONResponse(
                        status_code=429,
                        content={"detail": "Rate limit exceeded. Please try again later."}
                    )
                    response.headers["Retry-After"] = str(window_seconds)
                    response.headers["X-RateLimit-Limit"] = str(requests)
                    response.headers["X-RateLimit-Window"] = str(window_seconds)
                    response.headers["X-RateLimit-Remaining"] = "0"
                    return response
                    
            except Exception:
                # Fallback gracefully if Redis is temporarily unreachable
                pass

        return await call_next(request)