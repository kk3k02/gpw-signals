"""CLI: python -m gpw_signals {run,once,track,stats,backtest,preview}"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from .config import load_settings


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
    sub.add_parser("stats", help="Statystyki skuteczności")
    bt = sub.add_parser("backtest", help="Backtest silnika poziomów na historii notowań")
    bt.add_argument("tickers", nargs="+")
    bt.add_argument("--horizon", type=int, default=10)
    pv = sub.add_parser("preview", help="Policz i pokaż poziomy dla spółki (bez LLM i bez wysyłki)")
    pv.add_argument("ticker")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings(args.config)

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
    from .clock import utc_now
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
