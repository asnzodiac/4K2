import os
import sys
import json
import asyncio
import uuid
import hashlib
import time
import datetime
import tempfile
import webbrowser
from urllib.parse import quote_plus
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from groq import Groq
import edge_tts
import speech_recognition as sr

try:
    import colorama
    try:
        colorama.just_fix_windows_console()
    except Exception:
        pass
except ImportError:
    pass


CYAN  = '\x1b[96m'   # bright cyan terminal color
RESET = '\x1b[0m'    # reset terminal color

EDGE_VOICE = 'en-GB-RyanNeural'   # Microsoft Edge TTS voice (British male)
WAKE_WORD  = 'jarvis'              # lowercase, the spoken trigger word
STOP_PHRASES = ('stop listening', 'standby')  # spoken phrases that pause listening
MAX_HISTORY_MESSAGES = 20          # maximum messages kept in conversation history


conversation_history_by_chat = {}   # chat_id -> list[dict], holds full chat history
listening_state_by_chat = {}        # chat_id -> bool
WEATHER_CACHE: dict = {}            # global dict, caches weather results

_groq_clients_by_key = {}           # api_key -> Groq client
_groq_keys_cache = None             # list[str]
_groq_key_cursor = 0                # round-robin cursor


BASE_DIR = os.path.abspath(
    os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__)
)
TTS_CACHE_DIR = os.path.join(BASE_DIR, '.tts_cache')
ENV_FILE      = os.path.join(BASE_DIR, '.env')


def cyan_print(*args, **kwargs) -> None:
    """Print in cyan (main interaction only)."""
    print(CYAN, end='')
    print(*args, **kwargs)
    print(RESET, end='')


def cyan_write(text: str) -> None:
    """Write in cyan without adding newlines."""
    if not text:
        return
    sys.stdout.write(CYAN + text + RESET)
    sys.stdout.flush()


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


def get_env_ci(name: str):
    """Case-insensitive environment lookup, falling back to .env if needed."""
    for k, v in os.environ.items():
        try:
            if k.lower() == name.lower():
                return v
        except Exception:
            pass

    try:
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', maxsplit=1)
                if key.strip().lower() == name.lower():
                    return value.strip()
    except Exception:
        pass

    return None


def _load_preferred_key_into_env(service_name: str) -> None:
    """Load a key for service_name from .env (same directory) into os.environ."""
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', maxsplit=1)
                key = key.strip()
                if key == service_name:
                    os.environ[key] = value.strip()
                    return
    except Exception:
        pass


def _load_groq_keys_into_env() -> None:
    """Load GROQ_API_KEY, GROQ_API_KEY1..3 from .env into os.environ (if present)."""
    for k in ('GROQ_API_KEY', 'GROQ_API_KEY1', 'GROQ_API_KEY2', 'GROQ_API_KEY3'):
        _load_preferred_key_into_env(k)


def _get_all_groq_keys():
    """Return list of configured Groq keys (non-empty), in fixed priority order."""
    global _groq_keys_cache
    if isinstance(_groq_keys_cache, list) and _groq_keys_cache:
        return _groq_keys_cache

    keys = []
    for name in ('GROQ_API_KEY', 'GROQ_API_KEY1', 'GROQ_API_KEY2', 'GROQ_API_KEY3'):
        val = get_env_ci(name)
        if val is None:
            continue
        val = str(val).strip()
        if val:
            keys.append(val)

    # De-dup while preserving order
    seen = set()
    uniq = []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        uniq.append(k)

    _groq_keys_cache = uniq
    return uniq


def _get_groq_client_for_key(api_key: str):
    """Client cache per API key (saves setup time)."""
    if not api_key:
        return None
    client = _groq_clients_by_key.get(api_key)
    if client is None:
        client = Groq(api_key=api_key)
        _groq_clients_by_key[api_key] = client
    return client


def fetch_weather_from_env(city_override=None):
    """Fetch current weather via OpenWeatherMap using city and key from env (case-insensitive)."""
    api_key = get_env_ci('OPENWEATHER_API_KEY')
    if not api_key:
        return 'Weather unavailable: OPENWEATHER_API_KEY is not set in the environment or .env.'

    city = city_override or get_env_ci('OPENWEATHER_CITY') or get_env_ci('CITY')
    if not city:
        return 'Weather unavailable: CITY (or OPENWEATHER_CITY) is not set in the environment or .env.'

    try:
        cached_city = WEATHER_CACHE.get('city')
        cached_ts   = float(WEATHER_CACHE.get('timestamp', 0))
        cached_text = WEATHER_CACHE.get('text')
        if (
            cached_city
            and isinstance(cached_city, str)
            and cached_city.lower() == str(city).lower()
            and (time.time() - cached_ts) < 300
            and isinstance(cached_text, str)
            and cached_text.strip()
        ):
            return cached_text
    except Exception:
        pass

    try:
        resp = requests.get(
            'https://api.openweathermap.org/data/2.5/weather',
            params={'q': city, 'appid': api_key, 'units': 'metric'},
            timeout=8,
        )
    except Exception as e:
        return f'Weather unavailable: network error calling OpenWeatherMap ({e}).'

    if resp.status_code != 200:
        try:
            msg = resp.json().get('message', resp.text)
        except Exception:
            msg = resp.text
        return f'Weather unavailable: OpenWeatherMap error ({resp.status_code}): {msg}'

    try:
        data         = resp.json()
        main         = data.get('main', {})
        weather_list = data.get('weather', [])
        wind         = data.get('wind', {})
        sys_block    = data.get('sys', {})

        temp_c      = main.get('temp')
        feels_c     = main.get('feels_like')
        humidity    = main.get('humidity')
        description = weather_list[0]['description'] if weather_list else 'unknown'
        wind_speed  = wind.get('speed')
        country     = sys_block.get('country', '')
    except Exception as e:
        return f'Weather unavailable: error parsing OpenWeatherMap response ({e}).'

    fahrenheit_countries = {'US', 'LR', 'MM', 'BS', 'BZ', 'KY', 'FM', 'GU', 'MH', 'PR', 'PW', 'VI'}
    use_fahrenheit = country in fahrenheit_countries

    if use_fahrenheit and temp_c is not None:
        temp = temp_c * 9.0 / 5.0 + 32.0
        temp_unit = 'degrees Fahrenheit'
    else:
        temp = temp_c
        temp_unit = 'degrees Celsius'

    if use_fahrenheit and feels_c is not None:
        feels = feels_c * 9.0 / 5.0 + 32.0
    else:
        feels = feels_c

    parts = [f'Weather in {city} (via OpenWeatherMap): {description}']
    if temp is not None:
        parts.append(f'{temp:.1f} {temp_unit}')
    if feels is not None:
        parts.append(f'feels like {feels:.1f} {temp_unit}')
    if humidity is not None:
        parts.append(f'humidity {humidity}%')
    if wind_speed is not None:
        parts.append(f'wind {wind_speed} m/s')
    text = ', '.join(parts)

    WEATHER_CACHE['city']      = city
    WEATHER_CACHE['timestamp'] = time.time()
    WEATHER_CACHE['text']      = text
    return text


def open_search_tab(query: str) -> None:
    """Open a new browser tab for the given query."""
    if not query:
        return
    try:
        url = 'https://www.google.com/search?q=' + quote_plus(query.strip())
        webbrowser.open_new_tab(url)
    except Exception:
        pass


async def _edge_tts_to_mp3_async(text: str) -> str:
    """Create (or reuse cached) MP3 for given text via Edge TTS; return file path."""
    if not text:
        return ''
    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.md5((EDGE_VOICE + '\n' + text).encode('utf-8')).hexdigest()
    out_path = os.path.join(TTS_CACHE_DIR, f'{cache_key}.mp3')
    if not os.path.exists(out_path):
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        await communicate.save(out_path)
    return out_path


def tts_to_mp3_path(text: str) -> str:
    """Synchronous wrapper to get cached MP3 path from Edge TTS."""
    try:
        return asyncio.run(_edge_tts_to_mp3_async(text))
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(_edge_tts_to_mp3_async(text))
        return ''


def _history_for(chat_id: int):
    hist = conversation_history_by_chat.get(chat_id)
    if not isinstance(hist, list):
        hist = []
        conversation_history_by_chat[chat_id] = hist
    return hist


def _looks_like_key_exhausted_or_blocked(err_text: str) -> bool:
    """Best-effort detection for rate limit/quota/auth issues; triggers key rotation."""
    if not err_text:
        return False
    t = err_text.lower()
    markers = (
        'rate limit', 'ratelimit', 'too many requests', '429',
        'quota', 'insufficient', 'exceeded', 'limit exceeded',
        'unauthorized', '401', '403', 'invalid api key', 'authentication',
        'permission', 'forbidden',
    )
    return any(m in t for m in markers)


def call_groq_llama_stream(chat_id: int, user_text: str):
    """Stream Groq tokens using per-chat in-memory conversation for context, with multi-key failover."""
    global _groq_key_cursor

    keys = _get_all_groq_keys()
    if not keys:
        return "Error: No Groq API keys set. Fill GROQ_API_KEY (and optional GROQ_API_KEY1..3) in environment variables.", None

    history = _history_for(chat_id)

    history.append({'role': 'user', 'content': user_text})
    if len(history) > MAX_HISTORY_MESSAGES:
        history[:] = history[-MAX_HISTORY_MESSAGES:]

    weather_context = fetch_weather_from_env(None)
    system_content  = CONTEXT_SYSTEM_PROMPT
    if weather_context:
        system_content += (
            '\n\nLive weather context for the configured city '
            '(from OpenWeatherMap and environment variables):\n'
            + weather_context
            + '\n\nOnly mention this weather information if the user explicitly '
              'asks about weather, temperature, or conditions.'
        )

    messages = [{'role': 'system', 'content': system_content}] + history

    attempts = len(keys)
    start_idx = _groq_key_cursor % len(keys)

    last_error = None
    for i in range(attempts):
        idx = (start_idx + i) % len(keys)
        api_key = keys[idx]
        client = _get_groq_client_for_key(api_key)
        if client is None:
            last_error = 'No client (bad key).'
            continue

        parts = []
        try:
            stream = client.chat.completions.create(
                model='llama-3.3-70b-versatile',
                messages=messages,
                max_tokens=512,
                temperature=0.7,
                stream=True,
            )
            for chunk in stream:
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    delta = None
                if delta:
                    parts.append(delta)

            full = ''.join(parts).strip()
            _groq_key_cursor = idx  # stick to last successful key
            last_error = None
            break

        except Exception as e:
            last_error = str(e)
            if _looks_like_key_exhausted_or_blocked(last_error):
                continue
            return f'Error calling Groq: {e}', None
    else:
        return f'Error calling Groq (all keys failed): {last_error}', None

    if not full:
        full = "(No response.)"

    search_query = None
    if full:
        upper_full = full.upper()
        marker     = 'WEB_SEARCH:'
        idx        = upper_full.rfind(marker)
        if idx != -1:
            after = full[idx + len(marker):].strip()
            if after:
                search_query = after
                full = full[:idx].rstrip()

    if full:
        history.append({'role': 'assistant', 'content': full})
        if len(history) > MAX_HISTORY_MESSAGES:
            history[:] = history[-MAX_HISTORY_MESSAGES:]

    if search_query:
        try:
            open_search_tab(search_query)
        except Exception:
            pass

    return full, search_query


def _tg_api_base() -> str:
    token = get_env_ci('TELEGRAM_BOT_TOKEN') or ''
    return f'https://api.telegram.org/bot{token}'


def tg_send_message(chat_id: int, text: str) -> None:
    if not text:
        return
    try:
        requests.post(
            _tg_api_base() + '/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=12,
        )
    except Exception:
        pass


def tg_send_audio(chat_id: int, mp3_path: str, caption: str = None) -> None:
    if not mp3_path or not os.path.exists(mp3_path):
        return
    try:
        with open(mp3_path, 'rb') as f:
            files = {'audio': ('pixel.mp3', f, 'audio/mpeg')}
            data = {'chat_id': str(chat_id)}
            if caption:
                data['caption'] = caption
            requests.post(
                _tg_api_base() + '/sendAudio',
                data=data,
                files=files,
                timeout=60,
            )
    except Exception:
        pass


def tg_get_file_url(file_id: str) -> str:
    if not file_id:
        return ''
    try:
        r = requests.get(_tg_api_base() + '/getFile', params={'file_id': file_id}, timeout=12)
        data = r.json()
        file_path = data.get('result', {}).get('file_path')
        if not file_path:
            return ''
        token = get_env_ci('TELEGRAM_BOT_TOKEN') or ''
        return f'https://api.telegram.org/file/bot{token}/{file_path}'
    except Exception:
        return ''


def _convert_ogg_to_wav_ffmpeg(ogg_path: str, wav_path: str) -> bool:
    try:
        cmd = f'ffmpeg -y -i "{ogg_path}" "{wav_path}" -loglevel error'
        code = os.system(cmd)
        return code == 0 and os.path.exists(wav_path)
    except Exception:
        return False


def transcribe_telegram_voice_to_text(file_id: str) -> str:
    """Download Telegram voice (OGG/Opus), convert to WAV with ffmpeg, transcribe with Google SR."""
    url = tg_get_file_url(file_id)
    if not url:
        return ''

    with tempfile.TemporaryDirectory() as td:
        ogg_path = os.path.join(td, f'{uuid.uuid4().hex}.ogg')
        wav_path = os.path.join(td, f'{uuid.uuid4().hex}.wav')

        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                return ''
            with open(ogg_path, 'wb') as f:
                f.write(r.content)
        except Exception:
            return ''

        if not _convert_ogg_to_wav_ffmpeg(ogg_path, wav_path):
            return ''

        try:
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            text = recognizer.recognize_google(audio)
            return (text or '').strip()
        except Exception:
            return ''


def _allowed_chat(chat_id: int) -> bool:
    allowed = get_env_ci('TELEGRAM_ALLOWED_CHAT_ID')
    if not allowed:
        return True
    try:
        return str(chat_id) == str(int(str(allowed).strip()))
    except Exception:
        return str(chat_id) == str(allowed).strip()


# Health check server for Render
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is alive and running!')
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_health_server():
    """Run health check server on port 10000 for Render"""
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f'Health check server running on port {port}')
    server.serve_forever()


def main():
    _load_groq_keys_into_env()

    # Start health check server in background thread
    health_thread = Thread(target=run_health_server, daemon=True)
    health_thread.start()

    token = get_env_ci('TELEGRAM_BOT_TOKEN')
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN. Set it in environment variables.")
        return

    print('=== PROJECT PIXEL (TELEGRAM) ===')
    print(f"Wake word: '{WAKE_WORD}'. When asleep, it ignores messages unless you say the wake word.")
    print("Voice messages supported (requires ffmpeg installed).")
    print("Groq key rotation enabled: GROQ_API_KEY, GROQ_API_KEY1..3")
    print('Health check server started for Render deployment')
    print('Polling Telegram updates...\n')

    offset = 0

    while True:
        try:
            resp = requests.get(
                _tg_api_base() + '/getUpdates',
                params={
                    'timeout': 30,
                    'offset': offset,
                    'allowed_updates': json.dumps(['message']),
                },
                timeout=40,
            )
            data = resp.json()
            results = data.get('result', [])

            for upd in results:
                try:
                    offset = max(offset, int(upd.get('update_id', 0)) + 1)
                except Exception:
                    pass

                msg = upd.get('message') or {}
                chat = msg.get('chat') or {}
                chat_id = chat.get('id')
                if chat_id is None:
                    continue

                if not _allowed_chat(chat_id):
                    continue

                if chat_id not in listening_state_by_chat:
                    listening_state_by_chat[chat_id] = False

                text = (msg.get('text') or '').strip()

                if not text and msg.get('voice'):
                    file_id = (msg.get('voice') or {}).get('file_id')
                    text = transcribe_telegram_voice_to_text(file_id)

                if not text:
                    continue

                lowered = text.lower()
                is_listening = bool(listening_state_by_chat.get(chat_id, False))

                stamp = datetime.datetime.now().strftime('%H:%M:%S')
                cyan_print(f'[{stamp}] Chat {chat_id} | Listening={is_listening} | You: {text}')

                if not is_listening:
                    if WAKE_WORD in lowered:
                        listening_state_by_chat[chat_id] = True
                        tg_send_message(chat_id, 'Yes sir')
                        try:
                            mp3 = tts_to_mp3_path('Yes sir')
                            if mp3:
                                tg_send_audio(chat_id, mp3)
                        except Exception:
                            pass
                    continue

                if any(phrase in lowered for phrase in STOP_PHRASES):
                    listening_state_by_chat[chat_id] = False
                    continue

                if lowered in {'quit', 'exit', 'stop', 'that will be all', "that'll be all"}:
                    listening_state_by_chat[chat_id] = False
                    tg_send_message(chat_id, 'Goodbye.')
                    try:
                        mp3 = tts_to_mp3_path('Goodbye.')
                        if mp3:
                            tg_send_audio(chat_id, mp3)
                    except Exception:
                        pass
                    continue

                response, search_query = call_groq_llama_stream(chat_id, text)

                if search_query:
                    search_url = 'https://www.google.com/search?q=' + quote_plus(search_query.strip())
                    response_to_send = response + '\n\n' + 'Search link: ' + search_url
                else:
                    response_to_send = response

                tg_send_message(chat_id, response_to_send)

                try:
                    mp3 = tts_to_mp3_path(response)
                    if mp3:
                        tg_send_audio(chat_id, mp3)
                except Exception as e:
                    cyan_print(f'(TTS error, text-only this time: {e})\n')

        except KeyboardInterrupt:
            print('\nStopping bot.')
            return
        except Exception as e:
            cyan_print(f'(Error in main loop: {e})\n')
            time.sleep(1)


if __name__ == '__main__':
    main()
