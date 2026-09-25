from __future__ import annotations

import argparse
import json
import sys

from .bot import run_loop, run_once
from .config import load_settings
from .netcheck import diagnose
from .paper import PaperAccount, PaperLedger


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Paper trading Polymarket BTC Up/Down 5m avec Jev (TypeSafe)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="Boucle paper trading (aucun ordre réel).")
    once = sub.add_parser("once", help="Un snapshot + une décision, sans boucle.")
    once.add_argument("--trade", action="store_true", help="Enregistre un fill paper si les filtres passent.")
    sub.add_parser("status", help="Affiche le marché BTC 5m courant, sans appeler Jev.")
    sub.add_parser("ledger", help="Affiche le portefeuille paper et les derniers événements.")
    sub.add_parser("doctor", help="Vérifie si Gamma, CLOB, Binance et TypeSafe sont joignables.")

    args = parser.parse_args()
    settings = load_settings()

    if args.cmd == "run":
        run_loop(settings)
        return
    if args.cmd == "once":
        run_once(settings, decide=True, trade=args.trade)
        return
    if args.cmd == "status":
        run_once(settings, decide=False, trade=False)
        return
    if args.cmd == "doctor":
        for line in diagnose():
            print(line)
        return
    if args.cmd == "ledger":
        account = PaperAccount(settings.paper_cash, settings.state_path, PaperLedger(settings.ledger_path))
        print(account.summary())
        if account.state_path.exists():
            print(json.dumps(json.loads(account.state_path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False))
        if settings.ledger_path.exists():
            lines = settings.ledger_path.read_text(encoding="utf-8").splitlines()[-12:]
            print("\nDerniers événements:")
            for line in lines:
                print(line)


if __name__ == "__main__":
    main()
