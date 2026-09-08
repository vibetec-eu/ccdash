#!/usr/bin/env python3
"""Igapäevane Claude Code token-kasutuse rida logisse (launchd, kell 9:00).

Vana bash-versiooni kolm viga, mis siin parandatud on:

1. Kui ccusage'i hinnakiri kadus, kirjutas skript vaikselt "$0.00" ja tõelised
   tokeninumbrid (nt 2026-08-07: $0.00 / 129 759 914 tok, tegelik $99.26).
   Nüüd: `ccusage_lib.fetch_daily()` valideerib, proovib --offline vahemälu ja
   ebaõnnestumisel kirjutab selge VIGA-rea + macOS-teate. Valenumbrit ei teki.

2. "kuu kokku" arvutati ccusage'ist, mis kaotab vana ajaloo (Claude Code kustutab
   .jsonl-e) — nii KAHANES kuu kumulatiiv $518.88 -> $260.28. Nüüd liidetakse
   kumulatiiv LOGIST endast, mis on ainus täielik ajalooallikas.

3. Skripti kaks käivitust samal päeval tekitasid dubleeritud rea. Nüüd on kirjutus
   idempotentne: olemasolev sama kuupäeva rida asendatakse.

4. Kui skript jäi jooksmata (Mac magas, oli välja lülitatud), tekkis logisse
   JÄÄDAV auk: ta kirjutas ainult eilse päeva ja ccusage kustutab vanad
   transkriptid ~30 päevaga. Nüüd täidab iga käivitus tagantjärele KÕIK
   puuduvad päevad, mida ccusage veel mäletab — arhiiv paraneb ise.

Logi on ainus PÜSIV kasutusajalugu: 11.08.2026 seisuga oli seal 9 päeva
($345), mida ccusage enam ei näinud. Seda faili ei tohi kustutada ega
ccusage'ist "uuesti üles ehitada".

Kasutus:
    python3 token_usage_daily.py            # eilne + puuduvad päevad (launchd)
    python3 token_usage_daily.py --date D   # konkreetne päev
    python3 token_usage_daily.py --dry-run  # näita rida, ära kirjuta
    python3 token_usage_daily.py --no-backfill
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccusage_lib as c  # noqa: E402

# Hoiatusläved USD-s. Seadistus: ccdash.config.json -> thresholds.dayUsd / monthUsd.
DAY_THRESHOLD = c.threshold("dayUsd", 150.0)      # USD/päev
MONTH_THRESHOLD = c.threshold("monthUsd", 1500.0)  # USD/kuu kumulatiiv


def notify(title: str, msg: str, sound: str | None = None) -> None:
    safe = msg.replace('"', "'")
    script = f'display notification "{safe}" with title "{title}"'
    if sound:
        script += f' sound name "{sound}"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def append_or_replace(date: str, line: str) -> None:
    """Kirjuta rida logisse; asenda sama kuupäeva olemasolev rida."""
    path = c.LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, replaced = [], False
    for old in existing:
        m = c.LOG_LINE_RE.match(old.strip())
        if m and m.group(1) == date:
            if not replaced:
                out.append(line)
                replaced = True
            continue  # kustuta ka võimalikud vanad dubleeringud
        out.append(old)
    if not replaced:
        out.append(line)

    # Hoia kuupäeva järjekorras — tagantjärele täidetud päev satuks muidu faili
    # lõppu ja logi ei oleks enam silmaga loetav ega järjestikku võrreldav.
    # Kuupäevata read (VIGA-teated) jäävad oma kohale lõppu.
    dated = [(m.group(1), l) for l in out
             if (m := c.LOG_LINE_RE.match(l.strip()))]
    undated = [l for l in out if not c.LOG_LINE_RE.match(l.strip())]
    dated.sort(key=lambda t: t[0])
    path.write_text("\n".join([l for _, l in dated] + undated) + "\n",
                    encoding="utf-8")


def external_flag(row: dict | None) -> str:
    """'  [openclaw 0.25M tok hinnata]' — SELLE päeva võõra allika tokenid, mis on
    tokeninumbris sees, aga dollarites mitte. Päeva kaupa, mitte kogusummana: päev ilma
    nendeta ei saa märget (oponent 04.09). Sufiks ei riku LOG_LINE_RE sobitust."""
    if not row:
        return ""
    ext = c.external_unpriced([row])
    return "".join(f"  [{src} {tok / 1e6:.2f}M tok hinnata]" for src, tok in sorted(ext.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (vaikimisi eile, Eesti aeg)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backfill", action="store_true",
                    help="ära täida vahepealt puuduma jäänud päevi")
    args = ap.parse_args()

    today = c.today_str()
    target = args.date or (c.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # Teiste masinate koopia värskeks ENNE lugemist — muidu sõltuks päevalogi sellest,
    # kas ccdash-server juhtus öösel joosta. Ei tõsta erindit; vana koopia jääb.
    c.sync_peers()

    try:
        daily, used_offline = c.fetch_daily()
    except c.CcusageError as e:
        err = f"{c.now():%Y-%m-%d %H:%M}  VIGA: {e}"
        print(err, file=sys.stderr)
        if not args.dry_run:
            with c.LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(err + "\n")
        notify("Claude Code token-jälgija KATKI", str(e)[:200], sound="Basso")
        return 1

    by_date = {d["period"]: d for d in daily}

    # --- Tagantjärele täitmine ---------------------------------------------
    # Kõik päevad, mida ccusage veel mäletab, aga logis ei ole. Tänast ei
    # kirjuta (pooleli), sihtpäev käib allpool tavarada.
    if not args.no_backfill and not args.date:
        have = set(c.read_log())
        missing = sorted(d for d in by_date
                         if d not in have and d < today and d != target)
        for miss in missing:
            row = by_date[miss]
            cost_m = float(row["totalCost"])
            line_m = (f"{miss}  ${cost_m:.2f}  {int(row['totalTokens'])} tok"
                      f"  |  kuu kokku: "
                      f"${c.month_total_from_log(miss[:7], extra={miss: cost_m}, upto=miss):.2f}"
                      f"  |  tagantjärele{external_flag(row)}")
            print(f"[täidan puuduva] {line_m}")
            if not args.dry_run:
                append_or_replace(miss, line_m)

    day = by_date.get(target)
    cost = float(day["totalCost"]) if day else 0.0
    tokens = int(day["totalTokens"]) if day else 0
    today_cost = float(by_date[today]["totalCost"]) if today in by_date else 0.0

    # Kuu kumulatiiv LOGIST (ccusage kaotab vana ajaloo) + praegune sihtpäev.
    month = target[:7]
    month_cost = c.month_total_from_log(month, extra={target: cost}, upto=target)

    flag = "  [offline-hinnakiri]" if used_offline else ""
    line = (f"{target}  ${cost:.2f}  {tokens} tok  |  kuu kokku: ${month_cost:.2f}"
            f"  |  täna seni: ${today_cost:.2f}{flag}{external_flag(day)}")

    print(line)

    # Hinnakirja vananemise valve: ccusage.json ülekirjutused on käsitsi kirja pandud
    # LiteLLM-i tabelist, mis muutub. Ainult teavitab — automaatne parandus tähendaks
    # vaikset hinnamuutust, mis on täpselt see, mille vastu ccusage_lib võitleb.
    drift = c.check_pricing_drift()
    for w in drift:
        print(w, file=sys.stderr)

    if args.dry_run:
        return 0

    append_or_replace(target, line)
    if drift:
        with c.LOG_PATH.open("a", encoding="utf-8") as fh:
            for w in drift:
                fh.write(f"{target}  {w}\n")
        notify("ccusage hinnakiri aegunud", drift[0][:200], sound="Basso")

    if month_cost > MONTH_THRESHOLD:
        notify("Claude Code token-kasutus KÕRGE",
               f"KUU ${month_cost:.2f} (lävi ${MONTH_THRESHOLD:.0f}). "
               f"{target} ${cost:.2f}, täna seni ${today_cost:.2f}.", sound="Basso")
    elif cost > DAY_THRESHOLD:
        notify("Claude Code token-kasutus KÕRGE",
               f"{target}: ${cost:.2f} (lävi ${DAY_THRESHOLD:.0f}). "
               f"Kuu kokku ${month_cost:.2f}.", sound="Basso")
    return 0


if __name__ == "__main__":
    sys.exit(main())
