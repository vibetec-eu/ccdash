"""Jagatud ccusage-teek: token-usage-daily.sh ja ccdash.py kasutavad mõlemad seda.

Miks see olemas on — kolm ccusage'i lõksu, mis vana skripti katki tegid:

1. HINNAKIRI KAOB VAIKSELT. ccusage arvutab kulu LiteLLM-i hinnatabelist, mille ta
   tõmbab võrgust. Claude Code'i .jsonl-id EI sisalda `costUSD` välja (tellimuskasutus),
   seega kulu SÕLTUB ALATI võrgust. Kui tõmme ebaõnnestub, ei anna ccusage viga —
   ta tagastab `totalCost: 0` ja normaalsed tokeninumbrid. Nii tekkis logisse
   "2026-08-07  $0.00  129759914 tok", kuigi tegelik kulu oli $99.26.
   → `fetch()` valideerib: tokeneid on, aga kulu null = katki. Proovib uuesti
     `--offline` (vahemälust) ja alles siis annab vea.

1b. HINNAKIRI VÕIB KADUDA OSALISELT — ja see on salakavalam (leitud 2026-08-13).
   ccusage 20.0.19 sisseehitatud (offline) tabelis PUUDUB `claude-opus-5`, kuigi see oli
   selle masina põhimudel: `--offline` andis $1955.45, õige oli $3266.19 — puudu täpselt
   opus-5 kogukulu. Muud mudelid (fable-5, opus-4-8, sonnet-5) olid tabelis olemas.
   Rea kogusumma kontroll EI päästa segapäeval: 12.08 oli opus-5 $0.00 + sonnet-5 $16.16,
   `totalCost = 16.16 != 0` → vana valideerimine läks LÄBI ja logisse oleks läinud $16.16
   tegeliku $253.31 asemel.
   → `_rows_look_priced()` kontrollib nüüd MUDELITE KAUPA (`modelBreakdowns`).
   → `~/.claude/ccusage.json` annab `pricingOverrides` kaudu opus-5 hinnad ette; `_run()`
     lisab `--config`. Kontrollitud: offline+config == online sendi täpsusega (vahe $0.00
     iga mudeli peal), seega ccusage tuletab ka 1h-cache lisatasu neist ülekirjutustest.

2. AJALUGU KUSTUB. Claude Code koristab vanu .jsonl-e (vanim praegu 2026-07-11).
   ccusage näeb ainult alles olevaid faile, seega ajaloolised summad KAHANEVAD ajas:
   juuli oli logi järgi $2606, ccusage näitab nüüd $2187. Seega kuu kumulatiivi EI
   tohi ccusage'ist arvutada — see tuleb liita logist endast.
   → vt `month_total_from_log()`.

3. AJAVÖÖND. ccusage grupeerib päevi vaikimisi oma äranägemise järgi; ilma
   `--timezone` liputa ei pruugi päevapiir ühtida kohaliku kuupäevaga.
   → `TZ` tuleb seadistusest (`ccdash.config.json`), vaikimisi süsteemi ajavöönd,
     ja antakse ccusage'ile `--timezone` liputa edasi.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Kohalik transkriptipuu on `projects_dirs()[0]`; peer-koopiad tulevad sinna järele (vt „Mitu masinat").
LOG_PATH = Path.home() / ".claude" / "logs" / "token-usage-daily.log"

# --- Seadistus -------------------------------------------------------------
# Kõik masinaspetsiifiline (projektikaustade teed, hoiatusläved, ajavöönd) elab
# selles failis, mitte koodis. Faili puudumisel töötab kõik mõistlike vaikeväärtustega:
# projektinimi tuletatakse siis ainult transkripti kaustanimest.
# Vt examples/ccdash.config.json.
CONFIG_PATH = Path(os.environ.get("CCDASH_CONFIG")
                   or Path.home() / ".claude" / "ccdash.config.json")


def _load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


CONFIG = _load_config()


def _system_tz() -> ZoneInfo:
    """Süsteemi ajavöönd /etc/localtime pealt; UTC, kui ei õnnestu tuvastada."""
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            i = len(parts) - 1 - parts[::-1].index("zoneinfo")
            return ZoneInfo("/".join(parts[i + 1:]))
    except (OSError, ValueError, KeyError):
        pass
    return ZoneInfo("UTC")


TZ = ZoneInfo(CONFIG["timezone"]) if CONFIG.get("timezone") else _system_tz()
TZ_NAME = str(TZ)


def threshold(name: str, default: float) -> float:
    """Hoiatuslävi configist (`thresholds.<name>`), muidu `default`."""
    try:
        return float((CONFIG.get("thresholds") or {}).get(name, default))
    except (TypeError, ValueError):
        return default


# --- Mitu masinat (`peers`) -------------------------------------------------
# Claude Code jookseb Heikkil kahel masinal (Air + Mini) ja kumbki kirjutab oma
# `~/.claude/projects` alla. ccusage loeb ainult kohalikku puud, seega näitas dashboard
# POOLT tegelikust (mõõdetud 08.09.2026: Air $317 + Mini $309 septembris).
#
# Lahendus on sümmeetriline: iga masin tõmbab rsync'iga teise masina `~/.claude/projects`
# koopia kausta `~/.claude/peers/<host>/projects/` ja annab ccusage'ile MÕLEMAD puud
# komaga eraldatud `CLAUDE_CONFIG_DIR`-is. ccusage dedupib sama sessiooni (kontrollitud:
# sama kaust kaks korda = sama summa), seega topeltlugemist ei teki.
#
# Koopia on püsiv: kui teine masin ei vasta, jääb viimane koopia kasutusse ja
# dashboard ütleb, kui vana see on. Sünk EI kasuta `--delete` — teises masinas
# kustutatud sessioon jääb siia arhiivina alles ja ccusage loeb seda edasi.
PEERS = tuple(h for h in (CONFIG.get("peers") or []) if isinstance(h, str) and h)
PEERS_DIR = Path.home() / ".claude" / "peers"
PEERS_STATE = Path.home() / ".claude" / "logs" / "peers.json"
PEER_SYNC_TIMEOUT = 600      # esimene täiskoopia (~50 MB üle Tailscale'i) võib võtta minuteid
PEER_STALE_MIN = 30          # vanem õnnestunud koopia = hoiatus dashboardil


# --- Võõrad allikad ---------------------------------------------------------
# ccusage avastab ISE ka OpenClaw'i sessioonid (`~/.openclaw/agents/*/sessions/*.jsonl`)
# ja märgistab need mudelinimega `[openclaw] <mudel>`. OpenClaw kirjutab kulu nullina — ta
# ei arvuta seda ja ccusage annab nulli muutmata edasi. Valve `_rows_look_priced` pidas
# seda hinnakirja kaoks ja tappis kogu dashboardi (30.08–08.09.2026, 8,07 M tokenit).
#
# SULETUD loend meelega: tundmatu silt (nt `[bedrock] claude-opus-5`) tähendab endiselt
# hinnakirja kadu ja viskab vea. Lahtine muster `^\[.*\] ` oleks andnud etteulatuvalt loa
# kõigile tulevastele siltidele ja lammutanud lõks 1b kaitse (oponent 04.09.2026).
# Ülekirjutatav: `ccdash.config.json` → `"externalSources": ["openclaw"]`.
KNOWN_EXTERNAL_SOURCES = frozenset(
    s for s in (CONFIG.get("externalSources") or ["openclaw"]) if isinstance(s, str) and s)
_EXTERNAL_RE = re.compile(r"^\[([^\]]+)\]\s")


def _external_source(model_name: str | None) -> str | None:
    """'[openclaw] claude-opus-4-8' -> 'openclaw'; tundmatu silt või silt puudub -> None."""
    m = _EXTERNAL_RE.match(model_name or "")
    if m and m.group(1) in KNOWN_EXTERNAL_SOURCES:
        return m.group(1)
    return None


def external_unpriced(rows: list[dict]) -> dict[str, int]:
    """{allikas: tokenid}, mis on numbrites sees, aga hinnata (kulu 0).

    Kutsu AINULT `daily` ridade peal — `session` kirjeldab sama kasutust ja summa
    tuleks topelt (oponent 04.09).
    """
    out: dict[str, int] = {}
    for r in rows:
        for b in r.get("modelBreakdowns") or []:
            src = _external_source(b.get("modelName"))
            if src and _breakdown_tokens(b) > 0 and (b.get("cost") or 0) == 0:
                out[src] = out.get(src, 0) + _breakdown_tokens(b)
    return out


def config_dirs() -> list[Path]:
    """Kõik `.claude`-puud, mida ccusage peab lugema: kohalik + olemasolevad peer-koopiad.

    Dünaamiline, mitte konstant: peer'i kaust tekib alles esimese õnnestunud sünkiga.
    """
    dirs = [Path.home() / ".claude"]
    for host in PEERS:
        d = PEERS_DIR / host
        if (d / "projects").is_dir():
            dirs.append(d)
    return dirs


def projects_dirs() -> list[Path]:
    return [d / "projects" for d in config_dirs()]


def sync_peers() -> dict[str, dict]:
    """rsync iga peer'i `~/.claude/projects` → `PEERS_DIR/<host>/projects`.

    Ei tõsta kunagi erindit: iga peer saab `{"ok", "at", "error"}` ja tulemus kirjutatakse
    `PEERS_STATE`-i, et dashboard ja päevalogija näeksid sama seisu. Ebaõnnestunud sünk
    jätab vana koopia puutumata (rsync kirjutab faili kaupa atomaarselt).
    """
    state = _read_peers_state()
    for host in PEERS:
        dest = PEERS_DIR / host / "projects"
        entry = dict(state.get(host) or {})
        try:
            dest.mkdir(parents=True, exist_ok=True)
            cmd = ["rsync", "-a", "--timeout=20",
                   "-e", "ssh -o ConnectTimeout=5 -o BatchMode=yes",
                   f"{host}:.claude/projects/", f"{dest}/"]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=PEER_SYNC_TIMEOUT)
            if p.returncode == 0:
                entry = {"ok": True, "at": now().isoformat(), "error": None}
            else:
                tail = (p.stderr or "").strip().splitlines()[-1:] or ["rsync kukkus"]
                entry.update({"ok": False, "error": f"rc={p.returncode}: {tail[0][:160]}"})
        except subprocess.TimeoutExpired:
            entry.update({"ok": False, "error": f"rsync aegus ({PEER_SYNC_TIMEOUT}s)"})
        except OSError as e:
            entry.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        entry.setdefault("at", None)
        state[host] = entry
    try:
        PEERS_STATE.parent.mkdir(parents=True, exist_ok=True)
        PEERS_STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return state


def _read_peers_state() -> dict[str, dict]:
    try:
        st = json.loads(PEERS_STATE.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def peers_status() -> list[dict]:
    """Dashboardi payload: [{host, ok, at, ageMin, stale, error}] iga seadistatud peer'i kohta.

    `at` on VIIMASE ÕNNESTUNUD koopia aeg — ebaõnnestunud katse seda ei nulli, sest just
    see ütleb, kui vanad teise masina numbrid praegu on.
    """
    state = _read_peers_state()
    out = []
    for host in PEERS:
        e = state.get(host) or {}
        age = None
        if e.get("at"):
            try:
                age = (now() - datetime.fromisoformat(e["at"])).total_seconds() / 60
            except ValueError:
                age = None
        out.append({
            "host": host,
            "ok": bool(e.get("ok")),
            "at": e.get("at"),
            "ageMin": round(age) if age is not None else None,
            "stale": age is None or age > PEER_STALE_MIN or not e.get("ok"),
            "error": e.get("error"),
        })
    return out

# --- Valuuta ---------------------------------------------------------------
# ccusage arvutab kulu Anthropicu API listihinnast, mis on ALATI USD-s. Euroala
# arve tuleb eurodes, seega teisendame kuvamiseks. Kurss tuleb EKP päevakursist,
# vahemälus kettal; võrgu puudumisel kasutame viimast teadaolevat.
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_CACHE = Path.home() / ".claude" / "logs" / "eurusd.json"
FX_FALLBACK = 1.1555          # EKP 2026-08-10; kasutatakse ainult siis, kui
FX_FALLBACK_DATE = "2026-08-10"  # ei võrku ega vahemälu ole
FX_MAX_AGE_H = 18             # vanem kui see → proovi uuesti tõmmata

# npx on aeglane (~5 s). Vaikimisi ootame kuni 120 s, enne kui loeme katkiseks.
CCUSAGE_TIMEOUT = 120

# ccusage'i konfiguratsioon: `defaults.pricingOverrides` täidab augud ccusage'i enda
# hinnatabelis (vt lõks 1b). Ilma selleta hinnastatakse `claude-opus-5` offline'is nulliga.
# Tingimuslik: faili puudumisel taandume vanale käitumisele, mitte ei kuku kokku.
CCUSAGE_CONFIG = Path.home() / ".claude" / "ccusage.json"

# LiteLLM-i tabel, millest ccusage online-režiimis hinnad võtab. Kasutame seda
# `check_pricing_drift()`-is, et märgata, kui ccusage.json vananeb.
LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
# ccusage.json väli -> LiteLLM väli
_PRICE_FIELDS = {
    "inputCostPerToken": "input_cost_per_token",
    "outputCostPerToken": "output_cost_per_token",
    "cacheCreationInputTokenCost": "cache_creation_input_token_cost",
    "cacheReadInputTokenCost": "cache_read_input_token_cost",
}

# VARUANKUR: mille vastu võrrelda, kui LiteLLM ei tunne mudelit ENNAST.
#
# Varasem versioon võrdles opus-5 ülekirjutust ALATI opus-4-8 kirjega. See tegi valvest
# pimeda täpselt seal, mille jaoks ta ehitati: kui Anthropic muudab opus-5 hinda ja jätab
# 4-8 puutumata, hoiatust ei tulnud. Kontrollitud 2026-08-18: LiteLLM-il ON nüüd
# `claude-opus-5` olemas ja ühtib ccusage.json-iga sendi täpsusega.
# Nüüd: mudelit võrreldakse ta enda kirjega; ankrut kasutatakse ainult siis, kui enda
# kirjet ei ole (uus mudel, mida LiteLLM veel ei tea).
_DRIFT_FALLBACK = {"claude-opus-5": "claude-opus-4-8"}


class CcusageError(RuntimeError):
    """ccusage ei jooksnud või andis kasutuskõlbmatu väljundi."""


def now() -> datetime:
    return datetime.now(TZ)


def eur_rate() -> dict:
    """EUR/USD kurss EKP päevakursist. Tagastab {rate, date, source}.

    `rate` on USD ühe euro kohta (nt 1.1555) — teisendus on `eur = usd / rate`.
    Kolm astet: värske vahemälu → EKP tõmme → viimane teadaolev / konstant.
    Ei viska kunagi erindit; dashboard peab töötama ka ilma võrguta.
    """
    import json as _json
    import urllib.request

    cached = None
    if FX_CACHE.exists():
        try:
            cached = _json.loads(FX_CACHE.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(cached["fetchedAt"])
            if (now() - fetched).total_seconds() < FX_MAX_AGE_H * 3600:
                return cached          # piisavalt värske
        except (ValueError, KeyError, OSError):
            cached = None

    try:
        with urllib.request.urlopen(ECB_URL, timeout=8) as r:
            xml = r.read().decode("utf-8", "replace")
        m = re.search(r"currency='USD'\s+rate='([\d.]+)'", xml)
        d = re.search(r"time='([\d-]+)'", xml)
        if m:
            out = {"rate": float(m.group(1)),
                   "date": d.group(1) if d else "?",
                   "source": "EKP",
                   "fetchedAt": now().isoformat()}
            try:
                FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
                FX_CACHE.write_text(_json.dumps(out), encoding="utf-8")
            except OSError:
                pass
            return out
    except Exception:  # noqa: BLE001 — võrgutõrge ei tohi dashboardi tappa
        pass

    if cached:                          # vana, aga päris kurss
        return {**cached, "source": "EKP (vana)"}
    return {"rate": FX_FALLBACK, "date": FX_FALLBACK_DATE,
            "source": "varukurss", "fetchedAt": now().isoformat()}


def today_str() -> str:
    return now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- ccusage kutse

def _run(args: list[str], offline: bool) -> dict:
    cmd = ["npx", "--yes", "ccusage@latest", *args, "--json", "--timezone", TZ_NAME]
    if CCUSAGE_CONFIG.exists():
        cmd += ["--config", str(CCUSAGE_CONFIG)]
    if offline:
        cmd.append("--offline")
    # Ilma `peers`-ita jääb keskkond puutumata — ccusage otsib siis ise oma vaikekaustad.
    env = None
    if PEERS:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": ",".join(str(d) for d in config_dirs())}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CCUSAGE_TIMEOUT,
                           env=env)
    except subprocess.TimeoutExpired as e:
        raise CcusageError(f"ccusage aegus ({CCUSAGE_TIMEOUT}s): {' '.join(args)}") from e
    if p.returncode != 0 or not p.stdout.strip():
        tail = (p.stderr or "").strip().splitlines()[-3:]
        raise CcusageError(f"ccusage kukkus (rc={p.returncode}): {' '.join(tail) or 'tühi väljund'}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise CcusageError(f"ccusage andis vigase JSON-i: {e}") from e


def _breakdown_tokens(b: dict) -> int:
    return sum((b.get(k) or 0) for k in
               ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"))


def _rows_look_priced(rows: list[dict]) -> bool:
    """False, kui mõnel MUDELIL on tokeneid, aga kulu null — hinnakirja kadu.

    Kontroll käib mudelite kaupa, mitte rea kogusumma järgi. Rea kogusummast EI PIISA:
    segapäeval (12.08.2026: opus-5 $0.00 + sonnet-5 $16.16) on `totalCost` nullist erinev,
    kuigi 308M tokenit ehk $237 jäi hinnastamata — vana kontroll laskis selle läbi.
    Vt lõks 1b.

    Null kulu JA null tokenit on legitiimne (kasutuseta päev), seega vaatame ainult seda,
    kus tokeneid tegelikult on. `modelBreakdowns` on JSON-väljundis olemas ka ilma
    `--breakdown` liputa; kui mingil põhjusel puudub, langeme tagasi rea kogusummale.
    """
    for r in rows:
        if (r.get("totalTokens") or 0) <= 0:
            continue                                  # kasutuseta päev
        breakdowns = r.get("modelBreakdowns") or []
        if breakdowns:
            for b in breakdowns:
                if _external_source(b.get("modelName")):
                    continue                          # võõras allikas — kulu EI SAAGI olla
                if _breakdown_tokens(b) > 0 and (b.get("cost") or 0) == 0:
                    return False
        elif (r.get("totalCost") or 0) == 0:
            return False                              # varukontroll ilma breakdown'ita
    return True


def _unpriced(rows: list[dict], limit: int = 5) -> list[str]:
    """['2026-08-12 claude-opus-5', …] — mis täpselt jäi hinnastamata.

    Veateates on mudeli nimi olulisem kui kuupäev: just see ütleb, mille jaoks tuleb
    `~/.claude/ccusage.json`-i uus `pricingOverrides` kirje lisada.
    """
    out: list[str] = []
    for r in rows:
        if (r.get("totalTokens") or 0) <= 0:
            continue
        breakdowns = r.get("modelBreakdowns") or []
        if not breakdowns and (r.get("totalCost") or 0) == 0:
            out.append(f"{r.get('period')} (kogu rida)")
        for b in breakdowns:
            if _external_source(b.get("modelName")):
                continue
            if _breakdown_tokens(b) > 0 and (b.get("cost") or 0) == 0:
                out.append(f"{r.get('period')} {b.get('modelName') or '?'}")
        if len(out) >= limit:
            break
    return out[:limit]


def fetch(section: str, key: str) -> tuple[list[dict], bool]:
    """Küsi ccusage'ilt üks sektsioon. Tagastab (read, kas_kasutati_offline_varianti).

    Viskab CcusageError, kui ka --offline ei anna hinnastatud tulemust.
    """
    rows = _run([section], offline=False).get(key) or []
    if _rows_look_priced(rows):
        return rows, False

    # Hinnakiri kadus — proovi vahemälust.
    rows_off = _run([section], offline=True).get(key) or []
    if _rows_look_priced(rows_off):
        return rows_off, True

    raise CcusageError(
        "hinnakiri puudub (ka --offline vahemälust) — kulud tuleksid nullina. "
        f"Hinnastamata: {_unpriced(rows)}"
    )


def fetch_multi(sections: list[str]) -> tuple[dict[str, list[dict]], bool]:
    """Mitu sektsiooni ÜHE ccusage'i kutsega (`--sections`).

    npx-i käivitus maksab ~5 s, seega daily+session koos (~8 s) on tuntavalt
    kiirem kui kaks eraldi kutset (~10 s) — dashboard värskendab tsükliliselt.
    """
    args = ["daily", "--sections", ",".join(sections)]

    def grab(offline: bool) -> dict[str, list[dict]]:
        raw = _run(args, offline=offline)
        return {s: (raw.get(s) or []) for s in sections}

    data = grab(offline=False)
    if all(_rows_look_priced(rows) for rows in data.values()):
        return data, False

    data_off = grab(offline=True)
    if all(_rows_look_priced(rows) for rows in data_off.values()):
        return data_off, True

    broken = [x for rows in data_off.values() for x in _unpriced(rows)][:5]
    raise CcusageError(
        "hinnakiri puudub (ka --offline vahemälust) — kulud tuleksid nullina. "
        f"Hinnastamata: {broken}"
    )


def check_pricing_drift() -> list[str]:
    """Kas `~/.claude/ccusage.json` hinnad on LiteLLM-i tabelist maha jäänud?

    Tagastab inimloetavad hoiatusread (tühi = korras). EI paranda automaatselt: vaikne
    hinnamuutus on täpselt see, mille vastu see teek võitleb — muudatus peab jääma silma.
    Võrgutõrge = tühi loend (ei ole hoiatus, vaid teadmatus).

    Mõeldud päevajooksu jaoks (`token_usage_daily.py`), MITTE dashboardi tsüklisse —
    see koputaks võrku iga värskendusega.
    """
    import urllib.request

    if not CCUSAGE_CONFIG.exists():
        return []
    try:
        overrides = (json.loads(CCUSAGE_CONFIG.read_text(encoding="utf-8"))
                     .get("defaults", {}).get("pricingOverrides", {}))
    except (json.JSONDecodeError, OSError):
        return [f"⚠️ {CCUSAGE_CONFIG.name} on loetamatu — hinnakontroll vahele jäetud"]
    if not overrides:
        return []

    try:
        with urllib.request.urlopen(LITELLM_URL, timeout=8) as r:
            table = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — võrgutõrge ei ole hoiatus
        return []

    warn: list[str] = []
    for model, ours in overrides.items():
        ref, theirs = model, table.get(model)
        if theirs is None:                       # LiteLLM ei tunne mudelit ennast
            ref = _DRIFT_FALLBACK.get(model)
            theirs = table.get(ref) if ref else None
        via = "" if ref == model else f" (ankur: {ref})"
        if theirs is None:
            warn.append(f"⚠️ ccusage.json: LiteLLM ei tunne mudelit {model}"
                        + (f" ega ankrut {ref}" if ref else ""))
            continue
        for our_key, their_key in _PRICE_FIELDS.items():
            a, b = ours.get(our_key), theirs.get(their_key)
            if a is not None and b is not None and abs(a - b) > 1e-12:
                warn.append(f"⚠️ ccusage.json {model}.{our_key}: {a} → LiteLLM-is {b}{via}")
    return warn


def fetch_daily() -> tuple[list[dict], bool]:
    return fetch("daily", "daily")


def fetch_sessions() -> tuple[list[dict], bool]:
    return fetch("session", "session")


def fetch_blocks() -> list[dict]:
    """5 h aknad. Siin hinnavalideerimist ei tee — aktiivne aken võib olla alles tühi."""
    return _run(["blocks"], offline=False).get("blocks") or []


# ------------------------------------------------------- sessioon -> projektinimi

def _slug(path: str) -> str:
    """Kaustatee -> Claude Code'i kaustanimi: '/Users/x/Projects' -> '-Users-x-Projects'.

    HOIATUS: teisendus EI OLE pööratav — Claude Code asendab sidekriipsuga ka `.` ja `@`
    (`nimi.perenimi@gmail.com` -> `nimi-perenimi-gmail-com`), seega tagasi teisendades ei
    tea, kas sidekriips oli `/`, `.`, `@` või päris sidekriips. Just seepärast käib
    atributsioon transkripti `cwd`-välja pealt ja see funktsioon on ainult varuvariant.
    """
    return path.rstrip("/").replace("/", "-")


def _pretty_project(slug: str) -> str:
    """'-Users-x-Projects-minuprojekt' -> 'minuprojekt' (varuvariant, vt _slug)."""
    for root in _PROJECT_ROOTS:
        if not root.startswith("/"):
            continue                       # suhteline fragment ei ole slugi eesliide
        pref = _slug(root)
        if slug.startswith(pref + "-"):
            return slug[len(pref) + 1:] or slug
        if slug == pref:
            return GENERAL_LABEL           # täpselt projektijuures = üldtöö
    if slug == _slug(str(Path.home())):
        # Kodukaustast käivitatud sessioon: üldtööd (e-post, sõnumid, otsingud).
        # Eraldi ämbrina ei anna infot, ühendame projektijuurega.
        return GENERAL_LABEL
    return slug.lstrip("-").replace("-", "/")


# --- Sessiooni päris projekt + pealkiri ------------------------------------
# Kaustapõhine kaardistus (session_project_map) valetab: sessioon, mis käivitati
# `~/` või `.../Claude/Projects` alt, sildistatakse "(kodukaust)" või
# "(Projects juur)", kuigi ta tegelikult töötas mõne konkreetse projekti kallal.
# Need kaks ämbrit katsid 96% kulust ja ülevaadet ei andnud.
#
# Transkriptis on kaks paremat välja:
#   "cwd"     — päris töökaust, muutub sessiooni jooksul
#   "aiTitle" — inimloetav sessiooni pealkiri, nt "Lisada avalehele kontaktivorm"
# Skaneerime need toorest tekstist regexiga (JSON-i parse oleks 591 MB peal
# liiga aeglane) ja hoiame vahemälus mtime+suuruse järgi.

META_CACHE = Path.home() / ".claude" / "logs" / "ccdash-meta.json"
_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TITLE_RE = re.compile(r'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Konteksti pikkus = viimane cache_read. Kogu senine vestlus loetakse vahemälust
# igal käigul, seega see väli ONGI konteksti suurus. Mõõdetud 14 789 kõne pealt:
# sessioon, mis kasvab üle 350k, maksab sama töö eest ~1,9× rohkem kui 200k juures
# lõpetatu — cache-lugemine skaleerub lineaarselt ja maksustatakse igal käigul.
_CACHEREAD_RE = re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)')

CTX_WARN = 200_000    # üle selle hakkab tasuvus langema
CTX_HIGH = 350_000    # üle selle ~1,9× kallim optimumist

# Kaustatee -> projektinimi. Juured tulevad seadistusest, sest need on masinapõhised:
# projektid võivad elada mitmes puus korraga (kohalik kaust, pilvemount, teine
# kasutajakonto) ja tuletada neid kodukaustast EI SAA — vt README „Seadistus".
# Tühi loend = cwd-põhine atributsioon välja lülitatud, jääb ainult kaustanimi.
_PROJECT_ROOTS = tuple(r for r in (CONFIG.get("projectRoots") or []) if isinstance(r, str) and r)
GENERAL_LABEL = "(üldine)"


def _project_from_cwd(cwd: str) -> str | None:
    """'/Users/x/Projects/minuprojekt/alamkaust' -> 'minuprojekt'."""
    for root in _PROJECT_ROOTS:
        i = cwd.find(root)
        if i >= 0:
            rest = cwd[i + len(root):].strip("/")
            if rest:
                return rest.split("/")[0]
            return None      # täpselt juurkaustas — ei ütle projekti
    return None


def _label_from_cwd(cwd: str | None) -> str | None:
    """Loetav silt PÄRIS cwd-teest, kui ükski projektijuur ei sobinud.

    Varem langeti siin tagasi Claude Code'i kaustanime (slug'i) peale, aga see
    teisendus EI OLE pööratav — `.` ja `@` muutuvad samuti sidekriipsuks — ja
    tulemuseks olid read nagu `Users/heikki//openclaw/w…`. Päris tee on
    transkriptis olemas; sellest saab ausa nime ilma arvamiseta.
    """
    if not cwd or not cwd.startswith("/"):
        return None
    p = cwd.rstrip("/")
    # macOS ajutised kaustad (`/var/folders` on symlink `/private/var/folders`-ile):
    # nende viimane komponent on juhuslik räsi ja ei ütle mitte midagi.
    if p.startswith("/private/var/folders/") or p.startswith("/var/folders/"):
        return "(ajutine kaust)"
    if p == str(Path.home()).rstrip("/"):
        return GENERAL_LABEL
    for root in _PROJECT_ROOTS:
        if p == root.rstrip("/"):
            return GENERAL_LABEL       # täpselt projektijuures = üldtöö
    last = p.rsplit("/", 1)[-1]
    return last or None


def _scan_transcript(path: Path) -> dict:
    """Leia failist domineeriv projekt, domineeriv cwd ja viimane pealkiri."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"project": None, "cwd": None, "title": None}

    counts: dict[str, int] = {}
    cwds: dict[str, int] = {}
    for m in _CWD_RE.finditer(text):
        raw = m.group(1).replace("\\/", "/")
        cwds[raw] = cwds.get(raw, 0) + 1
        proj = _project_from_cwd(raw)
        if proj:
            counts[proj] = counts.get(proj, 0) + 1

    titles = _TITLE_RE.findall(text)
    title = None
    if titles:
        try:
            title = json.loads(f'"{titles[-1]}"')   # tühjenda escape'id
        except json.JSONDecodeError:
            title = titles[-1]

    # Viimane nullist erinev cache_read = praegune konteksti suurus.
    ctx = 0
    for m in reversed(_CACHEREAD_RE.findall(text)):
        if m != "0":
            ctx = int(m)
            break

    project = max(counts, key=counts.get) if counts else None
    cwd = max(cwds, key=cwds.get) if cwds else None
    return {"project": project, "cwd": cwd, "title": title, "ctx": ctx}


def session_meta() -> dict[str, dict]:
    """{sid: {"project":…, "title":…}} — vahemälustatud mtime+suuruse järgi."""
    cache: dict[str, dict] = {}
    if META_CACHE.exists():
        try:
            cache = json.loads(META_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    out: dict[str, dict] = {}
    dirty = False

    # Kohalik puu + peer-koopiad. Sama sessioon võib olla mõlemas (ccusage dedupib
    # kulu); siin võidab esimene ehk kohalik, sest tema fail on värskem.
    proj_dirs = [d for root in projects_dirs() if root.is_dir()
                 for d in root.iterdir() if d.is_dir()]
    for proj_dir in proj_dirs:
        fallback = _pretty_project(proj_dir.name)
        for entry in proj_dir.glob("*.jsonl"):
            sid = entry.stem
            if not uuid_re.match(sid):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            # v-number on OSA VÕTMEST meelega: ilma selleta jääks atributsiooni
            # muutmise järel kehtima vana tulemus ja katkine loogika avastataks alles
            # nädalate pärast, kui failid ise muutuvad. Tõsta seda, kui _scan_transcript
            # või _project_from_cwd loogika muutub.
            sig = f"v3:{int(st.st_mtime)}:{st.st_size}"
            hit = cache.get(sid)
            if hit and hit.get("sig") == sig:
                info = hit
            else:
                info = {**_scan_transcript(entry), "sig": sig}
                cache[sid] = info
                dirty = True
            if sid in out:
                continue                              # kohalik koopia võitis
            out[sid] = {
                # Kolm astet, täpsemast lõdvemani:
                #   1. projektijuurele vastav nimi (`_project_from_cwd`)
                #   2. loetav silt päris cwd-teest (`_label_from_cwd`)
                #   3. kaustanimi ehk slug — ainult siis, kui cwd-d ei ole
                "project": (info.get("project")
                            or _label_from_cwd(info.get("cwd"))
                            or fallback),
                "title": info.get("title"),
                "ctx": info.get("ctx") or 0,
            }

    if dirty:
        try:
            META_CACHE.parent.mkdir(parents=True, exist_ok=True)
            META_CACHE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return out


def session_project_map() -> dict[str, str]:
    """{sessiooni-UUID: loetav projektinimi}, loetud ~/.claude/projects puust.

    Alamagentide transkriptid elavad `<uuid>/subagents/agent-*.jsonl` all — need
    kuuluvad emasessiooni alla, seega piisab UUID-nimeliste failide ja kaustade
    korjamisest.
    """
    out: dict[str, str] = {}
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    for proj in (d for root in projects_dirs() if root.is_dir()
                 for d in root.iterdir() if d.is_dir()):
        name = _pretty_project(proj.name)
        for entry in proj.iterdir():
            stem = entry.name[:-6] if entry.name.endswith(".jsonl") else entry.name
            if uuid_re.match(stem):
                out.setdefault(stem, name)
    return out


def enrich_sessions(sessions: list[dict]) -> list[dict]:
    """Lisa igale sessioonile projektinimi, pealkiri ja Eesti-aja viimane tegevus."""
    meta = session_meta()
    out = []
    for s in sessions:
        sid = s.get("period") or ""
        m = meta.get(sid, {})
        # Võõra tööriista sessioon (`agent: "openclaw"`) ei ela ~/.claude/projects all,
        # seega meta't ei ole — nimeta allika järgi, mitte „(tundmatu)" $0.00 reana.
        agent = s.get("agent")
        source = agent if agent in KNOWN_EXTERNAL_SOURCES else None
        last_raw = (s.get("metadata") or {}).get("lastActivity")
        last_dt = None
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00")).astimezone(TZ)
            except ValueError:
                last_dt = None
        out.append({
            "id": sid,
            "short": sid[:8],
            "project": m.get("project") or source or "(tundmatu)",
            "external": source,
            "title": m.get("title") or "",
            "ctx": m.get("ctx") or 0,
            # ok / warn / high — kas restart tasub end ära
            "ctxLevel": ("high" if (m.get("ctx") or 0) >= CTX_HIGH
                         else "warn" if (m.get("ctx") or 0) >= CTX_WARN else "ok"),
            "cost": s.get("totalCost") or 0.0,
            "tokens": s.get("totalTokens") or 0,
            "input": s.get("inputTokens") or 0,
            "output": s.get("outputTokens") or 0,
            "cacheRead": s.get("cacheReadTokens") or 0,
            "cacheCreate": s.get("cacheCreationTokens") or 0,
            "models": s.get("modelsUsed") or [],
            "lastIso": last_dt.isoformat() if last_dt else None,
            "last": last_dt.strftime("%d.%m %H:%M") if last_dt else "—",
            "lastSort": last_dt.timestamp() if last_dt else 0.0,
        })
    out.sort(key=lambda x: x["lastSort"], reverse=True)
    return out


# ------------------------------------------------------------------ logi lugemine

LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+\$([\d.]+)\s+(\d+) tok")


def read_log() -> dict[str, dict]:
    """{kuupäev: {"cost":.., "tokens":..}} logifailist. Hilisem rida võidab dubleeringu."""
    out: dict[str, dict] = {}
    if not LOG_PATH.exists():
        return out
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        m = LOG_LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = {"cost": float(m.group(2)), "tokens": int(m.group(3))}
    return out


def month_total_from_log(month: str, extra: dict[str, float] | None = None,
                         upto: str | None = None) -> float:
    """Kuu kumulatiiv LOGIST, mitte ccusage'ist — ccusage kaotab vana ajaloo.

    `extra` lubab lisada/ülekirjutada päevi, mida logis veel ei ole (nt sihtpäev).
    `upto` piirab summa kuupäevaga (kaasa arvatud) — vajalik tagasiulatuval
    täitmisel, et 07.08 rida ei sisaldaks 09.08 ja 10.08 kulu.
    """
    days = {d: v["cost"] for d, v in read_log().items() if d.startswith(month)}
    if extra:
        days.update({d: c for d, c in extra.items() if d.startswith(month)})
    if upto:
        days = {d: c for d, c in days.items() if d <= upto}
    return sum(days.values())
