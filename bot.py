"""
Adimma-Kann: Voice-First Telegram Bot
A witty, sarcastic AI assistant for 'sir'
"""

import asyncio
import logging
import os
import random
from datetime import datetime

from flask import Flask, request
from groq import Groq
from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                           MessageHandler, filters)

from utils.conversation_manager import ConversationManager
from utils.language_detector import LanguageDetector
from utils.media_processor import MediaProcessor
from utils.stt_handler import STTHandler
from utils.tts_handler import TTSHandler, VOICE_CATALOGUE

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN   = os.getenv('TELEGRAM_TOKEN') or os.getenv('BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
PORT        = int(os.getenv('PORT', 10000))
OWNER_ID    = int(os.getenv('OWNER_ID', 733340342))


def get_groq_keys():
    keys = []
    multi = os.getenv('GROQ_API_KEYS', '')
    if multi:
        keys.extend([k.strip() for k in multi.split(',') if k.strip()])
    for i in range(4):
        name = f'GROQ_API_KEY{i}' if i > 0 else 'GROQ_API_KEY'
        k = os.getenv(name, '')
        if k and k not in keys:
            keys.append(k.strip())
    return keys


GROQ_API_KEYS = get_groq_keys()

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_TOKEN or BOT_TOKEN environment variable is required")
if not GROQ_API_KEYS:
    raise ValueError("At least one GROQ_API_KEY is required")

logger.info(f"Loaded {len(GROQ_API_KEYS)} Groq API keys")

# ============================================================================
# PERSISTENT EVENT LOOP  — single loop reused across all webhook requests
# ============================================================================

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# ============================================================================
# GLOBAL INSTANCES
# ============================================================================

app = Flask(__name__)

lang_detector       = LanguageDetector()
tts_handler         = TTSHandler()
stt_handler         = STTHandler()
media_processor     = MediaProcessor()
conversation_manager = ConversationManager()

CHARACTER_PROMPT = ""
try:
    with open('character.txt', 'r', encoding='utf-8') as f:
        CHARACTER_PROMPT = f.read().strip()
    logger.info("Character prompt loaded")
except Exception as e:
    logger.error(f"Failed to load character.txt: {e}")
    CHARACTER_PROMPT = "You are Adimma Kann, a witty and sarcastic AI assistant."

bot_state = {}   # chat_id → {"active": bool, "language": str}

# ============================================================================
# GROQ CLIENT MANAGER
# ============================================================================

class GroqClientManager:
    def __init__(self, api_keys):
        self.api_keys = api_keys
        self.current_index = 0

    def get_client(self):
        client = Groq(api_key=self.api_keys[self.current_index])
        self.current_index = (self.current_index + 1) % len(self.api_keys)
        return client

    def get_completion(self, messages, model="llama-3.3-70b-versatile", max_retries=3):
        for attempt in range(max_retries):
            try:
                client = self.get_client()
                resp = client.chat.completions.create(
                    messages=messages,
                    model=model,
                    temperature=0.8,
                    max_tokens=1024,
                )
                return resp.choices[0].message.content
            except Exception as e:
                logger.error(f"Groq error (attempt {attempt+1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return "Sorry sir, I'm having trouble thinking right now. Try again in a moment!"


groq_manager = GroqClientManager(GROQ_API_KEYS)

# ============================================================================
# HELPERS
# ============================================================================

def get_bot_state(chat_id):
    if chat_id not in bot_state:
        bot_state[chat_id] = {"active": True, "language": "en"}
    return bot_state[chat_id]


def should_sleep(text):
    cmds = ["bye", "standby", "stop listening", "sleep",
            "good night", "goodnight", "നല്ല രാത്രി", "പോയി വരാം"]
    t = text.lower().strip()
    return any(c in t for c in cmds)


def should_wake(text):
    cmds = ["hi", "hello", "wake up", "adimma", "hey",
            "ഹലോ", "എണീക്ക്", "അടിമ്മ"]
    t = text.lower().strip()
    return any(c in t for c in cmds)


async def notify_owner_new_user(context, user):
    try:
        msg = (
            f"🆕 *New User Started Bot*\n\n"
            f"👤 Name: {user.first_name} {user.last_name or ''}\n"
            f"🆔 User ID: `{user.id}`\n"
            f"📱 Username: @{user.username or 'N/A'}\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await context.bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")


def build_system_prompt(language="en"):
    instruction = {
        "en":       "Respond in English.",
        "ml":       "മലയാളത്തിൽ മറുപടി നൽകുക. Respond in Malayalam.",
        "manglish": "Respond in Manglish (Romanized Malayalam mixed with English).",
    }.get(language, "Respond in English.")
    return f"{CHARACTER_PROMPT}\n\nIMPORTANT: {instruction}"

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    get_bot_state(chat_id)["active"] = True

    if user.id != OWNER_ID:
        await notify_owner_new_user(context, user)

    welcome = (
        "🎭 Adimma Kann at your service!\n\n"
        "I'm your witty, slightly sarcastic AI assistant.\n\n"
        "Send me:\n"
        "• Voice messages (English / Malayalam / Manglish)\n"
        "• Text messages\n"
        "• Photos\n"
        "• Documents (PDFs)\n\n"
        "I'll respond in the same language you use!\n\n"
        "Commands:\n"
        "/help     — Usage guide\n"
        "/voice    — Change my voice\n"
        "/clear    — Clear chat history\n\n"
        "Say 'bye' or 'sleep' to put me on standby.\n"
        "Say 'hi' or 'wake up' to bring me back."
    )
    await update.message.reply_text(welcome)
    logger.info(f"User {user.id} started the bot")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎭 Adimma Kann - Usage Instructions\n\n"
        "How to Use:\n"
        "1. Just talk to me! Send voice or text messages\n"
        "2. I understand English, Malayalam, and Manglish\n"
        "3. I'll reply in the same language you use\n\n"
        "Voice Messages:\n"
        "🎤 Send voice notes - I'll transcribe and respond with voice + text\n\n"
        "Images & Documents:\n"
        "🖼  Send photos - I'll analyze and comment\n"
        "📄 Send PDFs - I'll read and discuss them\n\n"
        "Sleep / Wake:\n"
        "😴 Sleep: 'bye', 'standby', 'good night', 'sleep'\n"
        "👋 Wake: 'hi', 'hello', 'wake up', 'adimma'\n\n"
        "Commands:\n"
        "/start  - Restart\n"
        "/help   - This message\n"
        "/voice  - Change voice (type /voice to see options)\n"
        "/clear  - Clear conversation history\n\n"
        "Ready to chat? 🚀"
    )
    await update.message.reply_text(help_text)


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /voice          → show voice menu
    /voice <key>    → set voice to <key>
    """
    chat_id = update.effective_chat.id

    if not context.args:
        # Show the menu
        menu = tts_handler.get_voice_menu()
        await update.message.reply_text(menu, parse_mode='Markdown')
        return

    voice_key = context.args[0].lower().strip()
    if tts_handler.set_voice(chat_id, voice_key):
        name = VOICE_CATALOGUE[voice_key][0]
        await update.message.reply_text(
            f"✅ Voice changed to *{name}*\n\nI'll sound different from the next message!",
            parse_mode='Markdown'
        )
        # Send a short demo
        state = get_bot_state(chat_id)
        demo_text = "Hello sir! This is how I sound now. Like it?"
        demo_file = await tts_handler.generate_speech(demo_text, state["language"], chat_id)
        if demo_file and os.path.exists(demo_file):
            with open(demo_file, 'rb') as audio:
                await update.message.reply_voice(voice=audio)
    else:
        await update.message.reply_text(
            f"❌ Unknown voice `{voice_key}`.\n\nType /voice to see all options.",
            parse_mode='Markdown'
        )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conversation_manager.clear_history(chat_id)
    await update.message.reply_text("🗑️ Conversation history cleared!\n\nStarting fresh, sir! 🎬")

# ============================================================================
# MESSAGE HANDLERS
# ============================================================================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        voice_file = await update.message.voice.get_file()
        voice_path = f"voice_{chat_id}_{datetime.now().timestamp()}.ogg"
        await voice_file.download_to_drive(voice_path)

        await update.message.reply_text("🎧 Listening...")
        transcription = await stt_handler.transcribe(voice_path)

        if os.path.exists(voice_path):
            os.remove(voice_path)

        if not transcription:
            await update.message.reply_text("😕 Sorry sir, couldn't hear you clearly. Try again?")
            return

        logger.info(f"Voice transcribed: {transcription}")
        await process_message(update, context, transcription)

    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text("❌ Oops! Something went wrong with the voice message.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not get_bot_state(chat_id)["active"]:
        return
    try:
        await update.message.reply_text("🖼️ Analyzing image...")
        photo      = update.message.photo[-1]
        photo_file = await photo.get_file()
        photo_path = f"photo_{chat_id}_{datetime.now().timestamp()}.jpg"
        await photo_file.download_to_drive(photo_path)

        description = await media_processor.process_image(photo_path)
        if os.path.exists(photo_path):
            os.remove(photo_path)

        caption      = update.message.caption or "What do you think about this image?"
        user_message = f"{caption}\n\n[Image description: {description}]"
        await process_message(update, context, user_message)

    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        await update.message.reply_text("❌ Couldn't process the image, sir!")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not get_bot_state(chat_id)["active"]:
        return
    try:
        document = update.message.document
        if document.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("📄 File too large! Please send files under 10MB.")
            return

        await update.message.reply_text("📄 Processing document...")
        doc_file = await document.get_file()
        doc_path = f"doc_{chat_id}_{datetime.now().timestamp()}_{document.file_name}"
        await doc_file.download_to_drive(doc_path)

        content = await media_processor.process_document(doc_path, document.file_name)
        if os.path.exists(doc_path):
            os.remove(doc_path)

        if not content:
            await update.message.reply_text("😕 Couldn't read the document. Is it a valid PDF or text file?")
            return

        caption = update.message.caption or "Please analyze this document."
        if len(content) > 4000:
            content = content[:4000] + "... (truncated)"

        await process_message(update, context, f"{caption}\n\n[Document content:\n{content}]")

    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text("❌ Couldn't process the document, sir!")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_message(update, context, update.message.text)


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    state   = get_bot_state(chat_id)

    # Sleep / Wake
    if should_sleep(text):
        state["active"] = False
        await update.message.reply_text(random.choice([
            "😴 Going on standby, sir. Wake me when you need me!",
            "💤 Alright, taking a power nap. Just say 'hi' when you're back!",
            "🌙 Good night, sir! Standing by...",
        ]))
        return

    if should_wake(text) and not state["active"]:
        state["active"] = True
        await update.message.reply_text(random.choice([
            "👋 Wide awake, sir! What can I do for you?",
            "⚡ Back in action! What's up?",
            "🎯 Activated and ready, sir!",
        ]))
        return

    if not state["active"]:
        return

    try:
        detected_lang = await lang_detector.detect(text)
        state["language"] = detected_lang

        history  = conversation_manager.get_history(chat_id)
        messages = [{"role": "system", "content": build_system_prompt(detected_lang)}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        response = groq_manager.get_completion(messages)

        conversation_manager.add_message(chat_id, "user", text)
        conversation_manager.add_message(chat_id, "assistant", response)

        await update.message.reply_text(response)

        # Voice response
        await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
        voice_file = await tts_handler.generate_speech(response, detected_lang, chat_id)

        if voice_file and os.path.exists(voice_file):
            with open(voice_file, 'rb') as audio:
                await update.message.reply_voice(voice=audio)
        else:
            logger.info("TTS unavailable — text-only response sent")

    except Exception as e:
        logger.error(f"Message processing error: {e}")
        await update.message.reply_text("❌ Oops! My circuits got tangled. Give me a moment, sir!")

# ============================================================================
# WEBHOOK
# ============================================================================

@app.route('/')
def index():
    return "Adimma Kann is alive! 🎭", 200


@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Reuse the persistent event loop — never create/close a new one per request."""
    try:
        json_data = request.get_json(force=True)
        update    = Update.de_json(json_data, application.bot)
        _loop.run_until_complete(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

# ============================================================================
# APPLICATION INIT
# ============================================================================

application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start",       start_command))
application.add_handler(CommandHandler("help",        help_command))
application.add_handler(CommandHandler("instruction", help_command))
application.add_handler(CommandHandler("voice",       voice_command))
application.add_handler(CommandHandler("clear",       clear_command))
application.add_handler(MessageHandler(filters.VOICE,                  handle_voice))
application.add_handler(MessageHandler(filters.PHOTO,                  handle_photo))
application.add_handler(MessageHandler(filters.Document.ALL,           handle_document))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ============================================================================
# ENTRY POINT
# ============================================================================

def setup_webhook():
    async def _setup():
        await application.initialize()
        url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        await application.bot.set_webhook(url=url)
        logger.info(f"Webhook set to: {url}")
    _loop.run_until_complete(_setup())


if __name__ == '__main__':
    if WEBHOOK_URL:
        setup_webhook()
    else:
        logger.warning("No WEBHOOK_URL set — bot will not receive updates!")
    app.run(host='0.0.0.0', port=PORT)
