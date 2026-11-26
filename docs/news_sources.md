# Źródła newsów – robots.txt i regulaminy (issue #7)

Sprawdzone 2026-11-26. Nowe źródła są w
`config/settings.yaml` z `enabled: false` – włącz świadomie.

| Serwis | robots.txt | Regulamin / warunki | Wniosek |
|---|---|---|---|
| GPW – [gpw.pl/_rss](https://www.gpw.pl/_rss), feed [rss_komunikaty](https://www.gpw.pl/rss_komunikaty) | [robots.txt](https://www.gpw.pl/robots.txt): blokuje tylko jedną ścieżkę NewConnect | [Zastrzeżenia prawne](https://www.gpw.pl/zastrzezenia-prawne): tylko zastrzeżenie o rzetelności danych, brak zakazu pobierania; `/nota-prawna` – 404 | **Dodano** `gpw-komunikaty` (oficjalny RSS, komunikaty giełdy dla wszystkich spółek), wyłączone |
| espi.gpw.pl | brak – domena nie rozwiązuje się (DNS) | – | Nie da się użyć |
| PAP Biznes – [biznes.pap.pl](https://biznes.pap.pl) | za Incapsula (ochrona antybotowa), robots.txt zwraca stronę `noindex,nofollow` | [regulamin](https://biznes.pap.pl/regulamin) nieczytelny bez przeglądarki; serwis płatny | **Nie dodano** – ochrona antybotowa, nie obchodzimy jej |
| StockWatch – [stockwatch.pl](https://www.stockwatch.pl) | [robots.txt](https://www.stockwatch.pl/robots.txt): `*` blokuje m.in. `*?g=rsstopic*`, `/komunikaty-spolek/pliki.aspx`; osobna grupa dla botów AI | [EULA](https://www.stockwatch.pl/serwis/eula): §4 ust. 1–2 i §7 ust. 4 – tylko użytek własny niekomercyjny, zakaz kopiowania/przetwarzania bez zgody | **Nie dodano** – regulamin zabrania przetwarzania |
| Strefa Inwestorów – [strefainwestorow.pl](https://www.strefainwestorow.pl) | [robots.txt](https://strefainwestorow.pl/robots.txt): blokuje tylko `/core/`, `/profiles/` | [regulamin](https://www.strefainwestorow.pl/regulamin-strefainwestorowpl): §6 ust. 1 zakaz publicznego rozpowszechniania materiałów; brak oficjalnego RSS | **Nie dodano** – brak RSS, a sygnały są publikowane (Telegram) |
| Bankier – [bankier.pl/rss](https://www.bankier.pl/rss) | [robots.txt](https://www.bankier.pl/robots.txt): `*` nie blokuje `/rss/` ani `/wiadomosc/`; blokuje boty AI (GPTBot, CCBot…) | strona RSS odsyła do [regulaminu Bonnier](https://apps.bonnier.pl/img/regulamin.pdf) – PDF niesprawdzony | Bez zmian (feedy już w konfiguracji); `fetch_espi_body` zostaje wyłączone do ręcznej lektury PDF |

## Pełne ESPI/EBI dla wszystkich spółek

Brak darmowego, oficjalnego feedu ESPI dla wszystkich spółek: espi.gpw.pl nie działa, PAP Biznes
(dystrybutor ESPI) jest płatny i chroniony. `gpw-komunikaty` uzupełnia `bankier-espi` o komunikaty
samej giełdy (zawieszenia, dywidendy, zmiany systemu notowań), ale **nie zastępuje** raportów
bieżących spółek. Ten punkt issue #7 pozostaje otwarty.
