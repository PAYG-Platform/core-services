# telegram-service/app/handlers/balance.py
import re
import base64
import logging
from telethon import events, Button
from app.services.redis_client import RedisFSM
from app.services.api_client import BackendAPIClient
from app.keyboards import (
    main_menu_keyboard, 
    cancel_inline_keyboard, 
    status_dashboard_keyboard
)

logger = logging.getLogger(__name__)

# Translation table for Persian digits
PERSIAN_DIGITS_TRANS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(value: str) -> str:
    """Replaces English digits 0-9 with Persian digits ۰-۹, commas with '،', and dots with '٫'."""
    if not value:
        return ""
    text = str(value).translate(PERSIAN_DIGITS_TRANS)
    return text.replace(",", "،").replace(".", "٫")


def format_localized_number(value: float, lang: str = "en", decimals: int = 0) -> str:
    """Formats numeric amounts according to the user's language."""
    lang_clean = (lang or "en").strip().lower()

    if decimals > 0:
        base_formatted = f"{value:,.{decimals}f}"
    else:
        base_formatted = f"{value:,.0f}"

    if lang_clean.startswith("fa"):
        return to_persian_digits(base_formatted)
    elif lang_clean.startswith("ru"):
        # Russian format: space as thousands separator, comma as decimal separator
        return base_formatted.replace(",", " ").replace(".", ",")
    return base_formatted


def format_bytes_human(b: int, lang: str = "en") -> str:
    """Converts bytes to localized human-readable units."""
    lang_clean = (lang or "en").strip().lower()

    if not b or b <= 0:
        if lang_clean.startswith("fa"):
            return f"{to_persian_digits('0')} مگابایت"
        elif lang_clean.startswith("ru"):
            return "0 МБ"
        return "0 MB"

    mb = b / (1024 ** 2)
    if mb >= 1024:
        gb_val = mb / 1024
        gb_str = format_localized_number(gb_val, lang=lang, decimals=2)
        if lang_clean.startswith("fa"):
            return f"{gb_str} گیگابایت"
        elif lang_clean.startswith("ru"):
            return f"{gb_str} ГБ"
        return f"{gb_str} GB"

    mb_str = format_localized_number(mb, lang=lang, decimals=1)
    if lang_clean.startswith("fa"):
        return f"{mb_str} مگابایت"
    elif lang_clean.startswith("ru"):
        return f"{mb_str} МБ"
    return f"{mb_str} MB"


async def register_balance_handlers(bot):
    # ==========================================
    # 1. STATUS & DASHBOARD COMMAND
    # ==========================================
    STATUS_BUTTONS = [
        "📊 Status & Balance", "/status", "/balance",
        "📊 وضعیت و موجودی",
        "📊 Статус и Баланс"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in STATUS_BUTTONS))
    async def on_status_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ You have not connected your account yet. Send `/start <token>` to begin.")
            return

        username = await RedisFSM.get_username(telegram_id) or "User"
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id, username)

        # Refresh language from IAM if needed
        profile = await api.get_user_profile()
        if profile and profile.get("language"):
            user_lang = profile["language"].strip().lower()
            await RedisFSM.set_language(telegram_id, user_lang)

        balance_data = await api.get_balance()
        configs_data = await api.get_my_configs()

        if not balance_data:
            err_msg = (
                "⚠️ خطا در دریافت اطلاعات مالی. لطفاً مجدداً تلاش کنید."
                if user_lang.startswith("fa")
                else "⚠️ Не удалось получить информацию о балансе. Пожалуйста, попробуйте снова."
                if user_lang.startswith("ru")
                else "⚠️ Could not retrieve financial status. Please try again."
            )
            await event.reply(err_msg)
            return

        paid_bal = balance_data.get("paid_balance", 0.0)
        gift_bal = balance_data.get("gift_balance", 0.0)
        usable_credit = balance_data.get("usable_credit", 0.0)
        min_topup = balance_data.get("user_min_topup_amount", 10000.0)

        # Localize numbers
        usable_str = format_localized_number(usable_credit, lang=user_lang)
        paid_str = format_localized_number(paid_bal, lang=user_lang)
        gift_str = format_localized_number(gift_bal, lang=user_lang)
        min_topup_str = format_localized_number(min_topup, lang=user_lang)

        configs_list = configs_data.get("configs", []) if configs_data else []
        configs_count_str = format_localized_number(len(configs_list), lang=user_lang)

        nodes_preview = []
        for c in configs_list[:4]:
            loc = c.get("location", "Region")
            proto = c.get("protocol", "VLESS")
            used = format_bytes_human(c.get("data_used_bytes", 0), lang=user_lang)
            if user_lang.startswith("fa"):
                nodes_preview.append(f"• **{loc}** ({proto}) — مصرف: `{used}`")
            elif user_lang.startswith("ru"):
                nodes_preview.append(f"• **{loc}** ({proto}) — Использовано: `{used}`")
            else:
                nodes_preview.append(f"• **{loc}** ({proto}) — `{used}` used")

        # --------------------------------------------------
        # Render Localized Message
        # --------------------------------------------------
        if user_lang.startswith("fa"):
            nodes_text = "\n".join(nodes_preview) if nodes_preview else "هیچ کانفیگ فعالی ثبت نشده است."
            msg = (
                f"📊 **وضعیت سرویس و حساب کاربری: @{username}**\n\n"
                f"💰 **موجودی قابل استفاده:** `{usable_str} تومان`\n"
                f"├ شارژ نقدی: `{paid_str} تومان`\n"
                f"└ اعتبار هدیه: `{gift_str} تومان`\n\n"
                f"🌐 **سرورهای فعال:** `{configs_count_str}` لوکیشن\n"
                f"{nodes_text}\n\n"
                f"💳 **حداقل مبلغ افزایش موجودی:** `{min_topup_str} تومان`\n"
            )
        elif user_lang.startswith("ru"):
            nodes_text = "\n".join(nodes_preview) if nodes_preview else "Нет активных конфигураций."
            msg = (
                f"📊 **Статус аккаунта и сервиса: @{username}**\n\n"
                f"💰 **Доступный баланс:** `{usable_str} Томан`\n"
                f"├ Оплаченный баланс: `{paid_str} Томан`\n"
                f"└ Подарочный баланс: `{gift_str} Томан`\n\n"
                f"🌐 **Активные узлы:** `{configs_count_str}` сервер(ов)\n"
                f"{nodes_text}\n\n"
                f"💳 **Минимальное пополнение:** `{min_topup_str} Томан`\n"
            )
        else:
            nodes_text = "\n".join(nodes_preview) if nodes_preview else "No active nodes assigned."
            msg = (
                f"📊 **Account & Service Status: @{username}**\n\n"
                f"💰 **Usable Balance:** `{usable_str} Toman`\n"
                f"├ Paid Credit: `{paid_str} Toman`\n"
                f"└ Gift Credit: `{gift_str} Toman`\n\n"
                f"🌐 **Active Nodes:** `{configs_count_str}` server(s)\n"
                f"{nodes_text}\n\n"
                f"💳 **Minimum Top-up:** `{min_topup_str} Toman`\n"
            )

        await event.reply(msg, buttons=status_dashboard_keyboard(user_lang))

    # ==========================================
    # 2. TOP-UP DEPOSIT WIZARD (MULTI-STEP FSM)
    # ==========================================
    TOPUP_BUTTONS = [
        "💳 Deposit / Top-up", "/topup",
        "💳 افزایش موجودی",
        "💳 Пополнение счета"
    ]

    def parse_numeric_input(raw: str) -> Optional[int]:
        """Converts Persian, Arabic, and Western numbers and strips commas/spaces."""
        if not raw:
            return None
        # Replace Persian/Arabic numerals
        persian_arabic = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
        cleaned = raw.translate(persian_arabic)
        cleaned = re.sub(r"[,،\s_Ttتومان]", "", cleaned).strip()
        if cleaned.isdigit():
            return int(cleaned)
        return None

    @bot.on(events.NewMessage(func=lambda e: e.text in TOPUP_BUTTONS))
    async def on_topup_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Please connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        balance_info = await api.get_balance()
        fin_settings = await api.get_admin_financial_settings()

        min_topup = balance_info.get("user_min_topup_amount", 10000.0) if balance_info else 10000.0
        min_topup_str = format_localized_number(min_topup, lang=user_lang)

        user_curr = await RedisFSM.get_currency(telegram_id)
        # International USD accounts see strictly Crypto only (Shetab cards hidden)
        if user_curr == "USD":
            cards = []
        else:
            cards = fin_settings.get("admin_bank_cards", []) if fin_settings else []
        cryptos = fin_settings.get("admin_crypto_wallets", []) if fin_settings else []

        if user_lang.startswith("fa"):
            accounts_lines = []
            if cards:
                for c in cards:
                    accounts_lines.append(f"💳 کارت بانکی:\nشماره: `{c.get('card_number')}`\nبه نام: **{c.get('cardholder_name')}** ({c.get('bank_name')})")
            if cryptos:
                for w in cryptos:
                    accounts_lines.append(f"🪙 تتر ({w.get('network')}):\nآدرس: `{w.get('address')}`")

            if not accounts_lines:
                accounts_section = (
                    "ℹ️ **هیچ شماره کارت یا آدرس رسمی در سیستم ثبت نشده است.**\n"
                    "اگر شماره حساب را از پشتیبانی یا کانال دریافت کرده‌اید، می‌توانید مراحل را ادامه دهید."
                )
            else:
                accounts_section = "\n\n".join(accounts_lines)

            msg = (
                f"💳 **افزایش موجودی کیف پول**\n\n"
                f"{accounts_section}\n\n"
                f"────────────────────────────\n"
                f"📌 **حداقل مبلغ مجاز برای شما:** `{min_topup_str} تومان`\n\n"
                f"🔢 **مرحله ۱ از ۳:** لطفاً مبلغ واریزی خود را به **تومان** وارد کنید (فقط عدد):"
            )
        elif user_lang.startswith("ru"):
            accounts_lines = []
            if cards:
                for c in cards:
                    accounts_lines.append(f"💳 Банковская карта:\nНомер: `{c.get('card_number')}`\nИмя: **{c.get('cardholder_name')}** ({c.get('bank_name')})")
            if cryptos:
                for w in cryptos:
                    accounts_lines.append(f"🪙 Криптовалюта USDT ({w.get('network')}):\nАдрес: `{w.get('address')}`")

            if not accounts_lines:
                accounts_section = (
                    "ℹ️ **Официальные реквизиты не указаны в системе.**\n"
                    "Если вы получили реквизиты у поддержки, вы можете продолжить пополнение."
                )
            else:
                accounts_section = "\n\n".join(accounts_lines)

            msg = (
                f"💳 **Пополнение баланса**\n\n"
                f"{accounts_section}\n\n"
                f"────────────────────────────\n"
                f"📌 **Минимальная сумма для вас:** `{min_topup_str} Томан`\n\n"
                f"🔢 **Шаг 1 из 3:** Введите сумму пополнения в **Томанах** (только цифры):"
            )
        else:
            accounts_lines = []
            if cards:
                for c in cards:
                    accounts_lines.append(f"💳 Bank Card:\nCard: `{c.get('card_number')}`\nName: **{c.get('cardholder_name')}** ({c.get('bank_name')})")
            if cryptos:
                for w in cryptos:
                    accounts_lines.append(f"🪙 Crypto USDT ({w.get('network')}):\nAddress: `{w.get('address')}`")

            if not accounts_lines:
                accounts_section = (
                    "ℹ️ **No official deposit accounts are listed in the system.**\n"
                    "If you obtained payment details from support, you may still proceed."
                )
            else:
                accounts_section = "\n\n".join(accounts_lines)

            msg = (
                f"💳 **Wallet Top-Up / Deposit**\n\n"
                f"{accounts_section}\n\n"
                f"────────────────────────────\n"
                f"📌 **Minimum required deposit for you:** `{min_topup_str} Toman`\n\n"
                f"🔢 **Step 1 of 3:** Enter the payment amount in **Toman** (Numbers only):"
            )

        await RedisFSM.set_state(
            telegram_id, 
            "TOPUP_WAITING_AMOUNT", 
            {"min_amount": min_topup, "lang": user_lang}
        )
        await event.reply(msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))

    # Inline button callback trigger (from status view)
    @bot.on(events.CallbackQuery(pattern=r"^topup:start$"))
    async def on_cb_topup_start(event):
        await event.answer()
        # Dispatch the same flow
        await on_topup_command(event)

    # Top-up Step 2: Handle Screenshot Upload
    @bot.on(events.NewMessage(func=lambda e: e.photo is not None or (e.file and e.file.mime_type and e.file.mime_type.startswith("image/"))))
    async def on_topup_photo_upload(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)

        if state == "TOPUP_WAITING_RECEIPT":
            user_lang = await RedisFSM.get_language(telegram_id)
            data = await RedisFSM.get_data(telegram_id)

            wait_msg = (
                "⏳ در حال بارگیری و اعتبارسنجی تصویر رسید..."
                if user_lang.startswith("fa")
                else "⏳ Загрузка и проверка скриншота..."
                if user_lang.startswith("ru")
                else "⏳ Downloading and validating your receipt screenshot..."
            )
            status_msg = await event.reply(wait_msg)
            photo_bytes = await event.download_media(file=bytes)

            if not photo_bytes or len(photo_bytes) < 100:
                retry_msg = (
                    "⚠️ تصویر ارسالی نامعتبر است یا خوانده نشد. لطفاً یک عکس یا اسکرین‌شات واضح ارسال نمایید:"
                    if user_lang.startswith("fa")
                    else "⚠️ Не удалось прочитать изображение. Пожалуйста, отправьте четкий скриншот:"
                    if user_lang.startswith("ru")
                    else "⚠️ Could not read the uploaded image. Please send a clear screenshot:"
                )
                await event.reply(retry_msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))
                return

            b64_str = f"data:image/jpeg;base64,{base64.b64encode(photo_bytes).decode('utf-8')}"
            data["receipt_b64"] = b64_str

            await RedisFSM.set_state(telegram_id, "TOPUP_WAITING_NOTE", data)

            from app.keyboards import topup_note_keyboard
            if user_lang.startswith("fa"):
                step3_prompt = (
                    "✅ **تصویر فیش واریزی با موفقیت ذخیره شد!**\n\n"
                    "📝 **مرحله ۳ از ۳ (اختیاری):** در صورت تمایل، توضیحات، نام واریزکننده یا شماره پیگیری را ارسال فرمایید.\n"
                    "یا بر روی دکمه **رد کردن** کلیک کنید:"
                )
            elif user_lang.startswith("ru"):
                step3_prompt = (
                    "✅ **Скриншот квитанции успешно сохранен!**\n\n"
                    "📝 **Шаг 3 из 3 (необязательно):** Введите примечание (номер транзакции, имя отправителя) "
                    "или нажмите кнопку **Пропустить**:"
                )
            else:
                step3_prompt = (
                    "✅ **Payment screenshot saved!**\n\n"
                    "📝 **Step 3 of 3 (Optional):** Enter any note or memo (e.g. sender name, reference code) "
                    "or click **Skip Note** below:"
                )

            await event.reply(step3_prompt, buttons=topup_note_keyboard(lang=user_lang))
            return

    # Top-up Step 3: Handle Skip Note Callback
    @bot.on(events.CallbackQuery(pattern=r"^topup:skip_note$"))
    async def on_cb_skip_topup_note(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)

        if state != "TOPUP_WAITING_NOTE":
            await event.answer()
            return

        await event.answer()
        await complete_topup_submission(event, note="")

    async def complete_topup_submission(event_or_msg, note: str = ""):
        """Executes the final API call to Billing Service."""
        telegram_id = str(event_or_msg.sender_id)
        data = await RedisFSM.get_data(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        user_id = await RedisFSM.get_user_id(telegram_id)

        amount = data.get("amount", 0.0)
        receipt_b64 = data.get("receipt_b64")

        await RedisFSM.clear_state(telegram_id)
        api = BackendAPIClient(user_id)

        submitting_text = (
            "⏳ در حال ارسال درخواست افزایش موجودی برای مدیریت..."
            if user_lang.startswith("fa")
            else "⏳ Отправка запроса на пополнение администраторам..."
            if user_lang.startswith("ru")
            else "⏳ Submitting your deposit request to administrators..."
        )
        await event_or_msg.reply(submitting_text)

        res = await api.submit_fiat_topup(amount=amount, receipt_b64=receipt_b64, description=note)

        amount_str = format_localized_number(amount, lang=user_lang)

        if res.get("success"):
            tx_data = res.get("data", {})
            ref_id = tx_data.get("reference_id", "TOP-PENDING")

            if user_lang.startswith("fa"):
                confirm_msg = (
                    f"🎉 **درخواست افزایش موجودی با موفقیت ثبت شد!**\n\n"
                    f"• مبلغ: `{amount_str} تومان`\n"
                    f"• کد رهگیری: `{ref_id}`\n"
                    f"• وضعیت: `در انتظار تایید مدیریت`\n\n"
                    f"پس از بررسی فیش توسط تیم مالی، موجودی شما به صورت خودکار اعمال شده و اعلان آن در همین ربات برای شما ارسال خواهد شد."
                )
            elif user_lang.startswith("ru"):
                confirm_msg = (
                    f"🎉 **Запрос на пополнение успешно отправлен!**\n\n"
                    f"• Сумма: `{amount_str} Томан`\n"
                    f"• Код транзакции: `{ref_id}`\n"
                    f"• Статус: `На рассмотрении`\n\n"
                    f"После подтверждения квитанции средства поступят на счет, а бот пришлет уведомление."
                )
            else:
                confirm_msg = (
                    f"🎉 **Top-up Request Submitted Successfully!**\n\n"
                    f"• Amount: `{amount_str} Toman`\n"
                    f"• Reference ID: `{ref_id}`\n"
                    f"• Status: `Pending Review`\n\n"
                    f"Administrators will review your receipt slip. You will receive an instant notification here once approved."
                )
            await event_or_msg.reply(confirm_msg, buttons=main_menu_keyboard(user_lang))
        else:
            err_detail = res.get("detail", "Error")
            fail_msg = (
                f"⚠️ **خطا در ثبت درخواست:** {err_detail}"
                if user_lang.startswith("fa")
                else f"⚠️ **Ошибка отправки:** {err_detail}"
                if user_lang.startswith("ru")
                else f"⚠️ **Submission Failed:** {err_detail}"
            )
            await event_or_msg.reply(fail_msg, buttons=main_menu_keyboard(user_lang))

    # Generic FSM Text Handler (Amount & Optional Note)
    @bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
    async def on_text_input_fsm(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)
        text = event.text.strip()
        user_lang = await RedisFSM.get_language(telegram_id)

        # ---------------------------------------------------------------------
        # TOP-UP STEP 1: VALIDATE AMOUNT (MUST BE NUMERIC & >= MINIMUM THRESHOLD)
        # ---------------------------------------------------------------------
        if state == "TOPUP_WAITING_AMOUNT":
            parsed_amount = parse_numeric_input(text)

            if parsed_amount is None:
                # User entered non-numeric text -> Ask again!
                if user_lang.startswith("fa"):
                    invalid_msg = "⚠️ مقدار وارد شده نامعتبر است. لطفاً فقط عدد انگلیسی یا فارسی (به تومان) وارد نمایید:"
                elif user_lang.startswith("ru"):
                    invalid_msg = "⚠️ Неверный формат. Пожалуйста, введите только число (в Томанах):"
                else:
                    invalid_msg = "⚠️ Invalid amount. Please enter a valid number in Toman (Numbers only):"
                await event.reply(invalid_msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))
                return

            data = await RedisFSM.get_data(telegram_id)
            min_required = data.get("min_amount", 10000.0)

            if parsed_amount < min_required:
                min_req_str = format_localized_number(min_required, lang=user_lang)
                if user_lang.startswith("fa"):
                    below_min_msg = f"⚠️ حداقل مبلغ مجاز برای افزایش موجودی `{min_req_str} تومان` می‌باشد. لطفاً مبلغی مساوی یا بیشتر وارد کنید:"
                elif user_lang.startswith("ru"):
                    below_min_msg = f"⚠️ Минимальная сумма пополнения: `{min_req_str} Томан`. Введите сумму не меньше указанной:"
                else:
                    below_min_msg = f"⚠️ The minimum top-up requirement is `{min_req_str} Toman`. Please enter an amount of at least `{min_req_str}`:"
                await event.reply(below_min_msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))
                return

            # Amount is valid! Transition to Step 2 (Screenshot)
            data["amount"] = float(parsed_amount)
            await RedisFSM.set_state(telegram_id, "TOPUP_WAITING_RECEIPT", data)

            amount_confirmed_str = format_localized_number(parsed_amount, lang=user_lang)
            if user_lang.startswith("fa"):
                step2_msg = (
                    f"✅ مبلغ تایید شد: `{amount_confirmed_str} تومان`\n\n"
                    f"📸 **مرحله ۲ از ۳:** لطفاً تصویر یا اسکرین‌شات فیش واریزی خود را ارسال فرمایید:"
                )
            elif user_lang.startswith("ru"):
                step2_msg = (
                    f"✅ Сумма принята: `{amount_confirmed_str} Томан`\n\n"
                    f"📸 **Шаг 2 из 3:** Пожалуйста, отправьте скриншот или фото квитанции об оплате:"
                )
            else:
                step2_msg = (
                    f"✅ Amount accepted: `{amount_confirmed_str} Toman`\n\n"
                    f"📸 **Step 2 of 3:** Please upload a **photo or screenshot** of your payment transfer slip:"
                )
            await event.reply(step2_msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))
            return

        # ---------------------------------------------------------------------
        # TOP-UP STEP 2: USER SENT TEXT INSTEAD OF PHOTO -> REMIND & ASK AGAIN!
        # ---------------------------------------------------------------------
        if state == "TOPUP_WAITING_RECEIPT":
            if user_lang.startswith("fa"):
                photo_required_msg = (
                    "⚠️ ارسال تصویر فیش واریزی **الزامی** است.\n"
                    "لطفاً یک عکس یا اسکرین‌شات از رسید بانکی ارسال فرمایید:"
                )
            elif user_lang.startswith("ru"):
                photo_required_msg = (
                    "⚠️ Загрузка скриншота **обязательна**.\n"
                    "Пожалуйста, отправьте фото квитанции:"
                )
            else:
                photo_required_msg = (
                    "⚠️ A screenshot of the receipt is **required**.\n"
                    "Please upload a photo or image of your payment transfer slip:"
                )
            await event.reply(photo_required_msg, buttons=cancel_inline_keyboard("cancel_topup", lang=user_lang))
            return

        # ---------------------------------------------------------------------
        # TOP-UP STEP 3: USER SUBMITS OPTIONAL NOTE VIA TEXT
        # ---------------------------------------------------------------------
        if state == "TOPUP_WAITING_NOTE":
            await complete_topup_submission(event, note=text)
            return

        # ---------------------------------------------------------------------
        # REFUND WIZARD TEXT HANDLERS
        # ---------------------------------------------------------------------
        # STEP 1: REFUND AMOUNT VALIDATION
        if state == "REFUND_WAITING_AMOUNT":
            parsed_refund = parse_numeric_input(text)
            data = await RedisFSM.get_data(telegram_id)
            avail = data.get("available", 0.0)
            avail_str = format_localized_number(avail, lang=user_lang)

            if parsed_refund is None:
                if user_lang.startswith("fa"):
                    err_msg = "⚠️ مبلغ وارد شده نامعتبر است. لطفاً فقط عدد انگلیسی یا فارسی (به تومان) وارد نمایید:"
                elif user_lang.startswith("ru"):
                    err_msg = "⚠️ Неверный формат суммы. Введите только число (в Томанах):"
                else:
                    err_msg = "⚠️ Invalid numeric amount. Please enter numbers only (in Toman):"
                await event.reply(err_msg, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                return

            if parsed_refund <= 0:
                if user_lang.startswith("fa"):
                    err_msg = "⚠️ مبلغ استرداد باید بزرگتر از صفر باشد. لطفاً مجدداً وارد فرمایید:"
                elif user_lang.startswith("ru"):
                    err_msg = "⚠️ Сумма к возврату должна быть больше нуля. Пожалуйста, введите корректную сумму:"
                else:
                    err_msg = "⚠️ Refund amount must be greater than zero. Please enter a valid amount:"
                await event.reply(err_msg, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                return

            if parsed_refund > avail:
                if user_lang.startswith("fa"):
                    err_msg = (
                        f"⚠️ مبلغ درخواستی بیشتر از موجودی قابل استرداد شماست.\n"
                        f"حداکثر مبلغ مجاز: `{avail_str} تومان`\n"
                        f"لطفاً مبلغی کمتر یا مساوی موجودی وارد کنید:"
                    )
                elif user_lang.startswith("ru"):
                    err_msg = (
                        f"⚠️ Запрошенная сумма превышает доступный баланс.\n"
                        f"Максимальная сумма: `{avail_str} Томан`\n"
                        f"Введите сумму меньше или равную доступному балансу:"
                    )
                else:
                    err_msg = (
                        f"⚠️ The requested refund exceeds your available paid credit.\n"
                        f"Maximum allowed: `{avail_str} Toman`\n"
                        f"Please enter an amount less than or equal to your available balance:"
                    )
                await event.reply(err_msg, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                return

            data["amount"] = float(parsed_refund)
            await RedisFSM.set_state(telegram_id, "REFUND_WAITING_CARD", data)

            refund_amt_str = format_localized_number(parsed_refund, lang=user_lang)
            if user_lang.startswith("fa"):
                card_prompt = (
                    f"✅ مبلغ تایید شد: `{refund_amt_str} تومان`\n\n"
                    f"💳 **مرحله ۲ از ۴:** لطفاً شماره کارت ۱۶ رقمی شتاب را جهت واریز وجه وارد فرمایید (فقط ۱۶ رقم):"
                )
            elif user_lang.startswith("ru"):
                card_prompt = (
                    f"✅ Сумма подтверждена: `{refund_amt_str} Томан`\n\n"
                    f"💳 **Шаг 2 из 4:** Введите 16-значный номер банковской карты для перевода:"
                )
            else:
                card_prompt = (
                    f"✅ Amount accepted: `{refund_amt_str} Toman`\n\n"
                    f"💳 **Step 2 of 4:** Please enter your 16-digit Shetab Bank Card number:"
                )
            await event.reply(card_prompt, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
            return

        # STEP 2: CARD / CRYPTO WALLET VALIDATION
        if state == "REFUND_WAITING_CARD":
            user_curr = await RedisFSM.get_currency(telegram_id)
            clean_card = text.strip()

            # For IRT: strictly validate 16-digit Shetab card. For USD: validate valid Crypto address.
            if user_curr != "USD":
                persian_arabic = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
                numeric_digits = re.sub(r"[\s\-]", "", clean_card.translate(persian_arabic))
                if not numeric_digits.isdigit() or len(numeric_digits) != 16:
                    err_card = (
                        "⚠️ شماره کارت شتاب باید دقیقاً ۱۶ رقم عددی باشد. لطفاً دوباره وارد فرمایید:"
                        if user_lang.startswith("fa") else
                        "⚠️ Номер карты должен содержать ровно 16 цифр. Попробуйте еще раз:"
                        if user_lang.startswith("ru") else
                        "⚠️ Shetab card must be exactly 16 numeric digits. Please try again:"
                    )
                    await event.reply(err_card, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                    return
                clean_card = numeric_digits
            else:
                if len(clean_card) < 10:
                    err_crypto = "⚠️ Please provide a valid USDT (TRC20 / TON) wallet address for payout:"
                    await event.reply(err_crypto, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                    return

            data = await RedisFSM.get_data(telegram_id)
            data["card_number"] = clean_card
            await RedisFSM.set_state(telegram_id, "REFUND_WAITING_NAME", data)

            if user_lang.startswith("fa"):
                name_prompt = (
                    f"✅ شماره کارت: `{clean_card}`\n\n"
                    f"👤 **مرحله ۳ از ۴:** لطفاً **نام و نام خانوادگی صاحب کارت** را وارد فرمایید:"
                )
            elif user_lang.startswith("ru"):
                name_prompt = (
                    f"✅ Номер карты: `{clean_card}`\n\n"
                    f"👤 **Шаг 3 из 4:** Введите **ФИО владельца карты**:"
                )
            else:
                name_prompt = (
                    f"✅ Card accepted: `{clean_card}`\n\n"
                    f"👤 **Step 3 of 4:** Please enter the **full name of the cardholder**:"
                )
            await event.reply(name_prompt, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
            return

        # STEP 3: CARDHOLDER FULL NAME VALIDATION
        if state == "REFUND_WAITING_NAME":
            holder_name = text.strip()
            if len(holder_name) < 3:
                if user_lang.startswith("fa"):
                    err_name = "⚠️ نام صاحب کارت نامعتبر است. لطفاً نام و نام خانوادگی کامل را وارد فرمایید:"
                elif user_lang.startswith("ru"):
                    err_name = "⚠️ Некорректное имя. Пожалуйста, введите полное имя владельца карты:"
                else:
                    err_name = "⚠️ Invalid cardholder name. Please enter the full legal name on the card:"
                await event.reply(err_name, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))
                return

            data = await RedisFSM.get_data(telegram_id)
            data["cardholder_name"] = holder_name
            await RedisFSM.set_state(telegram_id, "REFUND_WAITING_MEMO", data)

            from app.keyboards import refund_memo_keyboard
            if user_lang.startswith("fa"):
                memo_prompt = (
                    f"✅ صاحب کارت: **{holder_name}**\n\n"
                    f"📝 **مرحله ۴ از ۴ (اختیاری):** در صورت تمایل، دلیل یا یادداشت درخواست استرداد را بنویسید، "
                    f"یا بر روی دکمه **رد کردن** کلیک کنید:"
                )
            elif user_lang.startswith("ru"):
                memo_prompt = (
                    f"✅ Владелец карты: **{holder_name}**\n\n"
                    f"📝 **Шаг 4 из 4 (необязательно):** Укажите причину возврата или нажмите **Пропустить**:"
                )
            else:
                memo_prompt = (
                    f"✅ Cardholder: **{holder_name}**\n\n"
                    f"📝 **Step 4 of 4 (Optional):** Enter a memo or reason for the refund, "
                    f"or click **Skip Memo** below:"
                )
            await event.reply(memo_prompt, buttons=refund_memo_keyboard(lang=user_lang))
            return

        # STEP 4: OPTIONAL MEMO VIA TEXT
        if state == "REFUND_WAITING_MEMO":
            await complete_refund_submission(event, memo=text)
            return

    # Callback to skip optional refund memo
    @bot.on(events.CallbackQuery(pattern=r"^refund:skip_memo$"))
    async def on_cb_skip_refund_memo(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)
        if state != "REFUND_WAITING_MEMO":
            await event.answer()
            return
        await event.answer()
        await complete_refund_submission(event, memo="")

    async def complete_refund_submission(event_or_msg, memo: str = ""):
        """Submits the validated refund claim to Billing service."""
        telegram_id = str(event_or_msg.sender_id)
        data = await RedisFSM.get_data(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        user_id = await RedisFSM.get_user_id(telegram_id)

        amount = data.get("amount", 0.0)
        card_number = data.get("card_number", "")
        cardholder_name = data.get("cardholder_name", "")

        await RedisFSM.clear_state(telegram_id)
        api = BackendAPIClient(user_id)

        submitting_msg = (
            "⏳ در حال ارسال درخواست استرداد به بخش مالی..."
            if user_lang.startswith("fa")
            else "⏳ Отправка запроса на возврат средств..."
            if user_lang.startswith("ru")
            else "⏳ Submitting your refund claim to the financial team..."
        )
        await event_or_msg.reply(submitting_msg)

        res = await api.submit_refund_request(
            amount=amount,
            card_number=card_number,
            cardholder_name=cardholder_name,
            reason=memo
        )

        amount_str = format_localized_number(amount, lang=user_lang)

        if res.get("success"):
            rfd = res.get("data", {})
            ref_id = rfd.get("reference_id", "RFD-XXXX")

            if user_lang.startswith("fa"):
                confirm_msg = (
                    f"🎉 **درخواست استرداد وجه با موفقیت ثبت شد!**\n\n"
                    f"• مبلغ درخواستی: `{amount_str} تومان`\n"
                    f"• شماره کارت مقصد: `{card_number}`\n"
                    f"• به نام: **{cardholder_name}**\n"
                    f"• کد رهگیری: `{ref_id}`\n\n"
                    f"تیم مالی پس از واریز وجه، تصویر فیش بانکی را به عنوان سند پیوست خواهند نمود. "
                    f"نتیجه بلافاصله در همین ربات به شما اطلاع‌رسانی می‌شود."
                )
            elif user_lang.startswith("ru"):
                confirm_msg = (
                    f"🎉 **Запрос на возврат средств успешно оформлен!**\n\n"
                    f"• Сумма: `{amount_str} Томан`\n"
                    f"• Карта зачисления: `{card_number}`\n"
                    f"• Владелец: **{cardholder_name}**\n"
                    f"• Номер заявки: `{ref_id}`\n\n"
                    f"После перевода средств администрация прикрепит квитанцию, и бот уведомит вас."
                )
            else:
                confirm_msg = (
                    f"🎉 **Refund Claim Submitted Successfully!**\n\n"
                    f"• Amount: `{amount_str} Toman`\n"
                    f"• Destination Card: `{card_number}`\n"
                    f"• Cardholder: **{cardholder_name}**\n"
                    f"• Reference ID: `{ref_id}`\n\n"
                    f"Administrators will execute the payout and attach proof of transfer. "
                    f"You will receive an instant notification here once completed."
                )
            await event_or_msg.reply(confirm_msg, buttons=main_menu_keyboard(user_lang))
        else:
            err_detail = res.get("detail", "Error")
            fail_msg = (
                f"⚠️ **خطا در ثبت استرداد:** {err_detail}"
                if user_lang.startswith("fa")
                else f"⚠️ **Ошибка оформления возврата:** {err_detail}"
                if user_lang.startswith("ru")
                else f"⚠️ **Refund Request Failed:** {err_detail}"
            )
            await event_or_msg.reply(fail_msg, buttons=main_menu_keyboard(user_lang))

    # ==========================================
    # 3. REFUND REQUEST START COMMAND
    # ==========================================
    REFUND_BUTTONS = [
        "💸 Request Refund", "/refund",
        "💸 درخواست استرداد",
        "💸 Запрос на возврат"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in REFUND_BUTTONS))
    async def on_refund_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Please connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        # 1. FETCH REFUND BALANCES & DYNAMIC AML COOLDOWN FROM BILLING SERVICE
        refund_info = await api.get_refund_balance()

        if not refund_info:
            err_text = (
                "⚠️ در حال حاضر امکان بررسی شرایط استرداد وجود ندارد. لطفاً دقایقی دیگر تلاش فرمایید."
                if user_lang.startswith("fa")
                else "⚠️ В настоящее время невозможно проверить возможность возврата. Попробуйте позже."
                if user_lang.startswith("ru")
                else "⚠️ Could not verify refund eligibility at this time. Please try again shortly."
            )
            await event.reply(err_text)
            return

        avail = float(refund_info.get("available_balance", 0.0))
        can_refund = bool(refund_info.get("can_refund", False))
        cooldown_days = int(refund_info.get("refund_cooldown_days", 30))
        days_left = int(refund_info.get("days_until_next_refund", 0))

        # RULE 1: ZERO BALANCE CHECK
        if avail <= 0:
            if user_lang.startswith("fa"):
                zero_msg = (
                    "ℹ️ **موجودی قابل استرداد شما صفر است.**\n\n"
                    "شما هیچ شارژ نقدی واریز شده‌ای در کیف پول خود ندارید. "
                    "لازم به ذکر است که اعتبارات هدیه سیستم غیرقابل استرداد می‌باشند."
                )
            elif user_lang.startswith("ru"):
                zero_msg = (
                    "ℹ️ **Ваш доступный баланс для возврата равен нулю.**\n\n"
                    "У вас нет оплаченных депозитов на балансе. "
                    "Обратите внимание: подарочные баллы и бонусы не подлежат возврату."
                )
            else:
                zero_msg = (
                    "ℹ️ **No Refundable Balance Available.**\n\n"
                    "You currently have 0 Toman in unconsumed paid deposits. "
                    "Please note that system gift credits are promotional and non-refundable."
                )
            await event.reply(zero_msg, buttons=main_menu_keyboard(user_lang))
            return

        # RULE 2: DYNAMIC AML MONEY LAUNDERING COOLDOWN TIMER
        if not can_refund:
            cd_str = format_localized_number(cooldown_days, lang=user_lang)
            left_str = format_localized_number(days_left, lang=user_lang)

            if user_lang.startswith("fa"):
                cooldown_msg = (
                    f"🛑 **محدودیت دوره انتظار استرداد (قوانین ضد پولشویی)**\n\n"
                    f"جهت رعایت قوانین مالی و جلوگیری از تراکنش‌های مشکوک، هر کاربر در هر **{cd_str} روز** مجاز به ثبت یک درخواست استرداد می‌باشد.\n\n"
                    f"این بازه زمانی بر اساس تاریخ آخرین شارژ تایید شده یا آخرین استرداد شما محاسبه می‌گردد.\n"
                    f"⏳ **زمان باقیمانده تا امکان ثبت درخواست بعدی:** حدود **{left_str} روز** دیگر."
                )
            elif user_lang.startswith("ru"):
                cooldown_msg = (
                    f"🛑 **Период ожидания возврата активен (AML)**\n\n"
                    f"В целях безопасности и соблюдения правил против отмывания средств, запросы на возврат "
                    f"доступны не чаще одного раза в **{cd_str} дн.**\n\n"
                    f"Таймер отсчитывается от даты последнего пополнения или возврата.\n"
                    f"⏳ **Следующий возврат будет доступен через:** **{left_str} дн.**"
                )
            else:
                cooldown_msg = (
                    f"🛑 **Refund Cooldown Active (Anti-Money-Laundering Policy)**\n\n"
                    f"In accordance with financial compliance rules, users may submit one refund request every **{cd_str} days**.\n\n"
                    f"The cooldown is anchored to the validation date of your latest deposit or approved refund.\n"
                    f"⏳ **Time remaining until your next refund request:** ~**{left_str} day(s)**."
                )
            await event.reply(cooldown_msg, buttons=main_menu_keyboard(user_lang))
            return

        # RULE 4: REQUIREMENTS SATISFIED -> START STEP 1 (AMOUNT)
        avail_str = format_localized_number(avail, lang=user_lang)

        if user_lang.startswith("fa"):
            step1_msg = (
                f"💸 **ثبت درخواست استرداد وجه**\n\n"
                f"💰 **حداکثر اعتبار نقدی قابل استرداد:** `{avail_str} تومان`\n\n"
                f"🔢 **مرحله ۱ از ۴:** لطفاً مبلغ مورد نظر جهت استرداد را به **تومان** وارد فرمایید (فقط عدد):"
            )
        elif user_lang.startswith("ru"):
            step1_msg = (
                f"💸 **Запрос на возврат средств**\n\n"
                f"💰 **Доступно для возврата:** `{avail_str} Томан`\n\n"
                f"🔢 **Шаг 1 из 4:** Введите сумму, которую хотите вернуть, в **Томанах** (только цифры):"
            )
        else:
            step1_msg = (
                f"💸 **Submit Refund Request**\n\n"
                f"💰 **Available Refundable Balance:** `{avail_str} Toman`\n\n"
                f"🔢 **Step 1 of 4:** Enter the amount you wish to withdraw in **Toman** (Numbers only):"
            )

        await RedisFSM.set_state(telegram_id, "REFUND_WAITING_AMOUNT", {"available": avail})
        await event.reply(step1_msg, buttons=cancel_inline_keyboard("cancel_refund", lang=user_lang))

    # Universal Cancel FSM callback
    @bot.on(events.CallbackQuery(pattern=r"^fsm:cancel.*"))
    async def on_fsm_cancel(event):
        telegram_id = str(event.sender_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        await RedisFSM.clear_state(telegram_id)

        if user_lang.startswith("fa"):
            cancel_text = "❌ **عملیات لغو شد.**\nمی‌توانید از منوی زیر گزینه دیگری را انتخاب نمایید:"
        elif user_lang.startswith("ru"):
            cancel_text = "❌ **Операция отменена.**\nВы можете выбрать другое действие из меню ниже:"
        else:
            cancel_text = "❌ **Operation cancelled.**\nYou can select another option from the menu below:"

        await event.edit(cancel_text, buttons=None)
        await event.respond(
            "👇",
            buttons=main_menu_keyboard(user_lang)
        )

    # Universal Back to Main Menu callback
    @bot.on(events.CallbackQuery(pattern=r"^menu:main$"))
    async def on_menu_main_callback(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        username = await RedisFSM.get_username(telegram_id) or "User"

        await event.delete()
        from app.handlers.start import get_localized_welcome_back_message
        await event.respond(
            get_localized_welcome_back_message(username, user_lang),
            buttons=main_menu_keyboard(user_lang)
        )