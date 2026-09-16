"""
بوت تيليجرام لاستقبال شكاوى استغلال الشاشة واقتراحات الطلاب
وإرسالها تلقائياً إلى جروب "طلاب الهيئة".

مصمم ليتحمل عدد كبير من الطلاب (~2700) وحتى ذروة ~1000 رسالة/دقيقة عبر:
- استخدام concurrent_updates لمعالجة عدة طلاب بالتوازي أثناء الاستقبال
- تجميع الرسائل النصية بـ queue وإرسالها للجروب على دفعات (batching) كل 5 ثواني
  بدل إرسال كل شكوى كرسالة منفصلة — لأن تيليجرام يحدد نحو 20 رسالة/دقيقة
  لنفس الجروب. الدفعات تخلي عدد نداءات الإرسال قليل جداً حتى لو وصلت آلاف
  الرسائل، وبتوصل للجروب خلال ثواني معدودة.
- الصور تُرسل مباشرة (أقل تكراراً عادة وما بتنضم بنفس القيود بسهولة)

طريقة التشغيل محلياً:
1. pip install -r requirements.txt
2. عبّي متغيرات البيئة BOT_TOKEN و TARGET_GROUP_CHAT_ID
3. python screen_bot.py

كيف تجيب TARGET_GROUP_CHAT_ID:
- ضيف البوت لجروب "طلاب الهيئة"
- اكتب /chatid داخل الجروب وبيطبعلك الرقم مباشرة (لازم تشغّل البوت أول بتوكن مؤقت)
"""

import logging
import os
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============ الإعدادات ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
TARGET_GROUP_CHAT_ID = int(os.environ.get("TARGET_GROUP_CHAT_ID", "0"))  # مثال: -1001234567890
COLLEGE_NAME = os.environ.get("COLLEGE_NAME", "كلية الهندسة")

# حالات المحادثة
CHOOSING, TYPING_MESSAGE = range(2)

COMPLAINT = "complaint"
SUGGESTION = "suggestion"

LABELS = {
    COMPLAINT: "📢 شكوى استغلال شاشة",
    SUGGESTION: "💡 اقتراح",
}


# ============ المعالجات ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton(LABELS[COMPLAINT], callback_data=COMPLAINT)],
        [InlineKeyboardButton(LABELS[SUGGESTION], callback_data=SUGGESTION)],
    ]
    await update.message.reply_text(
        f"أهلاً فيك 👋\n"
        f"هاد بوت {COLLEGE_NAME} لاستقبال شكاوى استغلال الشاشة والاقتراحات.\n\n"
        "اختر نوع رسالتك:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["report_type"] = query.data
    label = LABELS[query.data]
    await query.edit_message_text(
        f"اخترت: {label}\n\nتفضل، اكتب رسالتك بالتفصيل (تقدر ترفق صورة كمان إذا حابب):"
    )
    return TYPING_MESSAGE


BATCH_INTERVAL_SECONDS = 5
MAX_MESSAGE_LENGTH = 3800  # هامش أمان تحت حد تيليجرام 4096


async def receive_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if TARGET_GROUP_CHAT_ID == 0:
        await update.message.reply_text("⚠️ لسا ما تم ضبط جروب الاستلام من إدارة البوت. حاول لاحقاً.")
        return ConversationHandler.END

    report_type = context.user_data.get("report_type", COMPLAINT)
    label = LABELS[report_type]
    user = update.effective_user
    student_name = user.full_name
    username = f"@{user.username}" if user.username else "بدون يوزر"
    timestamp = datetime.now().strftime("%H:%M")

    entry_header = f"{label} | 👤 {student_name} ({username}) | 🕒 {timestamp}"

    try:
        if update.message.photo:
            # الصور تنبعث مباشرة، بدون تجميع
            caption = entry_header + "\n――――――――――\n" + (update.message.caption or "")
            await context.bot.send_photo(
                chat_id=TARGET_GROUP_CHAT_ID,
                photo=update.message.photo[-1].file_id,
                caption=caption,
            )
        else:
            body = update.message.text or ""
            entry_text = f"{entry_header}\n{body}\n――――――――――"
            # نحطها بقائمة الانتظار المشتركة بدل ما نرسلها فوراً
            pending: list = context.application.bot_data.setdefault("pending", [])
            pending.append(entry_text)

        await update.message.reply_text(
            "✅ تم استلام رسالتك وراح توصل لجروب طلاب الهيئة خلال ثواني. شكراً إلك!",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception as e:
        logger.error(f"فشل استلام/جدولة الرسالة: {e}")
        await update.message.reply_text(
            "❌ صار في مشكلة بمعالجة رسالتك. تأكد إن البوت عضو بجروب الهيئة وحاول مرة ثانية."
        )

    context.user_data.clear()
    return ConversationHandler.END


async def flush_pending_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    """كل BATCH_INTERVAL_SECONDS: يسحب كل الشكاوى/الاقتراحات المتراكمة
    ويرسلها كدفعة (أو دفعات إذا تجاوزت حد طول الرسالة) بدل رسالة لكل واحدة،
    عشان ما نتجاوز حد تيليجرام لعدد الرسائل بالدقيقة لنفس الجروب."""
    pending: list = context.application.bot_data.get("pending", [])
    if not pending:
        return

    context.application.bot_data["pending"] = []  # تفريغ القائمة فوراً

    count = len(pending)
    chunk = f"📥 دفعة جديدة ({count} رسالة)\n\n"
    for entry in pending:
        if len(chunk) + len(entry) + 2 > MAX_MESSAGE_LENGTH:
            await context.bot.send_message(chat_id=TARGET_GROUP_CHAT_ID, text=chunk)
            chunk = ""
        chunk += entry + "\n\n"

    if chunk.strip():
        await context.bot.send_message(chat_id=TARGET_GROUP_CHAT_ID, text=chunk)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية. اكتب /start للبدء من جديد.")
    return ConversationHandler.END


async def debug_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر مساعد مؤقت: اكتب /chatid داخل أي جروب فيه البوت ليطبع لك الـ chat_id."""
    await update.message.reply_text(f"chat_id هذا الجروب/المحادثة هو: {update.effective_chat.id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "الأوامر المتاحة:\n"
        "/start - إرسال شكوى أو اقتراح جديد\n"
        "/cancel - إلغاء العملية الحالية\n"
        "/help - عرض هذه الرسالة"
    )


def main() -> None:
    # concurrent_updates=True يسمح بمعالجة عدة طلاب بنفس الوقت بدون ما ينتظر واحد التاني
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(256)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [CallbackQueryHandler(choose_type)],
            TYPING_MESSAGE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, receive_message
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chatid", debug_chat_id))

    # مهمة دورية ترسل الدفعات المجمّعة لجروب الهيئة
    application.job_queue.run_repeating(
        flush_pending_reports, interval=BATCH_INTERVAL_SECONDS, first=BATCH_INTERVAL_SECONDS
    )

    logger.info("البوت شغّال...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
