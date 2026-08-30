#!/usr/bin/env python3
"""ccdash — Claude Code kasutuse elav dashboard, sessioonide kaupa.

Käivita:  ~/.claude/scripts/ccdash        (avab brauseri)
          python3 ccdash.py --port 8787 --no-open

Sõltuvusi ei ole peale Pythoni standardteegi + `npx ccusage` (mille lib ise kutsub).

Ehitus: taustalõim värskendab ccusage'i andmeid iga REFRESH_SEC tagant ja hoiab
vahemälus, sest üks ccusage'i jooks võtab ~5 s. Brauser pollib /api/data, mis
vastab vahemälust kohe. Nii ei sõltu lehe kiirus ccusage'ist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccusage_lib as c  # noqa: E402
import jobs_lib  # noqa: E402

REFRESH_SEC = 20
ACTIVE_WINDOW_SEC = 10 * 60      # roheline täpp: tegevus <10 min tagasi
WORK_WINDOW_SEC = 4 * 3600       # "töös": käimasolev tööseanss, mille juurde naased
IDLE_AFTER_SEC = 90          # kui keegi pole nii kaua pollinud, lõpeta värskendamine
# Kuu hoiatuslävi eurodes (kuvamiseks ümmargune). Seadistus: ccdash.config.json
# -> thresholds.monthEur.
MONTH_THRESHOLD_EUR = c.threshold("monthEur", 1300.0)

# Demo-režiim: asendab projektinimed ja sessioonipealkirjad üldistega, et vaatest
# saaks teha ekraanipildi ilma kliendi- ja projektinimesid avaldamata. Andmed
# (kulud, tokenid, ajad) jäävad päris. `CCDASH_DEMO=1 ccdash`.
DEMO = os.environ.get("CCDASH_DEMO") == "1"

_cache: dict = {"ok": False, "error": "laen…", "fetchedAt": None}
_lock = threading.Lock()
# Millal viimati keegi /api/data küsis. Server käib launchd all kogu aeg, aga
# `npx ccusage` on kallis (~10 s) — ilma selleta jooksutaks ta seda igavesti
# ka siis, kui ühtki akent lahti ei ole.
_last_request = 0.0
_wake = threading.Event()


# ------------------------------------------------------------------ andmekorje

def weekly_limits(blocks: list[dict], active: dict | None) -> dict:
    """Kolm rida nagu claude.ai → Settings → Usage: sessioon + kaks nädalaakent.

    ⚠️ Protsenti limiidist EI SAA arvutada — Anthropic ei avalda limiiti üheski
    masinloetavas kohas (kontrollitud: transkriptid, logid, vahemälu, ~/.claude.json).
    Seega näitame **tegelikke mahtusid ja lähtestamisaegu**, mitte täituvust.
    Protsendi enda jaoks: claude.ai → Settings → Usage.

    Nädalaaken lähtub claude.ai kuvatud ajast "Resets Mon 7:00 PM" = E 19:00.
    """
    now = c.now()

    # Viimane esmaspäev 19:00 (kaasa arvatud praegu, kui just möödus).
    anchor = now.replace(hour=19, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(anchor.weekday() - 0) % 7)
    if anchor > now:
        anchor -= timedelta(days=7)
    week_end = anchor + timedelta(days=7)

    all_cost = fable_cost = 0.0
    all_tok = fable_tok = 0
    for b in blocks:
        if b.get("isGap"):
            continue
        try:
            start = datetime.fromisoformat(str(b["startTime"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if start.astimezone(c.TZ) < anchor:
            continue
        cost = float(b.get("costUSD") or 0.0)
        tok = int(b.get("totalTokens") or 0)
        all_cost += cost
        all_tok += tok
        if any("fable" in str(m).lower() for m in (b.get("models") or [])):
            fable_cost += cost
            fable_tok += tok

    sess = None
    if active:
        try:
            s_end = datetime.fromisoformat(str(active["endTime"]).replace("Z", "+00:00"))
            sess = {
                "cost": float(active.get("costUSD") or 0.0),
                "tokens": int(active.get("totalTokens") or 0),
                "entries": int(active.get("entries") or 0),
                "resetsAt": s_end.astimezone(c.TZ).isoformat(),
                "resetsInMin": max(0, int((s_end.astimezone(c.TZ) - now).total_seconds() // 60)),
            }
        except (KeyError, ValueError):
            sess = None

    return {
        "session": sess,
        "week": {
            "allCost": all_cost, "allTokens": all_tok,
            "fableCost": fable_cost, "fableTokens": fable_tok,
            "resetsAt": week_end.isoformat(),
            "resetsInMin": max(0, int((week_end - now).total_seconds() // 60)),
            "startedAt": anchor.isoformat(),
        },
    }


def demo_anonymize(sessions: list[dict]) -> list[dict]:
    """Projektinimed -> demo1…demoN (kulu järgi), pealkirjad -> üldised.

    Mõlemad on vaja: `aiTitle` on vabas vormis lause päris tööst ja seda kuvatakse
    sessioonitabelis. Järjestus on kulu järgi, seega sama andmestik annab alati
    sama nummerduse.
    """
    totals: dict[str, float] = {}
    for s in sessions:
        totals[s["project"]] = totals.get(s["project"], 0.0) + (s["cost"] or 0.0)
    names = {p: f"demo{i}" for i, p in
             enumerate(sorted(totals, key=lambda k: totals[k], reverse=True), 1)}
    seen: dict[str, int] = {}
    for s in sessions:
        p = names[s["project"]]
        seen[p] = seen.get(p, 0) + 1
        s["project"] = p
        s["title"] = f"{p} — sessioon {seen[p]}"
    return sessions


def collect() -> dict:
    parts, used_offline = c.fetch_multi(["daily", "session"])
    daily = parts["daily"]
    sessions = c.enrich_sessions(parts["session"])
    if DEMO:
        sessions = demo_anonymize(sessions)
    # Taustatööd: collect_jobs() ei tõsta kunagi erindit (vt jobs_lib), sest
    # iga erind siin peidaks brauseris KOGU dashboardi veabänneri taha.
    jobs = jobs_lib.collect_jobs()
    if DEMO:
        jobs = jobs_lib.demo_anonymize_jobs(jobs)
    blocks = c.fetch_blocks()

    today = c.today_str()
    by_date = {d["period"]: d for d in daily}
    today_cost = float(by_date.get(today, {}).get("totalCost") or 0.0)
    today_tokens = int(by_date.get(today, {}).get("totalTokens") or 0)

    month = today[:7]
    month_cost = c.month_total_from_log(month, extra={today: today_cost})

    active = next((b for b in blocks if b.get("isActive")), None)
    now_ts = c.now().timestamp()
    limits = weekly_limits(blocks, active)

    # Viimased 14 KALENDRIPÄEVA, vanemast uuemani. Kasutuseta päevad tuleb nullina
    # sisse kirjutada — ccusage jätab need välja ja ajatelg läheks katki (08.08 auk).
    chart = []
    for back in range(13, -1, -1):
        day = (c.now() - timedelta(days=back)).strftime("%Y-%m-%d")
        row = by_date.get(day)
        chart.append({
            "date": day,
            "cost": round(float(row["totalCost"]), 2) if row else 0.0,
            "tokens": int(row["totalTokens"]) if row else 0,
        })

    # Koondnumbrid. Keskmine arvutatakse KASUTUSPÄEVADE, mitte 14 päeva peale —
    # puhkepäev nulliga vajutaks keskmise alla ja annaks vale pildi tempost.
    used = [r for r in chart if r["cost"] > 0]
    peak = max(chart, key=lambda r: r["cost"]) if chart else {"cost": 0, "date": ""}
    chart_stats = {
        "total": sum(r["cost"] for r in chart),
        "avg": (sum(r["cost"] for r in used) / len(used)) if used else 0.0,
        "peak": peak["cost"],
        "peakDate": peak["date"],
        "usedDays": len(used),
        "days": len(chart),
    }

    for s in sessions:
        age = (now_ts - s["lastSort"]) if s["lastSort"] else 1e12
        s["live"] = age < ACTIVE_WINDOW_SEC
        s["working"] = age < WORK_WINDOW_SEC

    # Projektide lõikes — "mis mu kvooti sööb" on kasulikum kui üksik sessioon.
    agg: dict[str, dict] = {}
    for s in sessions:
        a = agg.setdefault(s["project"], {"project": s["project"], "cost": 0.0,
                                          "tokens": 0, "sessions": 0, "live": False})
        a["cost"] += s["cost"]
        a["tokens"] += s["tokens"]
        a["sessions"] += 1
        a["live"] = a["live"] or s["live"]
    projects = sorted(agg.values(), key=lambda x: x["cost"], reverse=True)

    fx = c.eur_rate()

    return {
        "ok": True,
        "error": None,
        "fx": fx,
        "fetchedAt": c.now().isoformat(),
        "fetchedLabel": c.now().strftime("%H:%M:%S"),
        "offlinePricing": bool(used_offline),
        "today": {"date": today, "cost": today_cost, "tokens": today_tokens},
        # Lävi on ümmargune EUR-summa; hoiame teda USD-s, sest kõik muud summad
        # tulevad ccusage'ist USD-s ja teisendus toimub alles kuvamisel.
        "month": {"month": month, "cost": month_cost,
                  "threshold": MONTH_THRESHOLD_EUR * fx["rate"]},
        "active": active,
        "limits": limits,
        "sessions": sessions,
        "projects": projects,
        "chart": chart,
        "chartStats": chart_stats,
        "sessionCount": len(sessions),
        "jobs": jobs,
    }


def refresher() -> None:
    """Värskenda ainult siis, kui keegi vaatab.

    Server käib launchd all sisselogimisest peale, aga `npx ccusage` maksab ~10 s
    protsessoriaega. Ilma selle väravata jookseks see igavesti ka siis, kui ühtki
    akent lahti ei ole. Nüüd magab lõim sündmusel, mille päring äratab.
    """
    global _cache
    last_fetch = 0.0
    while True:
        if (time.time() - _last_request) > IDLE_AFTER_SEC:
            _wake.wait()      # maga, kuni keegi küsib
            _wake.clear()
            continue
        if time.time() - last_fetch < REFRESH_SEC:
            time.sleep(1)
            continue
        try:
            data = collect()
        except c.CcusageError as e:
            data = {"ok": False, "error": str(e), "fetchedAt": c.now().isoformat(),
                    "fetchedLabel": c.now().strftime("%H:%M:%S")}
        except Exception as e:  # noqa: BLE001 - dashboard ei tohi surra
            data = {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "fetchedAt": c.now().isoformat(),
                    "fetchedLabel": c.now().strftime("%H:%M:%S")}
        last_fetch = time.time()
        with _lock:
            _cache = data


# ------------------------------------------------------------------------ leht

PAGE = r"""<!doctype html>
<html lang="et">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ccdash — Claude Code kasutus</title>
<style>
:root{
  color-scheme: light;
  --surface-1:#fcfcfb; --plane:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-dim:#9ec5f4;
  /* Kategooriapalett projektiribale, fikseeritud järjekorras (mitte tsükliline).
     Järjestus on CVD-ohutuse mehhanism: naaberpaarid on eristatavad ka
     värvipimeda lugeja jaoks. 7. pesa taha läheb kõik "muu" alla. */
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
  --s5:#e87ba4; --s6:#008300; --s7:#898781;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --surface-1:#1a1a19; --plane:#0d0d0d;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-dim:#1c5cab;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
    --s5:#d55181; --s6:#008300; --s7:#898781;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --surface-1:#1a1a19; --plane:#0d0d0d;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; --series-dim:#1c5cab;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
  --s5:#d55181; --s6:#008300; --s7:#898781;
}
*{box-sizing:border-box}
body{
  margin:0; padding:20px 20px 48px;
  background:var(--plane); color:var(--text-primary);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--good);display:inline-block;margin-right:5px;vertical-align:1px}
.dot.stale{background:var(--warning)}
.dot.err{background:var(--critical)}
button{
  font:inherit;font-size:12.5px;padding:4px 11px;border-radius:7px;cursor:pointer;
  border:1px solid var(--border);background:var(--surface-1);color:var(--text-secondary);
}
button:hover{color:var(--text-primary)}
.card{
  background:var(--surface-1);border:1px solid var(--border);border-radius:11px;
  padding:12px 14px;
}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:12px}
.tile .label{font-size:12px;color:var(--muted);margin-bottom:5px}
.tile .val{font-size:27px;font-weight:600;letter-spacing:-.02em;line-height:1.12}
.tile .note{font-size:12px;color:var(--text-secondary);margin-top:5px}
.meter{height:6px;background:var(--grid);border-radius:3px;overflow:hidden;margin-top:9px}
.meter > i{display:block;height:100%;background:var(--series-1);border-radius:3px}
.meter > i.warning{background:var(--warning)}
.meter > i.critical{background:var(--critical)}
section,details{margin-top:12px}
h2{font-size:13.5px;margin:0 0 3px;font-weight:600}
.hint{font-size:12px;color:var(--muted);margin:0 0 9px}
.count{color:var(--muted);font-weight:400}

/* Kokkuklapitav plokk — täisnimekiri on harva vaja, aga peab käepärast olema. */
details > summary{cursor:pointer;list-style:none;user-select:none}
details > summary::-webkit-details-marker{display:none}
details > summary::before{content:"▸ ";color:var(--muted)}
details[open] > summary::before{content:"▾ "}
details > summary:focus-visible{outline:2px solid var(--series-1);outline-offset:3px;border-radius:4px}

/* ── Töös olevad sessioonid ──────────────────────────────────────────────
   Rida = üks sessioon. Konteksti riba on siin põhiline visuaal: number nõuab
   lugemist, riba loeb end ise ette. */
.worklist{display:flex;flex-direction:column;gap:1px}
.work{
  display:grid;grid-template-columns:minmax(0,1.5fr) 62px minmax(90px,1fr) 66px;
  align-items:center;gap:12px;padding:8px 6px;border-radius:7px;
}
.work:hover{background:color-mix(in oklab,var(--series-1) 7%,transparent)}
.work .who{min-width:0}
.work .proj{font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.work .ttl{
  font-size:11.5px;color:var(--muted);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
}
.work .when{font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums}
.work .ctxwrap{display:flex;align-items:center;gap:8px}
.work .ctxnum{
  font-size:11.5px;font-variant-numeric:tabular-nums;white-space:nowrap;min-width:64px;
}
.work .cost{text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
.work .meter{flex:1;margin:0;min-width:40px}
.empty{color:var(--muted);font-size:13px;padding:10px 6px}

/* ── 14 päeva koondnumbrid ─────────────────────────────────────────────── */
.statrow{
  display:flex;flex-wrap:wrap;gap:6px 26px;margin-bottom:10px;
  font-variant-numeric:tabular-nums;
}
.statrow div{display:flex;flex-direction:column;gap:1px}
.statrow .k{font-size:11px;color:var(--muted)}
.statrow .v{font-size:15px;font-weight:600;letter-spacing:-.01em}

/* ── Projektiriba ──────────────────────────────────────────────────────── */
/* Virnastatud riba asendab 15-realise tabeli. 2px pind segmentide vahel, et
   naabervärvid ei sulaks kokku. */
.stack{display:flex;height:26px;border-radius:5px;overflow:hidden;gap:2px;background:var(--grid)}
.stack > i{height:100%;display:block}
.projlegend{
  display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:11px;font-size:12.5px;
}
.projlegend span{display:inline-flex;align-items:center;gap:7px}
.projlegend .sw{width:10px;height:10px;border-radius:3px;flex:none}
.projlegend .amt{color:var(--muted);font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{
  text-align:left;font-size:11.5px;font-weight:600;color:var(--muted);
  padding:0 10px 7px;border-bottom:1px solid var(--grid);white-space:nowrap;
}
th.num,td.num{text-align:right}
td{padding:7px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:color-mix(in oklab,var(--series-1) 7%,transparent)}
.proj{font-weight:550}
.sid{color:var(--muted);font-size:11.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.model{font-size:11.5px;color:var(--text-secondary)}
.bar{height:7px;border-radius:3.5px;background:var(--series-1);min-width:2px}
.barcell{width:132px}
.livedot{width:6px;height:6px;border-radius:50%;background:var(--good);display:inline-block;margin-right:6px}
/* Konteksti tase. Värv EI kanna tähendust üksi — kõrvale tuleb märk (· / ▲),
   sest värvipimeda lugeja jaoks oleks pelk toon loetamatu. */
.ctx-warn{color:var(--warning)} .ctx-high{color:var(--critical);font-weight:600}
.ctx-warn::after{content:" ·"} .ctx-high::after{content:" ▲"}
.chart{display:flex;align-items:flex-end;gap:2px;height:132px;padding-top:6px}
.col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;position:relative;cursor:default}
.col .fill{background:var(--series-1);border-radius:4px 4px 0 0;min-height:2px}
.col.dim .fill{background:var(--series-dim)}
/* Kasutuseta päev: kriips baasjoonel, MITTE lühike riba — 2px riba loeks nagu
   väike kulu, mida seal ei olnud. */
.col .zero{height:1px;background:var(--baseline)}
.xaxis{display:flex;gap:2px;border-top:1px solid var(--baseline);padding-top:5px;margin-top:0}
.xaxis span{flex:1;text-align:center;font-size:10.5px;color:var(--muted)}
.tip{
  position:fixed;pointer-events:none;z-index:9;opacity:0;transition:opacity .1s;
  background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
  padding:7px 10px;font-size:12px;box-shadow:0 6px 22px rgba(0,0,0,.16);
  font-variant-numeric:tabular-nums;white-space:nowrap;
}
.err{border-color:var(--critical);color:var(--critical)}
footer{margin-top:22px;font-size:11.5px;color:var(--muted);line-height:1.65}
.toggle{background:none;border:none;color:var(--series-1);padding:8px 0;font-size:12.5px}

/* Taustatööd: ootajate hoiatusriba. Ilmub AINULT siis, kui keegi ootab vastust —
   just seepärast tohib ta olla nii jõuline. Null müra, kui kõik on korras. */
#jobAlert{display:none;border-left:4px solid var(--warning);margin-bottom:14px}
#jobAlert h2{color:var(--warning)}
.jobrow{display:grid;grid-template-columns:82px 1fr auto;gap:10px;align-items:baseline;
  padding:8px 6px;border-top:1px solid var(--border)}
.jobrow:first-of-type{border-top:0}
.jobrow .jid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.jobrow .jname{font-weight:600}
.jobrow .jneeds{color:var(--muted);font-size:12px;margin-top:2px}
.jobrow .jwait{white-space:nowrap;font-size:12px;color:var(--warning)}
.jobrow code{background:rgba(127,127,127,.14);padding:1px 5px;border-radius:4px;
  font-size:12px;user-select:all}
.badge{display:inline-block;font-size:10px;line-height:1.5;padding:0 5px;border-radius:4px;
  margin-left:5px;background:rgba(127,127,127,.14);color:var(--muted);
  vertical-align:middle}
.badge.wait{background:var(--warning);color:#1a1a1a;font-weight:600}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Claude Code kasutus</h1>
    <span class="sub" id="status"><span class="dot stale"></span>laen…</span>
    <span style="flex:1"></span>
    <button id="themeBtn" title="Vaheta teema">Teema</button>
    <button id="refreshBtn">Värskenda</button>
  </header>

  <div id="errBox"></div>

  <section class="card" id="jobAlert">
    <h2>⏳ Taustatööd ootavad sinu vastust <span class="count" id="jobWaitCount"></span></h2>
    <p class="hint">Need jooksevad daemonis, ilma terminalita — VS Code'i tabiribal ega
      <code>/tasks</code> all neid EI OLE. Ava käsuga <code>claude attach &lt;id&gt;</code>
      päris terminalis (vajab TTY-d, Claude'i sessiooni seest ei tööta).</p>
    <div id="jobList"></div>
  </section>

  <section class="card" style="margin-bottom:14px">
    <h2>Limiitide aknad</h2>
    <p class="hint">Tegelik maht ja lähtestumisaeg. <strong>Protsenti limiidist siin ei ole</strong> —
      Anthropic ei avalda limiiti masinloetavalt; % vaata claude.ai → Settings → Usage.
      Viimane veerg on <strong>möödunud aeg</strong> aknast, mitte ärakasutatud osa.
      Sessioonirea „Lähtestub" on <em>ccusage'i bloki</em> lõpp (esimese kõne tund + 5 h),
      mitte Anthropicu päris limiidiaken; nädalaread järgivad claude.ai aega (E 19:00).</p>
    <table>
      <thead><tr>
        <th>Aken</th><th>Lähtestub</th>
        <th class="num">Tokenid</th><th class="num">Väärtus</th><th>Aeg akent läbi</th>
      </tr></thead>
      <tbody id="limitBody"></tbody>
    </table>
  </section>

  <div class="tiles" id="tiles"></div>

  <section class="card">
    <h2>Töös <span class="count" id="workCount"></span></h2>
    <p class="hint">Viimase 4 tunni sessioonid. Riba näitab konteksti — täis riba tähendab,
      et restart tasub end ära.</p>
    <div class="worklist" id="workList"></div>
  </section>

  <section class="card">
    <h2>Viimased 14 päeva</h2>
    <div class="statrow" id="chartStats"></div>
    <div class="chart" id="chart"></div>
    <div class="xaxis" id="xaxis"></div>
  </section>

  <section class="card">
    <h2>Projektid</h2>
    <p class="hint">Kogu nähtav ajalugu.</p>
    <div class="stack" id="projStack"></div>
    <div class="projlegend" id="projLegend"></div>
  </section>

  <details class="card" id="allSess">
    <summary><h2 style="display:inline">Kõik sessioonid <span class="count" id="sessCount"></span></h2></summary>
    <div class="scroll" style="margin-top:12px">
      <table>
        <thead><tr>
          <th>Projekt</th><th>Viimane tegevus</th><th class="num">Kontekst</th>
          <th class="num">Tokenid</th><th class="num">Kulu</th><th>Suhteline kulu</th>
        </tr></thead>
        <tbody id="sessBody"></tbody>
      </table>
    </div>
    <button class="toggle" id="moreBtn"></button>
  </details>

  <footer>
    Summad on Anthropicu <strong>API listihind</strong>, teisendatud eurodesse
    (<span id="fxNote">…</span>) — tellimuse puhul ei ole see arve, vaid <em>saadud väärtus</em>.
    Projektinimi tuleb transkripti <code>cwd</code>-väljast, pealkiri <code>aiTitle</code>-st,
    kontekst viimasest <code>cache_read</code>-st (· üle 200k, ▲ üle 350k — mõõdetud
    14&nbsp;789 kõne pealt: üle 350k kasvanud sessioon maksab sama töö eest ~1,9× rohkem
    kui 200k juures lõpetatu, sest kogu kontekst loetakse uuesti igal käigul).
    <strong>„API-päringut" ei ole sinu promptide arv</strong> — üks prompt tekitab tavaliselt
    kümneid päringuid, sest iga tööriistakäik on eraldi päring ja <em>alamagentide</em>
    päringud lähevad samasse summasse. Enamik tokeneid on seetõttu <code>cache_read</code>
    (kogu konteksti taaslugemine igal käigul), mitte uus sisend: mõõdetud aknas 85% vs 0,3%.
    Andmed: <code>ccusage</code> üle <code>~/.claude/projects/*.jsonl</code>.
    Kuu kumulatiiv tuleb <code>~/.claude/logs/token-usage-daily.log</code>-ist, sest Claude Code
    kustutab vanu transkripte ja ccusage kaotaks ajaloo. Ajad Eesti ajas.
  </footer>
</div>
<div class="tip" id="tip"></div>

<script>
const tip = document.getElementById('tip');
let showAll = false, lastData = null;

// Kõik ccusage'i summad on USD-s (Anthropicu API listihind). Kuvame eurodes,
// EKP päevakursi järgi; `fxRate` seatakse iga laadimisega.
let fxRate = 1.1555;
const eur = n => (n / fxRate).toLocaleString('et-EE',
  {minimumFractionDigits:2, maximumFractionDigits:2}) + ' €';
const usd = eur;   // vana nimi, et kõik kutsujad kohe tööle jääks
const num = n => n.toLocaleString('et-EE');
const compact = n => n >= 1e9 ? (n/1e9).toFixed(2)+' mld'
                  : n >= 1e6 ? (n/1e6).toFixed(1)+' mln'
                  : n >= 1e3 ? (n/1e3).toFixed(0)+' tuh' : String(n);

function showTip(e, html){
  tip.innerHTML = html; tip.style.opacity = '1';
  const r = tip.getBoundingClientRect();
  let x = e.clientX + 13, y = e.clientY - r.height - 10;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 13;
  if (y < 8) y = e.clientY + 16;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
const hideTip = () => tip.style.opacity = '0';
// Sessioonipealkirjad tulevad transkriptidest — kohtle neid andmetena, mitte HTML-ina.
const esc = s => String(s).replace(/[&<>"']/g, ch =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
// Ilma selleta jääb tuletekst kerimisel ekraanile rippuma: position:fixed element
// ei saa mouseleave'i, kui märk ise ta alt ära keritakse.
addEventListener('scroll', hideTip, true);

function tile(label, val, note, meter){
  return `<div class="card tile"><div class="label">${label}</div>
    <div class="val">${val}</div>${note ? `<div class="note">${note}</div>` : ''}
    ${meter || ''}</div>`;
}

function renderTiles(d){
  const t = [];
  const a = d.active;
  if (a){
    const start = new Date(a.startTime), end = new Date(a.endTime), now = new Date();
    const pct = Math.max(0, Math.min(100, (now - start) / (end - start) * 100));
    const leftMin = Math.max(0, Math.round((end - now) / 60000));
    const hh = Math.floor(leftMin/60), mm = leftMin % 60;
    const rate = a.burnRate ? a.burnRate.costPerHour : 0;
    const proj = a.projection ? a.projection.totalCost : 0;
    // Staatusvärv projektsiooni järgi: aken, mis lõpeks üle $300, on erakordne.
    const cls = proj > 300 ? 'critical' : proj > 150 ? 'warning' : '';
    t.push(tile('Aktiivne 5 h aken', usd(a.costUSD),
      `${start.toLocaleTimeString('et-EE',{hour:'2-digit',minute:'2-digit'})}–` +
      `${end.toLocaleTimeString('et-EE',{hour:'2-digit',minute:'2-digit'})} · ` +
      `jäänud ${hh}h ${mm}min · ${num(a.entries)} API-päringut`,
      `<div class="meter"><i style="width:${pct.toFixed(1)}%"></i></div>`));
    t.push(tile('Põlemiskiirus', usd(rate) + '/h',
      `Selles tempos akna lõpuks <strong>${usd(proj)}</strong>` +
      (cls ? ` · ${cls === 'critical' ? '⚠︎ erakordselt kiire' : '⚠︎ kiire'}` : ''),
      `<div class="meter"><i class="${cls}" style="width:${Math.min(100, proj/400*100).toFixed(1)}%"></i></div>`));
  } else {
    t.push(tile('Aktiivne 5 h aken', '—', 'Praegu aktiivset akent ei ole', ''));
    t.push(tile('Põlemiskiirus', '—', 'Ootel', ''));
  }
  // Konteksti-plaat: vaatab ainult ELAVAID sessioone — vana sessiooni suur
  // kontekst ei maksa midagi, kuni sa temaga edasi ei kirjuta.
  const live = d.sessions.filter(s => s.live && s.ctx > 0);
  const worst = live.reduce((a, s) => s.ctx > (a ? a.ctx : 0) ? s : a, null);
  if (worst){
    const pct = Math.min(100, worst.ctx / 350000 * 100);
    const cls = worst.ctxLevel === 'high' ? 'critical'
              : worst.ctxLevel === 'warn' ? 'warning' : '';
    t.push(tile('Suurim elav kontekst', compact(worst.ctx),
      `${esc(worst.project)} · ` + (worst.ctxLevel === 'high'
        ? '<strong>restardi</strong> — ~1,9× kallim'
        : worst.ctxLevel === 'warn' ? 'restardi järgmisel ülesandepiiril'
        : 'optimaalses vahemikus'),
      `<div class="meter"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></div>`));
  } else {
    t.push(tile('Suurim elav kontekst', '—', 'Elavaid sessioone ei ole', ''));
  }
  t.push(tile('Täna kokku', usd(d.today.cost),
    `${compact(d.today.tokens)} tokenit · ${d.today.date}`, ''));
  const mp = Math.min(100, d.month.cost / d.month.threshold * 100);
  const mcls = mp >= 100 ? 'critical' : mp >= 75 ? 'warning' : '';
  t.push(tile('Kuu kokku', usd(d.month.cost),
    `${d.month.month} · hoiatuslävi ${usd(d.month.threshold)} (${mp.toFixed(0)}%)`,
    `<div class="meter"><i class="${mcls}" style="width:${mp.toFixed(1)}%"></i></div>`));
  document.getElementById('tiles').innerHTML = t.join('');
}

// Konteksti tooltip on nüüd kahes kohas (töös-plokk + täisnimekiri) — üks funktsioon.
function bindCtxTips(sel){
  document.querySelectorAll(sel).forEach(el => {
    const v = +el.dataset.ctx;
    if (!v) return;
    const msg = v >= 350000
      ? '<strong>Restardi</strong> — üle 350k maksab sama töö ~1,9× rohkem'
      : v >= 200000
      ? 'Läheneb piirile — restardi järgmisel ülesandepiiril'
      : 'Optimaalses vahemikus (kuni ~200k)';
    el.onmousemove = e => showTip(e, `${num(v)} tokenit konteksti<br>${msg}`);
    el.onmouseleave = hideTip;
  });
}

function dur(min){
  const h = Math.floor(min/60), m = min%60;
  if (h >= 24) return `${Math.floor(h/24)} p ${h%24} h`;
  return h ? `${h} h ${m} min` : `${m} min`;
}

function renderLimits(d){
  const L = d.limits, s = L.session, w = L.week;
  const clock = iso => new Date(iso).toLocaleString('et-EE',
    {weekday:'short', hour:'2-digit', minute:'2-digit'});
  // Riba = kui suur osa AJAAKNAST on läbi. See EI ole limiidi täituvus.
  const sessPct = s ? Math.max(0, Math.min(100, (1 - s.resetsInMin/300) * 100)) : 0;
  const weekPct = Math.max(0, Math.min(100, (1 - w.resetsInMin/(7*24*60)) * 100));
  const row = (name, sub, resets, inMin, tok, cost, pct, dimmed) => `
    <tr${dimmed ? ' style="opacity:.55"' : ''}>
      <td><span class="proj">${name}</span><div class="sid">${sub}</div></td>
      <td>${resets}<div class="sid">${dur(inMin)} pärast</div></td>
      <td class="num">${compact(tok)}</td>
      <td class="num">${eur(cost)}</td>
      <td class="barcell">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="meter" style="flex:1;margin:0"><i style="width:${pct.toFixed(1)}%"></i></div>
          <span class="model">${pct.toFixed(0)}%</span>
        </div>
      </td>
    </tr>`;
  document.getElementById('limitBody').innerHTML =
    (s ? row('Praegune sessioon', '5 h aken · ' + num(s.entries) + ' API-päringut',
             clock(s.resetsAt), s.resetsInMin, s.tokens, s.cost, sessPct, false)
       : `<tr><td><span class="proj">Praegune sessioon</span><div class="sid">5 h aken</div></td>
            <td colspan="4" class="model">aktiivset akent ei ole</td></tr>`) +
    row('Kõik mudelid', 'nädalaaken', clock(w.resetsAt), w.resetsInMin,
        w.allTokens, w.allCost, weekPct, false) +
    row('Fable', 'nädalaaken', clock(w.resetsAt), w.resetsInMin,
        w.fableTokens, w.fableCost, weekPct, w.fableTokens === 0);
}

function renderWorking(d){
  const rows = d.sessions.filter(s => s.working), jb = jobsById(d);
  document.getElementById('workCount').textContent = rows.length ? `· ${rows.length}` : '';
  if (!rows.length){
    document.getElementById('workList').innerHTML =
      '<div class="empty">Viimase 4 tunni jooksul pole ükski sessioon liikunud.</div>';
    return;
  }
  document.getElementById('workList').innerHTML = rows.map(s => {
    // Riba täitub 350k suunas — see on punkt, kus mõõtmiste järgi läheb ~1,9× kallimaks.
    const pct = Math.min(100, s.ctx / 350000 * 100);
    const cls = s.ctxLevel === 'high' ? 'critical' : s.ctxLevel === 'warn' ? 'warning' : '';
    return `
    <div class="work">
      <div class="who">
        <div class="proj">${s.live ? '<span class="livedot"></span>' : ''}${esc(s.project)}</div>
        <div class="ttl">${esc(s.title || s.short)}${jobBadges(jb[s.short])}</div>
      </div>
      <div class="when">${s.last.slice(6)}</div>
      <div class="ctxwrap" data-ctx="${s.ctx}">
        <div class="meter"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></div>
        <span class="ctxnum ctx-${s.ctxLevel}">${s.ctx ? compact(s.ctx) : '—'}</span>
      </div>
      <div class="cost">${eur(s.cost)}</div>
    </div>`;
  }).join('');
  bindCtxTips('#workList [data-ctx]');
}

function jobsById(d){
  const m = {};
  (d.jobs && d.jobs.items || []).forEach(j => { m[j.id] = j; });
  return m;
}

// "ootab 3 päeva" — mitte töö vanus, vaid kui kaua ta on VASTUSETA seisnud.
function waitedFor(iso){
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  if (isNaN(ms) || ms < 0) return '';
  const h = ms / 3600000;
  if (h < 1) return `ootab ${Math.max(1, Math.round(ms/60000))} min`;
  if (h < 48) return `ootab ${Math.round(h)} h`;
  return `ootab ${Math.round(h/24)} päeva`;
}

// Märgid sessioonireal: taustatöö, tabita, ootab.
function jobBadges(j){
  if (!j) return '';
  let b = '<span class="badge">bg</span>';
  if (!j.hasTerminal) b += '<span class="badge">tabita</span>';
  if (j.waiting) b += '<span class="badge wait">⏳ ootab</span>';
  return b;
}

function renderJobs(d){
  const box = document.getElementById('jobAlert');
  const jobs = (d.jobs && d.jobs.items || []).filter(j => j.waiting);
  if (!jobs.length){ box.style.display = 'none'; return; }
  box.style.display = '';
  document.getElementById('jobWaitCount').textContent = `· ${jobs.length}`;
  document.getElementById('jobList').innerHTML = jobs.map(j => `
    <div class="jobrow">
      <div class="jid">${esc(j.id)}</div>
      <div>
        <div class="jname">${esc(j.name)}${j.hasTerminal ? '' : '<span class="badge">tabita</span>'}</div>
        <div class="jneeds">${esc(j.needs || j.detail || 'blokeeritud — põhjus state.json-is puudub, vaata: claude logs ' + j.id)}</div>
        <div class="jneeds"><code>claude attach ${esc(j.id)}</code></div>
      </div>
      <div class="jwait">${waitedFor(j.updatedAt)}</div>
    </div>`).join('');
}

function renderChartStats(d){
  const s = d.chartStats;
  document.getElementById('chartStats').innerHTML = [
    ['Kokku', eur(s.total)],
    ['Keskmine päevas', eur(s.avg)],
    ['Tipp', `${eur(s.peak)} · ${s.peakDate.slice(8)}.${s.peakDate.slice(5,7)}`],
    ['Kasutuspäevi', `${s.usedDays} / ${s.days}`],
  ].map(([k,v]) => `<div><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}

function renderProjects(d){
  const rows = d.projects;
  const total = rows.reduce((a,p) => a + p.cost, 0) || 1;
  // Top-6 saab oma värvi, ülejäänu koondub üheks halliks segmendiks — kaheksas
  // genereeritud toon ei oleks enam usaldusväärselt eristatav.
  const top = rows.slice(0, 6);
  const restCost = rows.slice(6).reduce((a,p) => a + p.cost, 0);
  const segs = top.map((p,i) => ({name: p.project, cost: p.cost, c: `var(--s${i+1})`}));
  if (restCost > 0) segs.push({name: `muud (${rows.length-6})`, cost: restCost, c: 'var(--s7)'});

  document.getElementById('projStack').innerHTML = segs.map(s =>
    `<i style="width:${(s.cost/total*100).toFixed(2)}%;background:${s.c}"
        data-seg="${esc(s.name)}|${s.cost}"></i>`).join('');
  document.getElementById('projLegend').innerHTML = segs.map(s => `
    <span><i class="sw" style="background:${s.c}"></i>${esc(s.name)}
      <span class="amt">${eur(s.cost)} · ${(s.cost/total*100).toFixed(0)}%</span></span>`).join('');

  document.querySelectorAll('#projStack [data-seg]').forEach(el => {
    const [name, cost] = el.dataset.seg.split('|');
    el.onmousemove = e => showTip(e, `<strong>${name}</strong><br>${eur(+cost)} · ${(+cost/total*100).toFixed(1)}%`);
    el.onmouseleave = hideTip;
  });
}

function renderSessions(d){
  const all = d.sessions, rows = showAll ? all : all.slice(0, 20), jb = jobsById(d);
  // Skaleeri NÄHTAVA hulga suurima järgi — üks hiiglaslik vana sessioon surus
  // muidu kõik ülejäänud ribad punktideks kokku.
  const max = Math.max(...rows.map(s => s.cost), 0.01);
  document.getElementById('sessBody').innerHTML = rows.map(s => `
    <tr>
      <td><span class="proj">${s.live ? '<span class="livedot"></span>' : ''}${s.project}</span>
          <div class="sid">${s.title ? esc(s.title) : s.short}${jobBadges(jb[s.short])}</div></td>
      <td>${s.last}<div class="sid">${s.models.map(m => m.replace('claude-','')).join(', ')}</div></td>
      <td class="num ctx-${s.ctxLevel}" data-ctx="${s.ctx}">${s.ctx ? compact(s.ctx) : '—'}</td>
      <td class="num" data-tok="${s.tokens}">${compact(s.tokens)}</td>
      <td class="num">${usd(s.cost)}</td>
      <td class="barcell"><div class="bar" style="width:${Math.max(2, s.cost/max*100)}%"></div></td>
    </tr>`).join('');
  document.getElementById('moreBtn').textContent =
    showAll ? 'Näita vähem' : `Näita kõiki (${all.length})`;
  document.getElementById('moreBtn').style.display = all.length > 20 ? 'block' : 'none';
  document.getElementById('sessCount').textContent = `· ${all.length}`;
  document.querySelectorAll('#sessBody td[data-tok]').forEach(td => {
    td.onmousemove = e => showTip(e, `${num(+td.dataset.tok)} tokenit`);
    td.onmouseleave = hideTip;
  });
  bindCtxTips('#sessBody td[data-ctx]');
}

function renderChart(d){
  const rows = d.chart, max = Math.max(...rows.map(r => r.cost), 1);
  document.getElementById('chart').innerHTML = rows.map((r,i) => `
    <div class="col ${i === rows.length-1 ? 'dim' : ''}" data-i="${i}">
      ${r.cost > 0
        ? `<div class="fill" style="height:${Math.max(2, r.cost/max*100)}%"></div>`
        : `<div class="zero" title="kasutuseta"></div>`}
    </div>`).join('');
  document.getElementById('xaxis').innerHTML = rows.map(r =>
    `<span>${r.date.slice(8)}</span>`).join('');
  document.querySelectorAll('.col').forEach(col => {
    const r = rows[+col.dataset.i];
    col.onmousemove = e => showTip(e,
      `<strong>${r.date}</strong><br>${usd(r.cost)} · ${compact(r.tokens)} tokenit`);
    col.onmouseleave = hideTip;
  });
}

async function load(){
  try{
    const d = await (await fetch('/api/data')).json();
    lastData = d;
    const st = document.getElementById('status');
    if (!d.ok){
      st.innerHTML = '<span class="dot err"></span>ccusage viga';
      document.getElementById('errBox').innerHTML =
        `<div class="card err" style="margin-bottom:14px"><strong>ccusage ei anna andmeid:</strong> ${d.error}</div>`;
      return;
    }
    document.getElementById('errBox').innerHTML = d.offlinePricing
      ? `<div class="card" style="margin-bottom:14px;border-color:var(--warning)">
           Hinnakiri tuli <strong>offline-vahemälust</strong> — võrgutõmme ebaõnnestus. Numbrid võivad olla veidi vanad.</div>`
      : '';
    st.innerHTML = `<span class="dot"></span>uuendatud ${d.fetchedLabel}`;
    fxRate = (d.fx && d.fx.rate) || fxRate;
    document.getElementById('fxNote').textContent =
      `1 € = ${fxRate} $ (${d.fx ? d.fx.source : '?'}${d.fx ? ', ' + d.fx.date : ''})`;
    renderJobs(d); renderLimits(d); renderTiles(d); renderWorking(d);
    renderChartStats(d); renderChart(d); renderProjects(d); renderSessions(d);
  }catch(e){
    document.getElementById('status').innerHTML = '<span class="dot err"></span>server ei vasta';
  }
}

document.getElementById('refreshBtn').onclick = load;
document.getElementById('moreBtn').onclick = () => { showAll = !showAll; renderSessions(lastData); };
document.getElementById('themeBtn').onclick = () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const dark = cur ? cur === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.setAttribute('data-theme', dark ? 'light' : 'dark');
};
load(); setInterval(load, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        global _last_request
        if self.path.startswith("/api/data"):
            _last_request = time.time()
            _wake.set()  # ärata värskendaja, kui ta magas
            with _lock:
                body = json.dumps(_cache, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def log_message(self, *a) -> None:  # vaikne
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code kasutuse dashboard")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true", help="ära ava brauserit")
    args = ap.parse_args()

    threading.Thread(target=refresher, daemon=True).start()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"Port {args.port} ei ole vaba ({e}). Proovi --port 8788.", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print(f"ccdash → {url}   (Ctrl+C lõpetab)")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nsuletud")
    return 0


if __name__ == "__main__":
    sys.exit(main())
