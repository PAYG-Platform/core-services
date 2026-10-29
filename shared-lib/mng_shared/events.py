# shared-lib/mng_shared/events.py
from faststream.rabbit import RabbitExchange, ExchangeType


class EventKeys:
    """Standardized event queue and exchange names used across RabbitMQ Publishers and Subscribers"""

    # ==========================================
    # IAM & AUTH EVENTS
    # ==========================================
    USER_CREATED = "mng.events.user_created"
    USER_CREATED_EXCHANGE = RabbitExchange("user_created_fanout", type=ExchangeType.FANOUT)

    # Admin Account Suspension & Activation
    USER_SUSPENDED = "mng.events.user_suspended"
    USER_ACTIVATED = "mng.events.user_activated"

    # Client Self-Service Traffic Routing Pause & Resume
    USER_VPN_PAUSED = "mng.events.user_vpn_paused"
    USER_VPN_RESUMED = "mng.events.user_vpn_resumed"

    # Telegram Identity Account Linking Events
    USER_TELEGRAM_LINKED = "mng.events.user_telegram_linked"
    USER_TELEGRAM_UNLINKED = "mng.events.user_telegram_unlinked"

    # Security Alerts
    USER_PASSWORD_CHANGED = "mng.events.user_password_changed"
    USER_RECOVERY_KEYS_REGENERATED = "mng.events.user_recovery_keys_regenerated"

    # ==========================================
    # TRAFFIC & NODE EVENTS
    # ==========================================
    # Triggered when usage is detected on a node
    TRAFFIC_CONSUMED = "mng.events.traffic_consumed"

    # Shared Fanout Exchange so both billing-service and traffic-service receive every usage event
    TRAFFIC_CONSUMED_EXCHANGE = RabbitExchange("traffic_consumed_fanout", type=ExchangeType.FANOUT)

    # Triggered when a 3x-ui server is unreachable
    SERVER_OFFLINE = "mng.events.server_offline"

    # Triggered when a 3x-ui server recovers and is back online
    SERVER_RECOVERED = "mng.events.server_recovered"

    # Triggered by Node Service when a server node status or configuration changes
    SERVER_CHANGED = "mng.events.server_changed"

    # Triggered by Node Service when a blocked server is replaced and users are migrated
    SERVER_REPLACED = "mng.events.server_replaced"

    # Triggered by Node Service when location capacity/availability changes
    LOCATION_CAPACITY_CHANGED = "mng.events.location_capacity_changed"

    # Triggered by Node Service when a location runs out of capacity for a user
    LOCATION_CAPACITY_EXCEEDED = "mng.events.location_capacity_exceeded"

    # Triggered by Node Service when a client cannot be allocated a replacement route
    AUTO_SUPPORT_TICKET_REQUESTED = "mng.events.auto_support_ticket_requested"

    # ==========================================
    # BILLING & WALLET EVENTS
    # ==========================================
    # Triggered by Billing Service when a user's wallet reaches 0 Toman
    WALLET_EMPTY = "mng.events.wallet_empty"

    # Triggered by Billing Service when a user's top-up is approved
    WALLET_FUNDED = "mng.events.wallet_funded"

    # Triggered by Billing Service when a user's balance drops below their warning threshold
    WALLET_LOW = "mng.events.wallet_low"

    # Triggered by Billing Service when a top-up or refund is approved/rejected
    PAYMENT_PROCESSED = "mng.events.payment_processed"

    # Triggered by Billing Service during each day of the grace period
    GRACE_PERIOD_WARNING = "mng.events.grace_period_warning"

    # Triggered by Billing Service when a location config expires after the grace period
    CONFIG_RELEASED = "mng.events.config_released"

    # ==========================================
    # CONFIGURATION RESERVATION EVENTS (ATOMIC PIPELINE)
    # ==========================================
    # Billing -> Node: Request capacity hold for top-up
    CONFIG_RESERVATION_REQUESTED = "mng.events.config_reservation_requested"

    # Node -> Billing: Capacity hold created
    CONFIG_RESERVATION_RESERVED = "mng.events.config_reservation_reserved"
    CONFIG_RESERVATION_CREATED = "mng.events.config_reservation_reserved"  # Backward compatibility alias

    # Node -> Billing: User already owns a config for this region
    CONFIG_RESERVATION_NOT_REQUIRED = "mng.events.config_reservation_not_required"

    # Node -> Billing: Region is full, no capacity
    CONFIG_RESERVATION_REJECTED = "mng.events.config_reservation_rejected"

    # Billing -> Node: Top-up approved, provision config on 3x-ui
    CONFIG_RESERVATION_CONSUME_REQUESTED = "mng.events.config_reservation_consume_requested"

    # Node -> Billing: Config provisioned on 3x-ui
    CONFIG_RESERVATION_CONSUMED = "mng.events.config_reservation_consumed"
    CONFIG_RESERVATION_CONSUME_FAILED = "mng.events.config_reservation_consume_failed"

    # Billing -> Node: Top-up rejected or canceled, release hold
    CONFIG_RESERVATION_RELEASE_REQUESTED = "mng.events.config_reservation_release_requested"

    # Node -> Billing: Capacity hold released
    CONFIG_RESERVATION_RELEASED = "mng.events.config_reservation_released"

    # Node sweep: Capacity hold timed out
    CONFIG_RESERVATION_EXPIRED = "mng.events.config_reservation_expired"

    # Triggered by Config Service when platform settings (branding, cooldown, etc.) are updated
    CONFIG_SETTINGS_CHANGED = "mng.events.config_settings_changed"
    CONFIG_SETTINGS_EXCHANGE = RabbitExchange("config_settings_fanout", type=ExchangeType.FANOUT)

    # ==========================================
    # SUPPORT & TICKETING EVENTS
    # ==========================================
    # Triggered by Support Service when a new message is posted in a ticket
    TICKET_MESSAGE_CREATED = "mng.events.ticket_message_created"

    # ==========================================
    # SYSTEM & NOTIFICATION EVENTS
    # ==========================================
    # Triggered by Config Service when Admin issues a platform-wide announcement
    SYSTEM_BROADCAST = "mng.events.system_broadcast"

    # Triggered by Notification Service for external push workers (Telegram/Email)
    NOTIFICATION_CREATED = "mng.events.notification_created"

    # Triggered by all services to record system operations into Audit Log
    SYSTEM_AUDIT_LOG = "mng.events.system_audit_log"

    # Triggered by Config Service when Admin changes the PAYG polling interval
    CONFIG_SYNC_INTERVAL_CHANGED = "mng.events.config_sync_interval_changed"