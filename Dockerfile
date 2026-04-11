FROM python:3.12-slim

# Instalar FFmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Directorio de trabajo
WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código
COPY . .

# Crear directorio de downloads
RUN mkdir -p downloads

# Puerto (Railway usa la variable PORT)
EXPOSE 5000

# Arrancar la app con gunicorn (en modo shell para que expanda $PORT)
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2
