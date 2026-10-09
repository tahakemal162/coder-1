FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir numpy fastapi uvicorn pydantic
COPY . /app
COPY deploy/artifacts /app/c1art
ENV CODER1_MODEL_URL=http://127.0.0.1:9100 PORT=8080
CMD ["/bin/sh", "deploy/start.sh"]
