FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    freetds-dev \
    freetds-bin \
    unixodbc-dev \
    tdsodbc \
    && rm -rf /var/lib/apt/lists/*

# Configure FreeTDS
RUN echo "[MSSQL]\n\
host = 10.10.1.4\n\
port = 1433\n\
tds version = 7.4" > /etc/freetds.conf

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "app:app", "--bind", "0.0.0.0:8000", "--timeout", "120"]
