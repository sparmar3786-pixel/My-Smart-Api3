FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY orderflow-pro/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY orderflow-pro/app.py /app/app.py
COPY orderflow-pro/index.html /app/index.html
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]