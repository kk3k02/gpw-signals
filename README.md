# Sygnalista GPW

Bot, który czyta newsy z rynku (ESPI/EBI, portale, rekomendacje DM, kalendarz makro). Agent Claude ocenia każdy news, a deterministyczny algorytm liczy z notowań poziomy wejścia, T1–T3 i SL. Gotowy sygnał trafia automatycznie na kanał Telegram:

```
Nazwa spółki: ORLEN (PKN)
Wejście: 56.08-56.82
Target 1: 58.56, +3.7%
Target 2: 61.46, +8.9%
Target 3: 63.98, +13.3%
StopLoss: 53.94
```

## Przepływ

```
RSS/ESPI → dedup (SQLite) → mapowanie na ticker → pre-filtr szumu i płynności
  → agent Claude (ocena + narzędzia do danych, bez cen) → silnik poziomów (ATR, swingi, R)
  → walidacja → scoring/filtry (trend, wolumen, reżim WIG20, wyniki, cooldown) → Telegram
Tracker po sesji: T1/T2/T3/SL/wygaśnięcie → odpowiedź pod sygnałem + statystyki
  (wynik w R i %: po 1/3 pozycji na T1–T3, wejście po środku strefy, bez prowizji; opcje w sekcji `tracker`)
```

## Start

```powershell
uv venv; uv pip install -e ".[dev]"
copy .env.example .env      # uzupełnij klucze
python -m pytest
python -m gpw_signals fetch-prices         # notowania z archiwum GPW (pierwszy raz ok. 20 min)
python -m gpw_signals build-tickers        # dopisz nowe spółki z archiwum do data/gpw_tickers.csv
python -m gpw_signals fetch-prices         # ...i ich notowania (z cache, ok. 1 min)
python -m gpw_signals preview PKN          # poziomy bez LLM
python -m gpw_signals backtest PKN LPP     # kalibracja silnika poziomów
python -m gpw_signals once --dry-run       # pełny cykl, druk zamiast wysyłki
python -m gpw_signals run                  # produkcja (scheduler)
python -m gpw_signals stats
python -m gpw_signals usage                # newsy wg etapu, tokeny i szacowany koszt LLM na dzień
```

## Monitoring

- **Alerty:** po kilku awariach z rzędu (źródło, agent, notowania, cykl) i od razu przy błędzie publikacji, importu notowań lub trackera. Jeden alert na awarię i jeden po powrocie. Wymaga `TELEGRAM_ADMIN_CHAT_ID` w `.env`; bez niego alerty trafiają do logów.
- **Raport dzienny** na ten sam czat: newsy, odrzucenia wg powodu, sygnały, tokeny i koszt.
- **Healthcheck:** `python -m gpw_signals health` sprawdza `data/heartbeat`. Docker oznaczy kontener jako `unhealthy`, ale sam go nie zrestartuje.
- **Logi:** `data/logs/` z rotacją (tylko `run`), opcjonalnie JSON (`logging.json_format`).

## Do zrobienia przed produkcją

- **Dane cenowe:** domyślnie darmowe notowania dzienne z [archiwum GPW](https://www.gpw.pl/archiwum-notowan), importowane do `data/prices/` (`fetch-prices`, a w trybie `run` codziennie o `schedule.prices_at`). Surowe pliki są w cache `data/raw/gpw/`, więc po dopisaniu spółek do `gpw_tickers.csv` ponowny import nie pobiera nic z sieci. Ograniczenia:
  - dane są **dzienne**, więc w trakcie sesji poziomy liczą się od ostatniego zamknięcia (#4),
  - ceny są **nieskorygowane** o splity i prawa poboru; spółka ze skokiem kursu > `max_jump_pct` jest pomijana,
  - inne źródło podepniesz, zapisując CSV do `data/prices` albo dopisując własny `PriceProvider`.
- **Źródła:** zweryfikuj URL-e feedów w `config/settings.yaml`. Feed `bankier-rekomendacje` zwraca 0 pozycji.
- **Lista spółek:** `data/gpw_tickers.csv` obejmuje cały rynek główny (ok. 390 spółek, `build-tickers`). Ręcznie opisane są tylko największe spółki (pełna nazwa, aliasy, sektor). Dla pozostałych tickerem jest nazwa skrócona GPW (np. `POLIMEXMS`). Mapowanie newsów:
  - komunikat ESPI dotyczy tylko emitenta z tytułu ("EMITENT: temat"), nie kontrahentów z treści,
  - wiadomości z portali są mapowane tylko po tytule, bo lead często wymienia spółki poboczne,
  - skróty i nazwy skrócone dopasowujemy tylko WIELKIMI literami; jakość mierzy `tests/test_mapping.py` na oznaczonych tytułach z `tests/fixtures/mapping_cases.yaml`.
- **Krok notowań:** tabela RTS 11 (MiFID II) w `market/tick_size.py`; pasmo płynności spółki to kolumna `liquidity_band` w `gpw_tickers.csv`. Pasma zmieniają się co roku 1 kwietnia: odśwież je przez `python -m gpw_signals update-ticks` (rejestr ESMA FITRS), także po `build-tickers`.
- **Kalendarz:** dni bez sesji 2026–2027 są w `data/gpw_holidays.yaml` (ładowane przez `schedule.holidays_file`; brak kolejnego roku bot zgłosi w logu od grudnia). Terminy raportów spółek bot może czytać z komunikatów ESPI do `data/report_calendar.yaml` (`sources.fetch_report_schedules`, domyślnie wyłączone). RPP, GUS i terminy sprzed włączenia dopisz ręcznie w `data/macro_calendar.yaml`.
- Na start `publish.use_test_channel: true`. Kanał docelowy włączaj dopiero po okresie testów i analizie `stats`.

## Uruchomienie w Dockerze

1. Uzupełnij `.env` (na podstawie `.env.example`).
2. Zbuduj i uruchom bota w tle:

   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```

`config/` (tylko do odczytu) i `data/` (baza SQLite, notowania) są montowane z hosta, więc dane przetrwają przebudowę obrazu.
Pozostałe komendy CLI uruchamiasz w kontenerze, np.:

```bash
docker compose run --rm gpw-signals python -m gpw_signals fetch-prices
docker compose run --rm gpw-signals python -m gpw_signals stats
docker compose run --rm gpw-signals python -m gpw_signals once --dry-run
```

## Lokalny model (Ollama)

Zamiast API Anthropic newsy może oceniać lokalny model, bez kosztów API i bez `ANTHROPIC_API_KEY`:

1. Zainstaluj [Ollama](https://ollama.com/download) i pobierz model (`ollama pull <model>`), np. polski
   [Bielik](https://ollama.com/SpeakLeash). Modele 7–14B wymagają orientacyjnie 8–16 GB VRAM; na CPU działają wolniej.
2. W `config/settings.yaml` ustaw `agent.provider: ollama` i `agent.model: <model>`. Z Dockera serwer na hoście
   to zwykle `base_url: http://host.docker.internal:11434`.
3. Przed użyciem porównaj jakość: `python -m gpw_signals eval-agent --provider ollama --model <model>`
   (przypadki: `data/eval/agent_cases.yaml`; z `--provider anthropic` wywołania są płatne).

Model lokalny dostaje jedno zapytanie z danymi w treści (bez narzędzi). Niepoprawna odpowiedź = brak sygnału,
niedostępny serwer = news wraca do kolejki.
