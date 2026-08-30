"""Taustatööd (`claude --bg` sessioonid) ccdash'i jaoks.

Taustatööd on Claude Code'i "teine maailm": nad jooksevad daemonis, ilma
terminalita, ega paista VS Code'i tabiribal ega `/tasks` all. Ainus koht, kus
nad eksisteerivad, on `claude agents` ja `~/.claude/jobs/<id>/state.json`.
Selle mooduli mõte on tuua nad ccdash'i, et blokeeritud töö ei seisaks
nädalaid märkamatult.

Hübriidpollimine:
  * `~/.claude/jobs/*/state.json` — iga kutse, ~33 ms, null alamprotsessi.
  * `claude agents --json` — max iga STATE_TTL_SEC tagant, ~1000 ms. Seda on
    vaja, sest `state.json` `state` väli VÕIB OLLA AEGUNUD (nähtud: üks töö
    oli failis "working", CLI järgi "blocked"). CLI on autoriteet.

⛔ Väljastatakse ainult FIELDS-nimekirja väljad. `state.json` sisaldab lisaks
`sessionId`, `bridgeSessionId`, `bridgeOwnerAccountUuid`,
`bridgeOwnerOrganizationUuid` ja `cwd` — need ei tohi payload'i jõuda.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time

JOBS_DIR = os.path.expanduser("~/.claude/jobs")
LIVE_STATES = ("working", "blocked")
STATE_TTL_SEC = 300  # `claude agents` maksab ~1 s — küsi harva
CLI_TIMEOUT_SEC = 15

# Ainus koht, kus otsustatakse, mis kliendini jõuab.
FIELDS = ("id", "state", "name", "tokens", "needs", "detail", "intent",
          "startedAt", "updatedAt", "hasTerminal", "waiting")

_state_cache: dict[str, str] = {}
_state_fetched: float = 0.0


def _cli_states() -> dict[str, str]:
    """Autoriteetne {id: state} `claude agents --json`-ist, vahemäluga."""
    global _state_cache, _state_fetched
    if time.time() - _state_fetched < STATE_TTL_SEC:
        return _state_cache
    try:
        out = subprocess.run(["claude", "agents", "--json"], capture_output=True,
                             text=True, timeout=CLI_TIMEOUT_SEC).stdout
        rows = json.loads(out)
        rows = rows if isinstance(rows, list) else rows.get("agents", [])
        _state_cache = {str(r.get("id", "")): r.get("state", "") for r in rows}
    except Exception:
        # Vana vahemälu on parem kui mitte midagi; state.json jääb varuvariandiks.
        pass
    _state_fetched = time.time()
    return _state_cache


def _terminal_count() -> int:
    """Teine maailm: interaktiivsed `claude` protsessid päris terminalis (tty)."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid,tty,command"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return 0
    n = 0
    for line in out.splitlines():
        if not re.search(r"\bclaude$", line.rstrip()):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in ("??", "-"):
            n += 1
    return n


def _read_jobs() -> list[dict]:
    cli = _cli_states()
    items = []
    for path in glob.glob(os.path.join(JOBS_DIR, "*", "state.json")):
        jid = os.path.basename(os.path.dirname(path))
        try:
            with open(path, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            continue  # üksik katkine state.json ei tohi kogu loendit kaotada
        state = cli.get(jid) or st.get("state") or ""
        if state not in LIVE_STATES:
            continue
        needs = st.get("needs")
        items.append({
            # Ootaja = blokeeritud VÕI `needs`-iga. Ainult `needs`-ist ei piisa:
            # nähtud on blokeeritud tööd, mille `needs` oli vahepeal tühjaks
            # läinud — just selline töö seisakski jälle märkamatult.
            "waiting": state == "blocked" or bool(needs),
            "id": jid,
            "state": state,
            "name": st.get("name") or "(nimetu)",
            "tokens": st.get("tokens") if isinstance(st.get("tokens"), int) else None,
            "needs": str(needs)[:300] if needs else None,
            "detail": str(st.get("detail"))[:300] if st.get("detail") else None,
            "intent": str(st.get("intent"))[:300] if st.get("intent") else None,
            "startedAt": st.get("createdAt"),
            "updatedAt": st.get("updatedAt"),
            # Kas seda tööd on kunagi terminali avatud. False = päris nähtamatu.
            "hasTerminal": bool(st.get("firstTerminalAt")),
        })
    # Ootajad ette, siis vanim seisak enne — just neid on vaja märgata.
    items.sort(key=lambda x: (not x["waiting"], x.get("updatedAt") or ""))
    return items


def collect_jobs() -> dict:
    """Ei tõsta KUNAGI erindit.

    ccdash'i `refresher()` püüab iga erindi `collect()`-ist ja kirjutab
    `_cache = {"ok": False}`, mille peale brauser peidab KOGU dashboardi
    veabänneri taha. Taustatööde viga ei tohi pimestada kulusid ja limiite,
    seega kogu töö on siin try/except'i sees.
    """
    try:
        items = _read_jobs()
        return {
            "items": [{k: it.get(k) for k in FIELDS} for it in items],
            "terminals": _terminal_count(),
            "waiting": sum(1 for it in items if it["waiting"]),
        }
    except Exception as e:  # noqa: BLE001 — dashboard ei tohi surra
        return {"items": [], "terminals": 0, "waiting": 0,
                "error": f"{type(e).__name__}: {e}"}


def demo_anonymize_jobs(jobs: dict) -> dict:
    """DEMO-režiim: vabas vormis tekstiväljad KUSTUTATAKSE, mitte ei nimetata ümber.

    `intent`/`detail`/`needs` on päris laused päris tööst (kliendinimed, teed,
    ärisisu). Identifikaatori-stiilis ümbernimetamine, nagu sessioonidel, ei
    kaitseks siin midagi.
    """
    for i, it in enumerate(jobs.get("items", []), 1):
        it["name"] = f"taustatöö {i}"
        for k in ("intent", "detail", "needs"):
            if it.get(k):
                it[k] = "(demo)"
    return jobs
