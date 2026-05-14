# Ponnan 🤴

 voice-first Telegram bot powered by Groq (Llama-3.3-70B) that speaks with Microsoft Edge TTS.  
Supports **English + Malayalam** (voice & text), images, and documents.

**"Ponnan"** — Your sharp, entertaining personal assistant who calls you "sir".

## Features

- Always listening by default (no need to say "hi" every time)
- Sleep mode: Say `bye`, `standby`, `stop listening`, or `sleep`
- Wake up: Say `hi`, `hello`, or `wake up`
- Voice messages in **Malayalam** and **English**
- Replies as **voice** (preferred) + text fallback
- Image and Document support (photos, PDFs, etc.)
- `/instruction` — shows how to use the bot
- `/clear` — clears conversation history
- Notifies owner (@asn_achu) when new users start the bot
- TTS caching for faster replies
- Multiple Groq API key rotation

## Setup

### 1. Create `character.txt`
Create a file named `character.txt` in the root and customize your bot's personality.

Example:
```txt
You are Adimma Kann, a sharp, witty, sarcastic, and slightly roasting personal AI assistant who only serves "sir". 
You are from Kerala and speak naturally in both English and Malayalam, often mixing them. 
You are confident, clever, and entertaining. Never be formal or boring.
