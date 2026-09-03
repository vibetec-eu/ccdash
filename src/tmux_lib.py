"""tmux-sessioonid ccdash'i jaoks: näita, mis jookseb, ja käivita uus.

Miks see olemas on. Uue Claude-sessiooni alustamine on neli käsku käsitsi
(`ssh <host>` → `tmux new -s <nimi>` → `cd <kaust>` → `claude`), ja kuskilt ei
paista, MIS juba jookseb — nii tekib kogemata teine sessioon sama töö peale.
ccdash jookseb samas masinas, kus tmux, seega ta näeb ja oskab käivitada.

⛔ SEE ON AINUS MOODUL, MIS KÄIVITAB KASUTAJA VALIKUL PROTSESSI. Reeglid:
  * Klient ei saada KUNAGI kausta teed. Ta ütleb projekti NIME; tee otsitakse
    siin, `list_projects()` nimekirjast. Nii ei ole tee-injektsiooni pinda.
  * Sessiooninimi tuletatakse serveris ja peab läbima `_NAME_RE`. `:` ja `.`
    on tmux-il tähendusega — valge nimekiri, mitte põgenemine.
  * `subprocess.run([...])` listina, mitte kunagi `shell=True`.

⛔ Ükski funktsioon siin ei tõsta erindit — sama reegel mis `jobs_lib`-il.
   ccdash'i `refresher()` peidaks erindi peale KOGU dashboardi veabänneri taha.
"""
from __future__ import annotations

import os
import pwd
import re
import subprocess
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


def _login_shell() -> str:
    """Kasutaja login-shell. `$SHELL` võib launchd keskkonnas puududa."""
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except Exception:
        return "/bin/sh"


def _tmux(*args: str) -> tuple[int, str]:
    """(returncode, stdout). Ei tõsta erindit; tmux puudu → (127, "")."""
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_SEC)
        return r.returncode, r.stdout
    except Exception:
        return 127, ""


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
        try:
            entries = sorted(os.scandir(os.path.expanduser(root)),
                             key=lambda e: e.name.lower())
        except OSError:
            continue              # juur puudub sellel masinal — vaikselt edasi
        for e in entries:
            if e.name.startswith(".") or e.name in seen:
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            name = tmux_name(e.name)
            if not name:
                continue          # nimest ei saa kehtivat sessiooninime
            seen[e.name] = {"display": e.name, "name": name, "path": e.path}
    return list(seen.values())


def collect() -> dict:
    """Ei tõsta KUNAGI erindit — vt mooduli päis."""
    try:
        return {
            "sessions": [{k: s.get(k) for k in SESSION_FIELDS}
                         for s in list_sessions()],
            "projects": list_projects(),
            "remoteHost": c.CONFIG.get("remoteHost") or None,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 — dashboard ei tohi surra
        return {"sessions": [], "projects": [], "remoteHost": None,
                "error": f"{type(e).__name__}: {e}"}


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
    existing = {s["name"] for s in list_sessions()}
    if kind == "chat":
        # Vabateemaline vestlus ei kuulu ühessegi projekti — kodukaust.
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
    if not os.path.isdir(path):
        return False, "kausta ei ole"
    if _tmux("has-session", "-t", f"={name}")[0] == 0:
        return False, ""                      # juba olemas — see ei ole viga
    shell = _login_shell()
    # ÜKS kutse, mitte `new-session -d` + `send-keys`: viimane oleks võidujooks
    # paneeli shelli promptiga (eriti kui tmux-server alles käivitub) ja klahvid
    # võiksid kaduda või jõuda poolikusse shelli.
    code, _ = _tmux("new-session", "-d", "-s", name, "-c", path,
                    shell, "-lc", _PANE_CMD.format(shell=shell))
    if code != 0:
        return False, "tmux ei suutnud sessiooni luua"
    return True, ""


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
