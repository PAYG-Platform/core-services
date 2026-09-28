# telegram-service/app/events/consumer.py
import os
import logging
import asyncio
from faststream.rabbit import RabbitBroker
from app.config import RABBITMQ_URL
from app.services.redis_client import RedisFSM
from vpn_shared.events import EventKeys

logger = logging.getLogger(__name__)


async def start_rabbitmq_consumer(bot):
    """
    Subscribes to all platform events on RabbitMQ and dispatches
    real-time push notifications to linked Telegram users.
    """
    broker = RabbitBroker(RABBITMQ_URL)

    # 1. NOTIFICATION CREATED (Handles all user notifications: Disabled, Grace Days, Low Balance, etc.)
    @broker.subscriber(EventKeys.NOTIFICATION_CREATED)
    async def on_notification(msg: dict):
        user_id = str(msg.get("user_id") or "")
        title = msg.get("title", "Notification")
        body = msg.get("message", "")
        if not user_id:
            return

        telegram_id = await RedisFSM.get_telegram_id(user_id)
        if telegram_id:
            if "Suspended" in title or "⛔" in title:
                icon = "⛔"
            elif "Recovered" in title or "🟢" in title:
                icon = "🟢"
            elif "Reactivated" in title or "✅" in title:
                icon = "✅"
            elif "Disabled" in title or "Exhausted" in title:
                icon = "🛑"
            elif "Grace Period" in title or "⏳" in title:
                icon = "⏳"
            elif "Recovery" in title or "🔑" in title:
                icon = "🔑"
            elif "Security" in title or "Password" in title or "🔒" in title:
                icon = "🔒"
            elif "Low Balance" in title or "Warning" in title:
                icon = "⚠️"
            else:
                icon = "🔔"

            header = title if title.startswith(("⛔", "✅", "🟢", "🔑", "🔒", "🛑", "⏳", "⚠️", "🔔")) else f"{icon} **{title}**"
            text = f"**{header}**\n\n{body}"
            try:
                await bot.send_message(int(telegram_id), text)
                logger.info(f"Pushed notification to Telegram user {telegram_id}")
            except Exception as e:
                logger.error(f"Failed sending Telegram notification to {telegram_id}: {e}")

    # 2. PAYMENT PROCESSED (TOP-UP / REFUND APPROVED OR REJECTED WITH REASON)
    @broker.subscriber(EventKeys.PAYMENT_PROCESSED)
    async def on_payment_processed(msg: dict):
        user_id = str(msg.get("user_id") or "")
        status = str(msg.get("status", "Approved")).capitalize()
        tx_type = str(msg.get("tx_type", "Payment")).capitalize()
        amount = float(msg.get("amount", 0.0))
        ref_id = str(msg.get("reference_id") or "").strip()
        admin_notes = str(msg.get("admin_notes") or msg.get("notes") or "").strip()
        currency = str(msg.get("currency", "IRT") or "IRT").upper()

        if not user_id or status == "Pending":
            return

        telegram_id = await RedisFSM.get_telegram_id(user_id)
        if not telegram_id:
            return

        amt_str = f"${amount:.2f} USD" if currency == "USD" else f"{amount:,.0f} Toman"
        ref_suffix = f" (Ref: `{ref_id}`)" if ref_id else ""

        action = str(msg.get("action") or ("Credit" if amount >= 0 else "Debit")).capitalize()
        memo_block = f"\n\n• **Memo:** _{admin_notes}_" if admin_notes else ""

        if tx_type.lower() == "gift":
            if action == "Debit":
                title = "🎁 **Gift Credit Deducted**"
                lead = f"Administrator deducted **{amt_str}** gift credit from your wallet."
                text = f"{title}\n\n{lead}{memo_block}"
            else:
                title = "🎁 **Gift Credit Received**"
                lead = f"Administrator credited **{amt_str}** gift credit to your wallet."
                status_line = "\n\nYour VPN configurations are active."
                text = f"{title}\n\n{lead}{memo_block}{status_line}"

        elif tx_type.lower() == "adjustment":
            title = "💳 **Balance Adjustment**"
            if action == "Debit":
                lead = f"Administrator deducted **{amt_str}** from your wallet balance."
                text = f"{title}\n\n{lead}{memo_block}"
            else:
                lead = f"Administrator credited **{amt_str}** to your wallet balance."
                status_line = "\n\nYour VPN configurations are active."
                text = f"{title}\n\n{lead}{memo_block}{status_line}"

        elif status == "Rejected":
            if tx_type.lower() == "refund":
                title = "❌ **Refund Request Declined**"
                lead = f"Your refund request of **{amt_str}**{ref_suffix} was declined."
                footer = "The locked funds have been returned to your wallet balance."
            else:
                title = "❌ **Top-Up Rejected**"
                lead = f"Your top-up of **{amt_str}**{ref_suffix} was rejected."
                footer = "Please verify your transfer slip or submit a support ticket if you believe this was an error."

            reason_block = f"\n\n**Reason:**\n_{admin_notes}_" if admin_notes else ""
            text = f"{title}\n\n{lead}{reason_block}\n\n{footer}"
        else:
            # Approved status
            tracking_ref = str(msg.get("tracking_ref") or "").strip()
            if tx_type.lower() == "refund":
                title = "✅ **Refund Payout Completed**"
                track_line = f"\n\n• **Bank Reference:** `{tracking_ref}`" if tracking_ref else ""
                text = (
                    f"{title}\n\n"
                    f"Your refund request of **{amt_str}**{ref_suffix} has been approved and paid to your card."
                    f"{track_line}\n\n"
                    f"Please allow standard banking processing time."
                )
            else:
                title = "✅ **Top-Up Approved**"
                text = f"{title}\n\nYour top-up of **{amt_str}**{ref_suffix} has been approved.\nYour wallet balance and VPN credentials have been updated."

        try:
            await bot.send_message(int(telegram_id), text)
            logger.info(f"Sent payment outcome notice to Telegram user {telegram_id}: {status} {tx_type}")
        except Exception as e:
            logger.error(f"Failed sending payment notice to {telegram_id}: {e}")

    # 3. TICKET MESSAGE CREATED (SUPPORT STAFF REPLIES & ROUTE SHORTAGE ALERTS)
    @broker.subscriber(EventKeys.TICKET_MESSAGE_CREATED)
    async def on_ticket_message(msg: dict):
        recipient_id = str(msg.get("recipient_id") or "")
        preview = msg.get("preview_text", "New reply on your support ticket.")
        message_data = msg.get("message_data") or {}
        custom_title = message_data.get("title")

        if not recipient_id:
            return

        telegram_id = await RedisFSM.get_telegram_id(recipient_id)
        if telegram_id:
            if custom_title:
                header = custom_title if custom_title.startswith("📡") else f"📡 **{custom_title}**"
                text = (
                    f"{header}\n\n"
                    f"{preview}\n\n"
                    f"Use /support to track the ticket progress."
                )
            else:
                text = (
                    f"💬 **New Support Reply Received**\n\n"
                    f"{preview}\n\n"
                    f"Use /support or the buttons to view the conversation and reply."
                )
            try:
                await bot.send_message(int(telegram_id), text)
                logger.info(f"Pushed ticket message to Telegram user {telegram_id}")
            except Exception as e:
                logger.error(f"Failed sending ticket alert to {telegram_id}: {e}")

    # 4. WALLET EMPTY WARNING
    @broker.subscriber(EventKeys.WALLET_EMPTY)
    async def on_wallet_empty(msg: dict):
        user_id = str(msg.get("user_id") or "")
        telegram_id = await RedisFSM.get_telegram_id(user_id)
        if telegram_id:
            text = (
                f"🛑 **VPN Service Disconnected**\n\n"
                f"Your balance has reached 0 Toman or is below the safety threshold.\n"
                f"Tap **Deposit / Top-up** to restore your connection."
            )
            try:
                await bot.send_message(int(telegram_id), text)
            except Exception:
                pass

    # 5. SERVER REPLACED
    @broker.subscriber(EventKeys.SERVER_REPLACED)
    async def on_server_replaced(msg: dict):
        user_ids = msg.get("user_ids", [])
        old_srv = msg.get("old_server_name", "Blocked Node")
        replacements = ", ".join(msg.get("replacement_server_names", ["New Node"]))

        for uid in user_ids:
            telegram_id = await RedisFSM.get_telegram_id(str(uid))
            if telegram_id:
                text = (
                    f"⚠️ **VPN Server Node Replaced**\n\n"
                    f"Server `{old_srv}` was filtered and replaced with `{replacements}`.\n\n"
                    f"🔄 **Action Required:**\n"
                    f"• Open your VPN app and tap **Update Subscription** to fetch the new working node.\n"
                    f"• If you still experience issues, please replace the server via /configs or from your dashboard."
                )
                try:
                    await bot.send_message(int(telegram_id), text)
                except Exception:
                    pass

    # 6. CONFIG / SERVER CHANGED
    @broker.subscriber(EventKeys.SERVER_CHANGED)
    async def on_server_changed(msg: dict):
        raw_uids = msg.get("user_ids") or []
        if msg.get("user_id") and msg.get("user_id") not in raw_uids:
            raw_uids.append(msg.get("user_id"))

        srv = msg.get("server_name", "VPN Node")
        action = msg.get("action", "updated")
        loc = msg.get("location_name") or "VPN Node"
        custom_msg = msg.get("message")

        for uid in raw_uids:
            telegram_id = await RedisFSM.get_telegram_id(str(uid))
            if telegram_id:
                if action == "added":
                    title = f"🌐 **New VPN Configuration Added: {loc}**"
                    text = (
                        f"{title}\n\n"
                        f"A new VPN configuration for **{loc}** was added to your account by an administrator.\n\n"
                        f"🔄 **Action Required:**\n"
                        f"• **Update Subscription:** Tap **Update Subscription** in your VPN app (v2rayNG, Sing-box, Shadowrocket) to load it.\n"
                        f"• **Or View Config:** Use /configs in this bot or check the website to view and copy the connection URI."
                    )
                elif action in ("removed", "deleted"):
                    title = f"⚠️ **VPN Configuration Removed: {loc}**"
                    text = (
                        f"{title}\n\n"
                        f"Your configuration for **{loc}** was removed by an administrator.\n\n"
                        f"🔄 **Action Required:**\n"
                        f"• Please open your VPN app and tap **Update Subscription** to sync your active server list."
                    )
                else:
                    title = f"🔄 **VPN Configuration Updated: {loc}**"
                    desc = custom_msg if custom_msg else f"Your VPN configuration for **{loc}** ({srv}) has been {action}."
                    text = (
                        f"{title}\n\n"
                        f"{desc}\n\n"
                        f"🔄 **Action Required:**\n"
                        f"• Please open your VPN app and tap **Update Subscription** to sync the latest node settings.\n"
                        f"• If unreachable or filtered, you can replace the route via /configs or from your dashboard."
                    )

                try:
                    await bot.send_message(int(telegram_id), text)
                    logger.info(f"Pushed config change alert ({action}) to Telegram user {telegram_id}")
                except Exception as e:
                    logger.error(f"Failed sending config change alert to {telegram_id}: {e}")

    logger.info("Connecting Telegram Service to RabbitMQ event bus...")
    for attempt in range(1, 11):
        try:
            await broker.start()
            logger.info("Successfully started RabbitMQ broker listeners in Telegram Service.")
            break
        except Exception as e:
            logger.warning(f"RabbitMQ connection attempt {attempt}/10 failed: {e}. Retrying in 3s...")
            await asyncio.sleep(3)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        if hasattr(broker, "close"):
            await broker.close()