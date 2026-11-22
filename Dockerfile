FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Europe/Warsaw
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY data/gpw_tickers.csv data/macro_calendar.yaml ./data/
CMD ["python", "-m", "gpw_signals", "run"]
