# telegram-service/app/handlers/support.py
import logging
from telethon import events, Button
from app.services.redis_client import RedisFSM
from app.services.api_client import BackendAPIClient
from app.keyboards import (
    ticket_categories_keyboard,
    ticket_thread_keyboard,
    ticket_photo_keyboard,
    cancel_inline_keyboard,
    main_menu_keyboard
)
from app.handlers.balance import format_localized_number

logger = logging.getLogger(__name__)


def build_localized_tickets_list(items: list, lang: str = "en") -> tuple[str, list]:
    """
    Renders tickets list with status icons and a prominent 🔴 indicator
    when unseen messages exist.
    """
    lang_clean = (lang or "en").strip().lower()
    total_str = format_localized_number(len(items), lang=lang)
    buttons = []

    if not items:
        if lang_clean.startswith("fa"):
            return "ℹ️ شما هیچ تیکت پشتیبانی بازی ندارید.", [[Button.inline("➕ ایجاد تیکت پشتیبانی جدید", data="ticket:create:start")]]
        elif lang_clean.startswith("ru"):
            return "ℹ️ У вас нет обращений в службу поддержки.", [[Button.inline("➕ Создать новое обращение", data="ticket:create:start")]]
        else:
            return "ℹ️ You do not have any open support tickets.", [[Button.inline("➕ Create New Ticket", data="ticket:create:start")]]

    if lang_clean.startswith("fa"):
        msg = f"🎫 **تیکت‌های پشتیبانی شما ({total_str}):**\n\n"
    elif lang_clean.startswith("ru"):
        msg = f"🎫 **Ваши обращения в поддержку ({total_str}):**\n\n"
    else:
        msg = f"🎫 **Your Support Tickets ({total_str}):**\n\n"

    for t in items[:8]:
        tid = str(t.get("id"))
        subj = t.get("subject", "Support Ticket")
        status_val = t.get("status", "Open")
        has_unseen = bool(t.get("has_unseen_messages", False))

        # Status icon
        status_icon = "🟢" if status_val == "Open" else "🟡" if status_val == "In Progress" else "⚪"

        # Prominent unread notification indicator badge
        unseen_badge = "🔴 [پیام جدید] " if (has_unseen and lang_clean.startswith("fa")) else \
                       "🔴 [НОВОЕ] " if (has_unseen and lang_clean.startswith("ru")) else \
                       "🔴 [NEW] " if has_unseen else ""

        btn_prefix = "🔴 " if has_unseen else "💬 "
        msg += f"{status_icon} {unseen_badge}**{subj}** — وضعیت: `{status_val}`\n" if lang_clean.startswith("fa") else \
               f"{status_icon} {unseen_badge}**{subj}** — Статус: `{status_val}`\n" if lang_clean.startswith("ru") else \
               f"{status_icon} {unseen_badge}**{subj}** — Status: `{status_val}`\n"

        buttons.append([Button.inline(f"{btn_prefix}{subj[:26]}", data=f"ticket:view:{tid}")])

    create_label = "➕ ایجاد تیکت جدید" if lang_clean.startswith("fa") else "➕ Создать тикет" if lang_clean.startswith("ru") else "➕ Create New Ticket"
    buttons.append([Button.inline(create_label, data="ticket:create:start")])

    return msg, buttons


async def register_support_handlers(bot):
    SUPPORT_BUTTONS = [
        "🎫 Support Tickets", "/support",
        "🎫 تیکت‌های پشتیبانی",
        "🎫 Служба поддержки"
    ]

    # ==========================================
    # 1. LIST TICKETS
    # ==========================================
    @bot.on(events.NewMessage(func=lambda e: e.text in SUPPORT_BUTTONS))
    async def on_support_command(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        if not user_id:
            await event.reply("⚠️ Connect your account first using `/start <token>`.")
            return

        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        tickets_resp = await api.list_tickets()

        items = tickets_resp.get("items", []) if tickets_resp else []
        msg, buttons = build_localized_tickets_list(items, lang=user_lang)

        await event.reply(msg, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=r"^ticket:list$"))
    async def on_cb_list_tickets(event):
        telegram_id = str(event.sender_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)
        tickets_resp = await api.list_tickets()

        items = tickets_resp.get("items", []) if tickets_resp else []
        msg, buttons = build_localized_tickets_list(items, lang=user_lang)

        await event.edit(msg, buttons=buttons)

    # ==========================================
    # 2. VIEW TICKET THREAD (MARKS UNSEEN AS SEEN)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^ticket:view:([A-Za-z0-9\-]+)$"))
    async def on_cb_view_ticket(event):
        telegram_id = str(event.sender_id)
        ticket_id = event.pattern_match.group(1)
        user_id = await RedisFSM.get_user_id(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        api = BackendAPIClient(user_id)

        # GET ticket detail automatically marks messages as seen in support-service for this user
        ticket = await api.get_ticket_detail(ticket_id)
        if not ticket:
            not_found = (
                "⚠️ تیکت مورد نظر یافت نشد." if user_lang.startswith("fa") else
                "⚠️ Обращение не найдено." if user_lang.startswith("ru") else
                "⚠️ Ticket not found."
            )
            await event.edit(not_found, buttons=[[Button.inline("🔙 Back", data="ticket:list")]])
            return

        subj = ticket.get("subject")
        status_val = ticket.get("status")
        cat = ticket.get("category")
        messages = ticket.get("messages", [])

        if user_lang.startswith("fa"):
            header = (
                f"🎫 **تیکت: {subj}**\n"
                f"دسته‌بندی: `{cat}` | وضعیت: `{status_val}`\n"
                f"────────────────────\n"
            )
        elif user_lang.startswith("ru"):
            header = (
                f"🎫 **Обращение: {subj}**\n"
                f"Категория: `{cat}` | Статус: `{status_val}`\n"
                f"────────────────────\n"
            )
        else:
            header = (
                f"🎫 **Ticket: {subj}**\n"
                f"Category: `{cat}` | Status: `{status_val}`\n"
                f"────────────────────\n"
            )

        msg = header
        for m in messages[-6:]:
            sender = "👤 **شما**" if m.get("sender_role") == "Client" and user_lang.startswith("fa") else \
                     "👤 **Вы**" if m.get("sender_role") == "Client" and user_lang.startswith("ru") else \
                     "👤 **You**" if m.get("sender_role") == "Client" else \
                     "🛡️ **پشتیبانی**" if user_lang.startswith("fa") else \
                     "🛡️ **Поддержка**" if user_lang.startswith("ru") else \
                     "🛡️ **Support Staff**"

            content = m.get("content") or "[Attachment/تصویر]"
            msg += f"{sender}: {content}\n\n"

        is_closed = status_val.lower() in ["done", "closed"]
        await event.edit(msg, buttons=ticket_thread_keyboard(ticket_id, is_closed=is_closed, lang=user_lang))

    # ==========================================
    # 3. CREATE TICKET: STEP 1 (CATEGORY SELECTION)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^ticket:create:start$"))
    async def on_cb_create_start(event):
        telegram_id = str(event.sender_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        await RedisFSM.set_state(telegram_id, "TICKET_CHOOSE_CAT")

        prompt = (
            "📂 **لطفاً موضوع و دسته‌بندی تیکت خود را انتخاب فرمایید:**" if user_lang.startswith("fa") else
            "📂 **Пожалуйста, выберите категорию вашего обращения:**" if user_lang.startswith("ru") else
            "📂 **Please select the category for your support ticket:**"
        )
        await event.edit(prompt, buttons=ticket_categories_keyboard(lang=user_lang))

    # ==========================================
    # 3. CREATE TICKET: STEP 2 (ENTER SUBJECT)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^tc:cat:(.+)$"))
    async def on_cb_ticket_category(event):
        telegram_id = str(event.sender_id)
        category = event.pattern_match.group(1)
        user_lang = await RedisFSM.get_language(telegram_id)

        await RedisFSM.set_state(telegram_id, "TICKET_WAITING_SUBJECT", {"category": category})

        if user_lang.startswith("fa"):
            prompt = (
                f"✅ دسته‌بندی: **{category}**\n\n"
                f"📝 **مرحله ۱ از ۳:** لطفاً یک **عنوان کوتاه** برای تیکت خود ارسال کنید:"
            )
        elif user_lang.startswith("ru"):
            prompt = (
                f"✅ Категория: **{category}**\n\n"
                f"📝 **Шаг 1 из 3:** Введите **тему** вашего обращения:"
            )
        else:
            prompt = (
                f"✅ Category: **{category}**\n\n"
                f"📝 **Step 1 of 3:** Please enter a brief **subject line** for your ticket:"
            )

        await event.edit(prompt, buttons=cancel_inline_keyboard("cancel_ticket", lang=user_lang))

    # ==========================================
    # 4. CREATE TICKET: STEP 3 (ENTER MESSAGE TEXT)
    # ==========================================
    # Processed in on_ticket_text_input (transitions to TICKET_WAITING_PHOTO_CREATE)

    # ==========================================
    # 5. PHOTO UPLOAD HANDLER FOR CREATE & REPLY
    # ==========================================
    @bot.on(events.NewMessage(func=lambda e: e.photo is not None or (e.file and e.file.mime_type and e.file.mime_type.startswith("image/"))))
    async def on_ticket_photo_upload(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)

        if state == "TICKET_WAITING_PHOTO_CREATE":
            user_lang = await RedisFSM.get_language(telegram_id)
            data = await RedisFSM.get_data(telegram_id)
            user_id = await RedisFSM.get_user_id(telegram_id)
            api = BackendAPIClient(user_id)

            await event.reply("⏳ در حال بارگیری تصویر و ثبت تیکت..." if user_lang.startswith("fa") else "⏳ Загрузка изображения и отправка тикета..." if user_lang.startswith("ru") else "⏳ Uploading photo and creating ticket...")
            photo_bytes = await event.download_media(file=bytes)

            res = await api.create_ticket(
                subject=data.get("subject", "Support Request"),
                category=data.get("category", "General"),
                content=data.get("content", ""),
                file_bytes=photo_bytes,
                file_name="ticket_attachment.jpg"
            )
            await RedisFSM.clear_state(telegram_id)

            if res.get("success"):
                success_msg = (
                    "🎉 **تیکت شما به همراه تصویر با موفقیت ثبت شد!**\nپشتیبانان در اسرع وقت پاسخ خواهند داد."
                    if user_lang.startswith("fa") else
                    "🎉 **Обращение с прикрепленным фото успешно создано!**\nСпециалисты ответят в ближайшее время."
                    if user_lang.startswith("ru") else
                    "🎉 **Ticket created successfully with attached photo!**\nSupport staff will reply shortly."
                )
                await event.reply(success_msg, buttons=main_menu_keyboard(user_lang))
            else:
                await event.reply(f"⚠️ {res.get('detail')}", buttons=main_menu_keyboard(user_lang))

        elif state == "TICKET_WAITING_PHOTO_REPLY":
            user_lang = await RedisFSM.get_language(telegram_id)
            data = await RedisFSM.get_data(telegram_id)
            user_id = await RedisFSM.get_user_id(telegram_id)
            ticket_id = data.get("ticket_id")
            api = BackendAPIClient(user_id)

            await event.reply("⏳ در حال ارسال پاسخ به همراه تصویر..." if user_lang.startswith("fa") else "⏳ Отправка ответа с фото..." if user_lang.startswith("ru") else "⏳ Sending reply with photo...")
            photo_bytes = await event.download_media(file=bytes)

            res = await api.add_ticket_reply(
                ticket_id=ticket_id,
                content=data.get("content", ""),
                file_bytes=photo_bytes,
                file_name="reply_attachment.jpg"
            )
            await RedisFSM.clear_state(telegram_id)

            if res.get("success"):
                reply_success = (
                    "✅ **پاسخ شما با موفقیت ارسال شد!**" if user_lang.startswith("fa") else
                    "✅ **Ваш ответ успешно отправлен!**" if user_lang.startswith("ru") else
                    "✅ **Reply sent successfully!**"
                )
                await event.reply(reply_success, buttons=main_menu_keyboard(user_lang))
            else:
                await event.reply(f"⚠️ {res.get('detail')}", buttons=main_menu_keyboard(user_lang))

    # ==========================================
    # 6. SKIP PHOTO CALLBACKS (CREATE & REPLY)
    # ==========================================
    @bot.on(events.CallbackQuery(pattern=r"^ticket:skip_photo_create$"))
    async def on_cb_skip_photo_create(event):
        telegram_id = str(event.sender_id)
        data = await RedisFSM.get_data(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        api = BackendAPIClient(user_id)

        await event.answer()
        await RedisFSM.clear_state(telegram_id)

        wait_text = "⏳ در حال ثبت تیکت پشتیبانی..." if user_lang.startswith("fa") else "⏳ Создание обращения..." if user_lang.startswith("ru") else "⏳ Submitting ticket..."
        await event.edit(wait_text)

        res = await api.create_ticket(
            subject=data.get("subject", "Support Request"),
            category=data.get("category", "General"),
            content=data.get("content", "")
        )

        if res.get("success"):
            success_msg = (
                "🎉 **تیکت پشتیبانی شما با موفقیت ثبت شد!**\nپشتیبانان در اسرع وقت پاسخ خواهند داد."
                if user_lang.startswith("fa") else
                "🎉 **Обращение в службу поддержки успешно создано!**\nСпециалисты скоро вам ответят."
                if user_lang.startswith("ru") else
                "🎉 **Ticket created successfully!**\nOur support staff has been notified."
            )
            await event.respond(success_msg, buttons=main_menu_keyboard(user_lang))
        else:
            await event.respond(f"⚠️ {res.get('detail')}", buttons=main_menu_keyboard(user_lang))

    @bot.on(events.CallbackQuery(pattern=r"^ticket:skip_photo_reply$"))
    async def on_cb_skip_photo_reply(event):
        telegram_id = str(event.sender_id)
        data = await RedisFSM.get_data(telegram_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        user_id = await RedisFSM.get_user_id(telegram_id)
        ticket_id = data.get("ticket_id")
        api = BackendAPIClient(user_id)

        await event.answer()
        await RedisFSM.clear_state(telegram_id)

        wait_text = "⏳ در حال ارسال پاسخ..." if user_lang.startswith("fa") else "⏳ Отправка ответа..." if user_lang.startswith("ru") else "⏳ Sending reply..."
        await event.edit(wait_text)

        res = await api.add_ticket_reply(
            ticket_id=ticket_id,
            content=data.get("content", "")
        )

        if res.get("success"):
            reply_success = (
                "✅ **پاسخ شما با موفقیت ارسال شد!**" if user_lang.startswith("fa") else
                "✅ **Ваш ответ успешно отправлен!**" if user_lang.startswith("ru") else
                "✅ **Reply sent successfully!**"
            )
            await event.respond(reply_success, buttons=main_menu_keyboard(user_lang))
        else:
            await event.respond(f"⚠️ {res.get('detail')}", buttons=main_menu_keyboard(user_lang))

    # ==========================================
    # 7. TEXT INPUT FOR TICKET FSM
    # ==========================================
    @bot.on(events.NewMessage(func=lambda e: e.text and not e.text.startswith("/")))
    async def on_ticket_text_input(event):
        telegram_id = str(event.sender_id)
        state = await RedisFSM.get_state(telegram_id)
        text = event.text.strip()
        user_lang = await RedisFSM.get_language(telegram_id)

        # STEP 2 OF CREATE: SUBJECT ENTERED -> ASK FOR MESSAGE TEXT
        if state == "TICKET_WAITING_SUBJECT":
            await RedisFSM.update_data(telegram_id, {"subject": text})
            await RedisFSM.set_state(telegram_id, "TICKET_WAITING_MESSAGE")

            if user_lang.startswith("fa"):
                prompt = (
                    f"📝 عنوان تیکت: **{text}**\n\n"
                    f"💬 **مرحله ۲ از ۳:** لطفاً شرح کامل مشکل یا درخواست خود را به صورت متن ارسال نمایید:"
                )
            elif user_lang.startswith("ru"):
                prompt = (
                    f"📝 Тема: **{text}**\n\n"
                    f"💬 **Шаг 2 из 3:** Опишите вашу проблему или вопрос подробным текстом:"
                )
            else:
                prompt = (
                    f"📝 Subject: **{text}**\n\n"
                    f"💬 **Step 2 of 3:** Please describe your issue in detail in your message:"
                )

            await event.reply(prompt, buttons=cancel_inline_keyboard("cancel_ticket", lang=user_lang))
            return

        # STEP 3 OF CREATE: MESSAGE TEXT ENTERED -> ASK FOR OPTIONAL PICTURE
        if state == "TICKET_WAITING_MESSAGE":
            await RedisFSM.update_data(telegram_id, {"content": text})
            await RedisFSM.set_state(telegram_id, "TICKET_WAITING_PHOTO_CREATE")

            if user_lang.startswith("fa"):
                prompt_photo = (
                    "📸 **مرحله ۳ از ۳ (کاملاً اختیاری):**\n\n"
                    "در صورت تمایل، می‌توانید تصویر یا اسکرین‌شات خطای خود را ارسال فرمایید.\n"
                    "**توجه: ارسال تصویر کاملاً اختیاری است.** در صورتی که تصویری ندارید، روی دکمه زیر کلیک کنید تا تیکت ثبت شود:"
                )
            elif user_lang.startswith("ru"):
                prompt_photo = (
                    "📸 **Шаг 3 из 3 (необязательно):**\n\n"
                    "Вы можете прикрепить фото или скриншот ошибки.\n"
                    "**Внимание: отправка фото не обязательна.** Если фото нет, нажмите кнопку **Пропустить**, чтобы отправить обращение:"
                )
            else:
                prompt_photo = (
                    "📸 **Step 3 of 3 (Optional):**\n\n"
                    "If you have a screenshot of the issue, you can send it now.\n"
                    "**Note: Sending a picture is completely optional.** If you don't need to attach a picture, click **Skip** below to submit your ticket:"
                )

            await event.reply(prompt_photo, buttons=ticket_photo_keyboard(action_type="create", lang=user_lang))
            return

        # STEP 1 OF REPLY: TEXT ENTERED -> ASK FOR OPTIONAL PICTURE
        if state and state.startswith("TICKET_REPLYING_TO:"):
            ticket_id = state.split(":")[1]
            await RedisFSM.set_state(telegram_id, "TICKET_WAITING_PHOTO_REPLY", {"ticket_id": ticket_id, "content": text})

            if user_lang.startswith("fa"):
                prompt_photo = (
                    "📸 **پیوست تصویر (اختیاری):**\n\n"
                    "متن پاسخ شما دریافت شد. در صورت تمایل می‌توانید یک تصویر ضمیمه نمایید، یا با لمس دکمه زیر پاسخ خود را ثبت کنید:\n"
                    "*(ارسال تصویر کاملاً اختیاری است)*"
                )
            elif user_lang.startswith("ru"):
                prompt_photo = (
                    "📸 **Прикрепление фото (необязательно):**\n\n"
                    "Текст ответа принят. Вы можете отправить фото или нажать кнопку **Пропустить**, чтобы отправить ответ:"
                )
            else:
                prompt_photo = (
                    "📸 **Attach Picture (Optional):**\n\n"
                    "Text received. You can now send an image attachment, or click **Skip** below to submit your reply without a picture:\n"
                    "*(Sending a picture is completely optional)*"
                )

            await event.reply(prompt_photo, buttons=ticket_photo_keyboard(action_type="reply", lang=user_lang))
            return

    # Trigger reply state
    @bot.on(events.CallbackQuery(pattern=r"^ticket:reply:([A-Za-z0-9\-]+)$"))
    async def on_cb_reply_start(event):
        telegram_id = str(event.sender_id)
        ticket_id = event.pattern_match.group(1)
        user_lang = await RedisFSM.get_language(telegram_id)

        await RedisFSM.set_state(telegram_id, f"TICKET_REPLYING_TO:{ticket_id}")

        if user_lang.startswith("fa"):
            prompt = "💬 لطفاً متن پاسخ خود را در این چت تایپ و ارسال فرمایید (در مرحله بعد می‌توانید عکس هم ضمیمه کنید):"
        elif user_lang.startswith("ru"):
            prompt = "💬 Введите текст вашего ответа в чат (на следующем шаге можно прикрепить фото):"
        else:
            prompt = "💬 Type your reply message below and send it in this chat (you can attach an optional photo next):"

        await event.edit(prompt, buttons=cancel_inline_keyboard("cancel_ticket", lang=user_lang))

    # Cancel ticket wizard
    @bot.on(events.CallbackQuery(pattern=r"^fsm:cancel_ticket$"))
    async def on_cb_cancel_ticket(event):
        telegram_id = str(event.sender_id)
        user_lang = await RedisFSM.get_language(telegram_id)
        await RedisFSM.clear_state(telegram_id)

        if user_lang.startswith("fa"):
            cancel_msg = "❌ **عملیات ثبت یا پاسخ به تیکت پشتیبانی لغو شد.**"
        elif user_lang.startswith("ru"):
            cancel_msg = "❌ **Создание или ответ на обращение отменено.**"
        else:
            cancel_msg = "❌ **Support ticket operation cancelled.**"

        await event.edit(cancel_msg, buttons=None)
        await event.respond("👇", buttons=main_menu_keyboard(user_lang))