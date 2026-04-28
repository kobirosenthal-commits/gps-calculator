FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8080", "--timeout", "120", "main:app"]
