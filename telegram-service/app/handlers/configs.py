# telegram-service/app/handlers/configs.py
import io
import logging
import qrcode
from telethon import events, Button
from app.services.redis_client import RedisFSM
from app.services.api_client import BackendAPIClient
from app.keyboards import (
    configs_list_keyboard,
    config_actions_keyboard,
    isp_selection_keyboard,
    main_menu_keyboard
)
from app.handlers.balance import format_localized_number, format_bytes_human

logger = logging.getLogger(__name__)


def build_localized_configs_list_message(configs: list, lang: str = "en") -> str:
    """Constructs the configs list message header and items localized in user language."""
    lang_clean = (lang or "en").strip().lower()
    total_str = format_localized_number(len(configs), lang=lang)

    if lang_clean.startswith("fa"):
        msg = f"🌐 **سرورها و کانفیگ‌های فعال شما ({total_str} لوکیشن)**\n\n"
        for c in configs:
            loc = c.get("location", "لوکیشن")
            proto = c.get("protocol", "VLESS")
            used = format_bytes_human(c.get("data_used_bytes", 0), lang=lang)
            rate = format_localized_number(c.get("price_per_gb_toman", 1000), lang=lang)

            msg += f"📍 **{loc}** ({proto})\n"
            msg += f"   • تعرفه: `{rate} تومان/گیگ` | مصرف: `{used}`\n\n"

        msg += "برای دریافت QR Code، کپی لینک مستقیم یا تعویض مسیر سرور روی دکمه مدیریت آن کلیک کنید."
    elif lang_clean.startswith("ru"):
        msg = f"🌐 **Ваши активные конфигурации ({total_str})**\n\n"
        for c in configs:
            loc = c.get("location", "Регион")
            proto = c.get("protocol", "VLESS")
            used = format_bytes_human(c.get("data_used_bytes", 0), lang=lang)
            rate = format_localized_number(c.get("price_per_gb_toman", 1000), lang=lang)

            msg += f"📍 **{loc}** ({proto})\n"
            msg += f"   • Тариф: `{rate} Томан/ГБ` | Израсходовано: `{used}`\n\n"

        msg += "Выберите узел ниже для управления, просмотра QR-кода или замены маршрута."
    else:
        user_curr = configs[0].get("currency", "IRT") if configs else "IRT"
        unit_str = "$/GB" if user_curr == "USD" else "Toman/GB"
        msg = f"🌐 **Your Active Traffic Routing Nodes ({total_str})**\n\n"
        for c in configs:
            loc = c.get("location", "Region")
            proto = c.get("protocol", "VLESS")
            used = format_bytes_human(c.get("data_used_bytes", 0), lang=lang)
            rate_val = c.get("price_per_gb", c.get("price_per_gb_toman", 1000))
            rate = f"{rate_val:.2f}" if user_curr == "USD" else format_localized_number(rate_val, lang=lang)

            msg += f"📍 **{loc}** ({proto})\n"
            msg += f"   • Rate: `{rate} {unit_str}` | Used: `{used}`\n\n"

        msg += "Tap on any configuration below to view QR code, copy connection URI, or replace route."

    return msg


async def register_configs_handlers(bot):
    CONFIGS_BUTTONS = [
        "🌐 My Configs", "/configs",
        "🌐 کانفیگ‌های من",
        "🌐 Мои Конфигурации"
    ]

    # ==========================================
    # 1. LIST CONFIGURATIONS COMMAND
    # ==========================================
    @bot.on(events.NewMessage(func=lambda e: e.text in CONFIGS_BUTTONS))
    async def on_configs_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Please connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        username = await RedisFSM.get_username(telegram_id) or "User"
        api = BackendAPIClient(user_id, username)
        data = await api.get_my_configs()

        configs = data.get("configs", []) if data else []

        if not configs:
            if user_lang.startswith("fa"):
                empty_msg = (
                    "ℹ️ **هیچ کانفیگ فعالی برای حساب شما یافت نشد.**\n\n"
                    "اگر موجودی کیف پول شما به اتمام رسیده است، لطفاً نسبت به افزایش موجودی اقدام نمایید تا کانفیگ‌های شما مجدداً فعال شوند."
                )
            elif user_lang.startswith("ru"):
                empty_msg = (
                    "ℹ️ **Активные Traffic Routing-конфигурации не найдены.**\n\n"
                    "Если баланс исчерпан, пополните счет для активации серверных узлов."
                )
            else:
                empty_msg = (
                    "ℹ️ **No Active Traffic Routing Configurations Found.**\n\n"
                    "If your wallet balance is empty, top up to activate your routes."
                )
            await event.reply(empty_msg, buttons=main_menu_keyboard(user_lang))
            return

        msg_text = build_localized_configs_list_message(configs, lang=user_lang)
        await event.reply(msg_text, buttons=configs_list_keyboard(configs, lang=user_lang))

    # Callback for returning to list
    @bot.on(events.CallbackQuery(pattern=r"^cfg:list$"))
    async def on_cb_list_configs(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        configs = data.get("configs", []) if data else []
        if not configs:
            await event.edit("ℹ️ No active configs available.")
            return

        msg_text = build_localized_configs_list_message(configs, lang=user_lang)
        await event.edit(msg_text, buttons=configs_list_keyboard(configs, lang=user_lang))

    # ==========================================
    # 2. VIEW CONFIG DETAILS (SUBMENU)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:view:([A-Za-z0-9\-]+)$"))
    async def on_cb_view_config(event):
        telegram_id = str(event.sender_id)
        config_id = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        target = next((c for c in (data.get("configs", []) if data else []) if str(c.get("id")) == config_id), None)
        if not target:
            err_not_found = (
                "⚠️ این کانفیگ یافت نشد یا منقضی شده است."
                if user_lang.startswith("fa")
                else "⚠️ Конфигурация не найдена или была удалена."
                if user_lang.startswith("ru")
                else "⚠️ Configuration not found or has been revoked."
            )
            await event.edit(err_not_found, buttons=[[Button.inline("🔙 Back", data="cfg:list")]])
            return

        loc = target.get("location", "Region")
        proto = target.get("protocol", "VLESS")
        name = target.get("server_name", "Node")
        used = format_bytes_human(target.get("data_used_bytes", 0), lang=user_lang)
        rate = format_localized_number(target.get("price_per_gb_toman", 1000), lang=user_lang)

        if user_lang.startswith("fa"):
            msg = (
                f"📍 **جزئیات کانفیگ: {loc}**\n\n"
                f"• **نام سرور:** `{name}`\n"
                f"• **پروتکل ارتباطی:** `{proto}`\n"
                f"• **نرخ مصرف:** `{rate} تومان/گیگ`\n"
                f"• **میزان ترافیک مصرفی:** `{used}`\n\n"
                f"جهت استفاده در نرم‌افزارهای v2rayNG، Streisand یا Shadowrocket، بر روی **کپی لینک** یا **مشاهده QR Code** کلیک کنید.\n"
                f"در صورت قطعی یا فیلتر شدن این سرور، دکمه **تعویض مسیر مسدود** را انتخاب فرمایید."
            )
        elif user_lang.startswith("ru"):
            msg = (
                f"📍 **Сведения о конфигурации: {loc}**\n\n"
                f"• **Сервер:** `{name}`\n"
                f"• **Протокол:** `{proto}`\n"
                f"• **Тариф:** `{rate} Томан/ГБ`\n"
                f"• **Использованный трафик:** `{used}`\n\n"
                f"Нажмите **Скопировать ссылку** или **Показать QR-код** для импорта в приложение.\n"
                f"Если узел заблокирован или недоступен, нажмите **Заменить заблокированный маршрут**."
            )
        else:
            msg = (
                f"📍 **Config Detail: {loc}**\n\n"
                f"• **Name:** `{name}`\n"
                f"• **Protocol:** `{proto}`\n"
                f"• **Rate:** `{rate} Toman/GB`\n"
                f"• **Bandwidth Consumed:** `{used}`\n\n"
                f"Tap **Copy URI** or **View QR Code** to import into v2rayNG / Streisand / Shadowrocket.\n"
                f"If this server is unreachable or filtered, tap **Replace Blocked Route**."
            )

        await event.edit(msg, buttons=config_actions_keyboard(config_id, lang=user_lang))

    # ==========================================
    # 3. COPY SINGLE CONFIG URI
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:copy:([A-Za-z0-9\-]+)$"))
    async def on_cb_copy_uri(event):
        telegram_id = str(event.sender_id)
        config_id = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        target = next((c for c in (data.get("configs", []) if data else []) if str(c.get("id")) == config_id), None)
        if target and target.get("uri"):
            loc = target.get("location", "Node")
            if user_lang.startswith("fa"):
                uri_msg = f"📋 **لینک اتصال مستقیم سرور {loc}:**\n\n`{target.get('uri')}`"
                copied_toast = "لینک کپی شد!"
            elif user_lang.startswith("ru"):
                uri_msg = f"📋 **Прямая ссылка для {loc}:**\n\n`{target.get('uri')}`"
                copied_toast = "Ссылка скопирована!"
            else:
                uri_msg = f"📋 **Connection URI for {loc}:**\n\n`{target.get('uri')}`"
                copied_toast = "URI Copied to chat!"

            await event.respond(uri_msg)
            await event.answer(copied_toast)
        else:
            await event.answer("URI not available.", alert=True)

    # ==========================================
    # 4. VIEW QR CODE
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:qr:([A-Za-z0-9\-]+)$"))
    async def on_cb_qr_code(event):
        telegram_id = str(event.sender_id)
        config_id = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        target = next((c for c in (data.get("configs", []) if data else []) if str(c.get("id")) == config_id), None)
        if not target or not target.get("uri"):
            await event.answer("URI not available.", alert=True)
            return

        uri = target.get("uri")
        img = qrcode.make(uri)
        bio = io.BytesIO()
        img.save(bio, "PNG")
        bio.seek(0)
        bio.name = f"qr_{target.get('location', 'node')}.png"

        loc = target.get("location", "Region")
        proto = target.get("protocol", "VLESS")

        if user_lang.startswith("fa"):
            caption = f"📱 **کد QR برای سرور {loc} ({proto})**\nجهت اتصال، این تصویر را با v2rayNG یا Shadowrocket اسکن نمایید."
        elif user_lang.startswith("ru"):
            caption = f"📱 **QR-код для сервера {loc} ({proto})**\nОтсканируйте в приложении v2rayNG или Shadowrocket."
        else:
            caption = f"📱 **QR Code for {loc} ({proto})**\nScan with v2rayNG, Streisand, or Shadowrocket."

        await event.respond(file=bio, message=caption)
        await event.answer()

    # ==========================================
    # 5. COPY SUBSCRIPTION LINK
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:sub_link$"))
    async def on_cb_sub_link(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        if data and data.get("subscription_url"):
            sub_path = data.get("subscription_url")
            full_sub_url = f"{api.base_url}{sub_path}"

            if user_lang.startswith("fa"):
                sub_msg = (
                    f"📋 **لینک اشتراک هوشمند و یکپارچه (Subscription URL):**\n\n"
                    f"`{full_sub_url}`\n\n"
                    f"این لینک را در نرم‌افزارهای v2rayNG، Sing-Box، Streisand یا Shadowrocket وارد کرده و دکمه بروزرسانی اشتراک را بزنید تا تمام سرورهای فعال به صورت خودکار به لیست شما اضافه شوند."
                )
            elif user_lang.startswith("ru"):
                sub_msg = (
                    f"📋 **Универсальная ссылка на подписку (Subscription URL):**\n\n"
                    f"`{full_sub_url}`\n\n"
                    f"Добавьте эту ссылку в v2rayNG, Sing-Box или Shadowrocket для автоматической синхронизации всех рабочих серверов."
                )
            else:
                sub_msg = (
                    f"📋 **Universal V2Ray Subscription URL:**\n\n"
                    f"`{full_sub_url}`\n\n"
                    f"Import this link into v2rayNG, Sing-Box, Streisand, or Shadowrocket to sync all your active server nodes automatically."
                )

            await event.respond(sub_msg)
            await event.answer()

    # ==========================================
    # 6. COPY ALL CONFIGS DIRECT LINKS (NEW FEATURE)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:copy_all$"))
    async def on_cb_copy_all_configs(event):
        """
        Gathers all raw connection URIs from all active configs and presents
        them in a single code block for 1-tap copying into any Traffic Routing client.
        """
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        data = await api.get_my_configs()

        configs = data.get("configs", []) if data else []
        all_uris = [c.get("uri").strip() for c in configs if c.get("uri") and c.get("uri").strip()]

        if not all_uris:
            if user_lang.startswith("fa"):
                await event.answer("هیچ لینک فعالی یافت نشد.", alert=True)
            elif user_lang.startswith("ru"):
                await event.answer("Нет активных ссылок.", alert=True)
            else:
                await event.answer("No active config links available.", alert=True)
            return

        joined_uris = "\n".join(all_uris)
        total_str = format_localized_number(len(all_uris), lang=user_lang)

        if user_lang.startswith("fa"):
            copy_all_msg = (
                f"📑 **لینک مستقیم تمام کانفیگ‌های فعال ({total_str} سرور):**\n\n"
                f"با یک بار لمس یا کلیک بر روی باکس زیر، تمامی لینک‌ها به صورت یکجا کپی می‌شوند و می‌توانید آنها را در کلیپ‌بورد نرم‌افزار خود Import نمایید:\n\n"
                f"```{joined_uris}```"
            )
            toast_text = "تمامی لینک‌ها آماده کپی هستند!"
        elif user_lang.startswith("ru"):
            copy_all_msg = (
                f"📑 **Прямые ссылки на все активные серверы ({total_str}):**\n\n"
                f"Нажмите на блок ниже, чтобы скопировать все ссылки сразу для импорта из буфера обмена:\n\n"
                f"```{joined_uris}```"
            )
            toast_text = "Все ссылки готовы к копированию!"
        else:
            copy_all_msg = (
                f"📑 **Direct URIs for All Active Configurations ({total_str}):**\n\n"
                f"Tap or click the code block below to copy all links at once for direct clipboard import:\n\n"
                f"```{joined_uris}```"
            )
            toast_text = "All config URIs ready to copy!"

        await event.respond(copy_all_msg)
        await event.answer(toast_text)

    # ==========================================
    # 7. ROUTE REPLACEMENT FLOW (WITH POLICY & COOLDOWN VALIDATIONS)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^cfg:replace:([A-Za-z0-9\-]+)$"))
    async def on_cb_replace_route(event):
        telegram_id = str(event.sender_id)
        config_id = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        wait_text = (
            "⏳ در حال بررسی شرایط و اعتبارسنجی محدودیت تعویض مسیر..."
            if user_lang.startswith("fa")
            else "⏳ Проверка условий и ограничений на замену маршрута..."
            if user_lang.startswith("ru")
            else "⏳ Checking route replacement limits and server policies..."
        )
        await event.answer(wait_text, alert=False)

        # 1. RUN PRE-CHECK: Policy, Cooldown interval, and Node capacity
        check = await api.check_replace_status(config_id)

        can_auto = bool(check.get("can_auto_replace", False))
        requires_ticket = bool(check.get("requires_support_ticket", False))
        loc = check.get("location_name", "این منطقه" if user_lang.startswith("fa") else "this region")
        reason_code = check.get("reason")
        hours_left = check.get("remaining_hours", 0)
        cooldown_days = check.get("cooldown_days", 0)

        # Localize hours and days
        hours_left_str = format_localized_number(hours_left, lang=user_lang)
        cooldown_days_str = format_localized_number(cooldown_days, lang=user_lang)

        # Case A: Replacement NOT allowed directly (cooldown, admin disabled, or no unblocked capacity)
        if not can_auto or requires_ticket:
            if reason_code == "limit_exceeded":
                if user_lang.startswith("fa"):
                    status_explanation = (
                        f"🛑 **محدودیت تعداد درخواست تعویض مسیر فعال است.**\n\n"
                        f"بر اساس تنظیمات سیستم، شما در هر **{cooldown_days_str} روز** مجاز به یک بار تعویض مسیر خودکار در لوکیشن **{loc}** هستید.\n"
                        f"⏳ **زمان باقیمانده تا امکان تعویض خودکار بعدی:** حدود **{hours_left_str} ساعت** دیگر.\n\n"
                        f"با انتخاب اپراتور اینترنت خود، یک **تیکت اضطراری با اولویت بالا** برای تیم فنی ارسال می‌شود تا مسیر جدیدی را برای شما فعال کنند."
                    )
                elif user_lang.startswith("ru"):
                    status_explanation = (
                        f"🛑 **Лимит на замену маршрута исчерпан.**\n\n"
                        f"Согласно правилам сервиса, автозамена в регионе **{loc}** доступна 1 раз в **{cooldown_days_str} дн.**\n"
                        f"⏳ **До следующей доступной автозамены осталось:** около **{hours_left_str} ч.**\n\n"
                        f"Выберите вашего провайдера ниже, и система автоматически создаст **срочный запрос в техподдержку** для назначения рабочего узла."
                    )
                else:
                    status_explanation = (
                        f"🛑 **Route Replacement Request Limit Reached.**\n\n"
                        f"According to policy, you can auto-replace your route in **{loc}** once every **{cooldown_days_str} days**.\n"
                        f"⏳ **Next auto-replacement available in:** ~**{hours_left_str} hours**.\n\n"
                        f"If your route is filtered, select your ISP below. An **emergency support ticket** will be automatically created for administrators to assign you an unblocked route."
                    )
            elif reason_code == "admin_disabled":
                if user_lang.startswith("fa"):
                    status_explanation = (
                        f"🛑 **تعویض خودکار مسیر توسط مدیریت غیرفعال شده است.**\n\n"
                        f"با انتخاب اپراتور خود، سیستم به صورت مستقیم یک تیکت اولویت‌دار جهت تخصیص سرور باز خواهد کرد."
                    )
                elif user_lang.startswith("ru"):
                    status_explanation = (
                        f"🛑 **Автозамена маршрута временно отключена администрацией.**\n\n"
                        f"При выборе провайдера система автоматически создаст тикет с высоким приоритетом."
                    )
                else:
                    status_explanation = (
                        f"🛑 **Self-service route replacement is currently disabled by administrators.**\n\n"
                        f"By selecting your ISP, an urgent ticket will be filed for manual assignment."
                    )
            else:  # no alternative unblocked route with a different SNI currently has capacity
                if user_lang.startswith("fa"):
                    status_explanation = (
                        f"⚠️ **تمامی سرورها و مسیرهای جایگزین در لوکیشن {loc} مسدود یا فاقد ظرفیت هستند.**\n\n"
                        f"کانفیگ فعلی شما همچنان فعال نگه داشته می‌شود و با انتخاب اپراتور، تیکت فوری جهت اضافه کردن سرور جدید برای مدیریت ارسال خواهد شد."
                    )
                elif user_lang.startswith("ru"):
                    status_explanation = (
                        f"⚠️ **Нет свободных альтернативных маршрутов с другим SNI в регионе {loc}.**\n\n"
                        f"Ваша текущая конфигурация остается активной. Выберите провайдера для создания срочного тикета на добавление нового узла."
                    )
                else:
                    status_explanation = (
                        f"⚠️ **No alternative routes with a different SNI currently have capacity in {loc}.**\n\n"
                        f"Your current configuration is kept active. Select your ISP to file an urgent ticket for administrator provisioning."
                    )

            prompt_header = (
                f"{status_explanation}\n\n"
                f"📶 **لطفاً اپراتور اینترنت خود را مشخص فرمایید:**"
                if user_lang.startswith("fa")
                else f"{status_explanation}\n\n"
                f"📶 **Пожалуйста, укажите вашего интернет-провайдера:**"
                if user_lang.startswith("ru")
                else f"{status_explanation}\n\n"
                f"📶 **Please select or enter your Internet Service Provider (ISP):**"
            )

        # Case B: Replacement IS allowed directly!
        else:
            if user_lang.startswith("fa"):
                prompt_header = (
                    f"✅ **بررسی با موفقیت انجام شد: تعویض مسیر در لوکیشن {loc} مجاز است.**\n\n"
                    f"یک مسیر جایگزین با هاست و اس‌ان‌آی (SNI) متفاوت و ظرفیت آزاد شناسایی گردید.\n\n"
                    f"📶 **لطفاً اپراتور اینترنت خود را انتخاب نمایید تا بهترین مسیر بهینه فعال شود:**"
                )
            elif user_lang.startswith("ru"):
                prompt_header = (
                    f"✅ **Проверка успешна: замена маршрута в регионе {loc} разрешена.**\n\n"
                    f"Найден рабочий альтернативный узел с другим SNI и свободной емкостью.\n\n"
                    f"📶 **Выберите вашего провайдера для оптимизации маршрута:**"
                )
            else:
                prompt_header = (
                    f"✅ **Validation successful: Route replacement is available for {loc}.**\n\n"
                    f"An alternative server node with a different SNI and available capacity was found.\n\n"
                    f"📶 **Please select your current ISP so the optimal route is assigned:**"
                )

        await event.edit(prompt_header, buttons=isp_selection_keyboard(config_id, lang=user_lang))

    # Trigger custom ISP text input
    @bot.on(events.CallbackQuery(pattern=r"^isp_custom:([A-Za-z0-9\-]+)$"))
    async def on_cb_isp_custom_prompt(event):
        telegram_id = str(event.sender_id)
        config_id = event.pattern_match.group(1)
        user_lang = await RedisFSM.get_language(telegram_id)

        await RedisFSM.set_state(telegram_id, "REPLACE_WAITING_CUSTOM_ISP", {"config_id": config_id})

        if user_lang.startswith("fa"):
            prompt_custom = (
                "✏️ **وارد کردن نام اپراتور اینترنت**\n\n"
                "لطفاً نام شرکت یا اپراتور اینترنت خود را (مانند پیشگامان، رسپینا، زیتل، افرانت، یا اینترنت دانشگاه/شرکت) به صورت متن در این چت ارسال نمایید:"
            )
        elif user_lang.startswith("ru"):
            prompt_custom = (
                "✏️ **Ввод интернет-провайдера вручную**\n\n"
                "Пожалуйста, напишите название вашего интернет-провайдера (например, Ростелеком, МТС, Билайн, Дом.ру и т.д.):"
            )
        else:
            prompt_custom = (
                "✏️ **Custom ISP Name**\n\n"
                "Please type the name of your mobile or broadband internet service provider in this chat:"
            )

        from app.keyboards import cancel_inline_keyboard
        await event.edit(prompt_custom, buttons=cancel_inline_keyboard("cancel_replace", lang=user_lang))

    # Cancel Route Replacement FSM
    @bot.on(events.CallbackQuery(pattern=r"^fsm:cancel_replace$"))
    async def on_cb_cancel_replace(event):
        telegram_id = str(event.sender_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        await RedisFSM.clear_state(telegram_id)

        cancel_msg = (
            "❌ **عملیات تعویض مسیر لغو گردید.**" if user_lang.startswith("fa") else
            "❌ **Замена маршрута отменена.**" if user_lang.startswith("ru") else
            "❌ **Route replacement cancelled.**"
        )
        await event.edit(cancel_msg, buttons=[[Button.inline("🔙 Back to Configs" if not user_lang.startswith("fa") else "🔙 بازگشت به کانفیگ‌ها", data="cfg:list")]])

    # Catch custom ISP text input via FSM
    @bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
    async def on_custom_isp_text_received(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)

        if state == "REPLACE_WAITING_CUSTOM_ISP":
            custom_isp_name = event.text.strip()
            data = await RedisFSM.get_data(telegram_id)
            config_id = data.get("config_id")
            await RedisFSM.clear_state(telegram_id)

            await execute_route_replacement(event, config_id=config_id, chosen_isp=custom_isp_name)
            return

    # Pre-defined ISP button callback
    @bot.on(events.CallbackQuery(pattern=r"^isp:([A-Za-z0-9\-]+):([A-Za-z0-9\s]+)$"))
    async def on_cb_isp_chosen(event):
        config_id = event.pattern_match.group(1)
        chosen_isp = event.pattern_match.group(2).strip()
        await execute_route_replacement(event, config_id=config_id, chosen_isp=chosen_isp)

    async def execute_route_replacement(event_or_msg, config_id: str, chosen_isp: str):
        """Dispatches replacement API and handles auto-replace vs auto-ticket responses."""
        telegram_id = str(event_or_msg.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        processing_msg = (
            f"⏳ در حال پردازش تعویض مسیر برای اپراتور: **{chosen_isp}**..."
            if user_lang.startswith("fa")
            else f"⏳ Обработка замены маршрута для провайдера: **{chosen_isp}**..."
            if user_lang.startswith("ru")
            else f"⏳ Processing route replacement for ISP: **{chosen_isp}**..."
        )

        if hasattr(event_or_msg, "edit"):
            await event_or_msg.edit(processing_msg, buttons=None)
        else:
            await event_or_msg.reply(processing_msg)

        res = await api.replace_blocked_route(config_id, client_isp=chosen_isp)

        # BRANCH 1: Route replacement succeeded!
        if res.get("success"):
            msg = res.get("message", "")
            if user_lang.startswith("fa"):
                confirm = (
                    f"🎉 **مسیر ارتباطی با موفقیت جایگزین شد!**\n\n"
                    f"{msg}\n\n"
                    f"کانفیگ قدیمی از پنل سرور حذف و باطل گردید و مسیر ارتباطی جدید شما فعال شد.\n"
                    f"جهت دریافت لینک‌های جدید، دکمه زیر را لمس فرمایید:"
                )
                btn_label = "🌐 مشاهده کانفیگ‌های به‌روز شده"
            elif user_lang.startswith("ru"):
                confirm = (
                    f"🎉 **Маршрут успешно заменен!**\n\n"
                    f"{msg}\n\n"
                    f"Старая конфигурация отозвана, новый узел активирован.\n"
                    f"Нажмите кнопку ниже для просмотра новых ссылок:"
                )
                btn_label = "🌐 Просмотреть новые конфигурации"
            else:
                confirm = (
                    f"🎉 **Route Replaced Successfully!**\n\n"
                    f"{msg}\n\n"
                    f"The old config has been revoked and your new node is active.\n"
                    f"Tap below to view your updated configurations:"
                )
                btn_label = "🌐 View Updated Configs"

            if hasattr(event_or_msg, "edit"):
                await event_or_msg.edit(confirm, buttons=[[Button.inline(btn_label, data="cfg:list")]])
            else:
                await event_or_msg.reply(confirm, buttons=[[Button.inline(btn_label, data="cfg:list")]])

        # BRANCH 2: Auto-replacement not allowed -> Emergency ticket automatically created
        elif res.get("auto_ticket"):
            msg = res.get("message", "")
            if user_lang.startswith("fa"):
                ticket_confirm = (
                    f"🎫 **تیکت پشتیبانی اضطراری ثبت گردید**\n\n"
                    f"{msg}\n\n"
                    f"اطلاعات اپراتور اینترنت شما (**{chosen_isp}**) ضمیمه شد تا تیم مدیریت سرور جدید و مناسبی را برای شما فعال نمایند.\n"
                    f"پاسخ پشتیبانی به زودی از طریق اعلان در همین ربات ارسال خواهد شد."
                )
                btn_label = "🎫 مشاهده تیکت‌های پشتیبانی"
            elif user_lang.startswith("ru"):
                ticket_confirm = (
                    f"🎫 **Создан срочный запрос в поддержку**\n\n"
                    f"{msg}\n\n"
                    f"Информация о вашем провайдере (**{chosen_isp}**) передана администраторам для настройки рабочего узла.\n"
                    f"Как только администратор ответит, бот пришлет вам уведомление."
                )
                btn_label = "🎫 Просмотреть тикеты"
            else:
                ticket_confirm = (
                    f"🎫 **Emergency Support Ticket Dispatched**\n\n"
                    f"{msg}\n\n"
                    f"Your ISP (**{chosen_isp}**) details were attached so administrators can deploy a dedicated working route for you.\n"
                    f"You will receive an instant notification in this chat as soon as an agent replies."
                )
                btn_label = "🎫 View Support Tickets"

            if hasattr(event_or_msg, "edit"):
                await event_or_msg.edit(ticket_confirm, buttons=[[Button.inline(btn_label, data="ticket:list")]])
            else:
                await event_or_msg.reply(ticket_confirm, buttons=[[Button.inline(btn_label, data="ticket:list")]])

        # BRANCH 3: General API / Gateway Error
        else:
            err_msg = res.get("message", "Error")
            if user_lang.startswith("fa"):
                fail_text = f"⚠️ امکان تعویض مسیر در حال حاضر وجود ندارد: {err_msg}"
            elif user_lang.startswith("ru"):
                fail_text = f"⚠️ Не удалось выполнить замену маршрута: {err_msg}"
            else:
                fail_text = f"⚠️ Replacement could not be completed: {err_msg}"

            if hasattr(event_or_msg, "edit"):
                await event_or_msg.edit(fail_text, buttons=main_menu_keyboard(user_lang))
            else:
                await event_or_msg.reply(fail_text, buttons=main_menu_keyboard(user_lang))