"""tmux-sessioonid ccdash'i jaoks: näita, mis jookseb, ja käivita uus.

Miks see olemas on. Uue Claude-sessiooni alustamine on neli käsku käsitsi
(`ssh <host>` → `tmux new -s <nimi>` → `cd <kaust>` → `claude`), ja kuskilt ei
paista, MIS juba jookseb — nii tekib kogemata teine sessioon sama töö peale.
ccdash näeb ja oskab käivitada — kas samas masinas või (seadistus `tmuxHost`)
üle ssh masinas, kus tmux ja Claude päriselt jooksevad. Viimast on vaja, kui
dashboardi vaadatakse sülearvutist, aga töö käib teises Macis.

⛔ SEE ON AINUS MOODUL, MIS KÄIVITAB KASUTAJA VALIKUL PROTSESSI. Reeglid:
  * Klient ei saada KUNAGI kausta teed. Ta ütleb projekti NIME; tee otsitakse
    siin, `list_projects()` nimekirjast. Nii ei ole tee-injektsiooni pinda.
  * Sessiooninimi tuletatakse serveris ja peab läbima `_NAME_RE`. `:` ja `.`
    on tmux-il tähendusega — valge nimekiri, mitte põgenemine.
  * `subprocess.run([...])` listina, mitte kunagi `shell=True`. Üle ssh läheb
    argv `shlex.join`-iga JA `sh -c` alla — sshd käivitab kaugkäsu kasutaja
    login-shellis (zsh), mille `EQUALS`-laiendus teeks `-t =nimi`-st käsuotsingu.

⛔ Ükski funktsioon siin ei tõsta erindit — sama reegel mis `jobs_lib`-il.
   ccdash'i `refresher()` peidaks erindi peale KOGU dashboardi veabänneri taha.
"""
from __future__ import annotations

import os
import pwd
import re
import shlex
import queue
import subprocess
import threading
import time
import unicodedata

import ccusage_lib as c

CLI_TIMEOUT_SEC = 10

# tmux-sessiooni nimi. `:` ja `.` on tmux-i sihtmärgi-süntaksis eraldajad
# (`sessioon:aken.paneel`), seega `.` on siin lubatud ainult sellepärast, et
# `list-sessions` väljundit ei pöörata kunagi tagasi sihtmärgiks ilma `-t`-ta.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

# Väljastatav sessiooni-kirje. Nagu `jobs_lib.FIELDS` — ainus koht, kus
# otsustatakse, mis kliendini jõuab.
SESSION_FIELDS = ("name", "path", "attached", "createdAt")

# Käsurida, mille tmux paneelis käivitab. KONSTANT — kasutaja sisendit siin ei
# ole. `-l` (login) annab kasutaja päris PATH-i, mitte launchd plisti oma
# (`claude` elab nt ~/.local/node/bin all). `exec … -l` lõpus hoiab paneeli
# alles, kui claude väljub — muidu kaoks sessioon vaikselt ära.
_PANE_CMD = "claude; exec {shell} -l"


# Tagastuskoodid, mida `_sh` ise toodab (tmux/ssh ei anna neid):
RC_MISSING = 127     # binaar puudub (FileNotFoundError) või muu erind
RC_TIMEOUT = 254     # subprocess.TimeoutExpired — ssh hangus (Tailscale katkes?)
# ssh ise annab 255, kui ühendus lükati kohe tagasi.

_SHELL_CACHE: str | None = None


def _host() -> str | None:
    """Masin, kus tmux jookseb. Puudub = see masin."""
    h = c.CONFIG.get("tmuxHost")
    return h if isinstance(h, str) and h else None


def _sh(argv: list[str]) -> tuple[int, str]:
    """(returncode, stdout). Ei tõsta erindit.

    `tmuxHost` puudub -> `argv` jookseb siin. Olemas -> sama argv jookseb seal
    üle ssh, `sh -c` all: `shlex.join` kvoodib POSIX-sh reeglite järgi, aga
    sshd annaks rea kasutaja login-shellile (Minis zsh), kus `=xw` laieneks
    käsu asukohaks ja rida kukuks enne tmuxi jõudmist.
    """
    host = _host()
    # ssh liidab kõik käsuargumendid tühikuga ÜHEKS reaks ja annab selle
    # kaug-login-shellile. Seega `sh -c` + argv peab olema kvooditud KAKS korda:
    # sisemine `shlex.join` sh jaoks, välimine `shlex.quote` selle jaoks, et zsh
    # annaks kogu rea sh-le ühe argumendina (muidu jookseks `sh -c tmux` ilma
    # argumentideta ja tmux üritaks luua sessiooni ilma terminalita).
    cmd = argv if not host else [
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host,
        "sh -c " + shlex.quote(shlex.join(argv))]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_SEC)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return RC_TIMEOUT, ""
    except Exception:
        return RC_MISSING, ""


def _login_shell() -> str:
    """Kasutaja login-shell seal, kus tmux jookseb. Vahemälus — ei muutu."""
    global _SHELL_CACHE
    if _SHELL_CACHE:
        return _SHELL_CACHE
    shell = ""
    if _host():
        code, out = _sh(["sh", "-c", "echo $SHELL"])
        shell = out.strip() if code == 0 else ""
    else:
        try:
            shell = pwd.getpwuid(os.getuid()).pw_shell or ""
        except Exception:
            shell = ""
    _SHELL_CACHE = shell or "/bin/sh"
    return _SHELL_CACHE


def _isdir(path: str) -> int:
    """0 = kaust olemas, 1 = puudub, muu = ssh/tmux-tõrge (vt `_explain`).

    Üle ssh ei tohi ühendusviga paista „kausta ei ole"-na — see saadaks
    kasutaja valet asja parandama.
    """
    if _host():
        return _sh(["test", "-d", path])[0]
    return 0 if os.path.isdir(path) else 1


def _tmux(*args: str) -> tuple[int, str]:
    """(returncode, stdout). Ei tõsta erindit; tmux puudu -> (127, "")."""
    return _sh(["tmux", *args])


def tmux_name(display: str) -> str | None:
    """Kuvatav kaustanimi -> tmux-sessiooni nimi. Ei sobi -> None.

    `HA` -> `ha`, `Suur-Liiva KÜ` -> `suur-liiva-ku`. Ilma selleta murduks pool
    nimekirjast: projektikaustad on sageli suurtähtedega ja täpitähtedega.
    """
    # NFKD lahutab täpitähe põhitäheks + märgiks; märgid visatakse ära.
    flat = unicodedata.normalize("NFKD", display or "")
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    flat = flat.lower()
    flat = re.sub(r"[^a-z0-9._-]+", "-", flat).strip("-.")
    flat = flat[:32]
    return flat if _NAME_RE.match(flat) else None


# ------------------------------------------------------------------ lugemine

def list_sessions() -> list[dict]:
    """Jooksvad tmux-sessioonid. Väljastab ainult SESSION_FIELDS."""
    code, out = _tmux("list-sessions", "-F",
                      "#{session_name}\t#{session_attached}\t"
                      "#{session_created}\t#{pane_current_path}")
    if code != 0:                     # ükski sessioon ei jookse -> tmux annab 1
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, attached, created, path = parts[0], parts[1], parts[2], parts[3]
        rows.append({
            "name": name,
            "path": path,
            "attached": attached not in ("", "0"),
            "createdAt": int(created) if created.isdigit() else 0,
        })
    rows.sort(key=lambda r: (not r["attached"], r["name"].lower()))
    return rows


ROOT_SCAN_TIMEOUT_SEC = 3     # üks projectRoots juur; kohalik ketas võtab ms-e
ROOT_RETRY_SEC = 600          # aeglast juurt ei proovita iga avamise peale uuesti
_SLOW_ROOTS: dict[str, float] = {}


def _scan_root(root: str) -> list[tuple[str, str]] | None:
    """[(kaustanimi, tee)] juure alt või None, kui juur puudub/ripub.

    Skänn jookseb eraldi lõimes ajapiiranguga. Mõõdetud 09.09: vana juur
    `~/Documents/Claude/Projects/` (pilvesünki taga) võttis `scandir`-iga
    1010 s ja hoidis kogu `/api/tmux` päringut kinni — valija ei avanenud
    kunagi. Juur jääb seadistusse kulude atributsiooniks (see on ainult
    stringi-eesliide), aga siin ei tohi ta valijat tappa.
    """
    if time.time() - _SLOW_ROOTS.get(root, 0) < ROOT_RETRY_SEC:
        return None
    out: queue.Queue = queue.Queue(maxsize=1)

    def work() -> None:
        rows: list[tuple[str, str]] = []
        try:
            for e in sorted(os.scandir(os.path.expanduser(root)),
                            key=lambda e: e.name.lower()):
                try:
                    if not e.name.startswith(".") and e.is_dir():
                        rows.append((e.name, e.path))
                except OSError:
                    continue
            out.put(rows)
        except OSError:
            out.put(None)         # juur puudub sellel masinal — vaikselt edasi
        except Exception:         # noqa: BLE001 — lõim ei tohi midagi tõsta
            out.put(None)

    threading.Thread(target=work, daemon=True).start()
    try:
        return out.get(timeout=ROOT_SCAN_TIMEOUT_SEC)
    except queue.Empty:
        _SLOW_ROOTS[root] = time.time()
        return None


def list_projects() -> list[dict]:
    """`projectRoots` alamkaustad. Sama allikas, mida kulude atributsioon kasutab.

    ⚠️ KAHESTAMINE NIME JÄRGI, ESIMENE JUUR VÕIDAB. Sama nimi võib esineda mitmes
    juures (nt `~/dev/ccdash` = repo ja `~/Claude/Projects/ccdash` = ainult
    handoffid). Kaks ühesugust rida oleks kasutajale mõistatus ja teine neist
    viiks tmuxi valesse kausta, seega otsustab `projectRoots` JÄRJEKORD.
    """
    seen: dict[str, dict] = {}
    for root in (c.CONFIG.get("projectRoots") or []):
        if not isinstance(root, str) or not root:
            continue
        for display, path in (_scan_root(root) or []):
            if display in seen:
                continue
            name = tmux_name(display)
            if not name:
                continue          # nimest ei saa kehtivat sessiooninime
            seen[display] = {"display": display, "name": name, "path": path}
    return list(seen.values())


def collect() -> dict:
    """Ei tõsta KUNAGI erindit — vt mooduli päis."""
    try:
        return {
            "sessions": [{k: s.get(k) for k in SESSION_FIELDS}
                         for s in list_sessions()],
            "projects": list_projects(),
            "remoteHost": c.CONFIG.get("remoteHost") or None,
            "tmuxHost": _host(),
            "terminal": _terminal_uri() is not None,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 — dashboard ei tohi surra
        return {"sessions": [], "projects": [], "remoteHost": None,
                "tmuxHost": None, "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ käivitamine

def _free_chat_name(existing: set[str]) -> str | None:
    """Vaba nimi vabateemalisele vestlusele: chat, chat-2, chat-3, …"""
    for i in range(1, 100):
        cand = "chat" if i == 1 else f"chat-{i}"
        if cand not in existing:
            return cand
    return None


def resolve(kind: str, project: str | None) -> tuple[dict | None, str]:
    """(sihtkoht, viga). Sihtkoht = {"name":…, "path":…, "display":…}.

    Siin ja ainult siin muutub kliendi saadetud NIMI kausta TEEKS — teed valime
    alati oma nimekirjast, kliendilt teed vastu ei võeta.
    """
    if kind == "chat":
        # Vabateemaline vestlus ei kuulu ühessegi projekti — kodukaust.
        # Sessioonide loend ainult siin: projekti-haru ei vaja seda ja üle
        # ssh maksaks see terve käepigistuse.
        existing = {s["name"] for s in list_sessions()}
        name = _free_chat_name(existing)
        if not name:
            return None, "vabu chat-nimesid ei ole"
        return {"name": name, "path": os.path.expanduser("~"),
                "display": "uus chat"}, ""
    if kind == "project":
        for p in list_projects():
            if p["display"] == project:
                return dict(p), ""
        return None, "tundmatu projekt"
    return None, "tundmatu tüüp"


def start(target: dict) -> tuple[bool, str]:
    """(loodi_uus, viga). Olemasolevat sessiooni EI PUUDUTA.

    Kliki jooksva sessiooni peale ei tohi midagi juhtuda peale attach-rea
    andmise — `send-keys` tipiks sõnu jooksva Claude'i sisendisse.
    """
    name, path = target["name"], target["path"]
    if not _NAME_RE.match(name):
        return False, "lubamatu sessiooninimi"
    host = _host()
    code = _isdir(path)
    if code in (RC_TIMEOUT, 255, RC_MISSING):
        return False, _explain(code)
    if code != 0:
        return False, f"kausta ei ole masinas {host}" if host else "kausta ei ole"
    code, _ = _tmux("has-session", "-t", f"={name}")
    if code == 0:
        return False, ""                      # juba olemas — see ei ole viga
    if code in (RC_TIMEOUT, 255, RC_MISSING):
        return False, _explain(code)
    shell = _login_shell()
    # ÜKS kutse, mitte `new-session -d` + `send-keys`: viimane oleks võidujooks
    # paneeli shelli promptiga (eriti kui tmux-server alles käivitub) ja klahvid
    # võiksid kaduda või jõuda poolikusse shelli.
    code, _ = _tmux("new-session", "-d", "-s", name, "-c", path,
                    shell, "-lc", _PANE_CMD.format(shell=shell))
    if code != 0:
        return False, _explain(code)
    return True, ""


def _explain(code: int) -> str:
    """Miks tmux-kutse kukkus — kasutaja nägi „ei suutnud" ja ei saanud aru."""
    host = _host()
    if code == RC_TIMEOUT:
        return f"ssh {host} aegus (Tailscale?)" if host else "tmux aegus"
    if code == 255 and host:
        return f"ssh {host} ei vasta"
    if code == RC_MISSING:
        return (f"ssh või tmux puudub masinas {host}" if host
                else "tmux puudub selles masinas — seadista `tmuxHost`")
    return f"tmux ei suutnud sessiooni luua (rc={code})"


def _terminal_uri() -> str | None:
    """Seadistus `terminalUri` — mall, mille `open` annab redaktorile (nt
    Ritemarki laiendus `ritemark-ext/`), et see avaks tmux'iga ühendatud
    terminali. Puudub = vana käitumine (ainult kopeerimine)."""
    u = c.CONFIG.get("terminalUri")
    return u if isinstance(u, str) and "{session}" in u else None


def open_terminal(name: str) -> bool | None:
    """Ava redaktoris terminal sessiooniga `name`. None = pole seadistatud.

    Käivitab `open <uri>` SELLES masinas (kus ccdash jookseb ja ekraan on), mitte
    tmuxHost'is. Brauser seda teha ei saa: pärast `await fetch` on kasutaja žest
    kadunud ja Chrome lükkaks oma-skeemi lingi vaikselt tagasi.
    True tähendab ainult, et `open` võttis lingi vastu — kas terminal ka tekkis,
    seda siit näha ei ole (laiendus võib puududa).
    """
    tpl = _terminal_uri()
    if tpl is None:
        return None
    if not _NAME_RE.match(name):
        return False
    uri = tpl.replace("{session}", name)   # nimi on valge nimekirja järgi URL-ohutu
    try:
        return subprocess.run(["open", uri], timeout=5,
                              capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001 — vt mooduli päis
        return False


def attach_lines(name: str, remote_host: str | None) -> dict:
    """Käsurida, mille kasutaja terminali kleebib — kaks varianti.

    `local`  — terminal juba selles masinas (nt VS Code Remote-SSH).
    `remote` — terminal mujal, tuleb esmalt siia ssh-da. Puuduv `remoteHost`
               (seadistus) = varianti ei ole; host EI OLE koodi sees, sest
               repo on avalik ja masinanimi on seadistus.
    """
    local = f"tmux attach -t {name}"
    remote = f'ssh -t {remote_host} "{local}"' if remote_host else None
    return {"local": local, "remote": remote}
