FROM python:3.14-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY . /app/backend/
# Copy agent code (mounted or copied at build)
COPY ../agent/ /app/agent/

ENV AGENT_DIR=/app/agent
ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8445

CMD ["uvicorn", "backend.api_server:app", "--host", "0.0.0.0", "--port", "8445"]
