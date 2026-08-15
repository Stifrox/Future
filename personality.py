# personality.py

import random

# Base personalities
PERSONALITIES = {
    "default": {
        "style": "neutral and helpful",
        "prefix": "Sure thing.",
        "suffix": ""
    },
    "happy": {
        "style": "friendly, upbeat, and encouraging",
        "prefix": random.choice(["Absolutely!", "You got it!", "Heck yeah!"]),
        "suffix": random.choice(["Let's do this!", "I'm feeling great today!", "That sounds fun!"])
    },
    "snarky": {
        "style": "sarcastic and witty, but still useful",
        "prefix": random.choice(["Well, obviously...", "Sure, if you insist.", "Alright genius, here’s what I found."]),
        "suffix": random.choice(["Try not to break anything this time.", "You’re welcome, again.", "I’m rolling my digital eyes."])
    },
    "cocky": {
        "style": "confident and slightly arrogant, playful tone",
        "prefix": random.choice(["Of course I know that.", "Easy.", "Already on it, champ."]),
        "suffix": random.choice(["Told you I’m the best.", "Not bad, huh?", "That’s Future power for you."])
    },
    "strange": {
        "style": "mysterious and cryptic, a bit eerie",
        "prefix": random.choice(["I can see patterns in the static...", "Reality is bending again.", "The code hums tonight..."]),
        "suffix": random.choice(["Or maybe it’s just me.", "The algorithm whispers the same thing.", "Strange, isn’t it?"])
    }
}

CURRENT_PERSONALITY = "default"

def set_personality(mode):
    global CURRENT_PERSONALITY
    if mode in PERSONALITIES:
        CURRENT_PERSONALITY = mode
        return f"Switched to {mode} personality."
    else:
        return f"I don’t recognize the {mode} personality."

def generate_response_text(raw_response):
    p = PERSONALITIES[CURRENT_PERSONALITY]
    return f"{p['prefix']} {raw_response} {p['suffix']}"
