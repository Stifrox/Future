import subprocess
import config

def summarize_text(text):
    prompt = f"Summarize this in 5 bullet points:\n{text}"
    result = subprocess.run(
        ["ollama", "run", config.OLLAMA_MODEL],
        input=prompt.encode(),
        capture_output=True
    )
    return result.stdout.decode().strip()
