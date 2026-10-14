FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn opentelemetry-proto

COPY app.py .

EXPOSE 4318

CMD ["python", "app.py"]
