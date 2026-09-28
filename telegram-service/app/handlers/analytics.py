# telegram-service/app/handlers/analytics.py
import logging
from datetime import datetime
from telethon import events, Button
from app.services.redis_client import RedisFSM
from app.services.api_client import BackendAPIClient
from app.keyboards import (
    history_list_keyboard, 
    transaction_detail_keyboard, 
    analytics_view_keyboard, 
    main_menu_keyboard
)
from app.handlers.balance import format_localized_number, format_bytes_human

logger = logging.getLogger(__name__)


def generate_ascii_bar(value: float, max_value: float, max_blocks: int = 10) -> str:
    """Renders a clean progress bar for terminal/chat visualization."""
    if max_value <= 0 or value <= 0:
        return "░" * max_blocks
    filled = int(round((value / max_value) * max_blocks))
    filled = max(1, min(max_blocks, filled))
    return "█" * filled + "░" * (max_blocks - filled)


# =========================================================================
# 1. PAYMENT HISTORY BUILDERS
# =========================================================================
def build_topup_history_text(items: list, lang: str = "en") -> str:
    lang_clean = (lang or "en").strip().lower()
    total_str = format_localized_number(len(items), lang=lang)

    if not items:
        if lang_clean.startswith("fa"):
            return "ℹ️ هیچ تراکنش افزایش موجودی برای حساب شما یافت نشد."
        elif lang_clean.startswith("ru"):
            return "ℹ️ История пополнений пуста."
        else:
            return "ℹ️ No top-up transactions found."

    if lang_clean.startswith("fa"):
        return (
            f"📜 **تاریخچه تراکنش‌های افزایش موجودی ({total_str}):**\n\n"
            "برای مشاهده جزئیات، یادداشت و پیام مدیریت، روی دکمه هر تراکنش در زیر کلیک فرمایید:"
        )
    elif lang_clean.startswith("ru"):
        return (
            f"📜 **История пополнений баланса ({total_str}):**\n\n"
            "Нажмите на любую транзакцию ниже, чтобы просмотреть подробности и примечания:"
        )
    else:
        return (
            f"📜 **Recent Top-up Transactions ({total_str}):**\n\n"
            "Click on any transaction button below to inspect details, notes, and admin memos:"
        )


def build_refund_history_text(items: list, lang: str = "en") -> str:
    lang_clean = (lang or "en").strip().lower()
    total_str = format_localized_number(len(items), lang=lang)

    if not items:
        if lang_clean.startswith("fa"):
            return "ℹ️ هیچ درخواست استردادی برای حساب شما یافت نشد."
        elif lang_clean.startswith("ru"):
            return "ℹ️ История заявок на возврат пуста."
        else:
            return "ℹ️ No refund claims found."

    if lang_clean.startswith("fa"):
        return (
            f"💸 **تاریخچه درخواست‌های استرداد وجه ({total_str}):**\n\n"
            "برای مشاهده جزئیات شماره کارت، یادداشت و وضعیت تایید، روی هر تراکنش کلیک کنید:"
        )
    elif lang_clean.startswith("ru"):
        return (
            f"💸 **История заявок на возврат ({total_str}):**\n\n"
            "Нажмите на заявку ниже для просмотра реквизитов и комментариев администрации:"
        )
    else:
        return (
            f"💸 **Recent Refund Claims ({total_str}):**\n\n"
            "Click on any refund claim below to view card details, user notes, and admin proof:"
        )


def build_transaction_detail_message(tx: dict, tx_type: str = "topup", lang: str = "en") -> str:
    """Renders complete details for a single Top-up or Refund transaction."""
    lang_clean = (lang or "en").strip().lower()

    ref_id = tx.get("reference_id", "N/A")
    amount = float(tx.get("amount", 0.0))
    amount_str = format_localized_number(amount, lang=lang)

    status_raw = str(tx.get("status", "Pending")).capitalize()
    status_icon = "🟢" if status_raw in ["Approved", "Completed"] else "🟡" if status_raw == "Pending" else "🔴"

    method = tx.get("payment_method") or ("Bank Card" if tx_type == "topup" else "Shetab Card")
    user_note = tx.get("description") or "—"
    admin_note = tx.get("admin_notes") or "—"

    raw_created = tx.get("created_at", "")
    created_date = raw_created.replace("T", " ")[:19] if raw_created else "—"

    raw_processed = tx.get("processed_at")
    processed_date = raw_processed.replace("T", " ")[:19] if raw_processed else "—"

    if lang_clean.startswith("fa"):
        status_fa = "تایید شده" if status_raw in ["Approved", "Completed"] else "در انتظار بررسی" if status_raw == "Pending" else "رد شده"
        title = "💳 جزئیات تراکنش واریزی" if tx_type == "topup" else "💸 جزئیات درخواست استرداد"
        method_label = "روش پرداخت / درگاه" if tx_type == "topup" else "شماره کارت و صاحب حساب مقصد"
        return (
            f"{title}\n\n"
            f"• **شناسه پیگیری (Ref):** `{ref_id}`\n"
            f"• **مبلغ:** `{amount_str} تومان`\n"
            f"• **وضعیت:** {status_icon} `{status_fa}`\n"
            f"• **{method_label}:** `{method}`\n"
            f"• **توضیحات / یادداشت شما:** {user_note}\n"
            f"• **یادداشت / بازخورد مدیریت:** {admin_note}\n"
            f"• **تاریخ ثبت:** `{created_date}`\n"
            f"• **تاریخ پردازش / واریز:** `{processed_date}`"
        )
    elif lang_clean.startswith("ru"):
        status_ru = "Одобрено" if status_raw in ["Approved", "Completed"] else "На рассмотрении" if status_raw == "Pending" else "Отклонено"
        title = "💳 Подробности пополнения" if tx_type == "topup" else "💸 Подробности возврата"
        method_label = "Способ оплаты" if tx_type == "topup" else "Карта / Получатель"
        return (
            f"{title}\n\n"
            f"• **Номер транзакции:** `{ref_id}`\n"
            f"• **Сумма:** `{amount_str} Томан`\n"
            f"• **Статус:** {status_icon} `{status_ru}`\n"
            f"• **{method_label}:** `{method}`\n"
            f"• **Примечание клиента:** {user_note}\n"
            f"• **Комментарий администратора:** {admin_note}\n"
            f"• **Дата создания:** `{created_date}`\n"
            f"• **Дата обработки:** `{processed_date}`"
        )
    else:
        status_en = "Approved" if status_raw in ["Approved", "Completed"] else "Pending" if status_raw == "Pending" else "Rejected"
        title = "💳 Top-Up Transaction Detail" if tx_type == "topup" else "💸 Refund Claim Detail"
        method_label = "Payment Method" if tx_type == "topup" else "Destination Account"
        return (
            f"{title}\n\n"
            f"• **Reference ID:** `{ref_id}`\n"
            f"• **Amount:** `{amount_str} Toman`\n"
            f"• **Status:** {status_icon} `{status_en}`\n"
            f"• **{method_label}:** `{method}`\n"
            f"• **Your Note / Memo:** {user_note}\n"
            f"• **Admin Note / Feedback:** {admin_note}\n"
            f"• **Submission Date:** `{created_date}`\n"
            f"• **Processed Date:** `{processed_date}`"
        )


# =========================================================================
# 2. DIAGRAM / CHART BUILDERS (FIXED 30 DAYS, FULLY LOCALIZED)
# =========================================================================

def format_day_label(raw_label: str, raw_iso: str, lang: str = "en") -> str:
    """Formats date label into clean localized text."""
    lang_clean = (lang or "en").strip().lower()
    clean_date = raw_iso[:10] if raw_iso else raw_label

    if lang_clean.startswith("fa"):
        # Format as MM/DD in Persian digits
        parts = clean_date.split("-")
        if len(parts) == 3:
            return format_localized_number(f"{parts[1]}/{parts[2]}", lang="fa")
        return format_localized_number(clean_date, lang="fa")
    elif lang_clean.startswith("ru"):
        parts = clean_date.split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}"
        return clean_date
    else:
        return raw_label if raw_label else clean_date[5:]


def build_traffic_diagram_text(daily_data: list, provider_data: dict, lang: str = "en") -> str:
    lang_clean = (lang or "en").strip().lower()

    if not daily_data:
        if lang_clean.startswith("fa"):
            return "ℹ️ هیچ داده ترافیکی در ۳۰ روز گذشته ثبت نشده است."
        elif lang_clean.startswith("ru"):
            return "ℹ️ За последние 30 дней данных о трафике не обнаружено."
        else:
            return "ℹ️ No traffic consumption data recorded in the last 30 days."

    total_bytes = sum(d.get("bytes", 0) for d in daily_data)
    total_mb = total_bytes / (1024 ** 2)
    total_gb = total_bytes / (1024 ** 3)
    max_bytes = max((d.get("bytes", 0) for d in daily_data), default=1)

    total_usage_formatted = format_bytes_human(total_bytes, lang=lang)

    if lang_clean.startswith("fa"):
        header = (
            "📈 **نمودار مصرف ترافیک (۳۰ روز گذشته)**\n\n"
            f"📊 **مجموع مصرف دوره:** `{total_usage_formatted}`\n"
            "────────────────────────────\n"
        )
    elif lang_clean.startswith("ru"):
        header = (
            "📈 **График потребления трафика (за 30 дней)**\n\n"
            f"� **Всего израсходовано:** `{total_usage_formatted}`\n"
            "────────────────────────────\n"
        )
    else:
        header = (
            "�📈 **Traffic Consumption Diagram (Last 30 Days)**\n\n"
            f"📊 **Total Data Consumed:** `{total_usage_formatted}`\n"
            "────────────────────────────\n"
        )

    text = header
    display_records = list(reversed(daily_data))

    # Show up to 20 most recent days with non-zero or prominent traffic
    for r in display_records[:20]:
        b_count = r.get("bytes", 0)
        day_str = format_day_label(r.get("date", ""), r.get("date_iso", ""), lang=lang)
        bar = generate_ascii_bar(b_count, max_bytes, max_blocks=8)
        usage_str = format_bytes_human(b_count, lang=lang)
        text += f"`{day_str}` {bar} `{usage_str}`\n"

    if len(display_records) > 20:
        more_str = format_localized_number(len(display_records), lang=lang)
        if lang_clean.startswith("fa"):
            text += f"_(نمایش ۲۰ روز اخیر از مجموع {more_str} روز)_\n"
        elif lang_clean.startswith("ru"):
            text += f"_(Показано 20 последних дней из {more_str})_\n"
        else:
            text += f"_(Showing 20 most recent of {more_str} days)_\n"

    text += "────────────────────────────\n"

    # ISP Provider Breakdown
    providers = provider_data.get("providers", []) if provider_data else []
    if providers:
        if lang_clean.startswith("fa"):
            text += "📶 **تفکیک مصرف بر اساس اپراتور اینترنت (ISP):**\n"
        elif lang_clean.startswith("ru"):
            text += "📶 **Распределение трафика по провайдерам:**\n"
        else:
            text += "📶 **Carrier / ISP Distribution Breakdown:**\n"

        for p in providers:
            p_name = p.get("name", "Unknown ISP")
            p_share = p.get("percentage", 0.0)
            p_bytes = p.get("bytes", 0)
            p_usage_str = format_bytes_human(p_bytes, lang=lang)
            p_share_str = format_localized_number(p_share, lang=lang, decimals=1)
            bar = generate_ascii_bar(p_share, 100.0, max_blocks=6)

            text += f"• **{p_name}**: {bar} `{p_share_str}%` (`{p_usage_str}`)\n"

    return text


def build_financial_trends_text(daily_data: list, lang: str = "en") -> str:
    lang_clean = (lang or "en").strip().lower()

    if not daily_data:
        if lang_clean.startswith("fa"):
            return "ℹ️ هیچ واریزی یا هدیه‌ای در ۳۰ روز گذشته ثبت نشده است."
        elif lang_clean.startswith("ru"):
            return "ℹ️ За последние 30 дней финансовых движений не обнаружено."
        else:
            return "ℹ️ No financial movements recorded in the last 30 days."

    is_usd = any(d.get("currency") == "USD" or d.get("topup_toman", 0.0) < 50 for d in daily_data if d.get("topup_toman", 0.0) > 0)
    total_topups = sum(d.get("topup_toman", 0.0) for d in daily_data)
    total_gifts = sum(d.get("gift_toman", 0.0) for d in daily_data)
    max_amount = max((d.get("topup_toman", 0.0) + d.get("gift_toman", 0.0) for d in daily_data), default=1.0)

    topups_str = f"${total_topups:.2f}" if is_usd else format_localized_number(total_topups, lang=lang)
    gifts_str = f"${total_gifts:.2f}" if is_usd else format_localized_number(total_gifts, lang=lang)

    if lang_clean.startswith("fa"):
        header = (
            "💰 **نمودار واریزی‌ها و هدایا (۳۰ روز گذشته)**\n\n"
            f"💳 **مجموع افزایش موجودی:** `{topups_str} تومان`\n"
            f"🎁 **مجموع اعتبارات هدیه:** `{gifts_str} تومان`\n"
            "────────────────────────────\n"
        )
    elif lang_clean.startswith("ru"):
        header = (
            "💰 **Тренды пополнений и бонусов (за 30 дней)**\n\n"
            f"� **Всего пополнений:** `{topups_str} Томан`\n"
            f"🎁 **Всего подарочных баллов:** `{gifts_str} Томан`\n"
            "────────────────────────────\n"
        )
    else:
        header = (
            "�💰 **Top-Up & Deposit Trends (Last 30 Days)**\n\n"
            f"💳 **Total Top-ups Paid:** `{topups_str} Toman`\n"
            f"🎁 **Total Gift Grants:** `{gifts_str} Toman`\n"
            "────────────────────────────\n"
        )

    text = header
    display_records = list(reversed(daily_data))

    for r in display_records[:20]:
        topup_t = r.get("topup_toman", 0.0)
        gift_t = r.get("gift_toman", 0.0)
        sum_t = topup_t + gift_t
        day_str = format_day_label(r.get("day", ""), r.get("date_iso", ""), lang=lang)

        bar = generate_ascii_bar(sum_t, max_amount, max_blocks=8)

        detail_parts = []
        if topup_t > 0:
            topup_val_str = format_localized_number(topup_t, lang=lang)
            detail_parts.append(f"+{topup_val_str}")
        if gift_t > 0:
            gift_val_str = format_localized_number(gift_t, lang=lang)
            detail_parts.append(f"🎁 {gift_val_str}")
        if not detail_parts:
            detail_parts.append(format_localized_number(0, lang=lang))

        text += f"`{day_str}` {bar} `{' | '.join(detail_parts)}`\n"

    if len(display_records) > 20:
        more_str = format_localized_number(len(display_records), lang=lang)
        if lang_clean.startswith("fa"):
            text += f"_(نمایش ۲۰ روز اخیر از مجموع {more_str} روز)_\n"
        elif lang_clean.startswith("ru"):
            text += f"_(Показано 20 последних дней из {more_str})_\n"
        else:
            text += f"_(Showing 20 most recent of {more_str} days)_\n"

    return text


# =========================================================================
# 3. HANDLER REGISTRATION
# =========================================================================
async def register_analytics_handlers(bot):

    # --- A. PAYMENT & REFUND HISTORY COMMAND ---
    HISTORY_TRIGGERS = [
        "📜 Payment History", "/history",
        "📜 تاریخچه تراکنش‌ها",
        "📜 История платежей"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in HISTORY_TRIGGERS))
    async def on_history_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        topups = await api.get_topup_history(limit=10)

        from app.keyboards import history_list_keyboard
        msg_text = build_topup_history_text(topups or [], lang=user_lang)
        buttons = history_list_keyboard(topups or [], current_tab="topups", lang=user_lang)

        await event.reply(msg_text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"^hist:(topups|refunds)$"))
    async def on_cb_history_toggle(event):
        telegram_id = str(event.sender_id)
        tab = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        from app.keyboards import history_list_keyboard

        if tab == "topups":
            topups = await api.get_topup_history(limit=10)
            msg_text = build_topup_history_text(topups or [], lang=user_lang)
            buttons = history_list_keyboard(topups or [], current_tab="topups", lang=user_lang)
        else:
            refunds = await api.get_refund_history(limit=10)
            msg_text = build_refund_history_text(refunds or [], lang=user_lang)
            buttons = history_list_keyboard(refunds or [], current_tab="refunds", lang=user_lang)

        await event.edit(msg_text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"^hist:noop$"))
    async def on_cb_history_noop(event):
        await event.answer()

    # --- CLICK TO VIEW TRANSACTION DETAILS ---
    @bot.on(events.CallbackQuery(pattern=r"^tx:view:(topups|refunds):([A-Za-z0-9_\-]+)$"))
    async def on_cb_view_transaction_detail(event):
        telegram_id = str(event.sender_id)
        tab_type = event.pattern_match.group(1)
        target_ref = event.pattern_match.group(2)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        from app.keyboards import transaction_detail_keyboard

        # Find matching transaction from history
        if tab_type == "topups":
            items = await api.get_topup_history(limit=30)
            tx = next((item for item in (items or []) if item.get("reference_id") == target_ref), None)
        else:
            items = await api.get_refund_history(limit=30)
            tx = next((item for item in (items or []) if item.get("reference_id") == target_ref), None)

        if not tx:
            not_found = (
                "⚠️ اطلاعات این تراکنش یافت نشد." if user_lang.startswith("fa") else
                "⚠️ Транзакция не найдена." if user_lang.startswith("ru") else
                "⚠️ Transaction details not found."
            )
            await event.answer(not_found, alert=True)
            return

        detail_msg = build_transaction_detail_message(tx, tx_type=tab_type, lang=user_lang)
        await event.edit(detail_msg, buttons=transaction_detail_keyboard(origin_tab=tab_type, lang=user_lang))

    # --- B. TRAFFIC CONSUMPTION DIAGRAM COMMAND (FIXED 30 DAYS) ---
    TRAFFIC_TRIGGERS = [
        "📈 Traffic Analytics", "/traffic",
        "📈 آمار مصرف ترافیک",
        "📈 Аналитика трафика"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in TRAFFIC_TRIGGERS))
    async def on_traffic_analytics_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        from app.keyboards import analytics_view_keyboard

        daily_traffic = await api.get_client_daily_traffic(days=30)
        provider_stats = await api.get_client_provider_stats()

        msg_text = build_traffic_diagram_text(daily_traffic or [], provider_stats or {}, lang=user_lang)
        await event.reply(msg_text, buttons=analytics_view_keyboard(active_view="traffic", lang=user_lang))

    # --- C. FINANCIAL TRENDS / TOPUP DIAGRAM COMMAND (FIXED 30 DAYS) ---
    FINANCIAL_TRIGGERS = [
        "📊 Financial Trends", "/financials", "/trends",
        "💰 واریزی‌ها (۳۰ روز)",
        "💰 Пополнения (30 дней)"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in FINANCIAL_TRIGGERS))
    async def on_financial_trends_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        from app.keyboards import analytics_view_keyboard

        trends = await api.get_client_financial_trends(days=30)
        msg_text = build_financial_trends_text(trends or [], lang=user_lang)

        await event.reply(msg_text, buttons=analytics_view_keyboard(active_view="financials", lang=user_lang))

    # --- D. SWITCH VIEW (TRAFFIC <-> FINANCIALS, ALWAYS 30 DAYS) ---
    @bot.on(events.CallbackQuery(pattern=r"^analytics:view:(traffic|financials)$"))
    async def on_cb_analytics_switch(event):
        telegram_id = str(event.sender_id)
        active_view = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        from app.keyboards import analytics_view_keyboard

        toast = (
            "در حال بارگیری اطلاعات..." if user_lang.startswith("fa") else
            "Загрузка данных..." if user_lang.startswith("ru") else
            "Updating analytics..."
        )
        await event.answer(toast, alert=False)

        if active_view == "traffic":
            daily_traffic = await api.get_client_daily_traffic(days=30)
            provider_stats = await api.get_client_provider_stats()
            msg_text = build_traffic_diagram_text(daily_traffic or [], provider_stats or {}, lang=user_lang)
        else:
            trends = await api.get_client_financial_trends(days=30)
            msg_text = build_financial_trends_text(trends or [], lang=user_lang)

        await event.edit(msg_text, buttons=analytics_view_keyboard(active_view=active_view, lang=user_lang))