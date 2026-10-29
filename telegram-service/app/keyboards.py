# telegram-service/app/keyboards.py
from typing import List
from telethon import Button


# =========================================================================
# 1. PERSISTENT MAIN MENU KEYBOARDS
# =========================================================================

def main_menu_keyboard(lang: str = "en") -> List[List[Button]]:
    """
    Main persistent reply keyboard customized according to the user's language in IAM.
    Supports English ('en'), Persian ('fa'), and Russian ('ru').
    """
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        return [
            [Button.text("📊 وضعیت و موجودی"), Button.text("🌐 کانفیگ‌های من")],
            [Button.text("💳 افزایش موجودی"), Button.text("💸 درخواست استرداد")],
            [Button.text("📈 آمار مصرف ترافیک"), Button.text("📜 تاریخچه تراکنش‌ها")],
            [Button.text("🎫 تیکت‌های پشتیبانی"), Button.text("🌐 تغییر زبان")],
            [Button.text("❓ راهنما و اتصال حساب")]
        ]
    elif lang_clean.startswith("ru"):
        return [
            [Button.text("📊 Статус и Баланс"), Button.text("🌐 Мои Конфигурации")],
            [Button.text("💳 Пополнение счета"), Button.text("💸 Запрос на возврат")],
            [Button.text("📈 Аналитика трафика"), Button.text("📜 История платежей")],
            [Button.text("🎫 Служба поддержки"), Button.text("🌐 Сменить язык")],
            [Button.text("❓ Помощь и Привязка")]
        ]
    else:
        # Default: English
        return [
            [Button.text("📊 Status & Balance"), Button.text("🌐 My Configs")],
            [Button.text("💳 Deposit / Top-up"), Button.text("💸 Request Refund")],
            [Button.text("📈 Traffic Analytics"), Button.text("📜 Payment History")],
            [Button.text("🎫 Support Tickets"), Button.text("🌐 Change Language")],
            [Button.text("❓ Help & Link Account")]
        ]


def language_selection_keyboard() -> List[List[Button]]:
    """Inline buttons to choose the preferred system language."""
    return [
        [Button.inline("🇬🇧 English", data="lang:set:en")],
        [Button.inline("🇮🇷 فارسی (Persian)", data="lang:set:fa")],
        [Button.inline("🇷🇺 Русский (Russian)", data="lang:set:ru")],
        [Button.inline("❌ Cancel / انصراف / Отмена", data="fsm:cancel")]
    ]


def unlinked_welcome_keyboard() -> List[List[Button]]:
    """Buttons shown to an unlinked visitor."""
    return help_language_keyboard()


# =========================================================================
# 2. STATUS & DASHBOARD INLINE BUTTONS
# =========================================================================

def status_dashboard_keyboard(lang: str = "en") -> List[List[Button]]:
    """Inline actions displayed directly beneath the status & balance summary in the user's language."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        return [
            [
                Button.inline("🌐 مشاهده تمام کانفیگ‌ها", data="cfg:list"),
                Button.inline("💳 افزایش موجودی", data="topup:start")
            ],
            [
                Button.inline("📋 کپی لینک اشتراک هوشمند", data="cfg:sub_link")
            ]
        ]
    elif lang_clean.startswith("ru"):
        return [
            [
                Button.inline("🌐 Все Конфигурации", data="cfg:list"),
                Button.inline("💳 Пополнить баланс", data="topup:start")
            ],
            [
                Button.inline("📋 Скопировать ссылку подписки", data="cfg:sub_link")
            ]
        ]
    else:
        return [
            [
                Button.inline("🌐 View All Configs", data="cfg:list"),
                Button.inline("💳 Top-up Balance", data="topup:start")
            ],
            [
                Button.inline("📋 Copy Subscription Link", data="cfg:sub_link")
            ]
        ]


# =========================================================================
# 3. PAYMENT & REFUND HISTORY KEYBOARDS
# =========================================================================

def history_list_keyboard(items: list, current_tab: str = "topups", lang: str = "en") -> List[List[Button]]:
    """
    Generates an interactive keyboard listing each transaction as a clickable button,
    followed by tab switch controls (Top-ups vs Refunds) and Close.
    """
    lang_clean = (lang or "en").strip().lower()
    buttons = []

    # 1. Clickable button for each transaction item
    for tx in items[:10]:
        ref_id = tx.get("reference_id", "N/A")
        amount = float(tx.get("amount", 0.0))
        status_val = str(tx.get("status", "Pending")).capitalize()
        status_icon = "🟢" if status_val in ["Approved", "Completed"] else "🟡" if status_val == "Pending" else "🔴"
        
        btn_label = f"{status_icon} {amount:,.0f} T | {ref_id}"
        buttons.append([Button.inline(btn_label, data=f"tx:view:{current_tab}:{ref_id}")])

    # 2. Tab toggle controls
    if lang_clean.startswith("fa"):
        topups_active = "• واریزی‌ها (فعال) •"
        view_refunds = "💸 مشاهده استردادها"
        refunds_active = "• استردادها (فعال) •"
        view_topups = "💳 مشاهده واریزی‌ها"
        refresh_label = "🔄 بروزرسانی"
        close_label = "❌ بستن"
    elif lang_clean.startswith("ru"):
        topups_active = "• Пополнения (Акт.) •"
        view_refunds = "💸 Возвраты"
        refunds_active = "• Возвраты (Акт.) •"
        view_topups = "💳 Пополнения"
        refresh_label = "🔄 Обновить"
        close_label = "❌ Закрыть"
    else:
        topups_active = "• Top-ups (Active) •"
        view_refunds = "💸 View Refunds"
        refunds_active = "• Refunds (Active) •"
        view_topups = "💳 View Top-ups"
        refresh_label = "🔄 Refresh"
        close_label = "❌ Close"

    if current_tab == "topups":
        buttons.append([
            Button.inline(topups_active, data="hist:noop"),
            Button.inline(view_refunds, data="hist:refunds")
        ])
    else:
        buttons.append([
            Button.inline(view_topups, data="hist:topups"),
            Button.inline(refunds_active, data="hist:noop")
        ])

    buttons.append([
        Button.inline(refresh_label, data=f"hist:{current_tab}"),
        Button.inline(close_label, data="fsm:cancel")
    ])

    return buttons


def transaction_detail_keyboard(origin_tab: str = "topups", lang: str = "en") -> List[List[Button]]:
    """Navigation keyboard when inspecting an individual transaction detail."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        back_label = "🔙 بازگشت به لیست واریزی‌ها" if origin_tab == "topups" else "🔙 بازگشت به لیست استردادها"
    elif lang_clean.startswith("ru"):
        back_label = "🔙 Назад к пополнениям" if origin_tab == "topups" else "🔙 Назад к возвратам"
    else:
        back_label = "🔙 Back to Top-ups" if origin_tab == "topups" else "🔙 Back to Refunds"

    return [
        [Button.inline(back_label, data=f"hist:{origin_tab}")]
    ]


# =========================================================================
# 4. ANALYTICS & TELEMETRY KEYBOARDS (DIAGRAMS)
# =========================================================================

def analytics_view_keyboard(active_view: str = "traffic", lang: str = "en") -> List[List[Button]]:
    """
    Inline keyboard for switching between 30-day Traffic Usage and 30-day Financial Trends.
    Includes a dedicated localized Back button to return to the main dashboard menu.
    """
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        traffic_label = "• 📈 مصرف ترافیک (۳۰ روز) •" if active_view == "traffic" else "📈 مصرف ترافیک"
        financials_label = "• 💰 واریزی‌ها (۳۰ روز) •" if active_view == "financials" else "💰 واریزی‌ها"
        refresh_label = "🔄 بروزرسانی"
        back_label = "🔙 بازگشت به منوی اصلی"
    elif lang_clean.startswith("ru"):
        traffic_label = "• 📈 Трафик (30 дней) •" if active_view == "traffic" else "📈 Трафик"
        financials_label = "• 💰 Пополнения (30 дней) •" if active_view == "financials" else "💰 Пополнения"
        refresh_label = "🔄 Обновить"
        back_label = "🔙 Назад в главное меню"
    else:
        traffic_label = "• 📈 Traffic Usage (30D) •" if active_view == "traffic" else "📈 Traffic Usage"
        financials_label = "• 💰 Top-up Trends (30D) •" if active_view == "financials" else "💰 Top-up Trends"
        refresh_label = "🔄 Refresh"
        back_label = "🔙 Back to Main Menu"

    return [
        [
            Button.inline(traffic_label, data="analytics:view:traffic"),
            Button.inline(financials_label, data="analytics:view:financials")
        ],
        [
            Button.inline(refresh_label, data=f"analytics:view:{active_view}"),
            Button.inline(back_label, data="menu:main")
        ]
    ]


# =========================================================================
# 5. CONFIGURATIONS & ROUTE REPLACEMENT KEYBOARDS
# =========================================================================

def configs_list_keyboard(configs: list, lang: str = "en") -> List[List[Button]]:
    """
    Renders list of configurations with management buttons,
    the universal subscription copy button, and the copy-all-configs button.
    """
    lang_clean = (lang or "en").strip().lower()
    buttons = []

    if lang_clean.startswith("fa"):
        manage_prefix = "⚙️ مدیریت"
        sub_label = "📋 کپی لینک سابسکریپشن (اشتراک هوشمند)"
        copy_all_label = "📑 کپی لینک مستقیم تمام کانفیگ‌ها"
    elif lang_clean.startswith("ru"):
        manage_prefix = "⚙️ Управление"
        sub_label = "📋 Скопировать ссылку подписки"
        copy_all_label = "📑 Скопировать все прямые ссылки"
    else:
        manage_prefix = "⚙️ Manage"
        sub_label = "📋 Copy Subscription Link"
        copy_all_label = "📑 Copy All Configs Link"

    for c in configs:
        cid = str(c.get("id"))
        loc = c.get("location", "Region")
        proto = c.get("protocol", "VLESS")
        buttons.append([Button.inline(f"{manage_prefix} {loc} ({proto})", data=f"cfg:view:{cid}")])

    buttons.append([Button.inline(sub_label, data="cfg:sub_link")])
    buttons.append([Button.inline(copy_all_label, data="cfg:copy_all")])
    return buttons


def config_actions_keyboard(config_id: str, lang: str = "en") -> List[List[Button]]:
    """Localized actions for an individual Traffic Routing node configuration."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        qr_label = "📱 مشاهده QR Code"
        copy_label = "📋 کپی لینک مستقیم (URI)"
        replace_label = "🔄 تعویض مسیر مسدود / فیلتر شده"
        back_label = "🔙 بازگشت به لیست کانفیگ‌ها"
    elif lang_clean.startswith("ru"):
        qr_label = "📱 Показать QR-код"
        copy_label = "📋 Скопировать ссылку (URI)"
        replace_label = "🔄 Заменить заблокированный маршрут"
        back_label = "🔙 Назад к списку"
    else:
        qr_label = "📱 View QR Code"
        copy_label = "📋 Copy URI"
        replace_label = "🔄 Replace Blocked Route"
        back_label = "🔙 Back to Configs"

    return [
        [
            Button.inline(qr_label, data=f"cfg:qr:{config_id}"),
            Button.inline(copy_label, data=f"cfg:copy:{config_id}")
        ],
        [
            Button.inline(replace_label, data=f"cfg:replace:{config_id}")
        ],
        [
            Button.inline(back_label, data="cfg:list")
        ]
    ]


def isp_selection_keyboard(config_id: str, lang: str = "en") -> List[List[Button]]:
    """
    Prompts the user to select their mobile/home ISP carrier or type a custom one
    when replacing a route or dispatching an automatic support ticket.
    """
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        custom_label = "✏️ وارد کردن نام اپراتور دیگر"
        cancel_label = "❌ انصراف"
    elif lang_clean.startswith("ru"):
        custom_label = "✏️ Ввести своего провайдера вручную"
        cancel_label = "❌ Отмена"
    else:
        custom_label = "✏️ Enter Custom ISP Manually"
        cancel_label = "❌ Cancel"

    return [
        [
            Button.inline("همراه اول (MCI)", data=f"isp:{config_id}:MCI"),
            Button.inline("ایرانسل (Irancell)", data=f"isp:{config_id}:Irancell")
        ],
        [
            Button.inline("رایتل (Rightel)", data=f"isp:{config_id}:Rightel"),
            Button.inline("شاتل (Shatel)", data=f"isp:{config_id}:Shatel")
        ],
        [
            Button.inline("مخابرات (TCI)", data=f"isp:{config_id}:TCI"),
            Button.inline("آسیاتک (Asiatech)", data=f"isp:{config_id}:Asiatech")
        ],
        [
            Button.inline("مبین‌نت (MobinNet)", data=f"isp:{config_id}:MobinNet"),
            Button.inline("زیتل (Zitel)", data=f"isp:{config_id}:Zitel")
        ],
        [
            Button.inline(custom_label, data=f"isp_custom:{config_id}")
        ],
        [
            Button.inline(cancel_label, data="fsm:cancel")
        ]
    ]


# =========================================================================
# 6. SUPPORT TICKETS & CHAT KEYBOARDS
# =========================================================================

def ticket_categories_keyboard(lang: str = "en") -> List[List[Button]]:
    """Category selection when creating a new support ticket."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        return [
            [Button.inline("📡 مشکل در اتصال / سرور", data="tc:cat:Server Issue")],
            [Button.inline("💳 امور مالی و افزایش موجودی", data="tc:cat:Billing")],
            [Button.inline("💬 سوال عمومی / سایر موارد", data="tc:cat:General")],
            [Button.inline("❌ انصراف", data="fsm:cancel_ticket")]
        ]
    elif lang_clean.startswith("ru"):
        return [
            [Button.inline("📡 Проблема с сервером / сетью", data="tc:cat:Server Issue")],
            [Button.inline("💳 Вопросы оплаты и баланса", data="tc:cat:Billing")],
            [Button.inline("💬 Общий вопрос / другое", data="tc:cat:General")],
            [Button.inline("❌ Отмена", data="fsm:cancel_ticket")]
        ]
    else:
        return [
            [Button.inline("📡 Server / Network Issue", data="tc:cat:Server Issue")],
            [Button.inline("💳 Billing & Top-up", data="tc:cat:Billing")],
            [Button.inline("💬 General Question", data="tc:cat:General")],
            [Button.inline("❌ Cancel", data="fsm:cancel_ticket")]
        ]


def ticket_thread_keyboard(ticket_id: str, is_closed: bool = False, lang: str = "en") -> List[List[Button]]:
    """Inline controls when viewing an individual support ticket thread."""
    lang_clean = (lang or "en").strip().lower()
    buttons = []

    if not is_closed:
        reply_label = "✍️ ارسال پاسخ جدید" if lang_clean.startswith("fa") else "✍️ Ответить" if lang_clean.startswith("ru") else "✍️ Send Reply"
        buttons.append([Button.inline(reply_label, data=f"ticket:reply:{ticket_id}")])

    back_label = "🔙 بازگشت به تیکت‌ها" if lang_clean.startswith("fa") else "🔙 Назад к тикетам" if lang_clean.startswith("ru") else "🔙 Back to Tickets"
    buttons.append([Button.inline(back_label, data="ticket:list")])
    return buttons


def ticket_photo_keyboard(action_type: str = "create", lang: str = "en") -> List[List[Button]]:
    """Inline buttons for the optional screenshot step in ticket creation and replies."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        skip_label = "⏩ رد کردن بدون ارسال تصویر"
        cancel_label = "❌ انصراف"
    elif lang_clean.startswith("ru"):
        skip_label = "⏩ Пропустить отправку фото"
        cancel_label = "❌ Отмена"
    else:
        skip_label = "⏩ Skip (No Picture)"
        cancel_label = "❌ Cancel"

    skip_data = "ticket:skip_photo_create" if action_type == "create" else "ticket:skip_photo_reply"
    return [
        [Button.inline(skip_label, data=skip_data)],
        [Button.inline(cancel_label, data="fsm:cancel_ticket")]
    ]


# =========================================================================
# 7. HELP & EXTENSIVE GUIDE KEYBOARDS
# =========================================================================

def help_language_keyboard() -> List[List[Button]]:
    """Inline buttons to choose language for the comprehensive guide."""
    return [
        [Button.inline("🇬🇧 English Detailed Guide", data="help:guide:en")],
        [Button.inline("🇮🇷 راهنمای جامع فارسی", data="help:guide:fa")],
        [Button.inline("🇷🇺 Полное руководство на русском", data="help:guide:ru")]
    ]


def help_back_keyboard(lang: str = "en") -> List[List[Button]]:
    """Back button to return to the trilingual help overview."""
    lang_clean = (lang or "en").strip().lower()
    if lang_clean.startswith("fa"):
        back_label = "🔙 بازگشت به راهنمای کلی"
    elif lang_clean.startswith("ru"):
        back_label = "🔙 Назад к общему обзору"
    else:
        back_label = "🔙 Back to Overview"

    return [
        [Button.inline(back_label, data="help:main")]
    ]


# =========================================================================
# 8. GLOBAL UTILITY & CANCELLATION KEYBOARDS
# =========================================================================

def cancel_inline_keyboard(action: str = "cancel", lang: str = "en") -> List[List[Button]]:
    """
    Standard localized cancel button for all multi-step wizards (Top-up, Refund, Support).
    Clears the Redis FSM state and halts the wizard.
    """
    lang_clean = (lang or "en").strip().lower()
    if lang_clean.startswith("fa"):
        label = "❌ انصراف و لغو عملیات"
    elif lang_clean.startswith("ru"):
        label = "❌ Отмена операции"
    else:
        label = "❌ Cancel Operation"

    return [
        [Button.inline(label, data=f"fsm:{action}")]
    ]


def topup_note_keyboard(lang: str = "en") -> List[List[Button]]:
    """Inline buttons for the optional note step in the top-up flow."""
    lang_clean = (lang or "en").strip().lower()
    if lang_clean.startswith("fa"):
        skip_label = "⏩ رد کردن بدون یادداشت"
        cancel_label = "❌ انصراف"
    elif lang_clean.startswith("ru"):
        skip_label = "⏩ Пропустить примечание"
        cancel_label = "❌ Отмена"
    else:
        skip_label = "⏩ Skip Note"
        cancel_label = "❌ Cancel"

    return [
        [Button.inline(skip_label, data="topup:skip_note")],
        [Button.inline(cancel_label, data="fsm:cancel_topup")]
    ]


def refund_memo_keyboard(lang: str = "en") -> List[List[Button]]:
    """Inline buttons for the optional memo/reason step in the refund flow."""
    lang_clean = (lang or "en").strip().lower()
    if lang_clean.startswith("fa"):
        skip_label = "⏩ رد کردن بدون دلیل"
        cancel_label = "❌ انصراف"
    elif lang_clean.startswith("ru"):
        skip_label = "⏩ Пропустить причину"
        cancel_label = "❌ Отмена"
    else:
        skip_label = "⏩ Skip Memo"
        cancel_label = "❌ Cancel"

    return [
        [Button.inline(skip_label, data="refund:skip_memo")],
        [Button.inline(cancel_label, data="fsm:cancel_refund")]
    ]