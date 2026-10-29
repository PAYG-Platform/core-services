# backend/notification-service/app/events/consumer.py
import uuid
import logging
from datetime import datetime, timezone
from faststream.rabbit import RabbitRouter
from app.events.publisher import publish_notification_created

from app.database import SessionLocal
from app.models import Notification, UserNotificationPreferences, UserAccount
from mng_shared.events import EventKeys
from mng_shared.schemas import RoleEnum
from app.core.event_bus import manager

logger = logging.getLogger(__name__)
notification_router = RabbitRouter()


@notification_router.subscriber("notification_user_created_queue", exchange=EventKeys.USER_CREATED_EXCHANGE)
async def handle_user_created(msg: dict):
    """Replicates user account and role into notification_db."""
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    user_id = uuid.UUID(str(raw_user_id))
    username = msg.get("username", "")
    role_raw = msg.get("role", "Client")
    role_str = role_raw.value if hasattr(role_raw, "value") else str(role_raw)

    db = SessionLocal()
    try:
        existing = db.query(UserAccount).filter(UserAccount.user_id == user_id).first()
        if not existing:
            db.add(UserAccount(user_id=user_id, username=username, role=role_str.capitalize()))
            db.commit()
            logger.info(f"Replicated user @{username} ({role_str}) in notification_db.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling USER_CREATED in Notification Service: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.USER_TELEGRAM_LINKED)
async def handle_user_telegram_linked(msg: dict):
    """
    Notifies the user on the web app when their Telegram account is successfully connected.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "🤖 Telegram Account Connected"
        body = "Your Telegram account has been linked successfully! Your universal subscription link and automated notifications have been sent to your bot chat."

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="connection",
            link_url="/client/settings",
            is_seen=False
        )
        db.add(notif)
        db.commit()

        # Real-time SSE push to update the web dashboard
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "connection",
            "message": body
        })
        logger.info(f"[Notifications] Sent USER_TELEGRAM_LINKED alert to web app for User {user_uuid}")
    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_TELEGRAM_LINKED event: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.USER_TELEGRAM_UNLINKED)
async def handle_user_telegram_unlinked(msg: dict):
    """
    Notifies the user on the web app when their Telegram account is disconnected.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "⚠️ Telegram Account Disconnected"
        body = "Your Telegram account has been unlinked from this profile. You can reconnect at any time from Account Settings."

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="connection",
            link_url="/client/settings",
            is_seen=False
        )
        db.add(notif)
        db.commit()

        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "connection",
            "message": body
        })
    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_TELEGRAM_UNLINKED event: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.TICKET_MESSAGE_CREATED)
async def handle_ticket_message(msg: dict):
    """
    Creates notifications and pushes real-time SSE updates for tickets.
    Properly targets all Admins when a client posts a message.
    """
    logger.info(f"Notification Service received TICKET_MESSAGE_CREATED event: {msg}")
    recipient_id = msg.get("recipient_id")
    recipient_role = str(msg.get("recipient_role", "Client")).capitalize()
    ticket_id = msg.get("ticket_id")

    if not ticket_id:
        return

    message_data = msg.get("message_data") or {}
    custom_title = message_data.get("title") or "Support Ticket Message"
    preview = msg.get("preview_text", "Update on support ticket.")
    target_link = "/admin/support" if recipient_role == "Admin" else "/client/support"

    db = SessionLocal()
    try:
        target_user_ids = []
        if recipient_id:
            try:
                target_user_ids.append(uuid.UUID(str(recipient_id)))
            except ValueError:
                pass
        elif recipient_role == "Admin":
            # Target all platform Admins
            admins = db.query(UserAccount).filter(UserAccount.role == "Admin").all()
            target_user_ids = [a.user_id for a in admins]

        for user_uuid in target_user_ids:
            existing_unseen = db.query(Notification).filter(
                Notification.user_id == user_uuid,
                Notification.category == "ticket",
                Notification.link_url == target_link,
                Notification.is_seen == False
            ).first()

            if existing_unseen:
                existing_unseen.title = custom_title
                existing_unseen.message = preview
                existing_unseen.created_at = datetime.now(timezone.utc)
                existing_unseen.updated_at = datetime.now(timezone.utc)
            else:
                db.add(Notification(
                    user_id=user_uuid,
                    title=custom_title,
                    message=preview,
                    category="ticket",
                    link_url=target_link,
                    is_seen=False
                ))

        db.commit()
        
        # Broadcast SSE updates
        message_data = msg.get("message_data")
        sse_payload = {
            "ticket_id": str(ticket_id),
            "preview_text": preview,
            "message": message_data
        }

        if recipient_id:
            uid_str = str(recipient_id)
            await manager.broadcast_to_user(uid_str, "ticket_update", sse_payload)
            await manager.broadcast_to_user(uid_str, "notification", {"title": "Support Ticket Message"})
        else:
            await manager.broadcast_to_role("Admin", "ticket_update", sse_payload)
            await manager.broadcast_to_role("Admin", "notification", {"title": "Support Ticket Message"})

        logger.info(f"Broadcasted SSE ticket message update for recipient {recipient_id or recipient_role}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording ticket message notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.PAYMENT_PROCESSED)
async def handle_payment_processed(msg: dict):
    """
    Handles both new pending submissions (for Admin) and approved/rejected outcomes (for Client)
    with reference IDs and admin audit notes/rejection reasons.
    """
    user_id = msg.get("user_id")
    tx_type = msg.get("tx_type", "Topup")
    status_label = str(msg.get("status", "Pending")).capitalize()
    amount = float(msg.get("amount", 0.0))
    ref_id = str(msg.get("reference_id") or "").strip()
    admin_notes = str(msg.get("admin_notes") or msg.get("notes") or "").strip()
    currency = str(msg.get("currency", "IRT") or "IRT").upper()

    amt_str = f"${amount:.2f} USD" if currency == "USD" else f"{amount:,.0f} Toman"
    ref_suffix = f" (Ref: {ref_id})" if ref_id else ""

    db = SessionLocal()
    try:
        # Case A: New Top-Up or Refund submitted -> Notify all Admins!
        if status_label == "Pending":
            admins = db.query(UserAccount).filter(UserAccount.role == "Admin").all()
            title = f"New {tx_type} Request"
            notif_message = f"New {tx_type.lower()} of {amt_str} submitted{ref_suffix}. Verification required."
            link_url = "/admin/topups"

            for admin in admins:
                db.add(Notification(
                    user_id=admin.user_id,
                    title=title,
                    message=notif_message,
                    category="billing",
                    link_url=link_url,
                    is_seen=False
                ))
            db.commit()

            # Push SSE events to Admin
            payload_data = {"tx_type": tx_type, "amount": amount, "reference_id": ref_id}
            await manager.broadcast_to_role("Admin", "pending_topup", payload_data)
            await manager.broadcast_to_role("Admin", "notification", {"title": title, "category": "billing"})

            logger.info(f"Broadcasted pending {tx_type} alert to Admin users.")

        # Case B: Approved, Rejected, Gift, or Adjustment outcome -> Notify the Client
        elif user_id:
            user_uuid = uuid.UUID(str(user_id))
            is_unallocated = tx_type == "UnallocatedTopup"
            action = str(msg.get("action") or ("Credit" if amount >= 0 else "Debit")).capitalize()
            memo_part = f" Memo: {admin_notes}." if admin_notes else ""

            if is_unallocated:
                title = "Regional Capacity Full - Store Credit Issued"
                notif_message = msg.get("message") or (
                    f"Your payment was approved as Flexible Store Credit ({amt_str}) because the requested region reached full server capacity. "
                    "You can allocate this balance in 'Region Credits & Swap' or request a refund."
                )
                category = "unallocated_credit"
                link_url = "/client/vpn-servers"
                requires_ack = True
            elif tx_type.lower() == "gift":
                if action == "Debit":
                    title = "🎁 Gift Credit Deducted"
                    notif_message = f"Administrator deducted {amt_str} gift credit from your wallet.{memo_part}"
                else:
                    title = "🎁 Gift Credit Received"
                    notif_message = f"Administrator credited {amt_str} gift credit to your wallet.{memo_part} Your Traffic Routing configurations are active."
                category = "billing"
                link_url = "/client/wallet"
                requires_ack = False
            elif tx_type.lower() == "adjustment":
                title = "💳 Balance Adjustment"
                if action == "Debit":
                    notif_message = f"Administrator deducted {amt_str} from your wallet balance.{memo_part}"
                else:
                    notif_message = f"Administrator credited {amt_str} to your wallet balance.{memo_part} Your Traffic Routing configurations are active."
                category = "billing"
                link_url = "/client/wallet"
                requires_ack = False
            elif status_label == "Rejected":
                if tx_type.lower() == "refund":
                    title = "❌ Refund Request Declined"
                    reason_part = f" Reason: {admin_notes}" if admin_notes else ""
                    notif_message = f"Your refund request of {amt_str}{ref_suffix} was declined.{reason_part} The locked funds have been returned to your wallet balance."
                    link_url = "/client/history?tab=refunds"
                else:
                    title = "❌ Top-Up Rejected"
                    reason_part = f" Reason: {admin_notes}" if admin_notes else " Please verify your payment slip or contact support."
                    notif_message = f"Your top-up of {amt_str}{ref_suffix} was rejected.{reason_part}"
                    link_url = "/client/history?tab=topups"
                category = "billing"
                requires_ack = False
            else:
                # Approved status
                tracking_ref = str(msg.get("tracking_ref") or "").strip()
                if tx_type.lower() == "refund":
                    title = "✅ Refund Payout Completed"
                    track_part = f" Bank Reference: {tracking_ref}." if tracking_ref else ""
                    notif_message = f"Your refund request of {amt_str}{ref_suffix} has been approved and paid to your card.{track_part} Please allow standard banking processing time."
                    link_url = "/client/history?tab=refunds"
                else:
                    title = "✅ Top-Up Approved"
                    notif_message = f"Your top-up of {amt_str}{ref_suffix} has been approved. Your balance and Traffic Routing credentials have been updated."
                    link_url = "/client/wallet"
                category = "billing"
                requires_ack = False

            db.add(Notification(
                user_id=user_uuid,
                title=title,
                message=notif_message,
                category=category,
                link_url=link_url,
                is_seen=False,
                requires_acknowledgement=requires_ack,
                is_acknowledged=False
            ))
            db.commit()

            # Push SSE real-time update to web UI
            await manager.broadcast_to_user(str(user_uuid), "notification", {
                "title": title, 
                "category": category,
                "message": notif_message
            })
            logger.info(f"Recorded payment outcome for client {user_uuid}: {title}")

    except Exception as e:
        db.rollback()
        logger.error(f"Error handling payment notification: {e}")
    finally:
        db.close()



@notification_router.subscriber(EventKeys.WALLET_FUNDED)
async def handle_wallet_funded_in_notifications(msg: dict):
    """
    Triggered when credit is added to a user's wallet (top-up, gift, adjustment).
    Re-arms warning latches (low balance, empty credit, grace reminders).
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        prefs = db.query(UserNotificationPreferences).filter(
            UserNotificationPreferences.user_id == user_uuid
        ).first()

        if prefs:
            prefs.is_low_balance_warned = False
            prefs.is_empty_warned = False
            prefs.last_grace_day_notified = 0
            db.commit()
            logger.info(f"[Notifications] Re-armed all balance warning triggers for User {user_uuid} via WALLET_FUNDED.")
    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error re-arming balance warnings on WALLET_FUNDED: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.WALLET_EMPTY)
async def handle_wallet_empty_notification(msg: dict):
    """
    Triggered immediately when user balance drops to 0 or negative and configs are disabled.
    Publishes notification to website inbox, pushes via SSE, and triggers Telegram.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    currency = str(msg.get("currency", "IRT") or "IRT").upper()
    grace_days = int(msg.get("grace_days", 3) or 3)

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        prefs = db.query(UserNotificationPreferences).filter(
            UserNotificationPreferences.user_id == user_uuid
        ).first()

        if not prefs:
            prefs = UserNotificationPreferences(
                user_id=user_uuid,
                currency=currency,
                notify_low_balance=True,
                is_empty_warned=False,
            )
            db.add(prefs)
            db.commit()
            db.refresh(prefs)

        # Fire once per empty cycle; re-armed on top-up
        if prefs.is_empty_warned:
            return

        prefs.is_empty_warned = True
        db.commit()

        title = "🛑 Traffic Routing Connections Disabled - Balance Exhausted"
        body = (
            f"Your usable wallet balance has reached 0 (or entered negative debt).\n\n"
            f"• What happened: All Traffic Routing configurations have been temporarily disabled on server nodes to prevent further debt.\n"
            f"• What you must do: Your server slots are reserved during your {grace_days}-day grace period. "
            f"Please top up your wallet in the Wallet section to reactivate your connections immediately and prevent permanent configuration deletion."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="billing",
            link_url="/client/wallet",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push for browser
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "billing",
            "message": body
        })

        # Telegram delivery
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent WALLET_EMPTY alert to web and Telegram for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling WALLET_EMPTY notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.GRACE_PERIOD_WARNING)
async def handle_grace_period_warning_notification(msg: dict):
    """
    Fires once on each day of the grace period (Day 1, Day 2, etc.) until configs are released.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    grace_day = int(msg.get("grace_day", 1))
    grace_days_total = int(msg.get("grace_days_total", 3))
    days_remaining = int(msg.get("days_remaining", 1))

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        prefs = db.query(UserNotificationPreferences).filter(
            UserNotificationPreferences.user_id == user_uuid
        ).first()

        if prefs:
            if getattr(prefs, "last_grace_day_notified", 0) >= grace_day:
                return
            prefs.last_grace_day_notified = grace_day
            db.commit()

        title = f"⏳ Grace Period Alert: Day {grace_day} of {grace_days_total}"
        body = (
            f"Your Traffic Routing service has been disabled for {grace_day} day(s) due to an empty balance.\n\n"
            f"• Status: Your server node configurations are currently reserved, but only {days_remaining} day(s) remain in your grace period.\n"
            f"• Required Action: Please top up your wallet today. If your balance is not topped up before the grace period ends, your server configurations will be permanently deleted and slots released."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="billing",
            link_url="/client/wallet",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # SSE push
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "billing",
            "message": body
        })

        # Telegram delivery
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent Grace Day {grace_day}/{grace_days_total} alert for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling GRACE_PERIOD_WARNING notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.CONFIG_RELEASED)
async def handle_config_released_notification(msg: dict):
    """
    Notifies the user when their grace period has completely expired and configs were deleted.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "⚠️ Traffic Routing Configurations Released"
        body = (
            "Your grace period has expired without a top-up.\n\n"
            "• What happened: Your reserved Traffic Routing server configurations have been permanently removed from the panels.\n"
            "• What you must do: You can reconnect at any time by depositing funds in your wallet to automatically provision new server configurations."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="billing",
            link_url="/client/wallet",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "billing",
            "message": body
        })

        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent CONFIG_RELEASED notice for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling CONFIG_RELEASED notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.USER_PASSWORD_CHANGED)
async def handle_user_password_changed(msg: dict):
    """
    Creates an in-app security alert on the website, triggers a live SSE push,
    and publishes NOTIFICATION_CREATED for Telegram delivery.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "🔒 Security Alert: Password Changed"
        body = (
            "Your account password was recently changed. If you did not make this change, "
            "please recover your account immediately using your 12 recovery keys or contact support."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="security",
            link_url="/client/settings",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push for browser/web app
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "security",
            "message": body
        })

        # Broadcast NOTIFICATION_CREATED so Telegram Bot pushes the message immediately
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent USER_PASSWORD_CHANGED alert for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_PASSWORD_CHANGED notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.USER_RECOVERY_KEYS_REGENERATED)
async def handle_user_recovery_keys_regenerated(msg: dict):
    """
    Creates an in-app security alert on the website, triggers live SSE push,
    and publishes NOTIFICATION_CREATED for Telegram delivery.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "🔑 Recovery Keys Regenerated"
        body = "All existing recovery keys have been revoked and 12 new keys were issued. Make sure you have saved the new keys in a safe place."

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="security",
            link_url="/client/settings",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push for browser/web app
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "security",
            "message": body
        })

        # Broadcast NOTIFICATION_CREATED so Telegram Bot pushes the message immediately
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent USER_RECOVERY_KEYS_REGENERATED alert for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_RECOVERY_KEYS_REGENERATED notification: {e}")
    finally:
        db.close()


@notification_router.subscriber("notification.events.user_suspended")
@notification_router.subscriber(EventKeys.USER_SUSPENDED)
async def handle_user_suspended_notification(msg: dict):
    """
    Creates an in-app security alert on the website, pushes a live SSE update,
    and publishes NOTIFICATION_CREATED for Telegram delivery.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "⛔ Account Suspended by Administration"
        body = (
            "Your account and Traffic Routing services have been suspended by an administrator. "
            "Please submit a support inquiry or reply to your open tickets for review."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="security",
            link_url="/client/support",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push for browser/web app
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "security",
            "message": body
        })

        # Telegram delivery
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent USER_SUSPENDED alert for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_SUSPENDED notification: {e}")
    finally:
        db.close()


@notification_router.subscriber("notification.events.user_activated")
@notification_router.subscriber(EventKeys.USER_ACTIVATED)
async def handle_user_activated_notification(msg: dict):
    """
    Creates an in-app notice on the website, pushes a live SSE update,
    and publishes NOTIFICATION_CREATED for Telegram delivery.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(raw_user_id))
        title = "✅ Account Reactivated"
        body = (
            "Your account and Traffic Routing services have been reactivated by an administrator. "
            "Your access and configurations are restored."
        )

        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=body,
            category="security",
            link_url="/client/vpn-servers",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push for browser/web app
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "security",
            "message": body
        })

        # Telegram delivery
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=body
        )
        logger.info(f"[Notifications] Sent USER_ACTIVATED alert for User {user_uuid}")

    except Exception as e:
        db.rollback()
        logger.error(f"[Notifications] Error handling USER_ACTIVATED notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.WALLET_LOW)
async def handle_wallet_low(msg: dict):
    """
    Edge-Triggered Low Balance Warning:
    Fires ONLY ONCE when credit drops below user's custom low_balance_threshold for the first time.
    Stays completely silent as long as the user remains low.
    Re-arms automatically when user balance goes back above threshold or wallet is funded.
    """
    user_id = msg.get("user_id")
    balance = float(msg.get("balance", 0.0))
    currency = str(msg.get("currency", "IRT") or "IRT").upper()

    if not user_id:
        return

    db = SessionLocal()
    try:
        user_uuid = uuid.UUID(str(user_id))
        prefs = db.query(UserNotificationPreferences).filter(
            UserNotificationPreferences.user_id == user_uuid
        ).first()

        # If preferences row does not exist yet, initialize it
        if not prefs:
            is_usd = currency == "USD"
            prefs = UserNotificationPreferences(
                user_id=user_uuid,
                currency=currency,
                notify_low_balance=True,
                low_balance_threshold=1.00 if is_usd else 10000.0,
                is_low_balance_warned=False
            )
            db.add(prefs)
            db.commit()
            db.refresh(prefs)

        if not prefs.notify_low_balance:
            return

        # 1. Determine effective currency and custom user threshold
        eff_currency = (prefs.currency if prefs.currency else currency).upper()
        if eff_currency == "USD":
            user_threshold = float(prefs.low_balance_threshold if prefs.low_balance_threshold < 100 else 1.00)
        else:
            user_threshold = float(prefs.low_balance_threshold if prefs.low_balance_threshold is not None else 10000.0)

        # 2. RE-ARM TRIGGER: If balance has recovered above threshold, re-arm so next drop warns again
        if balance > user_threshold:
            if prefs.is_low_balance_warned:
                prefs.is_low_balance_warned = False
                db.commit()
                logger.info(f"[Notifications] User {user_uuid} balance ({balance}) recovered above threshold ({user_threshold}). Re-armed.")
            return

        # 3. EDGE TRIGGER: Balance is <= user_threshold. Check if already warned for this cycle!
        if prefs.is_low_balance_warned:
            # Already warned once when it went below threshold. DO NOT NOTIFY AGAIN until refilled!
            return

        # 4. First time dropping below threshold: mark warned so it never fires again until refilled
        prefs.is_low_balance_warned = True

        # Format localized message
        if eff_currency == "USD":
            bal_str = f"${balance:.2f} USD"
            thresh_str = f"${user_threshold:.2f} USD"
        else:
            bal_str = f"{balance:,.0f} Toman"
            thresh_str = f"{user_threshold:,.0f} Toman"

        title = "Low Balance Warning"
        warning_message = f"Warning: Your wallet balance ({bal_str}) has dropped below your threshold ({thresh_str}). Top up to avoid service interruption."

        # Add single inbox notification
        notif = Notification(
            user_id=user_uuid,
            title=title,
            message=warning_message,
            category="billing",
            link_url="/client/wallet",
            is_seen=False
        )
        db.add(notif)
        db.commit()
        db.refresh(notif)

        # Real-time SSE push to browser
        await manager.broadcast_to_user(str(user_uuid), "notification", {
            "title": title,
            "category": "billing",
            "message": warning_message
        })

        # Broadcast NOTIFICATION_CREATED so Telegram Bot pushes the warning immediately
        await publish_notification_created(
            user_id=str(user_uuid),
            notification_id=str(notif.id),
            title=title,
            message=warning_message
        )

        logger.info(f"Fired FIRST-TIME low balance warning for User {user_uuid} ({bal_str} <= {thresh_str})")
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording low balance notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.LOCATION_CAPACITY_EXCEEDED)
async def handle_location_capacity_exceeded(msg: dict):
    """
    Triggered when node-service has no capacity in a region to provision a client.
    Creates an alert notification for Admins and pushes real-time notification dot.
    """
    location_name = msg.get("location_name", "Unknown Region")
    username = msg.get("username", "Client")
    
    title = f"⚠️ Server Capacity Shortage: {location_name}"
    notif_message = (
        f"Region '{location_name}' has reached maximum server capacity. "
        f"A configuration could not be provisioned for client @{username}. "
        f"Please add a new server node or increase inbound capacity."
    )
    
    db = SessionLocal()
    try:
        admins = db.query(UserAccount).filter(UserAccount.role == "Admin").all()
        for admin in admins:
            db.add(Notification(
                user_id=admin.user_id,
                title=title,
                message=notif_message,
                category="server",
                link_url="/admin/servers",
                is_seen=False
            ))
        db.commit()
        
        await manager.broadcast_to_role("Admin", "notification", {
            "title": title,
            "category": "server"
        })
        logger.info(f"Dispatched capacity shortage alert to Admins for region '{location_name}'.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating location capacity exceeded notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.SERVER_OFFLINE)
async def handle_server_offline(msg: dict):
    """
    Triggered when a server is unreachable:
    1. Sends technical diagnostic alert to platform Admins.
    2. Sends sanitized, non-critical alert to all affected Clients hosted on that server.
    """
    server_name = msg.get("server_name", "Traffic Routing Node")
    api_url = msg.get("api_url", "")
    error_message = msg.get("error_message") or "Connection timed out or host unreachable."
    location_name = msg.get("location_name") or "Traffic Routing Region"
    affected_user_ids = msg.get("affected_user_ids") or []
    
    # 1. Technical Admin Alert
    admin_title = f"🔴 Server Unreachable: {server_name}"
    admin_message = (
        f"Server node '{server_name}' ({api_url}) is currently unreachable ({error_message}). "
        f"Configurations cannot be created on this server. The system is periodically retrying to connect."
    )

    # 2. Sanitized User Alert (No URLs, IPs, ports, or error traces)
    user_title = f"⚠️ Server Node Temporarily Unavailable: {location_name}"
    user_message = (
        f"The server node for '{location_name}' is currently experiencing a temporary network outage or undergoing maintenance. "
        f"Our team is investigating to restore it.\n\n"
        f"• What you can do: If your connection is affected, please switch to another location in your Traffic Routing app, "
        f"or use the dashboard to replace your route."
    )
    
    db = SessionLocal()
    try:
        # Notify Admins with full diagnostics
        admins = db.query(UserAccount).filter(UserAccount.role == "Admin").all()
        for admin in admins:
            recent = db.query(Notification).filter(
                Notification.user_id == admin.user_id,
                Notification.category == "server_offline",
                Notification.title == admin_title,
                Notification.is_seen == False
            ).first()
            if not recent:
                db.add(Notification(
                    user_id=admin.user_id,
                    title=admin_title,
                    message=admin_message,
                    category="server_offline",
                    link_url="/admin/servers",
                    is_seen=False
                ))
        db.commit()
        
        await manager.broadcast_to_role("Admin", "notification", {
            "title": admin_title,
            "category": "server_offline"
        })

        # Notify affected Users with sanitized info (In-App, SSE, and Telegram)
        for uid_str in affected_user_ids:
            try:
                user_uuid = uuid.UUID(str(uid_str))
                db.add(Notification(
                    user_id=user_uuid,
                    title=user_title,
                    message=user_message,
                    category="server",
                    link_url="/client/vpn-servers",
                    is_seen=False
                ))
                db.commit()

                await manager.broadcast_to_user(str(user_uuid), "notification", {
                    "title": user_title,
                    "category": "server",
                    "message": user_message
                })

                await publish_notification_created(
                    user_id=str(user_uuid),
                    notification_id=str(uuid.uuid4()),
                    title=user_title,
                    message=user_message
                )
            except Exception as user_err:
                logger.debug(f"Failed sending offline notice to user {uid_str}: {user_err}")

        logger.warning(f"Recorded server offline alert for '{server_name}' (Notified {len(affected_user_ids)} users).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling SERVER_OFFLINE event: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.SERVER_RECOVERED)
async def handle_server_recovered(msg: dict):
    """
    Triggered when a server recovers:
    1. Sends technical recovery alert to Admins.
    2. Sends sanitized recovery notification to all affected Clients hosted on that server.
    """
    server_name = msg.get("server_name", "Traffic Routing Node")
    api_url = msg.get("api_url", "")
    location_name = msg.get("location_name") or "Traffic Routing Region"
    affected_user_ids = msg.get("affected_user_ids") or []

    # 1. Technical Admin Alert
    admin_title = "🟢 Server Node Recovered"
    admin_message = (
        f"Server node '{server_name}' ({api_url}) is back online and responding. "
        f"Health probes passed and user configurations have been re-verified."
    )

    # 2. Sanitized User Alert (No sensitive server URLs or internal details)
    user_title = f"🟢 Server Node Restored: {location_name}"
    user_message = (
        f"The server node for '{location_name}' has recovered and is back online.\n\n"
        f"• What you should do: You can now reconnect. If needed, tap 'Update Subscription' in your Traffic Routing app to sync."
    )

    db = SessionLocal()
    try:
        # Notify Admins
        admins = db.query(UserAccount).filter(UserAccount.role == "Admin").all()
        for admin in admins:
            notif = Notification(
                user_id=admin.user_id,
                title=admin_title,
                message=admin_message,
                category="server",
                link_url="/admin/servers",
                is_seen=False
            )
            db.add(notif)
            db.flush()

            await publish_notification_created(
                user_id=str(admin.user_id),
                notification_id=str(notif.id),
                title=admin_title,
                message=admin_message
            )

        db.commit()

        await manager.broadcast_to_role("Admin", "notification", {
            "title": admin_title,
            "category": "server"
        })

        # Notify affected Users with sanitized info (In-App, SSE, and Telegram)
        for uid_str in affected_user_ids:
            try:
                user_uuid = uuid.UUID(str(uid_str))
                db.add(Notification(
                    user_id=user_uuid,
                    title=user_title,
                    message=user_message,
                    category="server",
                    link_url="/client/vpn-servers",
                    is_seen=False
                ))
                db.commit()

                await manager.broadcast_to_user(str(user_uuid), "notification", {
                    "title": user_title,
                    "category": "server",
                    "message": user_message
                })

                await publish_notification_created(
                    user_id=str(user_uuid),
                    notification_id=str(uuid.uuid4()),
                    title=user_title,
                    message=user_message
                )
            except Exception as user_err:
                logger.debug(f"Failed sending recovered notice to user {uid_str}: {user_err}")

        logger.info(f"Recorded server recovered notification for '{server_name}' (Notified {len(affected_user_ids)} users).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling SERVER_RECOVERED event: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.SERVER_CHANGED)
async def handle_server_changed(msg: dict):
    """Creates a notification and pushes real-time SSE when user configurations change."""
    raw_user_ids = msg.get("user_ids") or []
    if msg.get("user_id") and msg.get("user_id") not in raw_user_ids:
        raw_user_ids.append(msg.get("user_id"))

    if not raw_user_ids:
        return

    server_name = msg.get("server_name", "Traffic Routing Node")
    action = msg.get("action", "updated")
    location_name = msg.get("location_name") or "Traffic Routing Region"
    custom_msg = msg.get("message")

    if action == "added":
        title = f"🌐 New Traffic Routing Configuration Added: {location_name}"
        body = (
            f"{custom_msg}\n\n"
            if custom_msg else
            f"A new Traffic Routing configuration for '{location_name}' has been added to your account by an administrator.\n\n"
            f"• What you should do: Please update/refresh your subscription link in your Traffic Routing app (v2rayNG, Sing-box, Shadowrocket), "
            f"or check the website or Telegram bot to view and copy your new configuration."
        )
    elif action in ("removed", "deleted"):
        title = f"⚠️ Traffic Routing Configuration Removed: {location_name}"
        body = (
            f"{custom_msg}\n\n"
            if custom_msg else
            f"Your Traffic Routing configuration for '{location_name}' was removed by an administrator.\n\n"
            f"• What you should do: Please update/refresh your subscription link in your Traffic Routing app to sync your active server list."
        )
    else:
        title = f"🔄 Traffic Routing Configuration Changed: {location_name}"
        body = (
            f"{custom_msg}\n\n"
            if custom_msg else
            f"Your Traffic Routing configuration for '{location_name}' ({server_name}) has been {action}.\n\n"
            f"• What you should do: Please update/refresh your subscription link in your Traffic Routing app to sync the latest node settings. "
            f"If the connection fails or is blocked, please replace the server from your dashboard."
        )

    db = SessionLocal()
    try:
        for uid_str in raw_user_ids:
            try:
                user_uuid = uuid.UUID(str(uid_str))
            except (ValueError, TypeError):
                continue

            db.add(Notification(
                user_id=user_uuid,
                title=title,
                message=body,
                category="server",
                link_url="/client/vpn-servers",
                is_seen=False
            ))
            db.commit()

            # Push real-time SSE update to web dashboard
            await manager.broadcast_to_user(
                str(user_uuid),
                "notification",
                {"title": title, "category": "server", "message": body}
            )
            logger.info(f"Recorded and broadcasted server change notification for User {user_uuid}")
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording server change notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.SYSTEM_BROADCAST)
async def handle_system_broadcast(msg: dict):
    """Creates a notification for users when Admin issues a platform-wide announcement."""
    target_user_ids = msg.get("user_ids", [])
    title = msg.get("title", "System Announcement / Maintenance")
    message_text = msg.get("message", "System notification broadcasted by administrator.")

    if not target_user_ids:
        return

    db = SessionLocal()
    try:
        for uid in target_user_ids:
            try:
                user_uuid = uuid.UUID(str(uid))
                db.add(Notification(
                    user_id=user_uuid,
                    title=title,
                    message=message_text,
                    category="system",
                    link_url="/client",
                    is_seen=False
                ))
            except ValueError:
                continue
        db.commit()
        logger.info(f"Recorded broadcast notification for {len(target_user_ids)} users.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording system broadcast notification: {e}")
    finally:
        db.close()


@notification_router.subscriber(EventKeys.SERVER_REPLACED)
async def handle_server_replaced(msg: dict):
    """
    Creates an emergency notice for all clients whose server was replaced.
    Requires mandatory checkbox acknowledgment before dismissal.
    """
    user_ids = msg.get("user_ids", [])
    old_server_name = msg.get("old_server_name", "Blocked Node")
    replacements = msg.get("replacement_server_names", ["New High-Speed Node"])
    reason = msg.get("reason", "Great Firewall route filtering")

    if not user_ids:
        return

    rep_str = ", ".join(replacements)
    title = f"⚠️ Server Replaced: {old_server_name}"
    message = (
        f"Server node '{old_server_name}' was blocked by {reason} and has been replaced with: {rep_str}.\n\n"
        "Your universal subscription link has already been updated automatically. "
        "Please open your Traffic Routing app, tap 'Update Subscription', and connect to your new working node."
    )

    db = SessionLocal()
    try:
        for uid in user_ids:
            try:
                user_uuid = uuid.UUID(str(uid))
                db.add(Notification(
                    user_id=user_uuid,
                    title=title,
                    message=message,
                    category="server_replaced",
                    link_url="/client/vpn-servers",
                    is_seen=False,
                    requires_acknowledgement=True,
                    is_acknowledged=False,
                ))
            except (ValueError, TypeError):
                continue

        db.commit()
        logger.info(f"Broadcasted server replacement notice to {len(user_ids)} affected users.")

        # Broadcast SSE alert in real time to connected users
        for uid in user_ids:
            await manager.broadcast_to_user(
                str(uid), 
                "notification", 
                {"title": title, "category": "server_replaced"}
            )
    except Exception as e:
        db.rollback()
        logger.error(f"Error handling SERVER_REPLACED event: {e}")
    finally:
        db.close()