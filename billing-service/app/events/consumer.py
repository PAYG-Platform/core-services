# backend/billing-service/app/events/consumer.py
import uuid
import json
import logging
from datetime import datetime, timezone
from faststream.rabbit import RabbitRouter, RabbitExchange, ExchangeType
from sqlalchemy import func
from app.database import SessionLocal
from app.models import (
    Account,
    WalletBucket,
    Transaction,
    SystemFinancialSettings,
    LocationCapacity,
)
from app.events.publisher import publish_wallet_empty, publish_wallet_low, publish_wallet_funded
from vpn_shared.events import EventKeys
from vpn_shared.schemas import RoleEnum

logger = logging.getLogger(__name__)
billing_router = RabbitRouter()

# Safe fallback definition for the Fanout Exchange
TRAFFIC_EXCHANGE = getattr(
    EventKeys,
    "TRAFFIC_CONSUMED_EXCHANGE",
    RabbitExchange("traffic_consumed_fanout", type=ExchangeType.FANOUT)
)


@billing_router.subscriber("billing_user_created_queue", exchange=EventKeys.USER_CREATED_EXCHANGE)
async def handle_user_created(msg: dict):
    """
    Triggered when IAM service registers a new user.
    1. Creates local financial Account with replicated username.
    2. Reads dynamic default_signup_gift_credit from database and initializes GIFT WalletBucket.
    3. Records initial Gift Transaction ledger entry.
    4. Emits WALLET_FUNDED to trigger immediate node-service configuration provisioning.
    """
    logger.info(f"[Billing] Processing USER_CREATED event: {msg}")
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    user_id = uuid.UUID(str(raw_user_id))
    username = msg.get("username", "")

    role_raw = msg.get("role", "Client")
    if isinstance(role_raw, RoleEnum):
        role_str = role_raw.value
    elif hasattr(role_raw, "value"):
        role_str = role_raw.value
    else:
        role_str = str(role_raw)

    db = SessionLocal()
    try:
        existing_account = db.query(Account).filter(Account.user_id == user_id).first()
        if existing_account:
            if not existing_account.username and username:
                existing_account.username = username
                db.commit()
            return

        # 1. Create Local Financial Account with immutable billing_currency
        billing_currency = str(msg.get("billing_currency", "IRT") or "IRT").upper().strip()
        new_account = Account(
            user_id=user_id,
            username=username,
            role=role_str,
            billing_currency=billing_currency
        )
        db.add(new_account)
        db.flush()

        # 2. Read dynamic signup gift credit corresponding to user's currency
        settings = db.query(SystemFinancialSettings).first()
        if billing_currency == "USD":
            configured_gift = (
                float(settings.default_signup_gift_credit_usd)
                if (settings and hasattr(settings, "default_signup_gift_credit_usd") and settings.default_signup_gift_credit_usd is not None)
                else 0.50
            )
        else:
            configured_gift = (
                float(settings.default_signup_gift_credit)
                if (settings and settings.default_signup_gift_credit is not None)
                else 20000.0
            )

        raw_gift = msg.get("gift_balance")
        if raw_gift is not None and float(raw_gift) > 0:
            gift_balance = float(raw_gift)
        else:
            gift_balance = configured_gift

        # 3. Initial GIFT Wallet Bucket (Non-refundable, system-issued in native currency)
        if gift_balance > 0:
            gift_bucket = WalletBucket(
                user_id=user_id,
                bucket_type="GIFT",
                location_name="Gift Credit (All Regions)",
                balance=gift_balance,
                currency=billing_currency,
                is_active=True,
            )
            db.add(gift_bucket)
            db.flush()

            # Record Initial Gift Ledger Transaction
            unit_label = "$" if billing_currency == "USD" else "Toman"
            db.add(
                Transaction(
                    id=uuid.uuid4(),
                    reference_id=f"GIFT-{uuid.uuid4().hex[:12].upper()}",
                    user_id=user_id,
                    wallet_bucket_id=gift_bucket.id,
                    tx_type="Gift",
                    amount=gift_balance,
                    currency=billing_currency,
                    status="Completed",
                    payment_method="System Gift",
                    description=f"Initial System Gift Credit: {gift_balance:.2f} {unit_label}",
                    processed_at=datetime.now(timezone.utc),
                )
            )

        db.commit()

        # Immediately publish WALLET_FUNDED if initial credit exceeds safety cutoff threshold
        if billing_currency == "USD":
            safety_cutoff = float(getattr(settings, "safety_cutoff_threshold_usd", 0.01) or 0.01) if settings else 0.01
        else:
            safety_cutoff = float(getattr(settings, "safety_cutoff_threshold", 50.0) or 50.0) if settings else 50.0

        if gift_balance > safety_cutoff:
            await publish_wallet_funded(str(user_id))

        logger.info(f"[Billing] Initialized billing account ({billing_currency}) with {gift_balance:.2f} gift credit for User {user_id} (@{username}).")

    except Exception as e:
        db.rollback()
        logger.error(f"[Billing] Error handling USER_CREATED event for user {user_id}: {e}")
    finally:
        db.close()


# SEPARATE SUBSCRIBERS TO PREVENT FASTSTREAM DECORATOR WRAPPER CONFLICTS
@billing_router.subscriber("billing_traffic_consumed_queue", exchange=TRAFFIC_EXCHANGE)
async def handle_traffic_consumed_exchange(msg: dict):
    """Listens for traffic events broadcasted via the Fanout Exchange."""
    await process_traffic_consumed(msg)


@billing_router.subscriber(EventKeys.TRAFFIC_CONSUMED)
async def handle_traffic_consumed_queue(msg: dict):
    """Listens for traffic events sent directly to the direct queue."""
    await process_traffic_consumed(msg)


async def process_traffic_consumed(msg: dict):
    """
    Core PAYG Waterfall Billing Engine:
    1. Normalizes message payload and extracts consumed bandwidth.
    2. Calculates usage cost in Toman using dynamic server price_per_gb.
    3. Drains WalletBuckets in strict Waterfall order (GIFT -> FIAT -> CRYPTO).
    4. Gift credit overshoots NEVER create or increase negative debt.
    5. Paid overshoots are hard-capped in the database at -max_negative_balance.
    6. Disconnects user when usable credit (Gift + max(0, Paid)) <= safety_cutoff_threshold.
    """
    if isinstance(msg, str):
        try:
            msg = json.loads(msg)
        except Exception:
            pass
    elif hasattr(msg, "decoded_body") and isinstance(msg.decoded_body, dict):
        msg = msg.decoded_body

    logger.info(f"[Billing] Processing TRAFFIC_CONSUMED payload: {msg}")

    raw_user_id = msg.get("user_id") or msg.get("userId") or msg.get("client_id")
    used_bytes = int(msg.get("used_bytes") or msg.get("bytes") or msg.get("usedBytes") or 0)

    if not raw_user_id or used_bytes <= 0:
        logger.warning(f"[Billing] Skipping: invalid user_id ({raw_user_id}) or used_bytes ({used_bytes})")
        return

    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (ValueError, TypeError) as e:
        logger.error(f"[Billing] Invalid user_id UUID ({raw_user_id}): {e}")
        return

    db = SessionLocal()
    try:
        # 1. Ensure financial account exists in billing_db
        acc = db.query(Account).filter(Account.user_id == user_id).first()
        if not acc:
            acc = Account(user_id=user_id, role="Client", username=f"usr_{str(user_id)[:8]}", billing_currency="IRT")
            db.add(acc)
            db.commit()

        user_currency = str(getattr(acc, "billing_currency", "IRT") or "IRT").upper().strip()

        # 2. Determine price and calculate usage cost in user's native currency
        used_gb = used_bytes / (1024 ** 3)
        if user_currency == "USD":
            price_per_gb_active = float(msg.get("price_per_gb_usd") or 0.05)
            if price_per_gb_active <= 0:
                price_per_gb_active = 0.05
            total_cost = round(used_gb * price_per_gb_active, 4)
            if total_cost <= 0:
                total_cost = 0.0001
            currency_unit = "USD"
        else:
            price_per_gb_active = float(msg.get("price_per_gb") or msg.get("pricePerGb") or 1000.0)
            if price_per_gb_active <= 0:
                price_per_gb_active = 1000.0
            total_cost = round(used_gb * price_per_gb_active, 2)
            if total_cost <= 0:
                total_cost = 0.01
            currency_unit = "Toman"

        remaining_cost = total_cost

        # 3. Query all existing buckets for this user
        all_buckets = db.query(WalletBucket).filter(
            WalletBucket.user_id == user_id
        ).all()

        if not all_buckets:
            default_bucket = WalletBucket(
                id=uuid.uuid4(),
                user_id=user_id,
                bucket_type="FIAT" if user_currency == "IRT" else "CRYPTO",
                location_name="Primary Region",
                balance=0.0,
                currency=user_currency,
                is_active=True
            )
            db.add(default_bucket)
            db.commit()
            all_buckets = [default_bucket]

        # Track positive paid credit before deduction to determine if overage debt is allowed
        positive_paid_before = sum(
            float(b.balance or 0.0) for b in all_buckets if b.bucket_type != "GIFT" and float(b.balance or 0.0) > 0
        )

        # 4. Sort positive buckets by Waterfall: GIFT (0) -> FIAT (1) -> CRYPTO (2)
        positive_buckets = [b for b in all_buckets if float(b.balance or 0.0) > 0]

        def waterfall_sort_key(b: WalletBucket):
            type_order = 0 if b.bucket_type == "GIFT" else 1 if b.bucket_type == "FIAT" else 2
            created_dt = b.created_at or datetime.min.replace(tzinfo=timezone.utc)
            return (type_order, created_dt)

        sorted_buckets = sorted(positive_buckets, key=waterfall_sort_key)

        # 5. Deduct across positive buckets
        for bucket in sorted_buckets:
            if remaining_cost <= 0:
                break
            curr_bal = float(bucket.balance or 0.0)
            if curr_bal <= 0:
                continue

            deduct_amount = min(curr_bal, remaining_cost)
            bucket.balance = round(curr_bal - deduct_amount, 4 if user_currency == "USD" else 2)
            remaining_cost = round(remaining_cost - deduct_amount, 4 if user_currency == "USD" else 2)

            if bucket.balance <= 0 and not bucket.empty_since:
                bucket.empty_since = datetime.now(timezone.utc)

            # Record Ledger Transaction for positive bucket deduction
            try:
                tx = Transaction(
                    id=uuid.uuid4(),
                    reference_id=f"PAYG-{uuid.uuid4().hex[:12].upper()}",
                    user_id=user_id,
                    wallet_bucket_id=bucket.id,
                    tx_type="Deduction",
                    amount=deduct_amount,
                    currency=user_currency,
                    status="Completed",
                    description=f"PAYG Usage ({bucket.bucket_type}): {used_gb:.4f} GB @ {price_per_gb_active:.2f} {currency_unit}/GB.",
                    processed_at=datetime.now(timezone.utc),
                )
                if hasattr(tx, "reservation_status"):
                    tx.reservation_status = "NotRequired"
                db.add(tx)
            except Exception as tx_err:
                logger.warning(f"[Billing] Could not record deduction transaction: {tx_err}")

        # 6. Handle overage: ONLY generate debt if user had positive PAID balance before deduction
        if remaining_cost > 0:
            if positive_paid_before > 0:
                fin_settings = db.query(SystemFinancialSettings).first()
                if user_currency == "USD":
                    max_neg_limit = getattr(fin_settings, "max_negative_balance_usd", None) if fin_settings else None
                else:
                    max_neg_limit = fin_settings.max_negative_balance if (fin_settings and fin_settings.max_negative_balance is not None) else None

                target_bucket = next((b for b in all_buckets if b.bucket_type != "GIFT"), all_buckets[0])
                curr_bal = float(target_bucket.balance or 0.0)

                # Hard cap floor in the DB: balance cannot drop below -max_negative_balance
                if max_neg_limit is not None and max_neg_limit >= 0:
                    debt_floor = -float(max_neg_limit)
                    new_bal = max(debt_floor, curr_bal - remaining_cost)
                    actual_debt_amount = round(curr_bal - new_bal, 4 if user_currency == "USD" else 2)
                else:
                    new_bal = curr_bal - remaining_cost
                    actual_debt_amount = round(remaining_cost, 4 if user_currency == "USD" else 2)

                target_bucket.balance = round(new_bal, 4 if user_currency == "USD" else 2)
                if not target_bucket.empty_since:
                    target_bucket.empty_since = datetime.now(timezone.utc)

                if actual_debt_amount > 0:
                    try:
                        tx = Transaction(
                            id=uuid.uuid4(),
                            reference_id=f"DEBT-{uuid.uuid4().hex[:12].upper()}",
                            user_id=user_id,
                            wallet_bucket_id=target_bucket.id,
                            tx_type="Deduction",
                            amount=actual_debt_amount,
                            currency=user_currency,
                            status="Completed",
                            description=f"PAYG Overage Debt: {used_gb:.4f} GB (Floor: {target_bucket.balance:.2f} {currency_unit}).",
                            processed_at=datetime.now(timezone.utc),
                        )
                        if hasattr(tx, "reservation_status"):
                            tx.reservation_status = "NotRequired"
                        db.add(tx)
                    except Exception as tx_err:
                        logger.warning(f"[Billing] Could not record debt transaction: {tx_err}")
            else:
                logger.info(
                    f"[Billing] User {user_id} ({user_currency}) was operating on gift credit with 0 positive paid balance. "
                    f"Gift overshoot of {remaining_cost:.4f} {currency_unit} absorbed without creating debt."
                )

        db.commit()

        # 7. Check Usable Balance for Cutoff (Gift + max(0, Paid)) in native currency
        fin_settings = db.query(SystemFinancialSettings).first()
        if user_currency == "USD":
            safety_cutoff = float(getattr(fin_settings, "safety_cutoff_threshold_usd", 0.01) or 0.01) if fin_settings else 0.01
        else:
            safety_cutoff = float(getattr(fin_settings, "safety_cutoff_threshold", 50.0) or 50.0) if fin_settings else 50.0

        gift_after = sum(
            float(b.balance or 0.0) for b in all_buckets if b.bucket_type == "GIFT" and float(b.balance or 0.0) > 0
        )
        paid_after = sum(
            float(b.balance or 0.0) for b in all_buckets if b.bucket_type != "GIFT"
        )
        usable_after = gift_after + max(0.0, paid_after)

        logger.info(
            f"[Billing] DEDUCTION FINISHED: user={user_id} ({user_currency}), cost={total_cost:.4f} {currency_unit}, "
            f"paid_balance={paid_after:.2f}, gift_balance={gift_after:.2f}, "
            f"usable_after={usable_after:.2f} (Safety Cutoff: {safety_cutoff:.2f})"
        )

        # Disconnect client when usable balance drops to or below safety cutoff
        if usable_after <= safety_cutoff:
            logger.info(f"[Billing] User {user_id} usable balance ({usable_after:.2f}) <= safety cutoff ({safety_cutoff:.2f}). Emitting WALLET_EMPTY.")
            grace_days_val = int(fin_settings.region_grace_period_days) if (fin_settings and fin_settings.region_grace_period_days is not None) else 3
            await publish_wallet_empty(
                user_id=str(user_id),
                balance=usable_after,
                currency=user_currency,
                grace_days=grace_days_val,
            )
        else:
            # Broadcast updated usable balance so Notification Service evaluates against the user's custom threshold
            await publish_wallet_low(
                user_id=str(user_id), 
                balance=usable_after, 
                threshold=0.0, 
                currency=user_currency
            )

    except Exception as e:
        db.rollback()
        logger.error(f"[Billing] Error processing traffic deduction for user {user_id}: {e}")
    finally:
        db.close()


@billing_router.subscriber(EventKeys.CONFIG_RESERVATION_RESERVED)
async def handle_reservation_reserved(msg: dict):
    raw_tx_id = msg.get("topup_transaction_id")
    raw_res_id = msg.get("reservation_id")
    if not raw_tx_id:
        return

    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == uuid.UUID(str(raw_tx_id))).first()
        if tx:
            tx.reservation_id = uuid.UUID(str(raw_res_id)) if raw_res_id else None
            if hasattr(tx, "reservation_status"):
                tx.reservation_status = "Reserved"
            db.commit()
            logger.info(f"Topup {tx.reference_id} marked as Reserved (Reservation: {raw_res_id}).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating reservation status to Reserved: {e}")
    finally:
        db.close()


@billing_router.subscriber(EventKeys.CONFIG_RESERVATION_NOT_REQUIRED)
async def handle_reservation_not_required(msg: dict):
    raw_tx_id = msg.get("topup_transaction_id")
    if not raw_tx_id:
        return

    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == uuid.UUID(str(raw_tx_id))).first()
        if tx:
            if hasattr(tx, "reservation_status"):
                tx.reservation_status = "NotRequired"
            db.commit()
            logger.info(f"Topup {tx.reference_id} marked as NotRequired.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating reservation status to NotRequired: {e}")
    finally:
        db.close()


@billing_router.subscriber(EventKeys.CONFIG_RESERVATION_REJECTED)
async def handle_reservation_rejected(msg: dict):
    raw_tx_id = msg.get("topup_transaction_id")
    if not raw_tx_id:
        return

    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == uuid.UUID(str(raw_tx_id))).first()
        if tx and tx.status == "Pending":
            if hasattr(tx, "reservation_status"):
                tx.reservation_status = "Rejected"
            tx.status = "Rejected"
            tx.admin_notes = msg.get("reason", "No configuration capacity available in the requested region.")
            tx.processed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(f"Topup {tx.reference_id} rejected due to lack of server capacity.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating reservation status to Rejected: {e}")
    finally:
        db.close()


@billing_router.subscriber(EventKeys.CONFIG_RESERVATION_CONSUMED)
async def handle_reservation_consumed(msg: dict):
    raw_tx_id = msg.get("topup_transaction_id")
    if not raw_tx_id:
        return

    db = SessionLocal()
    try:
        tx = db.query(Transaction).filter(Transaction.id == uuid.UUID(str(raw_tx_id))).first()
        if tx:
            if hasattr(tx, "reservation_status"):
                tx.reservation_status = "Consumed"
            db.commit()
            logger.info(f"Topup {tx.reference_id} reservation consumed successfully.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating reservation status to Consumed: {e}")
    finally:
        db.close()


@billing_router.subscriber(EventKeys.LOCATION_CAPACITY_CHANGED)
async def handle_location_capacity_changed(msg: dict):
    raw_loc_id = msg.get("location_id")
    if not raw_loc_id:
        return

    loc_id = uuid.UUID(str(raw_loc_id))
    db = SessionLocal()
    try:
        loc = db.query(LocationCapacity).filter(LocationCapacity.location_id == loc_id).first()
        if not loc:
            loc = LocationCapacity(
                location_id=loc_id,
                location_name=msg.get("location_name"),
                total_capacity=int(msg.get("total_capacity", 0)),
                used_capacity=int(msg.get("used_capacity", 0)),
                is_full=bool(msg.get("is_full", False)),
            )
            db.add(loc)
        else:
            loc.location_name = msg.get("location_name", loc.location_name)
            loc.total_capacity = int(msg.get("total_capacity", 0))
            loc.used_capacity = int(msg.get("used_capacity", 0))
            loc.is_full = bool(msg.get("is_full", False))

        db.commit()
        logger.info(f"Replicated capacity for location {loc_id} (is_full: {loc.is_full}).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error replicating location capacity: {e}")
    finally:
        db.close()


def calculate_user_credit_state(user_id: uuid.UUID, db) -> dict:
    """Calculates separated financial balances (gift, paid, and usable credit)."""
    gift_bal = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type == "GIFT",
        WalletBucket.is_active == True
    ).scalar() or 0.0

    paid_bal = db.query(func.sum(WalletBucket.balance)).filter(
        WalletBucket.user_id == user_id,
        WalletBucket.bucket_type != "GIFT",
        WalletBucket.is_active == True
    ).scalar() or 0.0

    return {
        "gift_balance": max(0.0, float(gift_bal)),
        "paid_balance": float(paid_bal),
        "usable_credit": max(0.0, float(gift_bal)) + max(0.0, float(paid_bal))
    }


@billing_router.subscriber(EventKeys.USER_ACTIVATED)
async def handle_user_activated_in_billing(msg: dict):
    """
    Triggered when Admin activates a user in IAM.
    Checks if the user has gift or credit > safety_cutoff and emits WALLET_FUNDED to provision configs.
    """
    raw_user_id = msg.get("user_id")
    if not raw_user_id:
        return

    db = SessionLocal()
    try:
        user_id = uuid.UUID(str(raw_user_id))
        credit_state = calculate_user_credit_state(user_id, db)
        usable_credit = credit_state["usable_credit"]

        settings = db.query(SystemFinancialSettings).first() or SystemFinancialSettings()
        safety_cutoff = float(settings.safety_cutoff_threshold or 0.0)

        if usable_credit > safety_cutoff:
            logger.info(f"[Billing] User {user_id} activated with usable credit ({usable_credit} T > {safety_cutoff} T). Emitting WALLET_FUNDED.")
            await publish_wallet_funded(str(user_id))
        else:
            logger.info(f"[Billing] User {user_id} activated without credit ({usable_credit} T <= {safety_cutoff} T). Emitting WALLET_EMPTY.")
            await publish_wallet_empty(str(user_id))
    except Exception as e:
        logger.error(f"[Billing] Error evaluating balance for activated user {raw_user_id}: {e}")
    finally:
        db.close()