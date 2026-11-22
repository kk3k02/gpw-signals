"""Import notowań z archiwum GPW (#3) i kontrola jakości danych."""
import io
from datetime import date

import pandas as pd
import pytest

from gpw_signals.market.gpw_archive import (
    TYPE_SHARES,
    GpwArchiveClient,
    PriceStore,
    import_days,
    normalize_archive,
    parse_archive,
    session_days,
    sync,
)
from gpw_signals.market.quality import data_issues
from tests.conftest import make_ohlcv

# Nagłówki jak w prawdziwym pliku GPW – z uszkodzonymi polskimi znakami
HEADERS = ["Data", "Nazwa", "ISIN", "Waluta", "Kurs otwarcia", "Kurs max", "Kurs min", "Kurs zamkni�cia",
           "Zmiana", "Wolumen", "Liczba Transakcji", "Obr�t"]


def _rows(day, items):
    return [[day, name, isin, "PLN", o, h, lo, c, 0.0, v, 10, 1.0] for name, isin, o, h, lo, c, v in items]


def _xlsx(day: str, items) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(_rows(day, items), columns=HEADERS).to_excel(buf, index=False)
    return buf.getvalue()


SHARES = {
    "2026-11-19": [("PKNORLEN", "PLPKN0000018", 120, 123, 119, 122, 1000),
                   ("XYZ", "PLXYZ0000000", 1, 1, 1, 1, 5),
                   ("PKOBP", "PLPKO0000016", 0, 0, 0, 110, 0)],  # PKO bez obrotu tego dnia
    "2026-11-20": [("PKNORLEN", "PLPKN0000018", 122, 125, 120, 121.28, 2000),
                   ("PKOBP", "PLPKO0000016", 116, 116.2, 109.4, 109.48, 3000)],
}
# Indeksy mają w archiwum GPW zawsze wolumen 0 (obrót podawany osobno)
INDICES = {d: [("WIG20", "PL9999999987", 4100, 4150, 4000, 4016.3, 0),
               ("WIG", "PL9999999995", 1, 2, 1, 1, 0)] for d in SHARES}
HTML = b"<!DOCTYPE html><html>brak notowan</html>"
URL = "u?type={type}&date={date}"


class FakeServer:
    """Udaje gpw.pl: Excel dla dni z SHARES/INDICES, HTML dla pozostałych."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        type_ = int(url.split("type=")[1].split("&")[0])
        d, m, y = url.split("date=")[1].split("-")
        day = f"{y}-{m}-{d}"
        data = SHARES if type_ == TYPE_SHARES else INDICES
        return _xlsx(day, data[day]) if day in data else HTML


def _client(path, server=None):
    return GpwArchiveClient(path, url_template=URL, min_interval_s=0, fetch=server or FakeServer())


def test_normalize_matches_broken_headers_and_drops_untraded():
    raw = pd.DataFrame(_rows("2026-11-19", SHARES["2026-11-19"]), columns=HEADERS)
    df = normalize_archive(raw)
    assert list(df.columns) == ["date", "name", "isin", "open", "high", "low", "close", "volume"]
    assert df["name"].tolist() == ["PKNORLEN", "XYZ"]  # PKOBP bez obrotu pominięty
    assert df.loc[0, "close"] == 122


def test_normalize_rejects_unknown_format():
    with pytest.raises(ValueError, match="Nieznany format"):
        normalize_archive(pd.DataFrame({"A": [1]}))


def test_parse_archive_html_means_no_session():
    assert parse_archive(HTML) is None
    assert len(parse_archive(_xlsx("2026-11-20", SHARES["2026-11-20"]))) == 2


def test_client_caches_files_and_no_session_days(tmp_path):
    server = FakeServer()
    client = _client(tmp_path, server)
    today = date(2026, 11, 23)
    assert client.fetch_day(date(2026, 11, 20), TYPE_SHARES, today) is not None
    assert client.fetch_day(date(2026, 11, 21), TYPE_SHARES, today) is None  # sobota
    assert client.fetch_day(date(2026, 11, 23), TYPE_SHARES, today) is None  # dzisiejszy plik jeszcze nie gotowy
    n = len(server.calls)
    client.fetch_day(date(2026, 11, 20), TYPE_SHARES, today)
    client.fetch_day(date(2026, 11, 21), TYPE_SHARES, today)
    assert len(server.calls) == n  # z cache, bez sieci
    client.fetch_day(date(2026, 11, 23), TYPE_SHARES, today)
    assert len(server.calls) == n + 1  # "dziś bez danych" nie jest zapamiętywane


def test_import_days_writes_mapped_shares_and_indices(tmp_path):
    store = PriceStore(tmp_path / "prices")
    days = [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21)]
    s = import_days(days, _client(tmp_path / "raw"), store, {"PLPKN0000018": "pkn", "PLPKO0000016": "pko"},
                    {"WIG20": "wig20"}, today=date(2026, 11, 23))
    assert (s.sessions, s.no_session) == (2, 1)
    assert s.missing_symbols == {"pko": 1} and s.unmapped_isins == {"PLXYZ0000000"}
    pkn = store.read("pkn")
    assert pkn["close"].tolist() == [122, 121.28] and pkn.index[-1] == pd.Timestamp("2026-11-20")
    assert len(store.read("pko")) == 1
    wig20 = store.read("wig20")
    assert len(wig20) == 2 and wig20["close"].iloc[-1] == 4016.3  # indeks mimo wolumenu 0
    assert not (tmp_path / "prices" / "wig.csv").exists()  # tylko skonfigurowane indeksy


def test_store_upsert_overwrites_same_date(tmp_path):
    store = PriceStore(tmp_path)
    row = {"date": pd.Timestamp("2026-11-20"), "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10}
    store.upsert("abc", pd.DataFrame([row]))
    store.upsert("abc", pd.DataFrame([{**row, "close": 1.8}, {**row, "date": pd.Timestamp("2026-11-19")}]))
    df = store.read("abc")
    assert df.index.tolist() == [pd.Timestamp("2026-11-19"), pd.Timestamp("2026-11-20")]
    assert df.loc["2026-11-20", "close"] == 1.8 and store.last_date("abc") == date(2026, 11, 20)


def test_sync_starts_from_missing_window_then_incremental(tmp_path):
    server = FakeServer()
    client = _client(tmp_path / "raw", server)
    store = PriceStore(tmp_path / "prices")
    args = ({"PLPKN0000018": "pkn"}, {"WIG20": "wig20"})
    sync(client, store, *args, today=date(2026, 11, 20), history_days=10)
    assert store.last_date("pkn") == date(2026, 11, 20)
    first = len(server.calls)
    assert first > 2  # pełne okno historii przy pierwszym imporcie
    sync(client, store, *args, today=date(2026, 11, 24), history_days=10)
    new_days = {u.split("date=")[1] for u in server.calls[first:]}
    assert new_days == {"23-11-2026", "24-11-2026"}  # tylko dni po ostatnim notowaniu (bez weekendu)


def test_session_days_skip_weekends_and_holidays():
    days = session_days(date(2026, 12, 23), date(2026, 12, 28), ["2026-12-24", "2026-12-25"])
    assert days == [date(2026, 12, 23), date(2026, 12, 28)]


# ------------------------------------------------------------------ kontrola jakości

TODAY = date(2026, 11, 20)


def _fresh():
    return make_ohlcv(end="2026-11-20")


def test_quality_ok_for_clean_data():
    assert data_issues(_fresh(), TODAY) == []


def test_quality_flags_stale_data():
    assert any("sprzed" in i for i in data_issues(make_ohlcv(end="2026-11-10"), TODAY))


def test_quality_flags_suspected_split():
    df = _fresh()
    df.iloc[-30:, :4] = df.iloc[-30:, :4] / 10  # split 10:1 bez korekty historii
    assert any("split" in i for i in data_issues(df, TODAY))


def test_quality_flags_inconsistent_ohlc_and_duplicates():
    df = _fresh()
    df.iloc[-1, df.columns.get_loc("high")] = df["low"].iloc[-1] - 1
    issues = data_issues(pd.concat([df, df.tail(1)]), TODAY)
    assert any("niespójne OHLC" in i for i in issues) and any("zdublowane" in i for i in issues)
