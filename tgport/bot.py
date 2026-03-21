import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .claude import Error, Result, SessionNotFoundError, TextDelta, ToolUse, stream_claude
from .session import SessionManager

logger = logging.getLogger(__name__)

session_manager = SessionManager()
_chat_locks: dict[int, asyncio.Lock] = {}

MAX_MSG_LEN = 4000
MAX_MESSAGES_PER_RESPONSE = 10
TYPING_INTERVAL = 4.0  # Telegram expires typing after ~5s
LOG_ROTATE_CHECK_INTERVAL = 3600  # seconds

_last_rotate_check: float = 0.0


def _rotate_logs():
    """Rotate log files from previous days and delete backups older than 14 days."""
    global _last_rotate_check
    now = time.monotonic()
    if now - _last_rotate_check < LOG_ROTATE_CHECK_INTERVAL:
        return
    _last_rotate_check = now

    if not os.path.isdir(config.LOG_DIR):
        return

    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=config.LOG_RETENTION_DAYS)

    for fname in os.listdir(config.LOG_DIR):
        fpath = os.path.join(config.LOG_DIR, fname)
        if not os.path.isfile(fpath):
            continue

        # Delete old backups
        if "_bk-" in fname:
            try:
                date_str = fname.rsplit("_bk-", 1)[1].replace(".jsonl", "")
                bk_date = datetime.strptime(date_str, "%Y%m%d").date()
                if bk_date < cutoff:
                    os.remove(fpath)
                    logger.info("Deleted old log backup: %s", fname)
            except (ValueError, IndexError):
                pass
            continue

        # Rotate active logs from previous days
        if fname.startswith("chat_") and fname.endswith(".jsonl"):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc).date()
                if mtime < today:
                    backup = fpath.replace(".jsonl", f"_bk-{mtime:%Y%m%d}.jsonl")
                    os.rename(fpath, backup)
                    logger.info("Rotated log: %s -> %s", fname, os.path.basename(backup))
            except OSError as e:
                logger.error("Failed to rotate log %s: %s", fname, e)


def _log_event(chat_id: int, event_type: str, **fields):
    """Append an event entry to the JSONL log file."""
    os.makedirs(config.LOG_DIR, exist_ok=True)
    _rotate_logs()
    log_path = os.path.join(config.LOG_DIR, f"chat_{chat_id}.jsonl")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "event": event_type,
        **fields,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("Failed to write conversation log: %s", e)


USD_TO_JPY = 150.0


def _format_cost(cost_usd: float) -> str:
    mode = config.COST_DISPLAY.lower()
    if mode == "none":
        return ""
    if mode == "yen":
        yen = cost_usd * USD_TO_JPY
        return f"\n\n<i>cost: ¥{yen:.2f}</i>"
    # dollar (default)
    return f"\n\n<i>cost: ${cost_usd:.4f}</i>"


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


def restricted(func):
    """Allow only configured user IDs."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in config.ALLOWED_USER_IDS:
            logger.warning("Unauthorized access attempt from user %s", user.id if user else "unknown")
            return
        return await func(update, context)
    return wrapper


async def _edit_message(msg, text: str):
    """Edit message with HTML, falling back to plain text."""
    if not text.strip():
        return
    try:
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception as e:
            logger.debug("Failed to edit message: %s", e)


@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Claude bot ready. Send any message to interact.\n"
        "/new - start a new conversation"
    )


@restricted
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session_manager.reset(chat_id)
    await update.message.reply_text("New conversation started.")


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    prompt = update.message.text
    if not prompt:
        return

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    async with lock:
        await _process_message(update, chat_id, prompt)


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".txt", ".md", ".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DOC_EXTENSIONS = {".txt", ".md", ".pdf"}
ALLOWED_MIMES = {
    "image/jpeg", "image/png", "image/webp",
    "text/plain", "text/markdown",
    "application/pdf",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _get_download_dir(ext: str) -> str:
    if ext in DOC_EXTENSIONS:
        return os.path.expanduser("~/workspace/docs/downloads")
    return config.DOWNLOAD_DIR


def _safe_filepath(download_dir: str, filename: str) -> str:
    safe_name = os.path.basename(filename)
    filepath = os.path.join(download_dir, safe_name)
    if not os.path.abspath(filepath).startswith(os.path.abspath(download_dir)):
        raise ValueError("Invalid filename")
    return filepath


@restricted
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or "この画像を確認してください。"

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    if not update.message.photo:
        return
    photo = update.message.photo[-1]

    if photo.file_size and photo.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"ファイルが大きすぎます（上限: {MAX_FILE_SIZE // 1024 // 1024}MB）")
        return

    file = await context.bot.get_file(photo.file_id)

    download_dir = _get_download_dir(".jpg")
    os.makedirs(download_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{photo.file_unique_id}.jpg"
    filepath = _safe_filepath(download_dir, filename)
    await file.download_to_drive(filepath)

    prompt = f"{caption}\n\n[画像ファイル: {filepath}]"
    logger.info("Photo saved: %s", filepath)

    async with lock:
        await _process_message(update, chat_id, prompt)


@restricted
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or "このファイルを確認してください。"
    doc = update.message.document

    # Check file extension and MIME type
    original_name = doc.file_name or ""
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        await update.message.reply_text(f"非対応のファイル形式です。対応: {allowed}")
        return
    if doc.mime_type and doc.mime_type not in ALLOWED_MIMES:
        await update.message.reply_text("非対応のファイル形式です。")
        return

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"ファイルが大きすぎます（上限: {MAX_FILE_SIZE // 1024 // 1024}MB）")
        return

    lock = _get_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Please wait for the current response to finish.")
        return

    file = await context.bot.get_file(doc.file_id)

    download_dir = _get_download_dir(ext)
    os.makedirs(download_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = os.path.basename(original_name) if original_name else f"{doc.file_unique_id}{ext}"
    filename = f"{ts}_{safe_name}"
    filepath = _safe_filepath(download_dir, filename)
    await file.download_to_drive(filepath)

    label = "画像ファイル" if ext in IMAGE_EXTENSIONS else "ファイル"
    prompt = f"{caption}\n\n[{label}: {filepath}]"
    logger.info("Document saved: %s", filepath)

    async with lock:
        await _process_message(update, chat_id, prompt)


async def _send_typing(chat_id: int, bot, stop_event: asyncio.Event):
    """Send 'typing...' action repeatedly until stop_event is set."""
    try:
        while not stop_event.is_set():
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TYPING_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass  # Don't let typing indicator errors affect message processing


async def _process_message(update: Update, chat_id: int, prompt: str, retry: bool = False):
    session_id, is_new = session_manager.get_or_create(chat_id)
    if retry:
        is_new = True

    # Prepend user identity on first message of a session
    if is_new:
        user = update.effective_user
        if user:
            name = user.full_name or user.username or str(user.id)
            prompt = f"[User: {name}]\n{prompt}"

    # Log request
    user = update.effective_user
    _log_event(chat_id, "request",
               user_id=user.id if user else 0,
               username=(user.username or user.full_name) if user else None,
               prompt=prompt)

    # Start typing indicator
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(_send_typing(chat_id, update.get_bot(), typing_stop))

    bot_msg = await update.message.reply_text("...")
    accumulated = ""
    last_edit = 0.0
    msg_count = 1
    cost_usd: float | None = None

    try:
        async for event in stream_claude(prompt, session_id, is_new):
            if isinstance(event, TextDelta):
                _log_event(chat_id, "text_delta", text=event.text)
                accumulated += event.text

                now = time.monotonic()
                if now - last_edit >= config.EDIT_INTERVAL:
                    display = accumulated[:MAX_MSG_LEN]
                    await _edit_message(bot_msg, display)
                    last_edit = now

                    # Split if too long
                    if len(accumulated) > MAX_MSG_LEN and msg_count < MAX_MESSAGES_PER_RESPONSE:
                        await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                        accumulated = accumulated[MAX_MSG_LEN:]
                        bot_msg = await update.message.reply_text("...")
                        msg_count += 1

            elif isinstance(event, ToolUse):
                _log_event(chat_id, "tool_use", tool=event.tool)
                tool_indicator = f"\n<i>[{event.tool}]</i>"
                accumulated += tool_indicator
                now = time.monotonic()
                if now - last_edit >= config.EDIT_INTERVAL:
                    await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                    last_edit = now

            elif isinstance(event, Result):
                # Handle session-not-found errors from CLI
                if event.is_error and any("no conversation found" in e.lower() for e in event.errors):
                    raise SessionNotFoundError("; ".join(event.errors))

                if event.is_error:
                    error_msg = "; ".join(event.errors) if event.errors else event.text or "Unknown error"
                    await _edit_message(bot_msg, f"Error: {error_msg}")
                    return

                # Use result text as final output if we have it
                if event.text and not accumulated.strip(".\n "):
                    accumulated = event.text

                # Split remaining text into messages
                while len(accumulated) > MAX_MSG_LEN and msg_count < MAX_MESSAGES_PER_RESPONSE:
                    await _edit_message(bot_msg, accumulated[:MAX_MSG_LEN])
                    accumulated = accumulated[MAX_MSG_LEN:]
                    bot_msg = await update.message.reply_text("...")
                    msg_count += 1

                cost_usd = event.cost_usd
                clean_response = re.sub(r"\n<i>\[.*?\]</i>", "", accumulated).strip()
                _log_event(chat_id, "response",
                           response=clean_response, cost_usd=cost_usd)
                footer = _format_cost(event.cost_usd)
                final = (accumulated + footer).strip() if footer else accumulated.strip()
                await _edit_message(bot_msg, final)

            elif isinstance(event, Error):
                await _edit_message(bot_msg, f"Error: {event.message}")

    except SessionNotFoundError:
        if not retry:
            await _edit_message(bot_msg, "Session not found, starting new...")
            await _process_message(update, chat_id, prompt, retry=True)
        else:
            await _edit_message(bot_msg, "Error: Failed to start Claude session.")
    except RuntimeError as e:
        logger.error("Claude CLI error: %s", e)
        await _edit_message(bot_msg, f"Error: {e}")
    finally:
        typing_stop.set()
        typing_task.cancel()


def run():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    if config.CLAUDE_SKIP_PERMISSIONS:
        logger.warning("DANGEROUS: --dangerously-skip-permissions is enabled!")
    logger.info("Bot started. Allowed users: %s", config.ALLOWED_USER_IDS)
    app.run_polling()
