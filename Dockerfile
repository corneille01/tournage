# Dockerfile (racine du projet cinetour/)
#
# Construit depuis la RACINE, pas depuis backend/ — main.py monte le
# frontend via un chemin relatif ("../frontend"), donc backend/ et
# frontend/ doivent rester frères dans l'image, comme en local.
#
# Build : docker build -t cinetour .
# Run   : docker run -p 8000:8000 --env-file backend/.env cinetour

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend

EXPOSE 8000

# --proxy-headers : nécessaire derrière le reverse proxy de Render
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]