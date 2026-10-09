FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir numpy fastapi uvicorn pydantic
COPY . /app
COPY deploy/artifacts /app/c1art
ENV C1_CKPT=/app/c1art C1_TOK=/app/c1art/tok \
    CODER1_MODEL_URL=http://127.0.0.1:8080 PORT=8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
