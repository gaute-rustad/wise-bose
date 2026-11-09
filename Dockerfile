FROM python:3.11-slim

# Forhindrer at Python bufrer stdout/stderr (viktig for live logger i Dockge)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Installer avhengigheter
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopier kildekoden
COPY . .

# Kjør boten
CMD ["python", "bot.py"]
