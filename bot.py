import os
import json
import asyncio
import hashlib
import time
import tempfile
from urllib.parse import quote_plus

import requests
from flask import Flask, request
from groq import Groq
import edge_tts
import speech_recognition as sr

# =========================
# CONFIG
# =========================

EDGE_VOICE = "en-GB-RyanNeural"
WAKE_WORD = "pixel"  # change this if you want
STOP_PHRASES = ("stop listening", "standby", "bye")
MAX_HISTORY_MESSAGES = 20

conversation_history_by_chat = {}
listening_state_by_chat = {}
WEATHER_CACHE = {}

_groq_clients_by_key = {}
_groq_keys_cache = None
_groq_key_cursor = 0

TTS_CACHE_DIR = ".tts_cache"
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

app = Flask(__name__)

# =========================
# SYSTEM PROMPT
# =========================

CONTEXT_SYSTEM_PROMPT = """You are a witty, sarcastic AI assistant serving one user called sir. Stay confident, natural, and entertaining."""

# =========================
# ENV HELPERS
# =========================

def get_env(name):
    return os.environ.get(name)

def _get_all_groq_keys():
    global _groq_keys_cache
    if _groq_keys_cache:
        return _groq_keys_cache

    keys = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY1", "GROQ_API_KEY2", "GROQ_API_KEY3"):
        v = get_env(k)
        if v:
            keys.append(v.strip())

    _groq_keys_cache = list(dict.fromkeys(keys))
    return _groq_keys_cache

def _get_groq_client_for_key(key):
    if key not in _groq_clients_by_key:
        _groq_clients_by_key[key] = Groq(api_key=key)
    return _groq_clients_by_key[key]

# =========================
# PERMISSION SYSTEM
# =========================

def _allowed_chat(chat_id, chat_type):
    allowed_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    allowed_groups = os.environ.get("TELEGRAM_ALLOWED_GROUPS", "")

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

# =========================
# WEATHER
# =========================

def fetch_weather():
    api_key = get_env("OPENWEATHER_API_KEY")
    city = get_env("CITY")

    if not api_key or not city:
        return ""

    if WEATHER_CACHE.get("city") == city and time.time() - WEATHER_CACHE.get("timestamp", 0) < 300:
        return WEATHER_CACHE.get("text", "")

    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": city, "appid": api_key, "units": "metric"},
            timeout=10,
        )
        if r.status_code != 200:
            return ""

        data = r.json()
        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"]

        text = f"Weather in {city}: {desc}, {temp:.1f}°C"
        WEATHER_CACHE.update({"city": city, "timestamp": time.time(), "text": text})
        return text
    except:
        return ""

# =========================
# GROQ STREAMING
# =========================

def call_groq(chat_id, user_text):
    global _groq_key_cursor

    keys = _get_all_groq_keys()
    if not keys:
        return "No Groq keys configured.", None

    history = conversation_history_by_chat.get(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]
    conversation_history_by_chat[chat_id] = history

    system_prompt = CONTEXT_SYSTEM_PROMPT
    weather = fetch_weather()
    if weather:
        system_prompt += "\n\n" + weather

    messages = [{"role": "system", "content": system_prompt}] + history

    parts = []
    search_query = None

    start = _groq_key_cursor % len(keys)

    for i in range(len(keys)):
        key = keys[(start + i) % len(keys)]
        try:
            client = _get_groq_client_for_key(key)
            stream = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.7,
                max_tokens=512,
                stream=True,
            )

            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    parts.append(delta)

            full = "".join(parts).strip()

            if "WEB_SEARCH:" in full.upper():
                idx = full.upper().rfind("WEB_SEARCH:")
                search_query = full[idx + len("WEB_SEARCH:"):].strip()
                full = full[:idx].strip()

            history.append({"role": "assistant", "content": full})
            _groq_key_cursor = (start + i) % len(keys)
            return full, search_query

        except Exception as e:
            if "rate" in str(e).lower() or "quota" in str(e).lower():
                continue

    return "Groq failed.", None

# =========================
# TELEGRAM HELPERS
# =========================

def tg_api():
    return f"https://api.telegram.org/bot{get_env('TELEGRAM_BOT_TOKEN')}"

def tg_send_message(chat_id, text):
    if text:
        requests.post(tg_api() + "/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)

def tg_send_audio(chat_id, path):
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            requests.post(tg_api() + "/sendAudio", data={"chat_id": chat_id}, files={"audio": f})

def tg_get_file_url(file_id):
    r = requests.get(tg_api() + "/getFile", params={"file_id": file_id})
    file_path = r.json()["result"]["file_path"]
    token = get_env("TELEGRAM_BOT_TOKEN")
    return f"https://api.telegram.org/file/bot{token}/{file_path}"

# =========================
# TTS & VOICE
# =========================

async def _tts_async(text):
    key = hashlib.md5((EDGE_VOICE + text).encode()).hexdigest()
    path = os.path.join(TTS_CACHE_DIR, key + ".mp3")
    if not os.path.exists(path):
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(path)
    return path

def tts_to_mp3(text):
    try:
        return asyncio.run(_tts_async(text))
    except:
        return ""

def transcribe_voice(file_id):
    url = tg_get_file_url(file_id)
    with tempfile.TemporaryDirectory() as td:
        ogg = os.path.join(td, "voice.ogg")
        wav = os.path.join(td, "voice.wav")

        r = requests.get(url)
        with open(ogg, "wb") as f:
            f.write(r.content)

        os.system(f'ffmpeg -y -i "{ogg}" "{wav}" -loglevel error')

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav) as source:
            audio = recognizer.record(source)

        return recognizer.recognize_google(audio)

# =========================
# WEBHOOK
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive and running!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return "ok", 200

        msg = data.get("message", {})
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        chat_type = chat.get("type")

        if not chat_id or not _allowed_chat(chat_id, chat_type):
            return "ok", 200

        text = msg.get("text", "").strip()

        if not text and msg.get("voice"):
            text = transcribe_voice(msg["voice"]["file_id"])

        if not text:
            return "ok", 200

        lowered = text.lower()

        if chat_id not in listening_state_by_chat:
            listening_state_by_chat[chat_id] = False

        if not listening_state_by_chat[chat_id]:
            if WAKE_WORD in lowered:
                listening_state_by_chat[chat_id] = True
                tg_send_message(chat_id, "Yes sir.")
            return "ok", 200

        if any(p in lowered for p in STOP_PHRASES):
            listening_state_by_chat[chat_id] = False
            return "ok", 200

        response, search_query = call_groq(chat_id, text)

        if search_query:
            link = "https://www.google.com/search?q=" + quote_plus(search_query)
            response += f"\n\nSearch link: {link}"

        tg_send_message(chat_id, response)
        tg_send_audio(chat_id, tts_to_mp3(response))

        return "ok", 200

    except Exception as e:
        print("Webhook error:", e)
        return "ok", 200

# =========================
# RUN
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
