# telegram-service/app/handlers/start.py
import re
import logging
import httpx
from telethon import events
from app.config import IAM_SERVICE_URL
from app.services.redis_client import RedisFSM
from app.services.api_client import BackendAPIClient
from app.keyboards import main_menu_keyboard, unlinked_welcome_keyboard

logger = logging.getLogger(__name__)


def extract_token_from_text(text: str) -> str:
    """
    Extracts token whether the user clicks /start tk_xxx,
    pastes the full link (https://t.me/Bot?start=tk_xxx),
    or pastes the raw token directly.
    """
    if not text:
        return ""
    clean = text.strip()
    match = re.search(r'(tk_[A-Za-z0-9_\-]+)', clean)
    if match:
        return match.group(1)
    return ""


def get_multilingual_help_overview() -> str:
    """
    Trilingual introductory overview and account linking guide displayed
    simultaneously in English, Farsi, and Russian.
    """
    return (
        "📖 **PAYG MNG Guide & Account Linking / راهنما و اتصال حساب / Руководство и привязка**\n\n"
        "────────────────────────────\n"
        "🇬🇧 **English:**\n"
        "Welcome! To connect your Telegram to your PAYG MNG profile:\n"
        "1. Log in to your **Web Dashboard**.\n"
        "2. Go to **Account Settings** → **Telegram Notifications**.\n"
        "3. Click **Connect Telegram** or copy your link token (`tk_...`).\n"
        "4. Send `/start tk_your_token_here` in this chat or paste the token.\n\n"
        "────────────────────────────\n"
        "🇮🇷 **فارسی:**\n"
        "خوش آمدید! جهت اتصال حساب تلگرام به پروفایل خود:\n"
        "۱. وارد **داشبورد کاربری** خود در وب‌سایت شوید.\n"
        "۲. به بخش **تنظیمات حساب کاربری** → **اعلانات تلگرام** بروید.\n"
        "۳. روی دکمه **اتصال به تلگرام** کلیک کرده یا توکن را کپی کنید.\n"
        "۴. عبارت `/start tk_your_token` را ارسال کرده یا توکن را در چت پیست نمایید.\n\n"
        "────────────────────────────\n"
        "🇷🇺 **Русский:**\n"
        "Добро пожаловать! Чтобы привязать Telegram к вашему аккаунту:\n"
        "1. Войдите в личный кабинет на **веб-панели**.\n"
        "2. Перейдите в **Настройки аккаунта** → **Telegram уведомления**.\n"
        "3. Нажмите **Подключить Telegram** или скопируйте токен.\n"
        "4. Отправьте `/start tk_ваш_токен` в этот чат или вставьте токен.\n\n"
        "────────────────────────────\n"
        "👇 **Select your language below for an extensive complete manual:**\n"
        "👇 **جهت مطالعه راهنمای جامع و مرحله‌به‌مرحله، زبان مورد نظر را انتخاب کنید:**\n"
        "👇 **Для подробного пошагового руководства выберите язык ниже:**"
    )


get_multilingual_unlinked_guidance = get_multilingual_help_overview


def get_extensive_english_guide() -> str:
    return (
        "📘 **Extensive User Guide & Operations Manual (English)**\n\n"
        "🔗 **1. Account Linking & Security:**\n"
        "• Once linked via `/start tk_...`, your Telegram chat is bound to your profile.\n"
        "• The bot mints short-lived asymmetric tokens (RS256) to query backend services securely.\n\n"
        "📊 **2. Status & Balance:**\n"
        "• Use `/status` or `/balance` to view your total usable credit, paid deposits, gift balance, active nodes, and your database language.\n"
        "• **Safety Cutoff:** If your usable balance reaches 0 Toman or drops below the safety threshold, network traffic routing is automatically paused until topped up.\n\n"
        "🌐 **3. Traffic Routing Configurations & Universal Subscription:**\n"
        "• Tap **My Configs** or use `/configs` to inspect each regional server (VLESS, XHTTP, Trojan).\n"
        "• **QR Code:** Tap **View QR Code** for any node to import directly into mobile apps.\n"
        "• **Copy Subscription Link:** Imports the universal `/sub/{token}` link into v2rayNG, Sing-box, or Shadowrocket, syncing active servers automatically.\n"
        "• **Copy All Configs Link:** Copies all raw connection links at once to your clipboard.\n\n"
        "🔄 **4. Route Replacement (Blocked Node Auto-Swap):**\n"
        "• If a server is filtered by DPI, tap **Replace Blocked Route** under that configuration.\n"
        "• The system checks cooldowns and unblocked capacity. You can select your mobile/home ISP (MCI, Irancell, Shatel, etc.) or type a custom one.\n"
        "• If an alternative SNI is available, the node is swapped immediately. If all regional routes are filtered, an emergency priority ticket is filed for administrators.\n\n"
        "💳 **5. Top-up & Deposit:**\n"
        "• Tap **Deposit / Top-up** or `/topup` to view Shetab bank cards and USDT deposit addresses.\n"
        "• Enter the amount (meeting minimum threshold), upload your payment screenshot, and optionally add a note.\n\n"
        "💸 **6. Refund Requests:**\n"
        "• Tap **Request Refund** or `/refund`. Only paid fiat credit is refundable (gift credit is non-refundable).\n"
        "• In accordance with AML rules, refunds are subject to the system cooldown timer.\n\n"
        "🎫 **7. Support & Helpdesk:**\n"
        "• Tap **Support Tickets** or `/support` to view tickets, read responses, reply, or open a new ticket (attaching an optional screenshot).\n"
        "• Unread staff replies are marked with 🔴 and cleared once viewed.\n\n"
        "⌨️ **Quick Commands Reference:**\n"
        "`/status` — Account balance and active nodes\n"
        "`/configs` — Manage servers, QR codes, and route replacement\n"
        "`/topup` — Deposit funds with transfer slip\n"
        "`/refund` — Submit refund request to Shetab card\n"
        "`/traffic` — 30-day bandwidth diagram & ISP distribution\n"
        "`/history` — Payment and refund transaction history\n"
        "`/financials` — 30-day top-up & gift trend chart\n"
        "`/support` — Open and manage helpdesk tickets\n"
        "`/language` — Change default language across bot & web\n"
        "`/help` — Open this guide"
    )


def get_extensive_persian_guide() -> str:
    return (
        "📘 **راهنمای جامع و کامل استفاده از سرویس (فارسی)**\n\n"
        "🔗 **۱. اتصال حساب کاربری و امنیت:**\n"
        "• پس از ارسال دستور `/start tk_...` حساب تلگرام شما به پروفایل کاربری متصل می‌گردد.\n"
        "• ارتباطات ربات با هسته سیستم توسط توکن‌های رمزنگاری شده نامتقارن (RS256) با امنیت کامل برقرار می‌شود.\n\n"
        "📊 **۲. وضعیت حساب و موجودی:**\n"
        "• با دستور `/status` یا دکمه **وضعیت و موجودی** می‌توانید اعتبار قابل استفاده، اعتبار نقدی، هدیه تشویقی، سرورهای فعال و زبان انتخابی خود را مشاهده فرمایید.\n"
        "• **قطع اضطراری:** در صورتی که موجودی قابل استفاده به صفر برسد، جهت جلوگیری از بدهی ناخواسته، ترافیک سرورها موقتاً غیرفعال می‌شود تا شارژ جدید انجام گردد.\n\n"
        "🌐 **۳. کانفیگ‌ها و لینک اشتراک هوشمند:**\n"
        "• با دستور `/configs` لیست سرورهای فعال در تمام لوکیشن‌ها (VLESS Reality، XHTTP و...) نمایش داده می‌شود.\n"
        "• **کد QR:** با انتخاب هر سرور می‌توانید QR Code آن را مستقیماً اسکن نمایید.\n"
        "• **لینک سابسکریپشن:** آدرس اشتراک هوشمند `/sub/{token}` را در v2rayNG، Sing-box، Streisand یا Shadowrocket وارد کنید تا با آپدیت خودکار همیشه به سرورهای فعال متصل باشید.\n"
        "• **کپی یکجای کانفیگ‌ها:** با لمس این دکمه، تمام لینک‌های مستقیم برای Import از کلیپ‌بورد در یک پیام آماده کپی قرار می‌گیرند.\n\n"
        "🔄 **۴. تعویض مسیر سرور فیلتر شده:**\n"
        "• در صورت اختلال در اتصال، گزینه **تعویض مسیر مسدود** را انتخاب فرمایید.\n"
        "• اپراتور اینترنت خود (همراه اول، ایرانسل، رایتل، مخابرات و...) را انتخاب کرده یا نام آن را تایپ کنید.\n"
        "• اگر سرور جایگزین با SNI متفاوت موجود باشد، مسیر در لحظه عوض شده و کانفیگ قبلی باطل می‌شود. در صورت تکمیل ظرفیت، تیکت اضطراری با اولویت بالا برای مدیران ارسال خواهد شد.\n\n"
        "💳 **۵. افزایش موجودی و شارژ کیف پول:**\n"
        "• با دستور `/topup` شماره کارت‌های شتاب و آدرس تتر نمایش داده می‌شود.\n"
        "• مبلغ مورد نظر را وارد کرده، اسکرین‌شات فیش واریزی را ارسال کنید و در صورت تمایل یادداشت بنویسید.\n\n"
        "💸 **۶. درخواست استرداد وجه:**\n"
        "• با دستور `/refund` می‌توانید مانده اعتبار نقدی واریز شده را استرداد کنید (اعتبارات هدیه غیرقابل بازگشت هستند).\n"
        "• با توجه به قوانین مالی ضد پولشویی، بین هر دو استرداد دوره انتظار تعیین‌شده توسط سیستم اعمال می‌گردد.\n\n"
        "🎫 **۷. تیکت‌های پشتیبانی:**\n"
        "• با دستور `/support` به بخش پشتیبانی متصل شده، پاسخ کارشناسان را مشاهده و تیکت جدید با امکان پیوست اختیاری عکس ثبت فرمایید.\n"
        "• تیکت‌های حاوی پاسخ جدید با علامت 🔴 مشخص شده و به محض مشاهده در تلگرام یا وب‌سایت خوانده تلقی می‌شوند.\n\n"
        "⌨️ **لیست دستورات سریع ربات:**\n"
        "`/status` — وضعیت حساب، موجودی و لوکیشن‌های فعال\n"
        "`/configs` — مدیریت کانفیگ‌ها، QR Code و تعویض سرور\n"
        "`/topup` — ثبت فیش واریزی و افزایش اعتبار\n"
        "`/refund` — درخواست استرداد مانده به کارت شتاب\n"
        "`/traffic` — نمودار مصرف ترافیک ۳۰ روزه و سهم اپراتورها\n"
        "`/history` — تاریخچه تراکنش‌های شارژ و استرداد\n"
        "`/financials` — نمودار روند واریزی‌ها و هدایای ۳۰ روزه\n"
        "`/support` — مدیریت تیکت‌ها و مکاتبه با پشتیبانی\n"
        "`/language` — تغییر زبان در کل سیستم و ربات\n"
        "`/help` — باز کردن مجدد این راهنما"
    )


def get_extensive_russian_guide() -> str:
    return (
        "📘 **Подробное руководство пользователя (Русский)**\n\n"
        "🔗 **1. Привязка аккаунта и безопасность:**\n"
        "• После отправки команды `/start tk_...` ваш Telegram связывается с аккаунтом.\n"
        "• Все вызовы к шлюзу подписываются защищенными токенами RS256 с коротким временем жизни.\n\n"
        "📊 **2. Статус и баланс:**\n"
        "• Команда `/status` или `/balance` отображает доступный остаток, оплаченный баланс, бонусный баланс, активные узлы и язык системы.\n"
        "• **Автоотключение:** При нулевом доступном балансе доступ к серверам временно приостанавливается до пополнения.\n\n"
        "🌐 **3. Конфигурации и единая подписка:**\n"
        "• Нажмите **Мои Конфигурации** или введите `/configs` для просмотра серверов (VLESS Reality, XHTTP, Trojan).\n"
        "• **QR-код:** Выберите узел и нажмите **Показать QR-код** для быстрого сканирования в приложении.\n"
        "• **Ссылка подписки:** Добавьте URL `/sub/{token}` в v2rayNG, Sing-box или Shadowrocket для автообновления всех рабочих узлов.\n"
        "• **Скопировать все ссылки:** Копирует все прямые ссылки узлов в буфер обмена одним кликом.\n\n"
        "🔄 **4. Замена заблокированного маршрута:**\n"
        "• Если сервер блокируется провайдером, нажмите **Заменить заблокированный маршрут**.\n"
        "• Выберите вашего интернет-провайдера из списка или введите название вручную.\n"
        "• При наличии свободного узла с другим SNI замена происходит мгновенно. Если свободных узлов нет, автоматически создается срочный тикет в поддержку.\n\n"
        "💳 **5. Пополнение счета:**\n"
        "• Введите `/topup` для просмотра реквизитов (банковские карты и USDT).\n"
        "• Введите сумму, отправьте скриншот чека и при желании добавьте комментарий.\n\n"
        "💸 **6. Запрос на возврат средств:**\n"
        "• Введите `/refund` для оформления возврата оплаченных средств (бонусные баллы возврату не подлежат).\n"
        "• В соответствии с политикой AML действует период ожидания между возвратами.\n\n"
        "🎫 **7. Служба поддержки:**\n"
        "• Введите `/support` для просмотра тикетов, ответов операторов и создания обращений с возможностью прикрепления фото.\n"
        "• Новые сообщения помечаются значком 🔴 и автоматически отмечаются как прочитанные при открытии.\n\n"
        "⌨️ **Быстрые команды бота:**\n"
        "`/status` — Баланс, статус и активные узлы\n"
        "`/configs` — Конфигурации, QR-коды и замена маршрутов\n"
        "`/topup` — Пополнение баланса с чеком\n"
        "`/refund` — Запрос на возврат на банковскую карту\n"
        "`/traffic` — График расхода трафика за 30 дней и статистика по провайдерам\n"
        "`/history` — История пополнений и возвратов\n"
        "`/financials` — График пополнений и бонусов за 30 дней\n"
        "`/support` — Управление обращениями в поддержку\n"
        "`/language` — Смена языка в боте и на веб-панели\n"
        "`/help` — Открыть это руководство"
    )


def get_localized_link_success_message(username: str, lang: str, sub_url: str = "") -> str:
    """Returns onboarding confirmation message including the personal 1-tap subscription link."""
    lang_clean = (lang or "en").strip().lower()

    sub_section_fa = (
        f"\n\n📋 **لینک اشتراک هوشمند و اختصاصی شما:**\n"
        f"`{sub_url}`\n\n"
        f"🚀 **راهنمای سریع اتصال (در ۱ مرحله):**\n"
        f"۱. لینک بالا را کپی کنید (با یک بار لمس کپی می‌شود).\n"
        f"۲. برنامه **v2rayNG**، **Sing-box**، **Streisand** یا **Shadowrocket** را باز کنید.\n"
        f"۳. لینک را در بخش **Subscription (اشتراک)** وارد کرده و دکمه **Update Subscription** را بزنید تا تمام سرورهای فعال خودکار اضافه شوند!\n"
    ) if sub_url else ""

    sub_section_ru = (
        f"\n\n📋 **Ваша персональная ссылка подписки:**\n"
        f"`{sub_url}`\n\n"
        f"🚀 **Быстрое подключение (в 1 клик):**\n"
        f"1. Скопируйте ссылку выше (нажмите на блок текста для копирования).\n"
        f"2. Откройте **v2rayNG**, **Sing-box**, **Streisand** или **Shadowrocket**.\n"
        f"3. Добавьте как **Подписку** и нажмите **Обновить подписку** для загрузки всех серверов!\n"
    ) if sub_url else ""

    sub_section_en = (
        f"\n\n📋 **Your Universal Auto-Sync Subscription Link:**\n"
        f"`{sub_url}`\n\n"
        f"🚀 **Quick Setup (1-Tap Import):**\n"
        f"1. Copy the link above (tap the code block to copy).\n"
        f"2. Open **v2rayNG** / **Sing-box** / **Shadowrocket** / **Streisand**.\n"
        f"3. Add as **Subscription** and tap **Update Subscription**. All active server nodes will load automatically!\n"
    ) if sub_url else ""

    if lang_clean.startswith("fa"):
        return (
            f"🎉 **حساب کاربری شما با موفقیت متصل شد!**\n\n"
            f"خوش آمدید، **@{username}** عزیز!\n"
            f"اکانت تلگرام شما با موفقیت به پنل متصل گردید."
            f"{sub_section_fa}\n"
            f"از طریق دکمه‌های منوی زیر می‌توانید در هر زمان موجودی را بررسی، سرورها را تعویض یا کیف پول خود را شارژ فرمایید."
        )
    elif lang_clean.startswith("ru"):
        return (
            f"🎉 **Аккаунт успешно привязан!**\n\n"
            f"Добро пожаловать, **@{username}**!\n"
            f"Ваш профиль Telegram успешно подключен к сервису."
            f"{sub_section_ru}\n"
            f"С помощью кнопок меню ниже вы можете проверять баланс, управлять узлами и пополнять счет."
        )
    else:
        return (
            f"🎉 **Account Linked Successfully!**\n\n"
            f"Welcome, **@{username}**!\n"
            f"Your Telegram account is now bound to your PAYG MNG profile."
            f"{sub_section_en}\n"
            f"Use the menu below to check your balance, manage server configs, or top up your wallet anytime."
        )


def get_localized_welcome_back_message(username: str, lang: str) -> str:
    """Returns welcome-back message in the user's selected language."""
    lang_clean = (lang or "en").strip().lower()

    if lang_clean.startswith("fa"):
        return (
            f"👋 **خوش آمدید، @{username}!**\n\n"
            f"از منوی زیر می‌توانید وضعیت سرویس، موجودی و کانفیگ‌های فعال خود را مدیریت کنید."
        )
    elif lang_clean.startswith("ru"):
        return (
            f"👋 **С возвращением, @{username}!**\n\n"
            f"Используйте кнопки меню ниже для управления подпиской и серверами."
        )
    else:
        return (
            f"👋 **Welcome back, @{username}!**\n\n"
            f"Use the buttons below to manage your subscription and Traffic Routing nodes."
        )


async def link_account_with_token(event, token: str, telegram_id: str):
    await event.reply("⏳ Connecting your Telegram account to your PAYG MNG profile...")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.post(
                f"{IAM_SERVICE_URL}/users/telegram/link",
                json={"token": token, "telegram_id": telegram_id}
            )
            if res.status_code == 200:
                data = res.json()
                user_id = str(data["user_id"])
                username = data.get("username", "Member")

                # Fetch user language preference from IAM
                api = BackendAPIClient(user_id, username)
                profile = await api.get_user_profile()
                user_language = "en"
                if profile and profile.get("language"):
                    user_language = profile["language"].strip().lower()

                user_currency = str(profile.get("billing_currency", "IRT") if profile else "IRT").upper()

                # Save user mapping, language, and immutable billing currency in Redis
                await RedisFSM.set_user_mapping(
                    telegram_id, 
                    user_id, 
                    username, 
                    language=user_language, 
                    currency=user_currency
                )

                # Retrieve universal subscription link directly for 1-tap onboarding
                full_sub_url = ""
                try:
                    configs_data = await api.get_my_configs()
                    if configs_data and configs_data.get("subscription_url"):
                        full_sub_url = f"{api.base_url}{configs_data['subscription_url']}"
                except Exception as sub_err:
                    logger.warning(f"Could not fetch subscription url for user {user_id}: {sub_err}")

                # Send localized onboarding greeting containing the specific subscription link
                greeting = get_localized_link_success_message(username, user_language, sub_url=full_sub_url)
                await event.reply(greeting, buttons=main_menu_keyboard(user_language))
            else:
                detail = res.json().get("detail", "Invalid or expired link token.")
                await event.reply(
                    f"⚠️ **Linking Failed:** {detail}\n\n"
                    f"Please log in to your web portal, go to **Account Settings**, and generate a fresh link."
                )
        except Exception as e:
            logger.error(f"Error linking account: {e}")
            await event.reply("⚠️ **Connection Error:** Could not contact identity service. Please try again shortly.")


async def register_start_handler(bot):
    @bot.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
    async def on_start_command(event):
        telegram_id = str(event.sender_id)
        raw_payload = event.pattern_match.group(1)
        token = extract_token_from_text(raw_payload) if raw_payload else ""

        # Case 1: Token provided directly in /start payload
        if token:
            await link_account_with_token(event, token, telegram_id)
            return

        # Case 2: User already linked from the past
        user_id = await RedisFSM.get_user_id(telegram_id)
        if user_id:
            username = await RedisFSM.get_username(telegram_id) or "Member"

            # Check cached language or refresh from IAM profile
            user_language = await RedisFSM.get_language(telegram_id)
            api = BackendAPIClient(user_id, username)
            profile = await api.get_user_profile()
            if profile and profile.get("language"):
                user_language = profile["language"].strip().lower()
                await RedisFSM.set_language(telegram_id, user_language)

            welcome_msg = get_localized_welcome_back_message(username, user_language)
            await event.reply(welcome_msg, buttons=main_menu_keyboard(user_language))
            return

        # Case 3: Unlinked user and no token supplied -> Show trilingual overview with language selector buttons
        from app.keyboards import help_language_keyboard
        await event.reply(
            get_multilingual_help_overview(),
            buttons=help_language_keyboard()
        )

    # Catch token if user pastes raw string or URL without typing /start
    @bot.on(events.NewMessage(pattern=r"^(?:https?://t\.me/[^\s]+\?start=)?(tk_[A-Za-z0-9_\-]+)$"))
    async def on_paste_token(event):
        telegram_id = str(event.sender_id)
        token = event.pattern_match.group(1)
        await link_account_with_token(event, token, telegram_id)

    # =========================================================================
    # HELP & LINK COMMAND HANDLER (TRIGGERS TRILINGUAL OVERVIEW)
    # =========================================================================
    HELP_COMMAND_TRIGGERS = [
        "/help",
        "❓ Help & Link Account",
        "❓ راهنما و اتصال حساب",
        "❓ Помощь и Привязка",
        "❓ Help & Link / راهنما و اتصال / Помощь"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in HELP_COMMAND_TRIGGERS))
    async def on_help_command(event):
        from app.keyboards import help_language_keyboard
        await event.reply(
            get_multilingual_help_overview(),
            buttons=help_language_keyboard()
        )

    # =========================================================================
    # EXTENSIVE HELP CALLBACKS (LANGUAGE-SPECIFIC DETAILED MANUALS)
    # =========================================================================
    @bot.on(events.CallbackQuery(pattern=r"^help:guide:(en|fa|ru)$"))
    async def on_cb_help_guide_language(event):
        chosen_lang = event.pattern_match.group(1)
        from app.keyboards import help_back_keyboard

        if chosen_lang == "fa":
            guide_text = get_extensive_persian_guide()
        elif chosen_lang == "ru":
            guide_text = get_extensive_russian_guide()
        else:
            guide_text = get_extensive_english_guide()

        await event.edit(guide_text, buttons=help_back_keyboard(lang=chosen_lang))

    @bot.on(events.CallbackQuery(pattern=r"^help:main$"))
    async def on_cb_help_main_overview(event):
        from app.keyboards import help_language_keyboard
        await event.edit(
            get_multilingual_help_overview(),
            buttons=help_language_keyboard()
        )

    # =========================================================================
    # LANGUAGE SELECTION COMMAND & HANDLER
    # =========================================================================
    LANG_COMMAND_TRIGGERS = [
        "/language", "/lang",
        "🌐 Change Language",
        "🌐 تغییر زبان",
        "🌐 Сменить язык"
    ]

    @bot.on(events.NewMessage(func=lambda e: e.text in LANG_COMMAND_TRIGGERS))
    async def on_language_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply(
                get_multilingual_unlinked_guidance(),
                buttons=unlinked_welcome_keyboard()
            )
            return

        from app.keyboards import language_selection_keyboard

        trilingual_prompt = (
            "🌐 **Select Your Language / انتخاب زبان / Выберите язык**\n\n"
            "🇬🇧 Please choose your preferred language for the bot and web dashboard.\n"
            "🇮🇷 لطفاً زبان مورد نظر خود را برای ربات و پنل کاربری وب انتخاب کنید.\n"
            "🇷🇺 Пожалуйста, выберите язык для бота и веб-панели управления."
        )

        await event.reply(trilingual_prompt, buttons=language_selection_keyboard())

    @bot.on(events.CallbackQuery(pattern=r"^lang:set:(en|fa|ru)$"))
    async def on_language_selected_callback(event):
        telegram_id = str(event.sender_id)
        chosen_lang = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)

        if not user_id:
            await event.answer("Account not linked.", alert=True)
            return

        username = await RedisFSM.get_username(telegram_id) or "Member"
        api = BackendAPIClient(user_id, username)

        # 1. Update in IAM database (syncs web dashboard as well)
        await api.update_user_language(chosen_lang)

        # 2. Update in Redis cache
        await RedisFSM.set_language(telegram_id, chosen_lang)

        # 3. Localized confirmation messages
        if chosen_lang == "fa":
            confirm_text = (
                "✅ **زبان پیش‌فرض با موفقیت به فارسی تغییر یافت.**\n\n"
                "از این پس تمامی پیام‌های ربات و پنل وب‌سایت به زبان فارسی نمایش داده خواهند شد."
            )
        elif chosen_lang == "ru":
            confirm_text = (
                "✅ **Язык по умолчанию успешно изменен на русский.**\n\n"
                "Теперь все сообщения бота и веб-панели управления будут отображаться на русском языке."
            )
        else:
            confirm_text = (
                "✅ **Default language successfully changed to English.**\n\n"
                "All future bot messages and web dashboard views will be displayed in English."
            )

        await event.edit(confirm_text, buttons=None)

        # 4. Push updated main menu reply keyboard
        await event.respond(
            get_localized_welcome_back_message(username, chosen_lang),
            buttons=main_menu_keyboard(chosen_lang)
        )