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
import re
import subprocess
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccusage_lib as c  # noqa: E402
import jobs_lib  # noqa: E402
import tmux_lib  # noqa: E402

# Ainus muster, mille /open endpoint tohib vaikebrauserile edasi anda.
_CLAUDE_SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]{1,64}")

REFRESH_SEC = 20
ACTIVE_WINDOW_SEC = 10 * 60      # roheline täpp: tegevus <10 min tagasi
WORK_WINDOW_SEC = 4 * 3600       # "töös": käimasolev tööseanss, mille juurde naased
PEER_SYNC_SEC = 300          # teise masina `~/.claude/projects` koopia värskendus
IDLE_AFTER_SEC = 90          # kui keegi pole nii kaua pollinud, lõpeta värskendamine
LIMITS_SEC = 60              # Anthropicu limiidi-otspunkti küsimise vahe
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
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

    ⚠️ Siit tuleb ainult MAHT (tokenid, väärtus) ja ccusage'i hinnanguline aken.
    Protsent limiidist EI OLE nendest arvutatav — ccusage ei tea limiiti ega näe
    claude.ai veebi/pilvesessioone. Päris % tuleb `anthropic_limits()`-ist.
    (Kuni 16.09.2026 seisis siin väide, et Anthropic ei avalda limiiti masinloetavalt —
    see oli vale: `/usage` loeb `api/oauth/usage` otspunkti. Heikki oli 16.09 15:20
    limiidis, kui see tabel näitas „52 %" möödunud aega — sellest see parandus.)

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


_limits_cache: dict = {"at": 0.0, "data": None}


def _oauth_token() -> str | None:
    """Claude Code'i OAuth-token — sama, mida `/usage` kasutab.

    macOS-il Keychainis (`Claude Code-credentials`), mujal `~/.claude/.credentials.json`.
    NB: Keychain vastab ainult sisse logitud GUI-sessioonis; SSH alt (nt Mini) ei anna
    midagi — seepärast küsib limiiti see masin, kus server jookseb, mitte peer.
    """
    raw = ""
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        raw = ""
    if not raw.strip():
        try:
            raw = (Path.home() / ".claude" / ".credentials.json").read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        return json.loads(raw).get("claudeAiOauth", {}).get("accessToken") or None
    except (ValueError, AttributeError):
        return None


def _limit_window(x: dict | None) -> dict | None:
    """{utilization, resets_at} → {pct, resetsAt (Eesti aeg, minutini ümardatud), resetsInMin}."""
    if not isinstance(x, dict):
        return None
    pct = x.get("utilization", x.get("percent"))
    rs = x.get("resets_at")
    out: dict = {"pct": float(pct) if pct is not None else None,
                 "resetsAt": None, "resetsInMin": None}
    if rs:
        try:
            # Anthropic annab 18:59:59.5 — ümarda minutini, muidu näitab „E 18:59".
            t = datetime.fromisoformat(str(rs).replace("Z", "+00:00")) + timedelta(seconds=30)
            t = t.replace(second=0, microsecond=0).astimezone(c.TZ)
            out["resetsAt"] = t.isoformat()
            out["resetsInMin"] = max(0, int((t - c.now()).total_seconds() // 60))
        except ValueError:
            pass
    return out


def _fetch_anthropic_limits() -> dict:
    tok = _oauth_token()
    if not tok:
        return {"ok": False, "error": "OAuth-token puudub (Keychain / .credentials.json)"}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "ccdash",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = " — token aegunud, tee `claude` → /login" if e.code in (401, 403) else ""
        return {"ok": False, "error": f"HTTP {e.code}{hint}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)[:120]}

    # Mudelipõhised nädalaread tulevad `limits` loendist (kind=weekly_scoped),
    # nimi on scope.model.display_name (nt "Fable"). Uus mudel ilmub ise.
    models = []
    for l in d.get("limits") or []:
        if not isinstance(l, dict) or l.get("kind") != "weekly_scoped":
            continue
        name = (((l.get("scope") or {}).get("model") or {}).get("display_name")) or "mudel"
        w = _limit_window(l)
        if w:
            w["name"] = str(name)
            models.append(w)
    return {
        "ok": True,
        "fetchedLabel": c.now().strftime("%H:%M:%S"),
        "session": _limit_window(d.get("five_hour")),
        "weekAll": _limit_window(d.get("seven_day")),
        "models": models,
    }


def anthropic_limits() -> dict:
    """Päris % limiidist Anthropicult — sama otspunkt, mida Claude Code'i `/usage` ja
    claude.ai → Settings → Usage näitavad. Katab KÕIK konto kasutuse (veeb, pilv,
    teised masinad), mida kohalik ccusage ei näe.

    Vahemälu LIMITS_SEC; viga ei tõsta kunagi erindit (tagastab ok=False), sest
    collect() erind peidaks brauseris kogu dashboardi veabänneri taha.
    """
    now = time.time()
    if _limits_cache["data"] is not None and now - _limits_cache["at"] < LIMITS_SEC:
        return _limits_cache["data"]
    out = _fetch_anthropic_limits()
    _limits_cache.update(at=now, data=out)
    return out


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
    limits["real"] = anthropic_limits()

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
        "peers": c.peers_status(),
        # {allikas: tokenid}, mis on tokeninumbrites sees, aga dollarites MITTE
        "externalUnpriced": c.external_unpriced(daily),
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


def peer_syncer() -> None:
    """Tõmba teiste masinate transkriptikoopiad iga PEER_SYNC_SEC järel.

    Eraldi lõim, mitte `collect()`-i osa: ssh üle Tailscale'i võib venida või aeguda ja
    see ei tohi dashboardi värskendust kinni hoida. `sync_peers()` ei tõsta erindit.
    Jookseb ka siis, kui keegi ei vaata — 09:00 päevalogija vajab värsket koopiat.
    """
    while True:
        c.sync_peers()
        time.sleep(PEER_SYNC_SEC)


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
.badge.web{text-decoration:none;cursor:pointer}
.badge.web:hover{background:var(--good);color:#1a1a1a}
#jobAlert .jid a{color:inherit}
.badge.wait{background:var(--warning);color:#1a1a1a;font-weight:600}

/* --- Sessioonivalija (päise dropdown) --- */
.menuwrap{position:relative}
.menu{
  position:absolute;top:calc(100% + 6px);right:0;z-index:40;width:330px;
  max-height:min(70vh,560px);overflow-y:auto;
  background:var(--surface-1);border:1px solid var(--border);border-radius:11px;
  padding:8px;box-shadow:0 10px 30px rgba(0,0,0,.28);
}
.menu h3{
  font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  margin:10px 0 4px;padding:0 8px;font-weight:600;
}
.menu h3:first-child{margin-top:2px}
.menuopt{
  display:flex;align-items:center;gap:7px;padding:5px 8px;font-size:12.5px;
  color:var(--text-secondary);cursor:pointer;
}
.menufilter{
  font:inherit;font-size:12.5px;width:100%;margin:4px 0 2px;padding:5px 8px;
  border-radius:7px;border:1px solid var(--border);
  background:var(--plane);color:var(--text-primary);
}
.mi{
  display:flex;align-items:baseline;gap:8px;width:100%;text-align:left;
  padding:5px 8px;border:0;border-radius:7px;background:none;color:var(--text-primary);
  font:inherit;font-size:13px;cursor:pointer;
}
.mi:hover{background:rgba(127,127,127,.14)}
.mi .path{
  margin-left:auto;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:175px;
}
.mi .livedot{flex:none}
.mi .tag{
  font-size:10px;padding:0 4px;border-radius:4px;background:rgba(127,127,127,.18);
  color:var(--text-secondary);margin-right:4px;
}
.mi .tag.wait{background:var(--warning);color:#1a1a1a;font-weight:600}
/* Klikitud rida: silm on real, mitte päises — tagasiside peab olema SEAL. */
.mi.done{background:rgba(52,199,89,.22);transition:background .25s}
.mi.done .path{color:var(--good);font-weight:600}
.mi.busy .path{color:var(--text-secondary)}
.menuhead{
  position:sticky;top:-8px;z-index:2;margin:-8px -8px 0;padding:8px 8px 4px;
  background:var(--surface-1);border-bottom:1px solid var(--border);
}
.menu hr.thin{margin:6px 0 2px}
.menu hr{border:0;border-top:1px solid var(--border);margin:8px 0 0}
.menumsg{padding:7px 8px;font-size:12px;color:var(--text-secondary);line-height:1.45}
.menumsg code{
  display:block;margin-top:5px;background:rgba(127,127,127,.14);padding:4px 6px;
  border-radius:4px;font-size:11.5px;user-select:all;word-break:break-all;
}
.menumsg.err{color:var(--critical)}
.menuempty{padding:7px 8px;font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Claude Code kasutus</h1>
    <span class="sub" id="status"><span class="dot stale"></span>laen…</span>
    <span style="flex:1"></span>
    <div class="menuwrap" id="sessWrap" hidden>
      <button id="sessBtn" aria-haspopup="menu" aria-expanded="false"
              title="Ava või alusta Claude-sessioon">Sessioonid ▾</button>
      <div class="menu" id="sessMenu" hidden role="menu">
        <!-- Kleepuv päis: tagasiside peab olema nähtav ka siis, kui loend on
             keritud projekti nr 50 juurde. Enne oli teade loendi PÕHJAS. -->
        <div class="menuhead">
          <label class="menuopt" id="sessLocalWrap" hidden>
            <input type="checkbox" id="sessLocal"><span id="sessLocalLabel"></span>
          </label>
          <input class="menufilter" id="sessFilter" type="search" placeholder="otsi…"
                 autocomplete="off" aria-label="Otsi projekti">
          <div class="menumsg" id="sessMsg" hidden></div>
        </div>
        <div id="sessMenuBody"></div>
      </div>
    </div>
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
    <p class="hint"><strong>% limiidist</strong> ja lähtestusaeg tulevad Anthropicu otspunktist
      <code>api/oauth/usage</code> — sama allikas, mida <code>/usage</code> ja claude.ai → Settings → Usage
      näitavad, ja see katab kogu konto (veeb, pilv, teised masinad). Tokenid ja väärtus on
      ccusage'i kohalik loendus (see masin + peer-koopiad) — need ei ole limiidiga võrreldavad.
      Kui otspunkt ei vasta, on viimane veerg <strong>möödunud aeg</strong> aknast ja märgitud „aega".
      <span id="limitNote"></span></p>
    <table>
      <thead><tr>
        <th>Aken</th><th>Lähtestub</th>
        <th class="num">Tokenid</th><th class="num">Väärtus</th><th>Limiidist kasutatud</th>
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
    const rs = d.limits.real && d.limits.real.ok ? d.limits.real.session : null;
    const proj = a.projection ? a.projection.totalCost : 0;
    // Staatusvärv projektsiooni järgi: aken, mis lõpeks üle $300, on erakordne.
    const cls = proj > 300 ? 'critical' : proj > 150 ? 'warning' : '';
    t.push(tile('Aktiivne 5 h aken', usd(a.costUSD),
      `${start.toLocaleTimeString('et-EE',{hour:'2-digit',minute:'2-digit'})}–` +
      `${end.toLocaleTimeString('et-EE',{hour:'2-digit',minute:'2-digit'})} · ` +
      `jäänud ${hh}h ${mm}min · ${num(a.entries)} API-päringut` +
      (rs && rs.pct != null ? ` · <strong>limiidist ${rs.pct.toFixed(0)}%</strong>` : ''),
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
  const R = L.real && L.real.ok ? L.real : null;
  const clock = iso => new Date(iso).toLocaleString('et-EE',
    {weekday:'short', hour:'2-digit', minute:'2-digit'});
  // Riba: R olemas → Anthropicu päris % limiidist (värv 70/90 juures).
  // R puudub → kui suur osa AJAAKNAST on läbi — halb asendus, märgitud „aega".
  const timePct = s ? Math.max(0, Math.min(100, (1 - s.resetsInMin/300) * 100)) : 0;
  const weekTimePct = Math.max(0, Math.min(100, (1 - w.resetsInMin/(7*24*60)) * 100));
  const bar = (pct, real) => {
    if (pct == null) return '<span class="model">—</span>';
    const cls = !real ? '' : pct >= 90 ? 'critical' : pct >= 70 ? 'warning' : '';
    return `<div style="display:flex;align-items:center;gap:8px">
      <div class="meter" style="flex:1;margin:0"><i class="${cls}" style="width:${Math.min(100, pct).toFixed(1)}%"></i></div>
      <span class="model">${pct.toFixed(0)}%${real ? '' : ' aega'}</span></div>`;
  };
  const row = (name, sub, resets, inMin, tok, cost, pct, real, dimmed) => `
    <tr${dimmed ? ' style="opacity:.55"' : ''}>
      <td><span class="proj">${name}</span><div class="sid">${sub}</div></td>
      <td>${resets ? clock(resets) : '—'}<div class="sid">${inMin != null ? dur(inMin) + ' pärast' : ''}</div></td>
      <td class="num">${tok == null ? '—' : compact(tok)}</td>
      <td class="num">${cost == null ? '—' : eur(cost)}</td>
      <td class="barcell">${bar(pct, real)}</td>
    </tr>`;
  const rs = R && R.session, rw = R && R.weekAll;
  const sessRow = (s || rs)
    ? row('Praegune sessioon', '5 h aken' + (s ? ' · ' + num(s.entries) + ' API-päringut' : ''),
          rs ? rs.resetsAt : s.resetsAt, rs ? rs.resetsInMin : s.resetsInMin,
          s ? s.tokens : null, s ? s.cost : null, rs ? rs.pct : timePct, !!rs, false)
    : `<tr><td><span class="proj">Praegune sessioon</span><div class="sid">5 h aken</div></td>
         <td colspan="4" class="model">aktiivset akent ei ole</td></tr>`;
  const models = R ? R.models : [];
  const fable = models.find(m => /fable/i.test(m.name));
  const others = models.filter(m => m !== fable);
  document.getElementById('limitBody').innerHTML = sessRow +
    row('Kõik mudelid', 'nädalaaken', rw ? rw.resetsAt : w.resetsAt, rw ? rw.resetsInMin : w.resetsInMin,
        w.allTokens, w.allCost, rw ? rw.pct : weekTimePct, !!rw, false) +
    row('Fable', 'nädalaaken', fable ? fable.resetsAt : w.resetsAt, fable ? fable.resetsInMin : w.resetsInMin,
        w.fableTokens, w.fableCost, fable ? fable.pct : weekTimePct, !!fable, w.fableTokens === 0 && !fable) +
    others.map(m => row(m.name, 'nädalaaken', m.resetsAt, m.resetsInMin, null, null, m.pct, true, false)).join('');
  const note = document.getElementById('limitNote');
  if (note) note.textContent = R ? `Anthropic ${R.fetchedLabel}.`
    : `⚠︎ Anthropicu otspunkt ei vasta${L.real && L.real.error ? ': ' + L.real.error : ''}.`;
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

// Ava link VAIKEBRAUSERIS, mitte selles aknas: dashboard jookseb omaette
// Chrome-profiilis, kus ühtegi sisselogimist ei ole.
function openExternal(url){
  fetch('/open?url=' + encodeURIComponent(url)).catch(() => window.open(url, '_blank'));
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
  // Veebivaade claude.ai-s — ainus viis taustatööd ILMA terminalita lugeda.
  if (j.webUrl) b += `<a class="badge web" href="#" onclick="openExternal('${j.webUrl}');return false" title="Ava claude.ai-s (vaikebrauseris)">veeb ↗</a>`;
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
      <div class="jid">${j.webUrl ? `<a href="#" onclick="openExternal('${esc(j.webUrl)}');return false" title="Ava vaikebrauseris">${esc(j.id)}</a>` : esc(j.id)}</div>
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
    // Hoiatused LIIDETAKSE, mitte kas-või: offline-hinnakiri ja peer'i vana koopia
    // võivad kehtida korraga ja kumbki ei tohi teist varjata.
    const warns = [];
    if (d.offlinePricing) warns.push(
      `<div class="card" style="margin-bottom:14px;border-color:var(--warning)">
         Hinnakiri tuli <strong>offline-vahemälust</strong> — võrgutõmme ebaõnnestus. Numbrid võivad olla veidi vanad.</div>`);
    for (const [src, tok] of Object.entries(d.externalUnpriced || {})){
      warns.push(
        `<div class="card" style="margin-bottom:14px;border-color:var(--warning)">
           <strong>${src}</strong>: ${(tok/1e6).toFixed(2)} M tokenit on tokeninumbrites sees, aga dollarites <strong>mitte</strong> —
           see tööriist ei arvuta kulu. Tabelis projekt „${src}".</div>`);
    }
    for (const p of (d.peers || [])){
      if (!p.stale) continue;
      const when = p.at ? `viimane koopia ${p.at.slice(11,16)} (${p.ageMin} min tagasi)` : 'koopiat ei ole veel';
      warns.push(
        `<div class="card" style="margin-bottom:14px;border-color:var(--warning)">
           <strong>${p.host}</strong> tokenid võivad olla puudu — ${when}${p.error ? ' · ' + p.error : ''}</div>`);
    }
    document.getElementById('errBox').innerHTML = warns.join('');
    const peerNote = (d.peers || []).filter(p => !p.stale)
      .map(p => ` · ${p.host} koopia ${p.at.slice(11,16)}`).join('');
    st.innerHTML = `<span class="dot"></span>uuendatud ${d.fetchedLabel}${peerNote}`;
    fxRate = (d.fx && d.fx.rate) || fxRate;
    document.getElementById('fxNote').textContent =
      `1 € = ${fxRate} $ (${d.fx ? d.fx.source : '?'}${d.fx ? ', ' + d.fx.date : ''})`;
    renderJobs(d); renderLimits(d); renderTiles(d); renderWorking(d);
    renderChartStats(d); renderChart(d); renderProjects(d); renderSessions(d);
    if (!sessMenu.hidden) renderSessMenu();   // avatud valija seis ei tohi vananeda
  }catch(e){
    document.getElementById('status').innerHTML = '<span class="dot err"></span>server ei vasta';
  }
}

// ---------------------------------------------------------- sessioonivalija
//
// Eraldi /api/tmux-ist, MITTE /api/data-st: `load()` teeb `!d.ok` peal `return`,
// seega ccusage'i viga peidaks kogu payload'i ja koos sellega selle valija,
// mis ccusage'ist üldse ei sõltu.
let tmuxData = null;
const sessWrap = document.getElementById('sessWrap');
const sessMenu = document.getElementById('sessMenu');
const sessFilter = document.getElementById('sessFilter');
const sessMsg = document.getElementById('sessMsg');
const sessLocal = document.getElementById('sessLocal');

// Kumb käsurida: kohalik või läbi ssh. Valik jääb meelde, sest see sõltub sellest,
// KUS su terminal on (VS Code Remote-SSH vs kohalik aken) — ja see ei muutu tihti.
try { sessLocal.checked = localStorage.getItem('ccdashSessLocal') === '1'; } catch(e){}
sessLocal.onchange = () => {
  try { localStorage.setItem('ccdashSessLocal', sessLocal.checked ? '1' : '0'); } catch(e){}
};

async function fetchTmux(){
  try {
    // Üle ssh (tmuxHost) võib loend venida; 8 s pärast pigem tühi kui igavene ootus.
    const d = await (await fetch('/api/tmux', {signal: AbortSignal.timeout(8000)})).json();
    tmuxData = d;
    sessWrap.hidden = !d.enabled;
    const wrap = document.getElementById('sessLocalWrap');
    // Ilma seadistatud remoteHost'ita on ainult üks variant — lüliti oleks müra.
    wrap.hidden = !d.remoteHost;
    if (d.remoteHost)
      document.getElementById('sessLocalLabel').textContent =
        `olen juba masinas «${d.remoteHost}»`;
    return d;
  } catch(e){ return null; }
}

function shortPath(p){ return String(p).split('/').slice(-2).join('/'); }

// Suhteline aeg kitsasse ritta: „5 min", „2 h", „3 p". Absoluutne „dd.mm HH:MM" ei mahu.
function ago(ts){
  if (!ts) return '';
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 60) return 'nüüd';
  if (s < 3600) return `${Math.round(s/60)} min`;
  if (s < 86400) return `${Math.round(s/3600)} h`;
  return `${Math.round(s/86400)} p`;
}

const RECENT_SEC = 7 * 86400;   // „aktiivne" projekt tõuseb loendi algusesse

// Projekti seis /api/data payload'ist (lastData). Valija EI TOHI kuludest sõltuda:
// kui lastData puudub või on vigane, tuleb tühi Map ja read renderduvad ilma seisuta.
function projectStats(){
  const m = new Map();
  const d = lastData;
  if (!d || !d.ok) return m;
  const get = k => { if (!m.has(k)) m.set(k, {last:0, cost:0, live:false, bg:0, waiting:false}); return m.get(k); };
  for (const s of (d.sessions || [])){
    const p = get(s.project);
    p.last = Math.max(p.last, s.lastSort || 0);
    p.live = p.live || !!s.live;
  }
  for (const p of (d.projects || [])) get(p.project).cost = p.cost || 0;
  for (const j of ((d.jobs && d.jobs.items) || [])){
    if (!j.project) continue;                 // vabas vormis prompt — projekti ei tea
    const p = get(j.project);
    p.bg += 1; p.waiting = p.waiting || !!j.waiting;
  }
  return m;
}

// Rea parem pool: [bg] [⏳] 2 h · 614 €. Ilma andmeteta „—".
function statHtml(st){
  if (!st) return '<span class="path">—</span>';
  const tags = (st.bg ? '<span class="tag">bg</span>' : '')
             + (st.waiting ? '<span class="tag wait">⏳</span>' : '');
  const bits = [];
  if (st.last) bits.push(ago(st.last));
  if (st.cost) bits.push(eur(st.cost));
  return `<span class="path">${tags}${esc(bits.join(' · ') || '—')}</span>`;
}

function renderSessMenu(){
  const d = tmuxData;
  if (!d) return;
  const q = sessFilter.value.trim().toLowerCase();
  const hit = s => !q || String(s).toLowerCase().includes(q);
  const stats = projectStats();
  const now = Date.now()/1000;
  // tmux-nimi -> kuvatav projektinimi (sama nimekiri, mida server näitab)
  const byName = new Map((d.projects || []).map(p => [p.name, p.display]));
  const sessions = (d.sessions || []).filter(s => hit(s.name));
  // Jooksva sessiooni projekt on juba ülemises plokis — teine rida all oleks
  // sama asi kaks korda ja klikk sellel ei teeks midagi uut.
  const busy = new Set((d.sessions || []).map(s => s.name));
  const projects = (d.projects || []).filter(p => hit(p.display) && !busy.has(p.name));
  const st = p => stats.get(p.display);
  // Viimase 7 päeva jooksul liikunud projektid ette, värskuse järgi; ülejäänud tähestikus.
  const recent = projects.filter(p => st(p) && now - st(p).last < RECENT_SEC)
                         .sort((a, b) => st(b).last - st(a).last);
  const rest = projects.filter(p => !recent.includes(p));

  const row = p => `
      <button class="mi" data-act="start" data-project="${esc(p.display)}"
              title="${p.name !== p.display ? 'tmux: ' + esc(p.name) : esc(p.display)}">
        ${st(p) && st(p).live ? '<span class="livedot"></span>' : ''}${esc(p.display)}
        ${statHtml(st(p))}
      </button>`;

  const out = [];
  if (sessions.length){
    // Kust need sessioonid pärit on: `tmuxHost` = tmux jookseb teises masinas.
    out.push(`<h3>Jooksevad${d.tmuxHost ? ' · ' + esc(d.tmuxHost) : ''}</h3>`);
    out.push(sessions.map(s => {
      const ps = stats.get(byName.get(s.name));
      return `
      <button class="mi" data-act="attach" data-name="${esc(s.name)}" title="${esc(shortPath(s.path))}">
        ${(s.attached || (ps && ps.live)) ? '<span class="livedot"></span>' : ''}${esc(s.name)}
        ${statHtml(ps)}
      </button>`; }).join(''));
  }
  if (recent.length){
    out.push('<h3>Projektid</h3>');
    out.push(recent.map(row).join(''));
  }
  if (rest.length){
    out.push(recent.length ? '<hr class="thin">' : '<h3>Projektid</h3>');
    out.push(rest.map(row).join(''));
  }
  if (!sessions.length && !projects.length)
    out.push('<div class="menuempty">Midagi ei vasta otsingule.</div>');
  if (!q){
    out.push('<hr>');
    out.push('<button class="mi" data-act="chat">+ uus chat <span class="path">vaba teema</span></button>');
  }
  document.getElementById('sessMenuBody').innerHTML = out.join('');
}

// Rea-sisene tagasiside: silm on real, mitte päises. Rida leitakse nime järgi,
// sest käivituse järel renderdatakse loend üle ja projekt kolib „Jooksevad" alla
// TEISE atribuudiga (data-project="HA" -> data-name="ha").
function markRow(sel, text, cls){
  const el = document.querySelector(`#sessMenuBody ${sel}`);
  if (!el) return;
  el.classList.add(cls);
  const path = el.querySelector('.path');
  if (path){ path.dataset.orig = path.innerHTML; path.textContent = text; }
  if (cls === 'done') setTimeout(() => {
    el.classList.remove('done');
    if (path && path.dataset.orig !== undefined) path.innerHTML = path.dataset.orig;
  }, 4000);
}

function showSessMsg(html, isErr){
  sessMsg.className = 'menumsg' + (isErr ? ' err' : '');
  sessMsg.innerHTML = html;
  sessMsg.hidden = false;
}

// Server avab redaktoris terminali (seadistus terminalUri). Brauser ise ei saa:
// pärast `await fetch` on kasutaja žest kadunud ja Chrome blokeeriks lingi.
async function openTerminal(name){
  try {
    const r = await fetch('/api/tmux/open', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: name}), signal: AbortSignal.timeout(8000),
    });
    return (await r.json()).opened;
  } catch(e){ return false; }
}

// Lõikelaud jääb ALATI varuks: `opened` tähendab ainult, et link läks teele —
// kas terminal tekkis (laiendus olemas?), seda server ei näe.
function openedNote(opened){
  return opened ? '→ saadetud terminali. Kui aken ei avanenud, kleebi käsk ise. ' : '';
}

async function copyLine(name, opened){
  // Kaks varianti, sest terminal võib olla siinsamas masinas (VS Code
  // Remote-SSH) või mujal, kust tuleb esmalt ssh-da.
  const a = (tmuxData && tmuxData.remoteHost)
    ? `ssh -t ${tmuxData.remoteHost} "tmux attach -t ${name}"`
    : `tmux attach -t ${name}`;
  const cmd = (sessLocal.checked || !(tmuxData && tmuxData.remoteHost))
    ? `tmux attach -t ${name}` : a;
  let ok = false;
  try { await navigator.clipboard.writeText(cmd); ok = true; } catch(e){}
  // Käsk näidatakse ALATI, ka õnnestumisel: lõikelaud võib olla vahepeal üle
  // kirjutatud ja siis on tekst siin endiselt olemas.
  showSessMsg(openedNote(opened)
              + (ok ? (opened ? 'Kopeeritud ✓' : 'Kopeeritud ✓ — kleebi terminali.')
                    : 'Lõikelaud ei olnud lubatud — vali ja kopeeri käsitsi:')
              + `<code>${esc(cmd)}</code>`, false);
}

async function sessAction(el){
  const act = el.dataset.act;
  if (act === 'attach'){
    const opened = (tmuxData && tmuxData.terminal) ? await openTerminal(el.dataset.name) : null;
    await copyLine(el.dataset.name, opened);
    markRow(`[data-name="${CSS.escape(el.dataset.name)}"]`, opened ? '✓ avatud' : '✓ kopeeritud', 'done');
    return;
  }
  showSessMsg('Käivitan…', false);
  if (act === 'start')
    markRow(`[data-project="${CSS.escape(el.dataset.project)}"]`, '…käivitan', 'busy');
  try {
    const body = act === 'chat' ? {kind:'chat'}
                                : {kind:'project', project: el.dataset.project};
    // Halvimal juhul teeb server 4 ssh-kutset × 10 s — brauser ei tohi selle taga
    // lõputult „Käivitan…" näidata.
    const r = await fetch('/api/tmux/new', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    });
    const d = await r.json();
    if (!d.ok){ showSessMsg(esc(d.error || 'ei õnnestunud'), true); return; }
    await fetchTmux(); renderSessMenu();
    await copyLine(d.session, d.opened);
    // Uus rida leitakse SERVERI antud sessiooninime järgi (vt markRow).
    markRow(`[data-name="${CSS.escape(d.session)}"]`,
            (d.created ? '✓ käivitatud, ' : '✓ ') + (d.opened ? 'avatud' : 'kopeeritud'), 'done');
    if (!d.created)
      showSessMsg(sessMsg.innerHTML + '<br>Sessioon <strong>oli juba olemas</strong> — '
                  + 'midagi uut ei käivitatud.', false);
  } catch(e){
    const host = tmuxData && tmuxData.tmuxHost;
    showSessMsg(e && e.name === 'TimeoutError' && host
      ? `${esc(host)} ei vasta piisavalt kiiresti — proovi uuesti` : 'server ei vasta', true);
  }
}

document.getElementById('sessMenuBody').onclick = e => {
  const el = e.target.closest('.mi');
  if (el) sessAction(el);
};
sessFilter.oninput = renderSessMenu;

function toggleSessMenu(open){
  sessMenu.hidden = !open;
  document.getElementById('sessBtn').setAttribute('aria-expanded', String(open));
  if (open){ sessFilter.value = ''; sessMsg.hidden = true; sessFilter.focus(); }
}
document.getElementById('sessBtn').onclick = async e => {
  e.stopPropagation();
  if (!sessMenu.hidden){ toggleSessMenu(false); return; }
  toggleSessMenu(true);
  await fetchTmux();          // sessioonid muutuvad — värske loend igal avamisel
  renderSessMenu();
};
sessMenu.onclick = e => e.stopPropagation();
document.addEventListener('click', () => toggleSessMenu(false));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !sessMenu.hidden) toggleSessMenu(false);
});
fetchTmux();                  // otsustab, kas nuppu üldse näidata

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


def _tmux_payload() -> dict:
    """Sessioonivalija andmed. Küsitakse ainult siis, kui valija avatakse.

    DEMO-režiimis lülitatakse valija välja, mitte ei anonümiseerita: tmux- ja
    kaustanimed ON päris nimed ja neid ei saa nagu sessioone ümber sildistada.
    """
    if DEMO:
        return {"enabled": False, "sessions": [], "projects": [],
                "remoteHost": None, "error": None}
    return {"enabled": True, **tmux_lib.collect()}


class Handler(BaseHTTPRequestHandler):
    # ---- Ohutus: /api/tmux/new ja /api/tmux/open käivitavad protsessi ----
    #
    # Server kuulab 127.0.0.1-l, aga see EI kaitse brauseri eest: iga lahtine
    # veebileht võib teha päringu localhost'i. Seepärast kolm lukku.

    def _local_origin_ok(self) -> str:
        """"" kui päring tuleb sellelt lehelt endalt, muidu tõrke põhjus."""
        allowed = {f"http://127.0.0.1:{self.server.server_port}",
                   f"http://localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        # ⚠️ `file://` lehelt tuleb Origin: null — see on SÕNE "null", mitte
        # puuduv päis. Valge nimekiri lükkab ta tagasi; ära asenda `in`-kontrolli
        # tõeväärtuskontrolliga.
        if origin is not None and origin not in allowed:
            return "võõras Origin"
        # DNS rebinding: võõras nimi, mis laheneb 127.0.0.1-le. Origin oleks siis
        # küll võõras, aga Host on teine, sõltumatu lukk.
        host = (self.headers.get("Host") or "").strip()
        if host and host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return "võõras Host"
        return ""

    def _read_json_body(self) -> tuple[dict | None, int, str]:
        """(payload, veakood, sõnum). Nõuab JSON Content-Type'i.

        `application/json` sunnib cross-origin päringu preflighti; kuna
        `do_OPTIONS` puudub, blokeerib brauser sellise päringu juba enne meid.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            return None, 415, "nõuab Content-Type: application/json"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, 400, "vigane Content-Length"
        if n <= 0 or n > 4096:
            return None, 400, "vigane keha suurus"
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None, 400, "vigane JSON"
        if not isinstance(payload, dict):
            return None, 400, "keha peab olema objekt"
        return payload, 0, ""

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
        elif self.path == "/api/tmux":
            # Tahtlikult ERALDI /api/data-st. `load()` teeb `!d.ok` peal
            # `return`, seega ccusage'i viga peidaks kogu payload'i — ja koos
            # sellega sessioonivalija, mis ccusage'ist üldse ei sõltu.
            body = json.dumps(_tmux_payload(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/api/tmux/"):
            self._send(405, "text/plain; charset=utf-8", b"ainult POST")
        elif self.path.startswith("/open?"):
            # Dashboard elab omaette Chrome-profiilis (`--app` +
            # `--user-data-dir=~/.claude/.ccdash-chrome`), kus ei ole ühtegi
            # sisselogimist. `target=_blank` jääks samasse tühja profiili ja
            # kasutaja satuks login-ekraanile. macOS `open` annab lingi
            # VAIKEBRAUSERILE, kus sessioon juba on.
            url = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("url", [""])[0]
            if _CLAUDE_SESSION_URL.fullmatch(url):
                subprocess.Popen(["open", url])
                self._send(204, "text/plain; charset=utf-8", b"")
            else:
                # Server on lokaalne, aga see endpoint käivitab välise
                # avamise — lubatud on AINULT claude.ai sessiooni-URL.
                self._send(400, "text/plain; charset=utf-8", b"lubamatu url")
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/api/tmux/new", "/api/tmux/open"):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        if DEMO:
            self._json(403, {"ok": False, "error": "demo-režiimis välja lülitatud"})
            return
        why = self._local_origin_ok()
        if why:
            self._json(403, {"ok": False, "error": why})
            return
        payload, code, msg = self._read_json_body()
        if payload is None:
            self._json(code, {"ok": False, "error": msg})
            return

        if self.path == "/api/tmux/open":
            # Jooksva sessiooni klikk: midagi ei käivitata tmux'is, ainult
            # terminal avatakse. Nimi läbib open_terminal()-is _NAME_RE.
            name = payload.get("session")
            if not isinstance(name, str):
                self._json(400, {"ok": False, "error": "vigane session"})
                return
            self._json(200, {"ok": True, "session": name,
                             "opened": tmux_lib.open_terminal(name)})
            return

        kind = payload.get("kind")
        project = payload.get("project")
        if kind not in ("project", "chat") or (
                kind == "project" and not isinstance(project, str)):
            self._json(400, {"ok": False, "error": "vigane kind/project"})
            return

        # Kliendilt tuleb NIMI, mitte tee. Tee valib tmux_lib oma nimekirjast.
        target, err = tmux_lib.resolve(kind, project)
        if target is None:
            self._json(400, {"ok": False, "error": err})
            return
        created, err = tmux_lib.start(target)
        if err:
            self._json(500, {"ok": False, "error": err})
            return
        self._json(200, {
            "ok": True,
            "created": created,          # False = sessioon oli juba olemas
            "session": target["name"],
            "display": target["display"],
            "attach": tmux_lib.attach_lines(target["name"],
                                            c.CONFIG.get("remoteHost") or None),
            # None = terminalUri seadistamata; True = `open` võttis lingi vastu.
            "opened": tmux_lib.open_terminal(target["name"]),
        })

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *a) -> None:  # vaikne
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code kasutuse dashboard")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true", help="ära ava brauserit")
    args = ap.parse_args()

    threading.Thread(target=refresher, daemon=True).start()
    if c.PEERS:
        threading.Thread(target=peer_syncer, daemon=True).start()

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
