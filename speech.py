import speech_recognition as sr
from memory import store_memory

r = sr.Recognizer()

async def listen_for_wake_word(wake_word):
    with sr.Microphone() as source:
        audio = r.listen(source)
        try:
            text = r.recognize_google(audio).lower()
            if wake_word in text:
                return True
        except:
            pass
    return False

async def transcribe_command():
    with sr.Microphone() as source:
        audio = r.listen(source)
        try:
            text = r.recognize_google(audio)
            store_memory(text)   # keep transcript in memory DB
            return text
        except:
            return ""
