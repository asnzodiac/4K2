import os
import json
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta

import requests
import edge_tts
import speech_recognition as sr
from flask import Flask, request
from groq import Groq
from PIL import Image
import io
import PyPDF2

# ========================= CONFIG =========================
app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY"),
    os.getenv("GROQ_API_KEY1"),
    os.getenv("GROQ_API_KEY2"),
    os.getenv("GROQ_API_KEY3"),
]
GROQ_API_KEYS = [k for k in GROQ_API_KEYS if k]  # remove None

OWNER_ID = 733340342
WAKE_WORDS = ["hi", "hello", "wake up", "adimma"]
SLEEP_COMMANDS = ["bye", "standby", "stop listening", "sleep", "good night"]

# Global storage
conversation_history = {}      # chat_id -> list of messages
listening_state = {}           # chat_id -> bool (True = listening)
tts_cache = {}                 # md5 -> filename

# Load character prompt
def load_character():
    try:
        with open("character.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("character.txt not found. Using default.")
        return "You are a witty and sarcastic AI assistant called Adimma Kann serving sir."

SYSTEM_PROMPT = load_character()

# Telegram helper functions
def tg_send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def tg_send_audio(chat_id, audio_path):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVoice"
    with open(audio_path, "rb") as audio:
        requests.post(url, data={"chat_id": chat_id}, files={"voice": audio})

def tg_send_photo(chat_id, photo_path, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        requests.post(url, data={"chat_id": chat_id, "caption": caption}, files={"photo": photo})

# Notify owner about new users
def notify_owner(user):
    try:
        text = f"🆕 New user started the bot:\n" \
               f"ID: {user.get('id')}\n" \
               f"Name: {user.get('first_name')}\n" \
               f"Username: @{user.get('username') or 'N/A'}"
        tg_send_message(OWNER_ID, text)
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

# Check if chat is allowed (add your allowed users/groups if needed)
def is_allowed(chat_id, chat_type):
    return True  # You can add restrictions later

# TTS with caching
async def text_to_speech(text: str) -> str:
    if not text:
        return None
    hash_key = hashlib.md5(text.encode()).hexdigest()
    if hash_key in tts_cache:
        return tts_cache[hash_key]

    try:
        communicate = edge_tts.Communicate(text, voice="en-GB-RyanNeural")
        filename = f"tts_cache/{hash_key}.mp3"
        os.makedirs("tts_cache", exist_ok=True)
        await communicate.save(filename)
        tts_cache[hash_key] = filename
        return filename
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None

def tts_to_mp3(text):
    return asyncio.run(text_to_speech(text))

# Improved STT with Malayalam support
def transcribe_voice(file_id):
    try:
        # Download voice
        file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(file_url).json()["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        
        ogg_path = f"voice_{file_id}.ogg"
        with open(ogg_path, "wb") as f:
            f.write(requests.get(download_url).content)

        # Convert ogg to wav
        wav_path = f"voice_{file_id}.wav"
        os.system(f"ffmpeg -i {ogg_path} -ar 16000 -ac 1 -y {wav_path} -loglevel quiet")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        # Try Malayalam first, then English (India)
        for lang in ["ml-IN", "en-IN"]:
            try:
                text = recognizer.recognize_google(audio, language=lang)
                if text:
                    os.remove(ogg_path)
                    os.remove(wav_path)
                    return text.strip()
            except:
                continue

        os.remove(ogg_path)
        os.remove(wav_path)
        return None

    except Exception as e:
        logger.error(f"STT Error: {e}")
        return None

# Simple image/document description using Groq Vision
def describe_media(file_id, is_photo=True, mime_type=""):
    try:
        # Download file
        file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        file_path = requests.get(file_url).json()["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        
        content = requests.get(download_url).content

        if is_photo or "image" in mime_type:
            # For images - use Groq vision model later in main flow
            return "Image received. Describe this image in detail."

        elif "pdf" in mime_type.lower():
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
            text = ""
            for page in pdf_reader.pages[:3]:   # first 3 pages
                text += page.extract_text() or ""
            return f"PDF content: {text[:1500]}" if text else "PDF received but could not extract text."

        return "Document received."

    except Exception as e:
        logger.error(f"Media description error: {e}")
        return "Failed to process the media."

# Groq call (with key rotation)
current_key_index = 0

def call_groq(chat_id, user_message, media_description=None):
    global current_key_index
    if not GROQ_API_KEYS:
        return "Sorry sir, API keys are not configured.", None

    client = Groq(api_key=GROQ_API_KEYS[current_key_index])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add conversation history
    if chat_id in conversation_history:
        messages.extend(conversation_history[chat_id][-15:])

    # Add media context if any
    if media_description:
        messages.append({"role": "user", "content": f"[Media] {media_description}\n\nUser said: {user_message}"})
    else:
        messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.8,
            max_tokens=800
        )
        reply = response.choices[0].message.content.strip()

        # Save to history
        if chat_id not in conversation_history:
            conversation_history[chat_id] = []
        conversation_history[chat_id].append({"role": "user", "content": user_message})
        conversation_history[chat_id].append({"role": "assistant", "content": reply})

        return reply, None

    except Exception as e:
        logger.error(f"Groq Error: {e}")
        current_key_index = (current_key_index + 1) % len(GROQ_API_KEYS)
        return "Sir, I'm having some trouble thinking right now. Try again.", None

# ====================== WEBHOOK ======================
@app.route("/", methods=["GET"])
def home():
    return "Adimma Kann is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        if not update or "message" not in update:
            return "ok", 200

        msg = update["message"]
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")

        if not chat_id or not is_allowed(chat_id, chat_type):
            return "ok", 200

        user = msg.get("from", {})

        # Notify owner for new private chats
        if chat_type == "private" and msg.get("text") == "/start":
            notify_owner(user)

        # Get input
        user_text = None
        media_description = None
        is_media = False

        if "text" in msg:
            user_text = msg["text"].strip()

        elif "voice" in msg:
            user_text = transcribe_voice(msg["voice"]["file_id"])

        elif "photo" in msg:
            file_id = msg["photo"][-1]["file_id"]
            caption = msg.get("caption", "")
            media_description = describe_media(file_id, is_photo=True)
            user_text = caption or "Describe this image"
            is_media = True

        elif "document" in msg:
            file_id = msg["document"]["file_id"]
            mime = msg["document"].get("mime_type", "")
            media_description = describe_media(file_id, is_photo=False, mime_type=mime)
            user_text = msg.get("caption", "Analyze this document")
            is_media = True

        if not user_text:
            return "ok", 200

        user_text_lower = user_text.lower().strip()

        # Command Handling
        if user_text_lower in ["/instruction", "/help"]:
            help_text = (
                "✅ *Adimma Kann Instructions*\n\n"
                "• I am always listening by default.\n"
                "• Say 'bye', 'standby', 'stop listening' or 'sleep' → I will stop responding.\n"
                "• Say 'hi', 'hello', or 'wake up' → I will start listening again.\n"
                "• Send voice messages in English or Malayalam.\n"
                "• Send photos or documents — I can describe them.\n"
                "• /clear → Clear conversation history\n"
                "• /instruction → Show this message"
            )
            tg_send_message(chat_id, help_text)
            return "ok", 200

        if user_text_lower == "/clear":
            conversation_history[chat_id] = []
            tg_send_message(chat_id, "Conversation history cleared, sir.")
            return "ok", 200

        # Listening Logic
        if chat_id not in listening_state:
            listening_state[chat_id] = True   # Always awake by default

        if any(cmd in user_text_lower for cmd in SLEEP_COMMANDS):
            listening_state[chat_id] = False
            tg_send_message(chat_id, "Understood sir. Standing by.")
            return "ok", 200

        if any(w in user_text_lower for w in WAKE_WORDS):
            listening_state[chat_id] = True
            tg_send_message(chat_id, "Yes sir, I'm back and listening.")
            return "ok", 200

        if not listening_state[chat_id]:
            return "ok", 200   # Ignore messages when sleeping

        # Get AI Reply
        reply, _ = call_groq(chat_id, user_text, media_description)

        # Send reply as voice + text
        audio_path = tts_to_mp3(reply)
        if audio_path:
            tg_send_audio(chat_id, audio_path)
        else:
            tg_send_message(chat_id, reply)

    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        try:
            tg_send_message(chat_id, "Something went wrong sir.")
        except:
            pass

    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
