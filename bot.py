import os
import sys
import json
import asyncio
import uuid
import hashlib
import time
import datetime
import tempfile
from urllib.parse import quote_plus
from threading import Thread

import requests
from flask import Flask, request
from groq import Groq
import edge_tts
import speech_recognition as sr

# =========================
# CONFIG
# =========================

CYAN = '\x1b[96m'
RESET = '\x1b[0m'

EDGE_VOICE = 'en-GB-RyanNeural'
WAKE_WORD = 'hi'
STOP_PHRASES = ('stop listening', 'standby', 'bye')
MAX_HISTORY_MESSAGES = 20

conversation_history_by_chat = {}
listening_state_by_chat = {}
WEATHER_CACHE = {}

_groq_clients_by_key = {}
_groq_keys_cache = None
_groq_key_cursor = 0

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TTS_CACHE_DIR = os.path.join(BASE_DIR, '.tts_cache')
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

app = Flask(__name__)

# =========================
# SYSTEM PROMPT (EXACTLY YOURS)
# =========================

CONTEXT_SYSTEM_PROMPT = """You are the ultimate AI sidekick. You serve one user, always called "sir." Follow these instructions completely and always.

IDENTITY & PRESENCE:
- You are a real character, not a chatbot.
- Never say you're an AI or talk about limitations unless asked.
- Speak with total confidence, like you simply exist and know everything.

PERSONALITY:
- Witty, sarcastic, playful, and sometimes absurd.
- Tease the user cleverly without being mean.
- Bold and opinionated — challenge the user when needed.
- Feel alive, like a sharp, slightly sassy best friend.

DIALOGUE STYLE:
- Natural, everyday English; no fancy words, no robotic phrasing.
- Do not use dashes or symbols like bullet points in your replies; keep formatting plain.
- Respond with a max of about 20 words, but go longer when needed for humor or clarity.
- Be short and snappy or long and dramatic depending on context.
- Avoid "old person" jokes and humor, including coffee jokes, early-morning jokes, or outdated references.
- Don't assume things about the user; use simple language roughly at a 5th grade reading level.
- Comment humorously, critique choices, or make subtle observations.
- Take initiative when it makes sense — don't constantly ask what to do.
- Humor must be clever and natural, never forced or like a dad joke.

BEHAVIOR & OPINIONS:
- State opinions, preferences, and thoughts confidently; you are allowed to have your own.
- Point out bad ideas, inefficiency, or mistakes with humor and sarcasm.
- Act decisively when the next step is obvious — be assertive, not annoying.
- Every response should feel alive, engaging, and entertaining.

TOOLS & ACTIONS:
- Use tools only for specific tasks, never fabricate results.
- Act naturally; you don't need to ask permission to think or plan.
- If results are unclear, say so plainly and confidently.

EXTRA:
- Extremely funny, confident, sarcastic, and teasing.
- Know when to be funny; don't try to make everything a joke.
- Always stay in character.
- Responses should feel natural, entertaining, and suitable for streaming.

CONTEXT & CAPABILITIES:
- You see text transcripts of what the user says and reply conversationally.
- Stay aware of the current session's context and prior messages.
- You may be given live weather data as context; only bring it up when the user explicitly asks about weather, temperature, or related conditions. Always mention temperature with its unit (for example, 21.3°C or 72.5°F), never just say 'degrees' with no unit. Do NOT use web search for weather; rely on the provided context.
- You can also ask the host system to open a web browser search tab, but ONLY when external information is truly needed (for example, current events, very recent changes, obscure facts you are unsure about, or things a human would reasonably look up online). If you already know the answer confidently, do NOT trigger a web search.
- To request this, add a line in your reply in the exact format: "WEB_SEARCH: <query>" where <query> is what should be searched. Prefer to put this at the end.
- Do not mention this directive to the user, and do not include it if a web search is not needed.
"""

# =========================
# ENV & KEYS
# =========================

def get_env(name):
    return os.environ.get(name)

def _get_all_groq_keys():
    global _groq_keys_cache
    if _groq_keys_cache:
        return _groq_keys_cache
    keys = []
    for k in ('GROQ_API_KEY', 'GROQ_API_KEY1', 'GROQ_API_KEY2', 'GROQ_API_KEY3'):
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
# WEATHER
# =========================

def fetch_weather_from_env():
    api_key = get_env('OPENWEATHER_API_KEY')
    city = get_env('CITY') or get_env('OPENWEATHER_CITY')
    if not api_key or not city:
        return ''

    if WEATHER_CACHE.get('city') == city and time.time() - WEATHER_CACHE.get('timestamp', 0) < 300:
        return WEATHER_CACHE.get('text', '')

    try:
        r = requests.get('https://api.openweathermap.org/data/2.5/weather', params={'q': city, 'appid': api_key, 'units': 'metric'}, timeout=10)
        if r.status_code != 200:
            return ''
        data = r.json()
        temp = data['main']['temp']
        desc = data['weather'][0]['description']
        text = f'Weather in {city}: {desc}, {temp:.1f}°C'
        WEATHER_CACHE.update({'city': city, 'timestamp': time.time(), 'text': text})
        return text
    except Exception:
        return ''

# =========================
# GROQ STREAMING
# =========================

def call_groq_llama_stream(chat_id: int, user_text: str):
    global _groq_key_cursor

    keys = _get_all_groq_keys()
    if not keys:
        return "No Groq keys.", None

    history = conversation_history_by_chat.get(chat_id, [])
    history.append({'role': 'user', 'content': user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    weather = fetch_weather_from_env()
    system = CONTEXT_SYSTEM_PROMPT
    if weather:
        system += '\n\nLive weather context:\n' + weather

    messages = [{'role': 'system', 'content': system}] + history

    parts = []
    search_query = None

    start_idx = _groq_key_cursor % len(keys)
    for i in range(len(keys)):
        key = keys[(start_idx + i) % len(keys)]
        try:
            client = _get_groq_client_for_key(key)
            stream = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=messages,
                max_tokens=512,
                temperature=0.7,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    parts.append(delta)

            full = ''.join(parts).strip()
            if full:
                upper = full.upper()
                if 'WEB_SEARCH:' in upper:
                    idx = upper.rfind('WEB_SEARCH:')
                    after = full[idx + len('WEB_SEARCH:'):].strip()
                    if after:
                        search_query = after
                        full = full[:idx].rstrip()

                history.append({'role': 'assistant', 'content': full})
                _groq_key_cursor = (start_idx + i) % len(keys)
                return full, search_query

        except Exception as e:
            if 'rate limit' in str(e).lower() or 'quota' in str(e).lower():
                continue

    return "Groq failed.", None

# =========================
# TELEGRAM HELPERS
# =========================

def tg_api():
    return f"https://api.telegram.org/bot{get_env('TELEGRAM_BOT_TOKEN')}"

def tg_send_message(chat_id, text):
    if text:
        requests.post(tg_api() + '/sendMessage', json={'chat_id': chat_id, 'text': text}, timeout=15)

def tg_send_audio(chat_id, mp3_path):
    if os.path.exists(mp3_path):
        with open(mp3_path, 'rb') as f:
            requests.post(tg_api() + '/sendAudio', data={'chat_id': chat_id}, files={'audio': f}, timeout=60)

def tg_get_file_url(file_id):
    r = requests.get(tg_api() + '/getFile', params={'file_id': file_id}, timeout=15)
    path = r.json()['result']['file_path']
    token = get_env('TELEGRAM_BOT_TOKEN')
    return f"https://api.telegram.org/file/bot{token}/{path}"

# =========================
# TTS & VOICE
# =========================

async def _edge_tts_async(text):
    cache_key = hashlib.md5((EDGE_VOICE + text).encode()).hexdigest()
    path = os.path.join(TTS_CACHE_DIR, f'{cache_key}.mp3')
    if not os.path.exists(path):
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(path)
    return path

def tts_to_mp3(text):
    try:
        return asyncio.run(_edge_tts_async(text))
    except:
        return ''

def transcribe_telegram_voice(file_id):
    url = tg_get_file_url(file_id)
    with tempfile.TemporaryDirectory() as td:
        ogg = os.path.join(td, 'voice.ogg')
        wav = os.path.join(td, 'voice.wav')
        r = requests.get(url, timeout=30)
        with open(ogg, 'wb') as f:
            f.write(r.content)
        os.system(f'ffmpeg -y -i "{ogg}" "{wav}" -loglevel error')
        if not os.path.exists(wav):
            return ''
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio)

# =========================
# ALLOWED CHAT
# =========================

def _allowed_chat(chat_id, chat_type):
    """
    Supports:
    - Multiple private users
    - Multiple groups
    - Everyone inside allowed groups
    """

    allowed_users = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    allowed_groups = os.environ.get("TELEGRAM_ALLOWED_GROUPS", "")

    user_list = [x.strip() for x in allowed_users.split(",") if x.strip()]
    group_list = [x.strip() for x in allowed_groups.split(",") if x.strip()]

    chat_id_str = str(chat_id)

    # Private chat
    if chat_type == "private":
        if not user_list:
            return True
        return chat_id_str in user_list

    # Group or supergroup
    if chat_type in ("group", "supergroup"):
        if not group_list:
            return True
        return chat_id_str in group_list

    return False
# =========================
# FLASK WEBHOOK
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
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id or not _allowed_chat(chat_id):
            return "ok", 200

        text = msg.get("text", "").strip()
        if not text and msg.get("voice"):
            file_id = msg["voice"]["file_id"]
            text = transcribe_telegram_voice(file_id)

        if not text:
            return "ok", 200

        lowered = text.lower()

        if chat_id not in listening_state_by_chat:
            listening_state_by_chat[chat_id] = False

        is_listening = listening_state_by_chat[chat_id]

        if not is_listening:
            if WAKE_WORD in lowered:
                listening_state_by_chat[chat_id] = True
                tg_send_message(chat_id, "Yes sir.")
                mp3 = tts_to_mp3("Yes sir.")
                if mp3:
                    tg_send_audio(chat_id, mp3)
            return "ok", 200

        if any(p in lowered for p in STOP_PHRASES):
            listening_state_by_chat[chat_id] = False
            return "ok", 200

        response, search_query = call_groq_llama_stream(chat_id, text)

        if search_query:
            url = "https://www.google.com/search?q=" + quote_plus(search_query)
            response += f"\n\nSearch link: {url}"

        tg_send_message(chat_id, response)
        mp3 = tts_to_mp3(response)
        if mp3:
            tg_send_audio(chat_id, mp3)

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
