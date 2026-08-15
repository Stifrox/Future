import re
import sys
import time
import os
import audioop
import subprocess
from pathlib import Path

from tools.personality import load_personality, apply_personality
from tools.memory import load_memory, save_memory, remember
from tools.search import local_search
from tools.scraper import scrape_url
from tools.summarizer import summarize_text
from tools.integrations import (
    open_spotify,
    handle_spotify_command,
    handle_calendar_command,
    handle_gmail_command,
    has_pending_calendar_draft,
    should_handle_calendar_followup,
    has_pending_gmail_confirmation,
    send_gmail_message,
)
from tools.alpaca_trading import AutopilotPaperTrader, PaperTradingEngine, handle_trading_command
from openai import OpenAI
import config

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or str(getattr(config, "OPENAI_API_KEY", "")).strip()
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", str(getattr(config, "PRIMARY_MODEL", "gpt-5")).strip() or "gpt-5").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

trading_engine = PaperTradingEngine()
autopilot_trader = AutopilotPaperTrader()

# --- Voice imports ---
from vosk import Model, KaldiRecognizer
import pyaudio, json
import pyttsx3

BASE_DIR = Path(__file__).resolve().parent
VOSK_MODEL_PATH = BASE_DIR / "model"

engine = None
VOICE_GENDER = "male"

def set_voice_gender(gender: str):
    global VOICE_GENDER
    if gender.lower() in ["male", "female"]:
        VOICE_GENDER = gender.lower()
        return True
    return False

def detect_voice_gender_request(text: str):
    if not text:
        return None
    normalized = text.lower().strip()
    if any(phrase in normalized for phrase in ["use female voice", "switch to female", "female voice", "use male voice", "switch to male", "male voice"]):
        if "female" in normalized:
            return "female"
        elif "male" in normalized:
            return "male"
    return None


VOICE_GENDER = "male"

def set_voice_gender(gender: str):
    global VOICE_GENDER
    if gender.lower() in ["male", "female"]:
        VOICE_GENDER = gender.lower()
        return True
    return False

def detect_voice_gender_request(text: str):
    if not text:
        return None
    normalized = text.lower().strip()
    if any(phrase in normalized for phrase in ["use female voice", "switch to female", "female voice", "use male voice", "switch to male", "male voice"]):
        if "female" in normalized:
            return "female"
        elif "male" in normalized:
            return "male"
    return None


speech_backend = None
vosk_model = None

OUTPUT_DEVICE_HINTS = ["bluetooth", "airpods", "buds", "headset", "glasses", "a2dp"]
MIC_GAIN = max(1.0, min(4.0, float(os.getenv("FUTURE_MIC_GAIN", "1.8"))))
LISTEN_FRAMES = int(os.getenv("FUTURE_LISTEN_FRAMES", "1024"))
USE_SR_FALLBACK = os.getenv("FUTURE_USE_SR_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}

SLICER_PATH_CANDIDATES = [
    r"C:\Program Files\AnycubicSlicer\AnycubicSlicer.exe",
    r"C:\Program Files (x86)\AnycubicSlicer\AnycubicSlicer.exe",
    r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
]

try:
    if VOSK_MODEL_PATH.exists():
        vosk_model = Model(str(VOSK_MODEL_PATH))
except Exception as exc:
    print(f"⚠️ Vosk model init failed: {exc}")


def _init_engine():
    global engine, speech_backend
    if speech_backend is None:
        try:
            import win32com.client as wincl
            engine = wincl.Dispatch("SAPI.SpVoice")
            engine.Voice = engine.GetVoices().Item(0)

            preferred_output = os.getenv("FUTURE_AUDIO_OUTPUT_DEVICE", "").strip().lower()
            try:
                outputs = engine.GetAudioOutputs()
                selected_output = None
                for idx in range(outputs.Count):
                    token = outputs.Item(idx)
                    description = (token.GetDescription() or "").lower()
                    if preferred_output and preferred_output in description:
                        selected_output = token
                        break
                    if not preferred_output and any(hint in description for hint in OUTPUT_DEVICE_HINTS):
                        selected_output = token
                        if "hands-free" not in description:
                            break
                if selected_output is not None:
                    engine.AudioOutput = selected_output
                    print(f"🔊 Using SAPI output: {selected_output.GetDescription()}")
            except Exception as exc:
                print(f"⚠️ Could not switch SAPI output device: {exc}")

            speech_backend = "sapi"
        except Exception as exc:
            print(f"⚠️ SAPI voice init failed: {exc}")
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", 170)
                engine.setProperty("volume", 1.0)
                speech_backend = "pyttsx3"
            except Exception as exc2:
                print(f"⚠️ Text-to-speech init failed: {exc2}")
                engine = None
                speech_backend = "unavailable"
    return speech_backend, engine


def select_microphone_device(pyaudio_instance):
    for index in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(index)
        if info.get("maxInputChannels", 0) <= 0:
            continue
        if info.get("isDefaultInput"):
            return index

    for index in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(index)
        if info.get("maxInputChannels", 0) > 0:
            return index

    return None


def select_output_device(pyaudio_instance):
    preferred_output = os.getenv("FUTURE_AUDIO_OUTPUT_DEVICE", "").strip().lower()
    candidates = []

    for index in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(index)
        if info.get("maxOutputChannels", 0) <= 0:
            continue
        name = str(info.get("name", ""))
        lowered = name.lower()
        candidates.append((index, name, lowered))

    if not candidates:
        return None

    if preferred_output:
        for index, _, lowered in candidates:
            if preferred_output in lowered:
                return index

    bluetooth_matches = [item for item in candidates if any(hint in item[2] for hint in OUTPUT_DEVICE_HINTS)]
    for index, _, lowered in bluetooth_matches:
        if "hands-free" not in lowered:
            return index
    if bluetooth_matches:
        return bluetooth_matches[0][0]

    try:
        default_output = pyaudio_instance.get_default_output_device_info()
        default_index = int(default_output.get("index"))
        return default_index
    except Exception:
        return candidates[0][0]


def _boost_audio(data, gain):
    if gain <= 1.0:
        return data
    try:
        return audioop.mul(data, 2, gain)
    except Exception:
        return data


def _listen_with_speech_recognition(timeout=5, phrase_time_limit=12):
    if not USE_SR_FALLBACK:
        return ""

    try:
        import speech_recognition as sr
    except Exception:
        return ""

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = True
    recognizer.energy_threshold = int(os.getenv("FUTURE_SR_ENERGY_THRESHOLD", "170"))
    recognizer.pause_threshold = float(os.getenv("FUTURE_SR_PAUSE_THRESHOLD", "0.9"))

    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
        text = recognizer.recognize_google(audio).strip()
        if text:
            print("You (voice fallback):", text)
        return text
    except Exception:
        return ""


def detect_wake_word(text):
    if not text:
        return False
    normalized = text.lower().strip()
    normalized = normalized.replace("?", " ").replace("!", " ")
    return any(keyword in normalized for keyword in ["future", "hey future", "future please", "future now"])


def _find_slicer_executable():
    env_path = os.getenv("FUTURE_SLICER_PATH", "").strip().strip('"')
    if env_path and os.path.exists(env_path):
        return env_path

    for path in SLICER_PATH_CANDIDATES:
        if os.path.exists(path):
            return path

    return None


def open_slicer_app():
    slicer_path = _find_slicer_executable()
    if slicer_path:
        try:
            os.startfile(slicer_path)
            return True, f"Opening slicer at {slicer_path}."
        except Exception:
            try:
                subprocess.Popen([slicer_path])
                return True, f"Opening slicer at {slicer_path}."
            except Exception as exc:
                return False, f"I found the slicer, but I couldn't open it: {exc}"

    command_candidates = ["AnycubicSlicer.exe", "orca-slicer.exe", "prusa-slicer.exe"]
    for candidate in command_candidates:
        try:
            subprocess.Popen([candidate])
            return True, f"Opening {candidate}."
        except Exception:
            continue

    return (
        False,
        "I couldn't find your slicer app. Set FUTURE_SLICER_PATH to your slicer .exe path and try again.",
    )


def is_awake(active_until, now=None):
    if active_until is None:
        return False
    if now is None:
        now = time.time()
    return now < active_until


def format_memory(memory):
    if not memory:
        return "No past memories."
    lines = []
    for item in memory:
        user = item.get("user", "")
        bot = item.get("bot", "")
        lines.append(f"User said: {user}\nFuture replied: {bot}")
    return "\n".join(lines)

def _speak_elevenlabs(text):
    """Generate speech via ElevenLabs SDK and play PCM with pyaudio. Returns True on success."""
    api_key = getattr(config, "ELEVENLABS_API_KEY", "").strip()
    voice_id = getattr(config, "ELEVENLABS_VOICE_ID", "").strip()
    if not api_key or not voice_id:
        return False
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        audio_gen = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_22050",
        )
        audio_bytes = b"".join(audio_gen)

        p = pyaudio.PyAudio()
        output_device_index = select_output_device(p)
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=22050,
            output=True,
            output_device_index=output_device_index,
        )
        stream.write(audio_bytes)
        stream.stop_stream()
        stream.close()
        p.terminate()
        return True
    except Exception as exc:
        print(f"⚠️ ElevenLabs TTS failed: {exc}")
        return False


def speak(text):
    if not text or not text.strip():
        return

    print(f"Speaking: {text}")

    if _speak_elevenlabs(text):
        return

    # Fallback to local TTS
    backend, tts_engine = _init_engine()
    if backend == "unavailable":
        print(f"Future: {text}")
        return

    try:
        if backend == "sapi":
            tts_engine.Speak(text)
        else:
            tts_engine.say(text)
            tts_engine.runAndWait()
    except Exception as exc:
        print(f" Speech playback failed: {exc}")
        print(f"Future: {text}")


def generate_reply(text, personality, memory):
    memory_text = format_memory(memory)

    system_prompt = (
        f"{personality}\n\n"
        f"Here are your memories about the user:\n{memory_text}\n\n"
        "Use these memories to stay consistent and personal."
    )

    if not client:
        return "OpenAI API key is not configured. Set OPENAI_API_KEY in your environment to enable cloud chat."

    response = client.chat.completions.create(
        model=PRIMARY_MODEL or "gpt-5",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )
    return response.choices[0].message.content


def build_autopilot_email_report(
    trades,
    last_result,
    submission_results,
    liquidation_results,
    broker_start_equity=None,
    broker_end_equity=None,
    broker_profit=None,
):
    submitted_count = sum(1 for item in submission_results if item.get("status") == "submitted")
    simulated_count = sum(1 for item in submission_results if item.get("status") == "simulated")
    skipped_count = sum(1 for item in submission_results if item.get("status") == "skipped")
    liquidated_submitted = sum(1 for item in liquidation_results if item.get("status") == "submitted")
    liquidated_simulated = sum(1 for item in liquidation_results if item.get("status") == "simulated")
    liquidated_skipped = sum(1 for item in liquidation_results if item.get("status") == "skipped")

    trade_lines = []
    for index, trade in enumerate(trades, start=1):
        trade_lines.append(
            f"{index}. {trade['action'].upper()} {trade['symbol']} at ${trade['price']:.2f}; "
            f"why: {trade['reason']}; quantity: {trade['quantity']}; "
            f"learned weights: {trade['learned']['weights']}"
        )

    broker_lines = ""
    if broker_profit is not None and broker_start_equity is not None and broker_end_equity is not None:
        broker_lines = (
            f"Actual Alpaca start equity: ${broker_start_equity:.2f}\n"
            f"Actual Alpaca end equity: ${broker_end_equity:.2f}\n"
            f"Actual Alpaca session profit: ${broker_profit:.2f}\n"
        )

    return (
        "Future autopilot session report\n\n"
        f"Total trades: {len(trades)}\n"
        f"{broker_lines}"
        f"Alpaca sync: submitted={submitted_count}, simulated_fallback={simulated_count}, skipped={skipped_count}\n"
        f"Auto-cashout: submitted={liquidated_submitted}, simulated_fallback={liquidated_simulated}, skipped={liquidated_skipped}\n\n"
        "What the bot did and why:\n"
        + "\n".join(trade_lines)
    )


def route_command(command, personality, memory):
    command_lower = command.lower()

    if has_pending_calendar_draft() and should_handle_calendar_followup(command):
        reply = handle_calendar_command(command)
        print("Future:", reply)
        speak(reply)
        remember(memory, command, reply)
        return reply

    if has_pending_gmail_confirmation() and command_lower.strip() in {
        "yes", "y", "yeah", "yep", "confirm", "send", "send it", "do it", "go ahead", "correct",
        "no", "n", "nope", "cancel", "stop", "dont send", "don't send"
    }:
        reply = handle_gmail_command(command)
        print("Future:", reply)
        speak(reply)
        remember(memory, command, reply)
        return reply

    if any(keyword in command_lower for keyword in ["autopilot", "auto trade", "auto trading", "run autopilot"]):
        trades = 1
        match = re.search(r"(\d+)\s*(trade|trades)", command_lower)
        if match:
            trades = int(match.group(1))
        autopilot_trader.reset_portfolio(start_cash=100000.0, target_value=120000.0)
        if trades > 1:
            broker_start = trading_engine.get_alpaca_account_summary()
            results = autopilot_trader.run_test_sequence("AAPL", price=100.0, trades=trades)
            last_result = results[-1]
            submission_results = trading_engine.submit_autopilot_sequence(results, quantity_per_trade=1)
            submitted_count = sum(1 for item in submission_results if item.get("status") == "submitted")
            simulated_count = sum(1 for item in submission_results if item.get("status") == "simulated")
            skipped_count = sum(1 for item in submission_results if item.get("status") == "skipped")
            liquidation_results = []
            watched_prices = last_result.get("watched_prices", {})
            liquidation_results = trading_engine.liquidate_open_alpaca_positions()
            if not liquidation_results:
                liquidation_results = trading_engine.liquidate_positions(trading_engine.last_shadow_positions, prices=watched_prices)
            liquidated_submitted = sum(1 for item in liquidation_results if item.get("status") == "submitted")
            liquidated_simulated = sum(1 for item in liquidation_results if item.get("status") == "simulated")
            liquidated_skipped = sum(1 for item in liquidation_results if item.get("status") == "skipped")
            liquidation_text = (
                " Session end cashout ran: "
                f"submitted={liquidated_submitted}, simulated_fallback={liquidated_simulated}, skipped={liquidated_skipped}."
            )
            broker_end = trading_engine.get_alpaca_account_summary()
            actual_session_profit = None
            if broker_start.get("available") and broker_end.get("available"):
                actual_session_profit = float(broker_end.get("equity", 0.0)) - float(broker_start.get("equity", 0.0))
            trade_breakdown = "; ".join(
                f"{index + 1}. {trade['action'].upper()} {trade['symbol']} because {trade['reason']} (paper value ${trade['total_value']:.2f})"
                for index, trade in enumerate(results)
            )
            trade_learning_breakdown = "\n".join(
                f"{index + 1}. {trade['action'].upper()} {trade['symbol']} | reason: {trade['reason']} | learned_weights: {trade['learned']['weights']}"
                for index, trade in enumerate(results)
            )
            report_email = os.getenv("FUTURE_REPORT_EMAIL", "Hammus42108@gmail.com")
            email_status_text = f" Email report not sent."
            try:
                email_body = build_autopilot_email_report(
                    results,
                    last_result,
                    submission_results,
                    liquidation_results,
                    broker_start_equity=broker_start.get("equity") if broker_start.get("available") else None,
                    broker_end_equity=broker_end.get("equity") if broker_end.get("available") else None,
                    broker_profit=actual_session_profit,
                )
                send_gmail_message(
                    report_email,
                    "Future Autopilot Session Report",
                    email_body,
                )
                email_status_text = f" Email report sent to {report_email}."
            except Exception as exc:
                email_status_text = f" Email report failed: {exc}."
            broker_summary_text = "Actual Alpaca session data unavailable."
            if broker_start.get("available") and broker_end.get("available") and actual_session_profit is not None:
                broker_summary_text = (
                    f"Alpaca start equity=${broker_start['equity']:.2f}. "
                    f"Alpaca end equity=${broker_end['equity']:.2f}. "
                    f"Alpaca session profit=${actual_session_profit:.2f}."
                )
            reply = (
                f"Autopilot paper test started with $100,000. It completed {trades} paper trades. "
                f"{broker_summary_text} "
                f"Alpaca sync: submitted={submitted_count}, simulated_fallback={simulated_count}, skipped={skipped_count}. "
                f"{liquidation_text} "
                f"{email_status_text} "
                f"Trade breakdown: {trade_breakdown}."
            )
        else:
            result = autopilot_trader.run_cycle("AAPL", price=100.0)
            reply = (
                f"Autopilot paper trader engaged. Goal: grow the paper portfolio from ${autopilot_trader.portfolio['cash']:.2f} to ${autopilot_trader.goal['target_value']:.2f}. "
                f"Latest action={result['action']} on {result['symbol']} because {result['reason']}. "
                f"Total paper value=${result['total_value']:.2f}. "
                f"This trade learned: {result['learned']['weights']}."
            )
    elif any(keyword in command_lower for keyword in ["stock", "stocks", "trade", "trading", "alpaca", "portfolio", "buy", "sell", "ticker"]):
        reply = handle_trading_command(command, engine=trading_engine)
    elif "spotify" in command_lower or "play music" in command_lower or "play" in command_lower:
        reply = handle_spotify_command(command)
    elif (
        any(keyword in command_lower for keyword in ["slicer", "anycubic slicer", "any cubic slicer", "orca slicer", "prusa slicer"])
        and any(verb in command_lower for verb in ["open", "launch", "start", "run"])
    ):
        _, reply = open_slicer_app()
    elif any(keyword in command_lower for keyword in ["calendar", "google calendar", "schedule", "event", "remind", "reminder", "appointment"]):
        reply = handle_calendar_command(command)
    elif any(keyword in command_lower for keyword in ["gmail", "inbox", "email", "mail"]):
        reply = handle_gmail_command(command)
    else:
        reply = generate_reply(command, personality, memory)

    print("Future:", reply)
    speak(reply)
    remember(memory, command, reply)
    return reply


def listen(timeout=16):
    import time

    if vosk_model is None:
        print(" Vosk model unavailable; voice input disabled.")
        return ""

    p = pyaudio.PyAudio()
    device_index = select_microphone_device(p)

    if device_index is None:
        print("No microphone detected.")
        p.terminate()
        return ""

    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16000,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=LISTEN_FRAMES
    )
    stream.start_stream()
    recognizer = KaldiRecognizer(vosk_model, 16000)
    try:
        recognizer.SetWords(True)
    except Exception:
        pass

    print("\nListening... (speak clearly)")

    start = time.time()
    speech_started = False
    last_speech_time = start
    final_chunks = []
    try:
        while True:
            if time.time() - start > timeout:
                print("Voice timeout — no speech detected.")
                fallback_text = _listen_with_speech_recognition(timeout=4, phrase_time_limit=12)
                if fallback_text:
                    return fallback_text
                return ""

            data = stream.read(LISTEN_FRAMES, exception_on_overflow=False)
            data = _boost_audio(data, MIC_GAIN)
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                text = result.get("text", "").strip()
                if text:
                    final_chunks.append(text)
                    speech_started = True
                    last_speech_time = time.time()
            else:
                partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                if partial:
                    speech_started = True
                    last_speech_time = time.time()

            if speech_started and (time.time() - last_speech_time > 1.2):
                trailing = json.loads(recognizer.FinalResult()).get("text", "").strip()
                if trailing:
                    final_chunks.append(trailing)

                full_text = " ".join(chunk for chunk in final_chunks if chunk).strip()
                if full_text:
                    print("You (voice):", full_text)
                    return full_text
                fallback_text = _listen_with_speech_recognition(timeout=4, phrase_time_limit=12)
                if fallback_text:
                    return fallback_text
                return ""
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


def listen_for_wake_word(timeout=25):
    import time

    if vosk_model is None:
        fallback_text = _listen_with_speech_recognition(timeout=5, phrase_time_limit=5)
        if detect_wake_word(fallback_text):
            return fallback_text
        return ""

    p = pyaudio.PyAudio()
    device_index = select_microphone_device(p)

    if device_index is None:
        p.terminate()
        return ""

    stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16000,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=LISTEN_FRAMES
    )
    stream.start_stream()
    recognizer = KaldiRecognizer(vosk_model, 16000)
    try:
        recognizer.SetWords(True)
    except Exception:
        pass

    print("\nListening for wake word 'Future'...")

    start = time.time()
    try:
        while True:
            if time.time() - start > timeout:
                return ""

            data = stream.read(LISTEN_FRAMES, exception_on_overflow=False)
            data = _boost_audio(data, MIC_GAIN)
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                text = result.get("text", "").strip()
                if text:
                    print("Heard:", text)
                    if detect_wake_word(text):
                        return text
            else:
                partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                if partial and detect_wake_word(partial):
                    print("Heard (partial):", partial)
                    return partial
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


def run_future():
    personality = load_personality()
    memory = load_memory()

    print("Future Online.")
    print("Voice mode is now active by default. Say 'Future' to wake me up.")

    wake_window_until = None

    try:
        while True:
            if is_awake(wake_window_until):
                command = listen(timeout=12)
                if not command:
                    continue

                route_command(command, personality, memory)
                continue

            wake_text = listen_for_wake_word(timeout=20)
            if not wake_text:
                continue

            if detect_wake_word(wake_text):
                wake_window_until = time.time() + 300
                speak("Yes?")
                command = listen(timeout=12)
                if not command:
                    continue

                route_command(command, personality, memory)
                continue

            # Ignore non-wake speech and keep listening.
            continue
    except KeyboardInterrupt:
        save_memory(memory)
        print("\nMemory saved. Goodbye!")

def _run_command_line():
    personality = load_personality()
    memory = load_memory()

    if len(sys.argv) > 1:
        command = " ".join(sys.argv[1:]).strip()
        if not command:
            print("No command provided.")
            return

        reply = route_command(command, personality, memory)
        save_memory(memory)
        return

    run_future()


if __name__ == "__main__":
    _run_command_line()
