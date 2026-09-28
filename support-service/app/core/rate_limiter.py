import time
from collections import defaultdict
from typing import Dict, List
from fastapi import HTTPException, status

class TicketRateLimiter:
    """Enforces max requests per user per window."""
    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.user_requests: Dict[str, List[float]] = defaultdict(list)

    def check_rate_limit(self, user_id: str):
        now = time.time()
        user_history = self.user_requests[user_id]
        valid_timestamps = [ts for ts in user_history if now - ts < self.window_seconds]
        self.user_requests[user_id] = valid_timestamps
        
        if len(valid_timestamps) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Maximum ticket poll requests allowed per minute."
            )
        self.user_requests[user_id].append(now)

ticket_rate_limiter = TicketRateLimiter(max_requests=10, window_seconds=60)