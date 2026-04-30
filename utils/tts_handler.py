"""
Text-to-Speech Handler
Multiple voices including Jarvis-style, Sarvam AI for Malayalam.
Falls back: edge-tts → gTTS on failure.
"""

import asyncio
import base64
import hashlib
import io
import logging
import os
from pathlib import Path

import edge_tts
import requests

logger = logging.getLogger(__name__)

# ============================================================================
# VOICE CATALOGUE
# Format: "key": (display_name, engine, voice_id, language_tag)
# ============================================================================

VOICE_CATALOGUE = {
    # ── English ──────────────────────────────────────────────────────────
    "jarvis":           ("🤖 Jarvis (Deep Male EN)",        "edge",   "en-US-GuyNeural",      "en"),
    "ryan":             ("🎩 Ryan (British Male EN)",        "edge",   "en-GB-RyanNeural",     "en"),
    "davis":            ("🎙️ Davis (Male EN)",               "edge",   "en-US-DavisNeural",    "en"),
    "tony":             ("🦾 Tony (Male EN)",                "edge",   "en-US-TonyNeural",     "en"),
    "aria":             ("🌸 Aria (Female EN)",              "edge",   "en-US-AriaNeural",     "en"),
    "jenny":            ("💁 Jenny (Female EN)",             "edge",   "en-US-JennyNeural",    "en"),
    "neerja":           ("🇮🇳 Neerja (Indian Female EN)",    "edge",   "en-IN-NeerjaNeural",   "en"),
    "prabhat":          ("🇮🇳 Prabhat (Indian Male EN)",     "edge",   "en-IN-PrabhatNeural",  "en"),

    # ── Malayalam (Edge TTS) ─────────────────────────────────────────────
    "sobhana":          ("🌺 Sobhana (ML Female)",           "edge",   "ml-IN-SobhanaNeural",  "ml"),
    "midhun":           ("🎤 Midhun (ML Male)",              "edge",   "ml-IN-MidhunNeural",   "ml"),

    # ── Malayalam (Sarvam AI) ─────────────────────────────────────────────
    "sarvam_anushka":   ("✨ Sarvam Anushka (ML Female)",    "sarvam", "anushka",              "ml"),
    "sarvam_arvind":    ("💪 Sarvam Arvind (ML Male)",       "sarvam", "arvind",               "ml"),
    "sarvam_neel":      ("🎵 Sarvam Neel (ML Male)",         "sarvam", "neel",                 "ml"),
    "sarvam_misha":     ("🌸 Sarvam Misha (ML Female)",      "sarvam", "misha",                "ml"),
    "sarvam_amol":      ("🔊 Sarvam Amol (ML Male)",         "sarvam", "amol",                 "ml"),
    "sarvam_diya":      ("🌟 Sarvam Diya (ML Female)",       "sarvam", "diya",                 "ml"),
}

# Default voice per detected language (used when user hasn't set a preference)
DEFAULT_VOICE = {
    "en":       "jarvis",
    "ml":       "sarvam_anushka",
    "manglish": "prabhat",
}


class TTSHandler:
    """Handle text-to-speech with user-selectable voices"""

    CACHE_DIR = Path('tts_cache')
    SARVAM_API_KEY = os.getenv('SARVAM_API_KEY', '')

    def __init__(self):
        self.CACHE_DIR.mkdir(exist_ok=True)
        self._user_voices: dict = {}   # chat_id → voice key
        logger.info("TTS Handler initialised")

    # ── Voice management ─────────────────────────────────────────────────

    def set_voice(self, chat_id: int, voice_key: str) -> bool:
        if voice_key not in VOICE_CATALOGUE:
            return False
        self._user_voices[chat_id] = voice_key
        logger.info(f"Voice set to '{voice_key}' for chat {chat_id}")
        return True

    def get_voice_key(self, chat_id: int, language: str) -> str:
        if chat_id in self._user_voices:
            return self._user_voices[chat_id]
        return DEFAULT_VOICE.get(language, "jarvis")

    def get_current_voice_name(self, chat_id: int, language: str) -> str:
        key = self.get_voice_key(chat_id, language)
        return VOICE_CATALOGUE[key][0]

    def get_voice_menu(self) -> str:
        lines = ["🎙️ *Available Voices*\n"]
        sections = [
            ("🇬🇧🇺🇸 English", ["jarvis", "ryan", "davis", "tony", "aria", "jenny", "neerja", "prabhat"]),
            ("🇮🇳 Malayalam — Edge TTS", ["sobhana", "midhun"]),
            ("✨ Malayalam — Sarvam AI", ["sarvam_anushka", "sarvam_arvind", "sarvam_neel",
                                          "sarvam_misha", "sarvam_amol", "sarvam_diya"]),
        ]
        for section_title, keys in sections:
            lines.append(f"\n*{section_title}*")
            for k in keys:
                name = VOICE_CATALOGUE[k][0]
                lines.append(f"  `{k}` — {name}")
        lines.append("\n*Usage:* `/voice jarvis`")
        lines.append("_Voice sticks across all messages until you change it._")
        return "\n".join(lines)

    # ── Main generate ────────────────────────────────────────────────────

    async def generate_speech(self, text: str, language: str = 'en',
                               chat_id: int = 0):
        """
        Generate speech. Returns path to mp3 file, or None (text-only fallback).
        """
        voice_key = self.get_voice_key(chat_id, language)
        display_name, engine, voice_id, _ = VOICE_CATALOGUE[voice_key]

        cache_key = hashlib.md5(f"{voice_key}|{text}".encode()).hexdigest()
        cache_path = self.CACHE_DIR / f"{cache_key}.mp3"

        if cache_path.exists():
            logger.info(f"TTS cache hit ({voice_key})")
            return str(cache_path)

        logger.info(f"Generating TTS: voice='{voice_key}' engine='{engine}'")

        success = False
        if engine == "sarvam":
            success = await self._sarvam_tts(text, voice_id, cache_path)
            if not success:
                # Sarvam failed → try edge midhun as Malayalam fallback
                logger.warning("Sarvam failed, trying edge-tts Malayalam fallback")
                success = await self._edge_tts(text, "ml-IN-MidhunNeural", cache_path)
        else:
            success = await self._edge_tts(text, voice_id, cache_path)

        if not success:
            logger.warning("edge-tts failed, trying gTTS last resort")
            success = self._gtts_fallback(text, language, cache_path)

        if success and cache_path.exists():
            return str(cache_path)

        logger.error("All TTS engines failed — text-only response")
        return None

    # ── Engines ──────────────────────────────────────────────────────────

    async def _edge_tts(self, text: str, voice: str, out: Path) -> bool:
        """edge-tts with 3 retries and back-off."""
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(str(out))
                logger.info(f"edge-tts OK (voice={voice}, attempt={attempt+1})")
                return True
            except Exception as e:
                logger.warning(f"edge-tts attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(1.5)
        return False

    async def _sarvam_tts(self, text: str, speaker: str, out: Path) -> bool:
        """
        Sarvam AI TTS — high-quality Malayalam neural voices.
        https://docs.sarvam.ai/api-reference-docs/text-to-speech
        """
        if not self.SARVAM_API_KEY:
            logger.warning("SARVAM_API_KEY not configured")
            return False
        try:
            payload = {
                "inputs": [text],
                "target_language_code": "ml-IN",
                "speaker": speaker,
                "pitch": 0,
                "pace": 1.0,
                "loudness": 1.5,
                "speech_sample_rate": 22050,
                "enable_preprocessing": True,
                "model": "bulbul:v1",
            }
            headers = {
                "api-subscription-key": self.SARVAM_API_KEY,
                "Content-Type": "application/json",
            }
            resp = requests.post(
                "https://api.sarvam.ai/text-to-speech",
                json=payload,
                headers=headers,
                timeout=20,
            )
            if resp.status_code != 200:
                logger.error(f"Sarvam TTS {resp.status_code}: {resp.text[:200]}")
                return False

            audio_b64 = resp.json()["audios"][0]
            audio_bytes = base64.b64decode(audio_b64)

            # Convert WAV → MP3
            try:
                from pydub import AudioSegment
                seg = AudioSegment.from_wav(io.BytesIO(audio_bytes))
                seg.export(str(out), format="mp3")
            except Exception:
                # pydub unavailable — raw WAV works fine in Telegram
                out.write_bytes(audio_bytes)

            logger.info(f"Sarvam TTS OK (speaker={speaker})")
            return True

        except Exception as e:
            logger.error(f"Sarvam TTS exception: {e}")
            return False

    def _gtts_fallback(self, text: str, language: str, out: Path) -> bool:
        """Last-resort Google TTS fallback."""
        lang_map = {"en": "en", "ml": "ml", "manglish": "en"}
        try:
            from gtts import gTTS
            gTTS(text=text, lang=lang_map.get(language, "en"), slow=False).save(str(out))
            logger.info("gTTS fallback OK")
            return True
        except ImportError:
            logger.error("gTTS not installed — add 'gTTS' to requirements.txt")
        except Exception as e:
            logger.error(f"gTTS fallback failed: {e}")
        return False
