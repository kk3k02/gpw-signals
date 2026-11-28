FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Warsaw
WORKDIR /app

# Zależności osobno – warstwa cache'owana, dopóki nie zmieni się pyproject.toml
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config
COPY data/gpw_tickers.csv data/macro_calendar.yaml data/gpw_holidays.yaml ./data/

# Użytkownik bez uprawnień roota; /app/data montowane jako wolumen (baza, notowania)
RUN useradd --create-home --uid 1000 bot && mkdir -p data/prices data/raw && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "gpw_signals", "run"]
