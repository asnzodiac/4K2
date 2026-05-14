"""
Ponnan🤴: Voice-First Telegram Bot
Upgraded Admin Intelligence Version
"""

import asyncio
import logging
import os
import random
from datetime import datetime

from flask import Flask, request
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from utils.conversation_manager import ConversationManager
from utils.language_detector import LanguageDetector
from utils.media_processor import MediaProcessor
from utils.stt_handler import STTHandler
from utils.tts_handler import TTSHandler

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", 10000))
OWNER_ID = int(os.getenv("OWNER_ID"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")
if not OWNER_ID:
    raise ValueError("OWNER_ID missing")


def get_groq_keys():
    keys = []
    multi = os.getenv("GROQ_API_KEYS", "")
    if multi:
        keys.extend([k.strip() for k in multi.split(",") if k.strip()])
    for i in range(4):
        name = f"GROQ_API_KEY{i}" if i > 0 else "GROQ_API_KEY"
        k = os.getenv(name, "")
        if k and k not in keys:
            keys.append(k.strip())
    return keys


GROQ_API_KEYS = get_groq_keys()

if not GROQ_API_KEYS:
    raise ValueError("At least one GROQ_API_KEY required")

# ============================================================================
# GLOBALS
# ============================================================================

app = Flask(__name__)

lang_detector = LanguageDetector()
tts_handler = TTSHandler()
stt_handler = STTHandler()
media_processor = MediaProcessor()
conversation_manager = ConversationManager()

bot_state = {}        # chat_id → {"active": bool, "language": str}
user_profiles = {}    # user_id → metadata

# ============================================================================
# CHARACTER SYSTEM (Dynamic Reload)
# ============================================================================

def load_character_prompt():
    try:
        with open("character.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"Character load failed: {e}")
        return "You are Adimma Kann, a witty AI assistant."


def build_system_prompt(language="en"):
    character = load_character_prompt()

    instruction = {
        "en": "Respond in English.",
        "ml": "മലയാളത്തിൽ മറുപടി നൽകുക. Respond in Malayalam.",
        "manglish": "Respond in Manglish (Romanized Malayalam mixed with English).",
    }.get(language, "Respond in English.")

    return f"{character}\n\nIMPORTANT: {instruction}"


# ============================================================================
# GROQ CLIENT
# ============================================================================

class GroqClientManager:
    def __init__(self, keys):
        self.keys = keys
        self.index = 0

    def get_client(self):
        client = Groq(api_key=self.keys[self.index])
        self.index = (self.index + 1) % len(self.keys)
        return client

    def get_completion(self, messages):
        try:
            client = self.get_client()
            resp = client.chat.completions.create(
                messages=messages,
                model="llama-3.3-70b-versatile",
                temperature=0.8,
                max_tokens=1024,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq error: {e}")
            return "Sorry sir, my brain lagged a bit."


groq_manager = GroqClientManager(GROQ_API_KEYS)

# ============================================================================
# ADMIN MONITORING
# ============================================================================

async def forward_to_admin(context, user, user_text, bot_reply):
    if user.id == OWNER_ID:
        return

    try:
        report = (
            f"📩 *User Message*\n\n"
            f"👤 {user.first_name} {user.last_name or ''}\n"
            f"🆔 `{user.id}`\n"
            f"📱 @{user.username or 'N/A'}\n\n"
            f"💬 {user_text}\n\n"
            f"🤖 *Bot Reply:*\n{bot_reply}"
        )

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=report,
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Admin forward error: {e}")


# ============================================================================
# STATE HELPERS
# ============================================================================

def get_bot_state(chat_id):
    if chat_id not in bot_state:
        bot_state[chat_id] = {"active": True, "language": "en"}
    return bot_state[chat_id]


def should_sleep(text):
    cmds = ["bye", "sleep", "standby", "good night"]
    return any(c in text.lower() for c in cmds)


def should_wake(text):
    cmds = ["hi", "hello", "wake up", "adimma"]
    return any(c in text.lower() for c in cmds)


# ============================================================================
# COMMANDS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎭 Adimma Kann activated, sir.")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_manager.clear_history(update.effective_chat.id)
    await update.message.reply_text("🗑️ History cleared, sir.")


async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid ID.")
        return

    if uid not in user_profiles:
        await update.message.reply_text("No data on this user.")
        return

    history = conversation_manager.get_history(uid)

    summary_prompt = [
        {"role": "system", "content": "Summarize this user briefly based on chat history."},
        {"role": "user", "content": str(history)},
    ]

    summary = groq_manager.get_completion(summary_prompt)
    profile = user_profiles[uid]

    report = (
        f"👤 Name: {profile['name']}\n"
        f"Username: @{profile['username']}\n"
        f"First Seen: {profile['first_seen']}\n\n"
        f"🧠 Summary:\n{summary}"
    )

    await update.message.reply_text(report)


# ============================================================================
# MESSAGE HANDLER
# ============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    text = update.message.text
    state = get_bot_state(chat_id)

    # Save profile
    if user.id not in user_profiles:
        user_profiles[user.id] = {
            "name": f"{user.first_name} {user.last_name or ''}",
            "username": user.username,
            "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # Sleep / Wake
    if should_sleep(text):
        state["active"] = False
        await update.message.reply_text("😴 Standing by, sir.")
        return

    if should_wake(text) and not state["active"]:
        state["active"] = True
        await update.message.reply_text("👋 Back, sir.")
        return

    if not state["active"]:
        return

    # Detect language
    lang = await lang_detector.detect(text)
    state["language"] = lang

    history = conversation_manager.get_history(chat_id)

    messages = [{"role": "system", "content": build_system_prompt(lang)}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = groq_manager.get_completion(messages)

    conversation_manager.add_message(chat_id, "user", text)
    conversation_manager.add_message(chat_id, "assistant", response)

    await update.message.reply_text(response)

    # Voice reply
    voice_file = await tts_handler.generate_speech(response, lang, chat_id)
    if voice_file and os.path.exists(voice_file):
        with open(voice_file, "rb") as audio:
            await update.message.reply_voice(voice=audio)

    # Forward to admin
    await forward_to_admin(context, user, text, response)


# ============================================================================
# WEBHOOK
# ============================================================================

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("clear", clear_command))
application.add_handler(CommandHandler("userinfo", userinfo_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


@app.route("/")
def index():
    return "Bot alive", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        _loop.run_until_complete(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500


# ============================================================================
# START
# ============================================================================

def setup_webhook():
    async def _setup():
        await application.initialize()
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    _loop.run_until_complete(_setup())


if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=PORT)
