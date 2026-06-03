import os
import requests

OLLAMA_HOST = os.environ["OLLAMA_HOST"]

response = requests.post(
    f"{OLLAMA_HOST}/api/embed",
    json={
        "model": "qwen3-embedding:latest",
        "input": "The memory layer stores structured facts and uses embeddings as semantic pointers."
    },
    timeout=120,
)

response.raise_for_status()
data = response.json()

embedding = data["embeddings"][0]

print("Embedding generated")
print("Dimensions:", len(embedding))
print("First 5 values:", embedding[:5])
