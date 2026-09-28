import asyncio
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # Maps user_id -> List of active asyncio Queues
        self.active_connections: Dict[str, List[asyncio.Queue]] = {}
        # Maps role -> List of active asyncio Queues
        self.role_connections: Dict[str, List[asyncio.Queue]] = {}

    def connect(self, user_id: str, role: Optional[str] = None) -> asyncio.Queue:
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        queue = asyncio.Queue()
        self.active_connections[user_id].append(queue)

        if role:
            role_key = role.strip().capitalize()
            if role_key not in self.role_connections:
                self.role_connections[role_key] = []
            self.role_connections[role_key].append(queue)

        return queue

    def disconnect(self, user_id: str, queue: asyncio.Queue, role: Optional[str] = None):
        if user_id in self.active_connections:
            if queue in self.active_connections[user_id]:
                self.active_connections[user_id].remove(queue)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

        if role:
            role_key = role.strip().capitalize()
            if role_key in self.role_connections:
                if queue in self.role_connections[role_key]:
                    self.role_connections[role_key].remove(queue)
                if not self.role_connections[role_key]:
                    del self.role_connections[role_key]

    async def broadcast_to_user(self, user_id: str, event_type: str, data: dict):
        if user_id and user_id in self.active_connections:
            for queue in list(self.active_connections[user_id]):
                await queue.put({"event": event_type, "data": data})

    async def broadcast_to_role(self, role: str, event_type: str, data: dict):
        role_key = role.strip().capitalize()
        if role_key in self.role_connections:
            for queue in list(self.role_connections[role_key]):
                await queue.put({"event": event_type, "data": data})


manager = ConnectionManager()