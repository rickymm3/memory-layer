import os
import requests
import psycopg

OLLAMA_HOST = os.environ["OLLAMA_HOST"]
DATABASE_URL = os.environ["DATABASE_URL"]

content = "The memory layer stores structured facts in Postgres and uses embeddings as semantic pointers."

response = requests.post(
    f"{OLLAMA_HOST}/api/embed",
    json={
        "model": "qwen3-embedding:latest",
        "input": content,
    },
    timeout=120,
)
response.raise_for_status()

embedding = response.json()["embeddings"][0]
embedding_literal = "[" + ",".join(str(value) for value in embedding) + "]"

with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_atoms (content, memory_type, scope, embedding)
            VALUES (%s, %s, %s, %s::vector)
            RETURNING id;
            """,
            (content, "fact", "memory-layer-project", embedding_literal),
        )

        memory_id = cur.fetchone()[0]

        cur.execute(
            """
            SELECT id, content, 1 - (embedding <=> %s::vector) AS similarity
            FROM memory_atoms
            ORDER BY embedding <=> %s::vector
            LIMIT 3;
            """,
            (embedding_literal, embedding_literal),
        )

        rows = cur.fetchall()

print("Stored memory:", memory_id)
print("Nearest memories:")
for row in rows:
    print(row)
