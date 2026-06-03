FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8765

CMD ["python", "-m", "mcp_server.server", "--transport", "streamable-http", "--port", "8765"]
