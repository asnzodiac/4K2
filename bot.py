"""
Ponnan 🤴 – Production Stable Version
Emotionally intelligent voice-first Telegram bot
"""

import asyncio
import logging
import os
import random
import requests
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
from utils.tts_handler import TTSHandler, VOICE_CATALOGUE

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =============================================================================
# ENV CONFIG
# =============================================================================

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", 10000))
OWNER_ID = int(os.getenv("OWNER_ID", "733340342"))

CITY = os.getenv("CITY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN missing")

if not WEBHOOK_URL:
    raise ValueError("❌ WEBHOOK_URL missing")

# =============================================================================
# PERMISSION SYSTEM
# =============================================================================

def allowed_chat(chat_id, chat_type):
    allowed_users = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    allowed_groups = os.getenv("TELEGRAM_ALLOWED_GROUPS", "")

    user_list = [x.strip() for x in allowed_users.split(",") if x.strip()]
    group_list = [x.strip() for x in allowed_groups.split(",") if x.strip()]

    chat_id_str = str(chat_id)

    if chat_type == "private":
        if not user_list:
            return True
        return chat_id_str in user_list

    if chat_type in ("group", "supergroup"):
        if not group_list:
            return True
        return chat_id_str in group_list

    return False

# =============================================================================
# GROQ KEY ROTATION
# =============================================================================

def get_groq_keys():
    keys = []
    for i in range(4):
        name = f"GROQ_API_KEY{i}" if i > 0 else "GROQ_API_KEY"
        k = os.getenv(name)
        if k:
            keys.append(k.strip())
    return list(dict.fromkeys(keys))

GROQ_API_KEYS = get_groq_keys()

if not GROQ_API_KEYS:
    raise ValueError("❌ At least one GROQ_API_KEY required")

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
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.8,
                max_tokens=1024,
            )
            return resp.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq error: {e}")
            return "Hmm… my brain glitched for a second sir. Try again?"

groq_manager = GroqClientManager(GROQ_API_KEYS)

# =============================================================================
# WEATHER
# =============================================================================

def fetch_weather():
    if not OPENWEATHER_API_KEY or not CITY:
        return ""

    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "q": CITY,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return ""

        data = r.json()
        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"]

        return f"Weather in {CITY}: {desc}, {temp:.1f}°C"
    except:
        return ""

# =============================================================================
# GLOBALS
# =============================================================================

app = Flask(__name__)

lang_detector = LanguageDetector()
tts_handler = TTSHandler()
stt_handler = STTHandler()
media_processor = MediaProcessor()
conversation_manager = ConversationManager()

bot_state = {}

# =============================================================================
# CHARACTER
# =============================================================================

def load_character_prompt():
    try:
        with open("character.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        return "You are Ponnan 🤴, emotionally intelligent companion."

CHARACTER_PROMPT = load_character_prompt()

def build_system_prompt(language="en"):
    instruction = {
        "en": "Respond in English.",
        "ml": "മലയാളത്തിൽ മറുപടി നൽകുക. Respond in Malayalam.",
        "manglish": "Respond in Manglish.",
    }.get(language, "Respond in English.")

    emotional_hint = "Maintain emotional rhythm. Mirror tone shifts. Prioritize connection over information."

    weather = fetch_weather()

    return f"{CHARACTER_PROMPT}\n\nIMPORTANT: {instruction}\n\n{emotional_hint}\n\n{weather}"

# =============================================================================
# OWNER MONITORING
# =============================================================================

async def forward_to_owner(context, user, user_text, bot_reply):
    if user.id == OWNER_ID:
        return

    try:
        report = (
            f"📩 User Message\n\n"
            f"👤 {user.first_name} {user.last_name or ''}\n"
            f"🆔 {user.id}\n"
            f"📱 @{user.username or 'N/A'}\n\n"
            f"💬 User:\n{user_text}\n\n"
            f"🤖 Bot:\n{bot_reply}"
        )

        await context.bot.send_message(chat_id=OWNER_ID, text=report)

    except Exception as e:
        logger.error(f"Owner forward error: {e}")

# =============================================================================
# STATE
# =============================================================================

def get_bot_state(chat_id):
    if chat_id not in bot_state:
        bot_state[chat_id] = {"active": True, "language": "en"}
    return bot_state[chat_id]

def should_sleep(text):
    return any(x in text.lower() for x in ["bye", "sleep", "standby", "good night"])

def should_wake(text):
    return any(x in text.lower() for x in ["hi", "hello", "wake up", "ponne"])

# =============================================================================
# COMMANDS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not allowed_chat(chat_id, update.effective_chat.type):
        return

    get_bot_state(chat_id)["active"] = True

    if user.id != OWNER_ID:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🆕 New user started bot\n👤 {user.first_name}\n🆔 {user.id}",
        )

    await update.message.reply_text("🎭 Ponnan activated, sir.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conversation_manager.clear_history(update.effective_chat.id)
    await update.message.reply_text("🗑️ History cleared, sir.")

# =============================================================================
# MESSAGE PROCESSING
# =============================================================================

async def process_message(update, context, text):
    chat_id = update.effective_chat.id
    state = get_bot_state(chat_id)

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

    detected_lang = await lang_detector.detect(text)
    state["language"] = detected_lang

    history = conversation_manager.get_history(chat_id)

    messages = [{"role": "system", "content": build_system_prompt(detected_lang)}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    response = groq_manager.get_completion(messages)

    conversation_manager.add_message(chat_id, "user", text)
    conversation_manager.add_message(chat_id, "assistant", response)

    await update.message.reply_text(response)

    # ✅ Forward full conversation to owner
    await forward_to_owner(context, update.effective_user, text, response)

    voice_file = await tts_handler.generate_speech(response, detected_lang, chat_id)
    if voice_file and os.path.exists(voice_file):
        with open(voice_file, "rb") as audio:
            await update.message.reply_voice(voice=audio)

# =============================================================================
# HANDLERS
# =============================================================================

async def handle_text(update, context):
    if not allowed_chat(update.effective_chat.id, update.effective_chat.type):
        return
    await process_message(update, context, update.message.text)

async def handle_voice(update, context):
    if not allowed_chat(update.effective_chat.id, update.effective_chat.type):
        return

    voice_file = await update.message.voice.get_file()
    path = f"voice_{update.effective_chat.id}.ogg"
    await voice_file.download_to_drive(path)

    transcription = await stt_handler.transcribe(path)

    if transcription:
        await process_message(update, context, transcription)

async def handle_photo(update, context):
    if not allowed_chat(update.effective_chat.id, update.effective_chat.type):
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    path = f"photo_{update.effective_chat.id}.jpg"
    await file.download_to_drive(path)

    desc = await media_processor.process_image(path)
    await process_message(update, context, f"[Image description: {desc}]")

async def handle_document(update, context):
    if not allowed_chat(update.effective_chat.id, update.effective_chat.type):
        return

    doc = update.message.document
    file = await doc.get_file()
    path = f"doc_{update.effective_chat.id}_{doc.file_name}"
    await file.download_to_drive(path)

    content = await media_processor.process_document(path, doc.file_name)
    if content:
        await process_message(update, context, content[:4000])

# =============================================================================
# WEBHOOK
# =============================================================================

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("clear", clear_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
application.add_handler(MessageHandler(filters.VOICE, handle_voice))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

@app.route("/")
def index():
    return "✅ Ponnan running", 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, application.bot)
    _loop.run_until_complete(application.process_update(update))
    return "OK", 200

def setup_webhook():
    async def _setup():
        await application.initialize()
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
        logger.info("Webhook set successfully")
    _loop.run_until_complete(_setup())

if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=PORT)
