import os
from pathlib import Path

try:
	from dotenv import load_dotenv
except Exception:
	load_dotenv = None

if load_dotenv:
	# Ensure config values are available even when the shell did not export env vars.
	load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# Configuration for Future AI
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "gpt-5")
BACKUP_MODEL = os.getenv("BACKUP_MODEL", "gpt-4.1")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_MODEL_SMALL_EDIT = os.getenv("ANTHROPIC_MODEL_SMALL_EDIT", "claude-sonnet-4-5")
ELEVENLABS_VOICE_ID_FEMALE = os.getenv("ELEVENLABS_VOICE_ID_FEMALE", "")
ANTHROPIC_MODEL_FULL_REWRITE = os.getenv("ANTHROPIC_MODEL_FULL_REWRITE", "claude-opus-4-1")
MEMORY_FILE = "future_memory.json"
PERSONALITY_FILE = "future_personality.json"
ALPACA_PAPER_TRADING = True
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# ElevenLabs TTS
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
