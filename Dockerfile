FROM python:3.11-slim

WORKDIR /app

COPY requirements_railway.txt .
RUN pip install --no-cache-dir -r requirements_railway.txt

# Копируем Mini App статику
COPY miniapp/ ./miniapp/

# Копируем бэкенд
COPY railway/main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
