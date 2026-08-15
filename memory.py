# memory.py
import os
import time

# Simple file-logging memory for prototype
os.makedirs("logs", exist_ok=True)

def store_memory(event, data=None):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    if data:
        print(f"Storing memory: {event} -> {data}")
        with open("logs/memory.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{event}\t{data}\n")
    else:
        print(f"Storing memory: {event}")
        with open("logs/memory.log", "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{event}\n")

def recall_handler(query: str = ""):
    """
    Very simple recall: search the memory log for the query substring.
    Returns the last matching line or a default message.
    """
    try:
        if not os.path.exists("logs/memory.log"):
            return "No memories stored yet."
        with open("logs/memory.log", "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if query.lower() in l.lower()]
        if not lines:
            return "I couldn't find anything relevant."
        return "Most recent memory: " + lines[-1]
    except Exception as e:
        return f"Memory error: {e}"
