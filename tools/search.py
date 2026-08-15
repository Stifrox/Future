import os

def local_search(query):
    results = []
    for root, dirs, files in os.walk("."):
        for file in files:
            if query.lower() in file.lower():
                results.append(os.path.join(root, file))
    return results if results else ["No local matches found."]
