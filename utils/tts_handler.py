"""
Text-to-Speech Handler using edge-tts
Supports English and Malayalam with caching
"""

import edge_tts
import hashlib
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class TTSHandler:
    """Handle text-to-speech conversion"""
    
    # Voice mapping for different languages
    VOICES = {
        'en': 'en-GB-RyanNeural',           # British English male
        'ml': 'ml-IN-SobhanaNeural',        # Malayalam female
        'manglish': 'en-IN-NeerjaNeural',   # Indian English female
    }
    
    CACHE_DIR = Path('tts_cache')
    
    def __init__(self):
        """Initialize TTS handler"""
        self.CACHE_DIR.mkdir(exist_ok=True)
        logger.info("TTS Handler initialized")
    
    def _get_cache_path(self, text: str, language: str) -> Path:
        """Generate cache file path based on text hash"""
        text_hash = hashlib.md5(f"{text}_{language}".encode()).hexdigest()
        return self.CACHE_DIR / f"{text_hash}.mp3"
    
    async def generate_speech(self, text: str, language: str = 'en') -> str:
        """
        Generate speech from text
        Returns: Path to audio file or None
        """
        try:
            # Check cache first
            cache_path = self._get_cache_path(text, language)
            if cache_path.exists():
                logger.info(f"Using cached TTS for: {text[:30]}...")
                return str(cache_path)
            
            # Get appropriate voice
            voice = self.VOICES.get(language, self.VOICES['en'])
            
            logger.info(f"Generating TTS with voice: {voice}")
            
            # Generate speech
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(cache_path))
            
            logger.info(f"TTS generated and cached: {cache_path}")
            return str(cache_path)
            
        except Exception as e:
            logger.error(f"TTS generation error: {e}")
            return None
