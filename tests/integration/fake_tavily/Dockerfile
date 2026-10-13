FROM python:3.12-slim

WORKDIR /app

# Same mcp range as mcp-server/pyproject.toml
RUN pip install --no-cache-dir "mcp>=1.30.0,<2" uvicorn

COPY app.py .

EXPOSE 8000

CMD ["python", "app.py"]
