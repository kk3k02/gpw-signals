# Metodologia sygnałów gpw-signals

Dokument opisuje, jak faktycznie powstaje każdy sygnał publikowany na kanale. Opis odpowiada
kodowi w `src/gpw_signals` i domyślnym parametrom z `config/settings.yaml`. Przy zmianie
parametrów w konfiguracji wartości liczbowe poniżej trzeba zaktualizować.

Link do tego dokumentu jest dołączany do każdego sygnału, gdy ustawiono `publish.methodology_url`.

## 1. Charakter sygnałów

- Wyłącznie pozycje długie (kupno) na akcjach notowanych na GPW.
- Sygnał zawiera: strefę wejścia, trzy cele (Target 1–3) i poziom StopLoss oraz krótki opis katalizatora.
- Sygnały są generowane automatycznie, bez ręcznej weryfikacji każdego z nich.
- Horyzont sygnału to 1–30 sesji, wskazany przez model językowy dla danej tezy.

## 2. Źródła informacji

1. Kanały RSS (`sources.rss`): komunikaty ESPI/EBI spółek, portale finansowe, rekomendacje domów
   maklerskich i informacje makro. Każde źródło ma typ i wagę wiarygodności
   (ESPI 1.0, dom maklerski 0.9, portal 0.75, makro 0.6).
2. Notowania dzienne z publicznego archiwum GPW (po sesji); opcjonalnie bieżący kurs w trakcie sesji.
3. Kalendarz terminów raportów okresowych spółek.

Newsy starsze niż 12 godzin są pomijane. Każdy news jest przypisywany do spółek na podstawie
nazw i tickerów (`mapping.py`); news bez rozpoznanej spółki jest odrzucany.

## 3. Etapy decyzji (pipeline.py)

Każdy news przechodzi kolejne etapy. Odrzucenie na dowolnym etapie kończy przetwarzanie,
a decyzja jest zapisywana w bazie (ślad audytowy).

1. **Filtr szumu** (`prefilter.py`) – komunikaty ESPI bez znaczenia cenotwórczego (zwołanie WZA,
   projekty uchwał, zmiany w radzie nadzorczej, wybór audytora, terminy raportów, transakcje
   insiderów poniżej 200 tys. zł) nie są oceniane. Słowa kluczowe (dywidenda, skup, wezwanie,
   umowa, wyniki itp.) zawsze przepuszczają news dalej.
2. **Cooldown** – nie więcej niż jeden sygnał na spółkę w ciągu 5 dni.
3. **Kontrola jakości danych** (`market/quality.py`) – notowania nie starsze niż 5 dni, bez
   skoków kursu ponad 40% między sesjami, wystarczająca historia.
4. **Wyznaczenie poziomów** (`levels/engine.py`) – deterministycznie, z notowań (szczegóły w p. 4).
   Model językowy nigdy nie ustala cen.
5. **Walidacja poziomów** – m.in. stosunek zysku do ryzyka do Target 1 co najmniej 1.0.
6. **Filtry rynkowe przed oceną modelu** (`scoring.py`): średni obrót z 20 sesji co najmniej
   1 mln zł; kurs nie wzrósł już o ponad 6% (sygnał „nie goni” rynku); maksymalny możliwy
   wynik punktowy musi przekraczać próg.
7. **Ocena modelu językowego** (`agent/`) – model (domyślnie Claude firmy Anthropic, opcjonalnie
   model lokalny) ocenia, czy news jest katalizatorem wzrostowym, jego siłę (0–1), typ,
   horyzont i czy informacja jest już zdyskontowana przez rynek. Model może korzystać z narzędzi
   (historia newsów spółki, notowania).
8. **Filtry po ocenie i wynik punktowy** – odrzucenie, gdy: brak kierunku long, news zdyskontowany,
   rynek w bessie (WIG20 poniżej SMA50 i SMA200) lub kurs poniżej SMA50 – chyba że siła
   katalizatora wynosi co najmniej 0.8; wyniki finansowe spółki w ciągu 3 dni (o ile sam news
   nie dotyczy wyników).
   Wynik = siła katalizatora × waga źródła × wolumen (1.0 gdy wolumen ≥ 1.2× średniej, inaczej 0.85)
   × trend (1.0 gdy kurs > SMA50 > SMA200, inaczej 0.9) × reżim rynku (hossa 1.05, neutralny 1.0,
   bessa 0.85) × współczynnik zysku do ryzyka (0.9 + 0.1·R do T1, maks. 1.1).
   Sygnał wymaga wyniku co najmniej 0.55.
9. **Limit dzienny** – maksymalnie 5 sygnałów dziennie.
10. **Publikacja** – sygnał jest najpierw zapisywany w bazie, potem wysyłany na kanał Telegram.
    Sygnał nieopublikowany w ciągu 60 minut nie jest już wysyłany.

## 4. Poziomy cenowe

Na podstawie ATR(14) i lokalnych ekstremów (swingów) z ostatnich 120 sesji:

- **Strefa wejścia**: od ceny − 0.3·ATR do ceny + 0.2·ATR.
- **StopLoss**: dolna granica wejścia − 1.5·ATR, a jeśli w pobliżu jest wsparcie – pod nim
  (z buforem 0.2·ATR). Sygnał jest odrzucany, gdy StopLoss jest dalej niż 8% od środka strefy.
- **Cele**: 1R, 2R i 3R od środka strefy wejścia (R = środek strefy − StopLoss), przesuwane tuż
  pod najbliższy opór, jeśli ten leży niedaleko poniżej celu.
- Ceny są zaokrąglane do kroku notowań GPW właściwego dla pasma płynności spółki.

## 5. Rozliczanie wyników (tracker/)

Po każdej sesji sygnały są rozliczane na świecach dziennych:

- wejście, gdy kurs znalazł się w strefie wejścia; brak wejścia w horyzoncie = „wygasł bez wejścia”,
- osiągnięcie kolejnych celów i StopLoss; gdy w jednej świecy możliwe są oba, liczony jest StopLoss
  (podejście konserwatywne),
- koniec horyzontu zamyka sygnał po kursie zamknięcia,
- wynik podawany jest w R i w procentach. Wyniki są publikowane na kanale.

Ograniczenia: rozliczenie na świecach dziennych nie odzwierciedla kolejności zdarzeń w sesji,
nie uwzględnia prowizji, poślizgu ani płynności przy realizacji zleceń.

## 6. Archiwum i konflikty interesów

- Wszystkie opublikowane sygnały wraz z wynikami są przechowywane w bazie i mogą być
  wyeksportowane: `python -m gpw_signals export-signals --format csv|json [--from] [--to] [--out]`.
  Domyślnie archiwum nie jest usuwane (`archive.retention_days` puste).
- Spółki, w których autor kanału ma pozycję, są wpisane w `publish.conflict_tickers`; sygnały dla
  nich zawierają dopisek o konflikcie interesów (`publish.conflict_note`).

## 7. Ryzyka i ograniczenia metody

- Model językowy może błędnie ocenić znaczenie newsa; jego ocena nie jest weryfikowana ręcznie.
- Wyniki historyczne (backtest, archiwum) nie gwarantują przyszłych wyników.
- Dane pochodzą z publicznych źródeł i mogą być opóźnione lub niepełne.
