import json
import subprocess
import config

def load_personality():
    try:
        with open(config.PERSONALITY_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        personality = {
            "name": "Future",
            "traits": ["helpful", "witty", "loyal"],
            "tone": "friendly but intelligent"
        }
        with open(config.PERSONALITY_FILE, "w") as f:
            json.dump(personality, f, indent=2)
        return personality

def apply_personality(user_input, personality, memory):
    history_text = "\n".join([f"User: {m['user']}\nFuture: {m['ai']}" for m in memory[-5:]])
    prompt = f"""You are {personality['name']} with traits {personality['traits']} and tone {personality['tone']}.
Past few interactions:
{history_text}

User: {user_input}
Future:"""

    result = subprocess.run(
        ["ollama", "run", config.OLLAMA_MODEL],
        input=prompt.encode(),
        capture_output=True
    )
    return result.stdout.decode().strip()
