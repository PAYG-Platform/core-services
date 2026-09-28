# backend/iam-service/app/core/rate_limiter.py

import time
from collections import defaultdict
from typing import Dict, List

from fastapi import HTTPException, status


class UserRateLimiter:
    """
    Simple in-memory sliding-window rate limiter.

    The key is based on the authenticated user ID, so changing IP
    addresses does not bypass the limit.
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)

    def check(self, user_id: str, action: str):
        now = time.time()

        history = self.requests[user_id]

        history = [
            timestamp
            for timestamp in history
            if now - timestamp < self.window_seconds
        ]

        if len(history) >= self.max_requests:
            retry_after = max(
                1,
                int(self.window_seconds - (now - history[0]))
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for {action}. "
                    f"Please try again in {retry_after} seconds."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        history.append(now)
        self.requests[user_id] = history


directory_rate_limiter = UserRateLimiter(
    max_requests=30,
    window_seconds=60,
)

my_sellers_rate_limiter = UserRateLimiter(
    max_requests=30,
    window_seconds=60,
)

connection_change_rate_limiter = UserRateLimiter(
    max_requests=10,
    window_seconds=60,
)


def rate_limit_user(
    user: dict,
    limiter: UserRateLimiter,
    action: str,
):
    user_id = str(user["sub"])
    limiter.check(user_id, action)