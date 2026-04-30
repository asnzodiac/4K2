"""
Adimma-Kann: Voice-First Telegram Bot
A witty, sarcastic AI assistant for 'sir'
"""

import os
import logging
import hashlib
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import asyncio
from groq import Groq
import random
from datetime import datetime
import json

# Import custom utilities
from utils.language_detector import LanguageDetector
from utils.tts_handler import TTSHandler
from utils.stt_handler import STTHandler
from utils.media_processor import MediaProcessor
from utils.conversation_manager import ConversationManager

# ============================================================================
# CONFIGURATION
# ============================================================================

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
GROQ_API_KEYS = os.getenv('GROQ_API_KEYS', '').split(',')  # Comma-separated keys
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
PORT = int(os.getenv('PORT', 8443))
OWNER_ID = 733340342  # Sir's user ID

# Validate configuration
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not GROQ_API_KEYS or GROQ_API_KEYS == ['']:
    raise ValueError("GROQ_API_KEYS environment variable is required")

# ============================================================================
# GLOBAL INSTANCES
# ============================================================================

app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

# Initialize utilities
lang_detector = LanguageDetector()
tts_handler = TTSHandler()
stt_handler = STTHandler()
media_processor = MediaProcessor()
conversation_manager = ConversationManager()

# Load character prompt
CHARACTER_PROMPT = ""
try:
    with open('character.txt', 'r', encoding='utf-8') as f:
        CHARACTER_PROMPT = f.read().strip()
    logger.info("Character prompt loaded successfully")
except Exception as e:
    logger.error(f"Failed to load character.txt: {e}")
    CHARACTER_PROMPT = "You are Adimma Kann, a witty and sarcastic AI assistant."

# Bot state management (wake/sleep per chat)
bot_state = {}  # {chat_id: {"active": True/False, "language": "en/ml/manglish"}}

# ============================================================================
# GROQ CLIENT WITH KEY ROTATION
# ============================================================================

class GroqClientManager:
    """Manages multiple Groq API keys with rotation"""
    
    def __init__(self, api_keys):
        self.api_keys = [key.strip() for key in api_keys if key.strip()]
        self.current_index = 0
        logger.info(f"Initialized with {len(self.api_keys)} Groq API keys")
    
    def get_client(self):
        """Get current Groq client and rotate"""
        client = Groq(api_key=self.api_keys[self.current_index])
        self.current_index = (self.current_index + 1) % len(self.api_keys)
        return client
    
    def get_completion(self, messages, model="llama-3.3-70b-versatile", max_retries=3):
        """Get completion with retry logic"""
        for attempt in range(max_retries):
            try:
                client = self.get_client()
                response = client.chat.completions.create(
                    messages=messages,
                    model=model,
                    temperature=0.8,
                    max_tokens=1024,
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.error(f"Groq API error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    raise
                continue

groq_manager = GroqClientManager(GROQ_API_KEYS)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_bot_state(chat_id):
    """Get or initialize bot state for a chat"""
    if chat_id not in bot_state:
        bot_state[chat_id] = {
            "active": True,  # Always awake by default
            "language": "en"
        }
    return bot_state[chat_id]

def should_sleep(text):
    """Check if message contains sleep command"""
    sleep_commands = [
        "bye", "standby", "stop listening", "sleep", 
        "good night", "goodnight", "നല്ല രാത്രി", "പോയി വരാം"
    ]
    text_lower = text.lower().strip()
    return any(cmd in text_lower for cmd in sleep_commands)

def should_wake(text):
    """Check if message contains wake command"""
    wake_commands = [
        "hi", "hello", "wake up", "adimma", "hey",
        "ഹലോ", "എണീക്ക്", "അടിമ്മ"
    ]
    text_lower = text.lower().strip()
    return any(cmd in text_lower for cmd in wake_commands)

async def notify_owner_new_user(user):
    """Notify owner when new user starts the bot"""
    try:
        message = (
            f"🆕 *New User Started Bot*\n\n"
            f"👤 Name: {user.first_name} {user.last_name or ''}\n"
            f"🆔 User ID: `{user.id}`\n"
            f"📱 Username: @{user.username or 'N/A'}\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await bot.send_message(
            chat_id=OWNER_ID,
            text=message,
            parse_mode='Markdown'
        )
        logger.info(f"Notified owner about new user: {user.id}")
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

def build_system_prompt(language="en"):
    """Build system prompt with language instruction"""
    lang_instruction = {
        "en": "Respond in English.",
        "ml": "മലയാളത്തിൽ മറുപടി നൽകുക. Respond in Malayalam.",
        "manglish": "Respond in Manglish (Romanized Malayalam mixed with English, like 'Entha sir, nallapole aanallo?')."
    }
    
    instruction = lang_instruction.get(language, lang_instruction["en"])
    return f"{CHARACTER_PROMPT}\n\nIMPORTANT: {instruction}"

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Initialize bot state
    state = get_bot_state(chat_id)
    state["active"] = True
    
    # Notify owner if new user (not owner)
    if user.id != OWNER_ID:
        await notify_owner_new_user(user)
    
    welcome_message = (
        "🎭 *Adimma Kann at your service!*\n\n"
        "I'm your witty, slightly sarcastic AI assistant.\n\n"
        "💬 Send me:\n"
        "• Voice messages (English/Malayalam/Manglish)\n"
        "• Text messages\n"
        "• Photos\n"
        "• Documents (PDFs)\n\n"
        "🌐 I'll respond in the same language you use!\n\n"
        "⌨️ Commands:\n"
        "/help - Show instructions\n"
        "/clear - Clear conversation history\n\n"
        "😴 Say 'bye' or 'sleep' to put me on standby\n"
        "👋 Say 'hi' or 'wake up' to activate me again"
    )
    
    await update.message.reply_text(welcome_message, parse_mode='Markdown')
    logger.info(f"User {user.id} started the bot")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help or /instruction command"""
    help_text = """
🎭 *Adimma Kann - Usage Instructions*

*How to Use:*
1️⃣ Just talk to me! Send voice or text messages
2️⃣ I understand English, Malayalam, and Manglish
3️⃣ I'll reply in the same language you use

*Voice Messages:*
🎤 Send voice notes - I'll transcribe and respond with voice + text

*Text Messages:*
💬 Type anything - I'll reply with wit and sarcasm

*Images & Documents:*
🖼️ Send photos - I'll analyze and comment
📄 Send PDFs - I'll read and discuss them

*Sleep/Wake Commands:*
😴 Sleep: "bye", "standby", "good night", "sleep"
👋 Wake: "hi", "hello", "wake up", "adimma"

*Language Examples:*
🇬🇧 English: "What's the weather like?"
🇮🇳 Malayalam: "ഇന്ന് എന്താ പ്രോഗ്രാം?"
🔤 Manglish: "Entha sir, nallapole aanallo?"

*Commands:*
/start - Restart the bot
/help - Show this message
/clear - Clear conversation history

*Pro Tips:*
✨ I'm always listening by default
✨ I remember our last 20 messages
✨ I can handle multiple languages in one conversation
✨ My personality is slightly roasting but always helpful!

Ready to chat? 🚀
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clear command"""
    chat_id = update.effective_chat.id
    conversation_manager.clear_history(chat_id)
    
    await update.message.reply_text(
        "🗑️ Conversation history cleared!\n\nStarting fresh, sir! 🎬"
    )
    logger.info(f"Cleared conversation history for chat {chat_id}")

# ============================================================================
# MESSAGE HANDLERS
# ============================================================================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages"""
    chat_id = update.effective_chat.id
    state = get_bot_state(chat_id)
    
    try:
        # Download voice file
        voice_file = await update.message.voice.get_file()
        voice_path = f"voice_{chat_id}_{datetime.now().timestamp()}.ogg"
        await voice_file.download_to_drive(voice_path)
        
        # Transcribe
        await update.message.reply_text("🎧 Listening...")
        transcription = await stt_handler.transcribe(voice_path)
        
        # Clean up
        if os.path.exists(voice_path):
            os.remove(voice_path)
        
        if not transcription:
            await update.message.reply_text("😕 Sorry sir, couldn't hear you clearly. Try again?")
            return
        
        logger.info(f"Voice transcribed: {transcription}")
        
        # Process as text message
        await process_message(update, context, transcription)
        
    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text("❌ Oops! Something went wrong with the voice message.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages"""
    chat_id = update.effective_chat.id
    state = get_bot_state(chat_id)
    
    if not state["active"]:
        return
    
    try:
        await update.message.reply_text("🖼️ Analyzing image...")
        
        # Download photo
        photo = update.message.photo[-1]  # Get highest resolution
        photo_file = await photo.get_file()
        photo_path = f"photo_{chat_id}_{datetime.now().timestamp()}.jpg"
        await photo_file.download_to_drive(photo_path)
        
        # Process image
        description = await media_processor.process_image(photo_path)
        
        # Clean up
        if os.path.exists(photo_path):
            os.remove(photo_path)
        
        # Get caption if any
        caption = update.message.caption or "What do you think about this image?"
        
        # Process with description
        user_message = f"{caption}\n\n[Image description: {description}]"
        await process_message(update, context, user_message)
        
    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        await update.message.reply_text("❌ Couldn't process the image, sir!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document messages (especially PDFs)"""
    chat_id = update.effective_chat.id
    state = get_bot_state(chat_id)
    
    if not state["active"]:
        return
    
    try:
        document = update.message.document
        
        # Check file size (limit to 10MB)
        if document.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("📄 File too large! Please send files under 10MB.")
            return
        
        await update.message.reply_text("📄 Processing document...")
        
        # Download document
        doc_file = await document.get_file()
        doc_path = f"doc_{chat_id}_{datetime.now().timestamp()}_{document.file_name}"
        await doc_file.download_to_drive(doc_path)
        
        # Process document
        content = await media_processor.process_document(doc_path, document.file_name)
        
        # Clean up
        if os.path.exists(doc_path):
            os.remove(doc_path)
        
        if not content:
            await update.message.reply_text("😕 Couldn't read the document. Is it a valid PDF or text file?")
            return
        
        # Get caption if any
        caption = update.message.caption or "Please analyze this document."
        
        # Process with content (truncate if too long)
        if len(content) > 4000:
            content = content[:4000] + "... (truncated)"
        
        user_message = f"{caption}\n\n[Document content:\n{content}]"
        await process_message(update, context, user_message)
        
    except Exception as e:
        logger.error(f"Document handling error: {e}")
        await update.message.reply_text("❌ Couldn't process the document, sir!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    text = update.message.text
    await process_message(update, context, text)

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Core message processing logic"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    state = get_bot_state(chat_id)
    
    # Check for sleep command
    if should_sleep(text):
        state["active"] = False
        response = random.choice([
            "😴 Going on standby, sir. Wake me when you need me!",
            "💤 Alright, taking a power nap. Just say 'hi' when you're back!",
            "🌙 Good night, sir! Standing by...",
            "😌 Okay sir, I'll be here when you need me."
        ])
        await update.message.reply_text(response)
        logger.info(f"Bot sleeping for chat {chat_id}")
        return
    
    # Check for wake command
    if should_wake(text):
        if not state["active"]:
            state["active"] = True
            response = random.choice([
                "👋 Wide awake, sir! What can I do for you?",
                "⚡ Back in action! What's up?",
                "🎯 Activated and ready, sir!",
                "😎 I'm here! What do you need?"
            ])
            await update.message.reply_text(response)
            logger.info(f"Bot waking up for chat {chat_id}")
            return
    
    # If bot is sleeping, ignore
    if not state["active"]:
        return
    
    try:
        # Detect language
        detected_lang = await lang_detector.detect(text)
        state["language"] = detected_lang
        logger.info(f"Detected language: {detected_lang} for message: {text[:50]}")
        
        # Get conversation history
        history = conversation_manager.get_history(chat_id)
        
        # Build messages for Groq
        messages = [
            {"role": "system", "content": build_system_prompt(detected_lang)}
        ]
        
        # Add conversation history
        for msg in history:
            messages.append(msg)
        
        # Add current message
        messages.append({"role": "user", "content": text})
        
        # Show typing indicator
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        
        # Get AI response
        response = groq_manager.get_completion(messages)
        
        # Save to conversation history
        conversation_manager.add_message(chat_id, "user", text)
        conversation_manager.add_message(chat_id, "assistant", response)
        
        # Send text response
        await update.message.reply_text(response)
        
        # Generate and send voice response
        await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
        
        voice_file = await tts_handler.generate_speech(response, detected_lang)
        
        if voice_file and os.path.exists(voice_file):
            with open(voice_file, 'rb') as audio:
                await update.message.reply_voice(voice=audio)
            logger.info(f"Sent voice response in {detected_lang}")
        else:
            logger.warning("Voice generation failed, text-only response sent")
        
    except Exception as e:
        logger.error(f"Message processing error: {e}")
        await update.message.reply_text(
            "❌ Oops! My circuits got tangled. Give me a moment, sir!"
        )

# ============================================================================
# WEBHOOK SETUP
# ============================================================================

@app.route('/')
def index():
    """Health check endpoint"""
    return "Adimma Kann is alive! 🎭", 200

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Handle incoming updates via webhook"""
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        asyncio.run(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

# ============================================================================
# APPLICATION INITIALIZATION
# ============================================================================

# Create application
application = Application.builder().token(BOT_TOKEN).build()

# Register handlers
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("instruction", help_command))
application.add_handler(CommandHandler("clear", clear_command))
application.add_handler(MessageHandler(filters.VOICE, handle_voice))
application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    # Set webhook
    asyncio.run(bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}"))
    logger.info(f"Webhook set to: {WEBHOOK_URL}/{BOT_TOKEN}")
    
    # Run Flask app
    app.run(host='0.0.0.0', port=PORT)
