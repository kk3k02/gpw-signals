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
RSS/ESPI → dedup (SQLite) → mapowanie na ticker → pre-filtr płynności
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
```

## Do zrobienia przed produkcją

- **Dane cenowe:** domyślnie darmowe notowania dzienne z [archiwum GPW](https://www.gpw.pl/archiwum-notowan), importowane do `data/prices/` (`fetch-prices`, a w trybie `run` codziennie o `schedule.prices_at`). Surowe pliki są w cache `data/raw/gpw/`, więc po dopisaniu spółek do `gpw_tickers.csv` ponowny import nie pobiera nic z sieci. Ograniczenia:
  - dane są **dzienne**, więc w trakcie sesji poziomy liczą się od ostatniego zamknięcia (#4),
  - ceny są **nieskorygowane** o splity i prawa poboru; spółka ze skokiem kursu > `max_jump_pct` jest pomijana,
  - przed publicznym kanałem sprawdź warunki korzystania z danych GPW (#16). Inne źródło podepniesz, zapisując CSV do `data/prices` albo dopisując własny `PriceProvider`.
- **Źródła:** zweryfikuj URL-e feedów i regulaminy serwisów w `config/settings.yaml`. Feed `bankier-rekomendacje` zwraca 0 pozycji.
- **Lista spółek:** `data/gpw_tickers.csv` obejmuje cały rynek główny (ok. 390 spółek, `build-tickers`). Ręcznie opisane są tylko największe spółki (pełna nazwa, aliasy, sektor). Dla pozostałych tickerem jest nazwa skrócona GPW (np. `POLIMEXMS`). Mapowanie newsów:
  - komunikat ESPI dotyczy tylko emitenta z tytułu ("EMITENT: temat"), nie kontrahentów z treści,
  - wiadomości z portali są mapowane tylko po tytule, bo lead często wymienia spółki poboczne,
  - skróty i nazwy skrócone dopasowujemy tylko WIELKIMI literami; jakość mierzy `tests/test_mapping.py` na oznaczonych tytułach z `tests/fixtures/mapping_cases.yaml`.
- **Krok notowań:** tabela RTS 11 (MiFID II) w `market/tick_size.py`; pasmo płynności spółki to kolumna `liquidity_band` w `gpw_tickers.csv`. Pasma zmieniają się co roku 1 kwietnia: odśwież je przez `python -m gpw_signals update-ticks` (rejestr ESMA FITRS), także po `build-tickers`.
- **Kalendarz:** święta GPW w `settings.yaml` oraz kalendarz wyników w `data/macro_calendar.yaml`.
- **Prawo:** publiczne sygnały kup/cel/SL to w UE rekomendacje inwestycyjne (MAR art. 20, rozporządzenie 2016/958, nadzór KNF). Przed uruchomieniem publicznego kanału skonsultuj się z prawnikiem.
- Na start `publish.use_test_channel: true`. Kanał docelowy włączaj dopiero po okresie testów i analizie `stats`.
