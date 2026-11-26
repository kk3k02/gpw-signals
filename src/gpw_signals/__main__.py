"""CLI: python -m gpw_signals {run,once,track,stats,backtest,preview,export-signals,prune-signals,...}"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from .clock import utc_now
from .config import load_settings
from .monitor import heartbeat_problem, setup_logging


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # polskie znaki/emoji w konsoli Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="gpw-signals", description="Sygnalista GPW")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Scheduler: polling newsów + tracker po sesji")
    once = sub.add_parser("once", help="Jeden cykl przetwarzania newsów")
    once.add_argument("--dry-run", action="store_true", help="Drukuj zamiast wysyłać, baza w pamięci")
    sub.add_parser("track", help="Sprawdź wyniki otwartych sygnałów")
    sub.add_parser("build-tickers", help="Uzupełnij data/gpw_tickers.csv o wszystkie spółki z archiwum GPW")
    sub.add_parser("update-ticks", help="Uzupełnij pasma płynności (krok notowań) z rejestru ESMA")
    fp = sub.add_parser("fetch-prices", help="Import notowań dziennych z archiwum GPW do data/prices")
    fp.add_argument("--from", dest="start", type=date.fromisoformat, help="YYYY-MM-DD (domyślnie: od braków)")
    fp.add_argument("--to", dest="end", type=date.fromisoformat, help="YYYY-MM-DD (domyślnie: dziś)")
    ev = sub.add_parser("eval-agent", help="Jakość oceny na oznaczonych newsach (wywołuje model!)")
    ev.add_argument("--cases", default="data/eval/agent_cases.yaml")
    ev.add_argument("--provider", choices=["anthropic", "ollama"])
    ev.add_argument("--model")
    sub.add_parser("usage", help="Newsy wg etapu, tokeny i koszt LLM na dzień")
    sub.add_parser("health", help="Kod wyjścia 1, gdy bot nie kończy cykli (healthcheck)")
    sub.add_parser("stats", help="Statystyki skuteczności")
    bt = sub.add_parser("backtest", help="Backtest silnika poziomów na historii notowań")
    bt.add_argument("tickers", nargs="*", help="Backtest samych poziomów dla tych spółek")
    bt.add_argument("--horizon", type=int, default=10)
    bt.add_argument("--from", dest="start", type=date.fromisoformat,
                    help="Backtest całego pipeline'u na historycznych newsach od YYYY-MM-DD (wywołuje model!)")
    bt.add_argument("--to", dest="end", type=date.fromisoformat)
    bt.add_argument("--news", default="data/backtest/news.yaml", help="Zbiór historycznych newsów (YAML)")
    bt.add_argument("--details", action="store_true", help="Wypisz decyzję dla każdego newsa")
    ex = sub.add_parser("export-signals", help="Eksport archiwum opublikowanych sygnałów (CSV/JSON)")
    ex.add_argument("--format", choices=["csv", "json"], default="csv")
    ex.add_argument("--out", help="Plik wynikowy (domyślnie: standardowe wyjście)")
    ex.add_argument("--from", dest="start", type=date.fromisoformat, help="Od dnia YYYY-MM-DD (włącznie)")
    ex.add_argument("--to", dest="end", type=date.fromisoformat, help="Do dnia YYYY-MM-DD (włącznie)")
    pr = sub.add_parser("prune-signals", help="Usuń zamknięte sygnały starsze niż archive.retention_days")
    pr.add_argument("--yes", action="store_true", help="Potwierdź usunięcie (bez tego tylko informacja)")
    pv = sub.add_parser("preview", help="Policz i pokaż poziomy dla spółki (bez LLM i bez wysyłki)")
    pv.add_argument("ticker")
    args = p.parse_args(argv)

    s = load_settings(args.config)
    if args.cmd in ("export-signals", "prune-signals"):  # bez App: nie wymaga tokenów
        return _archive(s, args)
    if args.cmd == "health":  # bez App: healthcheck ma być lekki i niezależny od tokenów
        return _health(s)
    setup_logging(s.logging, args.verbose, to_file=args.cmd == "run")

    # Importy leniwe – szybszy start i brak zależności od klucza API tam, gdzie niepotrzebny
    from .pipeline import App

    if args.cmd == "run":
        from .scheduler import run_forever
        run_forever(App.build(s))
    elif args.cmd == "once":
        sigs = App.build(s, dry_run=args.dry_run).run_once()
        print(f"Sygnałów: {len(sigs)}")
    elif args.cmd == "build-tickers":
        _build_tickers(s)
    elif args.cmd == "update-ticks":
        _update_ticks(s)
    elif args.cmd == "fetch-prices":
        # Import notowań nie publikuje ani nie używa bazy sygnałów – nie wymaga tokenu Telegrama
        summary = App.build(s, dry_run=True).sync_prices(args.start, args.end)
        print(f"Sesji: {summary.sessions}, dni bez sesji: {summary.no_session}, wierszy: {summary.rows}")
    elif args.cmd == "track":
        App.build(s).track()
    elif args.cmd == "eval-agent":
        _eval_agent(s, args)
    elif args.cmd == "usage":
        from .db import Database
        print(json.dumps(Database(s.db_path).daily_report(), indent=2, ensure_ascii=False))
    elif args.cmd == "stats":
        from .tracker.stats import summarize
        print(json.dumps(summarize(App.build(s).db.all_signals()), indent=2, ensure_ascii=False))
    elif args.cmd == "backtest" and (args.start or not args.tickers):
        _backtest_news(s, args)
    elif args.cmd in ("backtest", "preview"):
        app = App.build(s, dry_run=True)
        if args.cmd == "backtest":
            from .backtest import backtest_levels
            for tk in args.tickers:
                c = app.tickers.get(tk)
                df = app.prices.daily(c.symbol, 2000)
                print(tk, dict(backtest_levels(df, s.levels, args.horizon, band=c.liquidity_band)))
        else:
            _preview(app, args.ticker)
    return 0


def _archive(s, args) -> int:
    from datetime import datetime, time
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from . import archive
    from .db import Database

    db = Database(s.db_path)
    if args.cmd == "prune-signals":
        days = s.archive.retention_days
        if days is None:
            print("Retencja wyłączona (archive.retention_days puste) – nic nie usunięto")
            return 0
        if not args.yes:
            print(f"Usunięte zostałyby zamknięte sygnały starsze niż {days} dni. Uruchom z --yes.")
            return 0
        print(f"Usunięto sygnałów: {archive.prune(db.conn, days, utc_now())}")
        return 0
    tz = ZoneInfo(s.timezone)
    since = datetime.combine(args.start, time(), tz) if args.start else None
    until = datetime.combine(args.end + timedelta(days=1), time(), tz) if args.end else None
    rows = archive.published_signals(db.conn, since, until)
    text = archive.to_csv(rows) if args.format == "csv" else archive.to_json(rows)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Zapisano {len(rows)} sygnałów do {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _health(s) -> int:
    m = s.monitoring
    problem = heartbeat_problem(m.heartbeat_file, utc_now(), timedelta(minutes=m.max_heartbeat_age_min))
    print(problem or "OK")
    return 1 if problem else 0


def _build_tickers(s, sessions: int = 20) -> None:
    """Lista spółek z ostatnich `sessions` plików w cache archiwum GPW (najpierw fetch-prices)."""
    from pathlib import Path

    import pandas as pd

    from .market.gpw_archive import TYPE_SHARES, parse_archive
    from .universe import read_rows, update_universe, write_rows

    files = sorted((Path(s.market.raw_dir) / str(TYPE_SHARES)).glob("*.xls"))[-sessions:]
    if not files:
        raise SystemExit("Brak plików archiwum w cache – uruchom najpierw: python -m gpw_signals fetch-prices")
    archive = pd.concat([parse_archive(f.read_bytes(), require_volume=False) for f in files])
    upd = update_universe(read_rows(s.tickers_csv), archive)
    write_rows(s.tickers_csv, upd.rows)
    print(f"Spółek na liście: {len(upd.rows)} (dodane: {len(upd.added)}, uzupełnione nazwy skrócone: "
          f"{len(upd.filled_short_names)})")
    if upd.not_in_archive:
        print("Brak w ostatnich sesjach (sprawdź, czy nie wycofane):", ", ".join(upd.not_in_archive))
    if upd.skipped_conflicts:
        print("Pominięte (konflikt tickera/symbolu):", ", ".join(upd.skipped_conflicts))
    print("Teraz zaimportuj notowania nowych spółek z cache: python -m gpw_signals fetch-prices")


def _update_ticks(s) -> None:
    """Pasma płynności obowiązujące dziś. Spółka bez ADNT w rejestrze zachowuje dotychczasową wartość."""
    from collections import Counter

    from .clock import MARKET_TZ, market_date, utc_now
    from .market.liquidity import current_bands, fetch_records
    from .universe import read_rows, write_rows

    today = market_date(utc_now(), MARKET_TZ)
    last_april = date(today.year - (today.month < 4), 4, 1)
    rows = read_rows(s.tickers_csv)
    bands = current_bands(fetch_records(r["isin"] for r in rows if r["isin"]), today)
    changed, missing, stale = [], [], []
    for r in rows:
        if r["isin"] not in bands:
            missing.append(r["ticker"])
            continue
        band, start = bands[r["isin"]]
        if start < last_april:
            stale.append(r["ticker"])
        if r["liquidity_band"] != str(band):
            changed.append(r["ticker"])
            r["liquidity_band"] = str(band)
    write_rows(s.tickers_csv, rows)
    print(f"Spółek: {len(rows)}, zmienione pasma: {len(changed)}")
    print("Pasma:", dict(sorted(Counter(r["liquidity_band"] or "brak" for r in rows).items())))
    if stale:
        print(f"ADNT sprzed ostatniego 1 kwietnia ({len(stale)}):", ", ".join(stale))
    if missing:
        print("Brak ADNT w rejestrze:", ", ".join(missing))


def _preview(app, ticker: str) -> None:
    from .levels.engine import compute_levels
    from .levels.validate import validate_levels
    from .market.indicators import snapshot
    from .models import Assessment, Signal
    from .publish.formatter import format_signal

    c = app.tickers.get(ticker)
    snap = snapshot(app.prices.daily(c.symbol, 260))
    lr = compute_levels(snap, app.settings.levels, c.liquidity_band)
    if lr.levels is None:
        print("Brak poziomów:", lr.reason)
        return
    a = Assessment(ticker, True, "long", "inne", 0.0, 10, False, "(podgląd – bez oceny agenta)")
    print(format_signal(Signal(ticker, c.name, lr.levels, 0.0, a, "", utc_now())))
    print("Walidacja:", validate_levels(lr.levels, app.settings.filters.min_rr_t1) or "OK")


def _backtest_news(s, args) -> None:
    from zoneinfo import ZoneInfo

    from .agent.factory import make_assessor
    from .ingest.macro import EventCalendar
    from .mapping import TickerMap
    from .market.prices import make_provider
    from .news_backtest import backtest_news, load_news

    m = s.market
    rep = backtest_news(s, TickerMap.from_csv(s.tickers_csv), make_provider(m.provider, m.stooq_url, m.csv_dir),
                        None, load_news(args.news), args.start, args.end,
                        calendar=EventCalendar(s.sources.macro_calendar_file, ZoneInfo(s.timezone),
                                               extra=[s.sources.report_calendar_file]),
                        make_assessor=lambda ctx: make_assessor(s.agent, ctx))
    if args.details:
        print(*rep.details, sep="\n")
    print(json.dumps(rep.summary(), indent=2, ensure_ascii=False))


def _eval_agent(s, args) -> None:
    from .agent.evaluate import load_cases, run_cases
    from .config import AgentConfig
    from .mapping import TickerMap

    over = {k: v for k, v in (("provider", args.provider), ("model", args.model)) if v}
    cfg = AgentConfig(**{**s.agent.model_dump(), **over})
    r = run_cases(load_cases(args.cases), cfg, TickerMap.from_csv(s.tickers_csv))
    print(*r.details, sep="\n")
    print(f"{cfg.provider}/{cfg.model}: przypadków {r.total}, błędów {r.errors}, fałszywych sygnałów "
          f"{r.false_positives}, przeoczonych {r.false_negatives}, siła w zakresie {r.strength_in_range}")


if __name__ == "__main__":
    raise SystemExit(main())
