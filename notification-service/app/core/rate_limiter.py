# backend/notification-service/app/core/rate_limiter.py
import time
from collections import defaultdict
from typing import Dict, List
from fastapi import HTTPException, status


class NotificationRateLimiter:
    """
    Enforces a strict sliding-window rate limit of maximum 3 requests 
    per user per 60-second window.
    """
    def __init__(self, max_requests: int = 3, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.user_requests: Dict[str, List[float]] = defaultdict(list)

    def check_rate_limit(self, user_id: str):
        now = time.time()
        user_history = self.user_requests[user_id]
        
        # Keep timestamps inside the window_seconds interval
        valid_timestamps = [ts for ts in user_history if now - ts < self.window_seconds]
        self.user_requests[user_id] = valid_timestamps
        
        if len(valid_timestamps) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Maximum 5 notification poll requests allowed per minute."
            )
        
        self.user_requests[user_id].append(now)


notification_rate_limiter = NotificationRateLimiter(max_requests=5, window_seconds=60)