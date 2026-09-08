# ccdash

> A local dashboard for Claude Code usage — and a documented account of what `ccusage`
> silently gets wrong.

Built at **[vibetec.eu](https://vibetec.eu)** — AI-orchestrated MVP sprints, idea to
working product in 2–4 weeks.

**[English](#english) · [Eesti](#eesti)**

![ccdash](docs/screenshot.png)

*Screenshot taken in demo mode (`CCDASH_DEMO=1`): the numbers are real, project and
session names are replaced.*

---

## English

Which project is eating your quota, how bloated your sessions have grown, and what the
month has cost so far. One Python file serves the whole UI; no dependencies beyond the
standard library and `npx ccusage`.

> **Note on language:** the code comments, docstrings and the dashboard UI are in
> Estonian. This README covers the substance in full, so you don't need to read the
> Estonian to understand what the tool knows.

### Why this exists

The dashboard is decoration. The value is in the header of
[`src/ccusage_lib.py`](src/ccusage_lib.py): **three ways `ccusage` silently reports wrong
numbers.** All three were measured, not theorized.

**1. Pricing disappears.** Claude Code's `.jsonl` transcripts contain no `costUSD` field
for subscription usage, so cost is *always* computed from a price table fetched over the
network. When that fetch fails you don't get an error — you get `totalCost: 0` alongside
perfectly normal token counts. That is how a log line reading `$0.00 / 129,759,914 tok`
appeared for a day that actually cost `$99.26`.
→ `fetch()` validates: tokens present but cost zero means broken. It retries `--offline`
(from cache) and only then raises.

**2. Pricing can disappear *partially*** — and this one is nastier. `ccusage` 20.0.19's
built-in offline table was missing `claude-opus-5`: `--offline` reported $1955.45 when the
true figure was $3266.19. A row-total check does not save you, because on a mixed day
(`claude-opus-5` $0.00 + `claude-sonnet-5` $16.16) `totalCost` is non-zero and the check
passes while $237 of usage goes unpriced.
→ `_rows_look_priced()` validates **per model** (`modelBreakdowns`), not per row total.
→ `~/.claude/ccusage.json` supplies the missing prices via `pricingOverrides`; verified
that offline+config matches online to the cent, including cache surcharges.

**3. History is deleted.** Claude Code prunes old transcripts, so historical totals
*shrink over time* — one month read $2606 when logged and $2187 a few weeks later. You
therefore cannot compute a month-to-date total from `ccusage`; it has to be summed from
your own log.
→ see `month_total_from_log()` and the daily logger below.

On top of that, `check_pricing_drift()` compares your price overrides against the LiteLLM
table and warns when they diverge — comparing each model against **its own** entry, with a
fallback anchor only for models LiteLLM doesn't know yet.

### Install

Requirements: macOS, Python 3.11+, Node (`npx`), optionally Google Chrome (for the app
window).

```bash
git clone https://github.com/vibetec-eu/ccdash.git ~/dev/ccdash
cd ~/dev/ccdash && ./install.sh
```

`install.sh` symlinks the code into `~/.claude/scripts/`, installs two launchd jobs
(`eu.vibetec.ccdash`, `eu.vibetec.token-usage-daily`) and starts the server. **It
overwrites nothing** — if a target exists and isn't a symlink into this repo, it stops and
tells you what's in the way.

> ⚠️ If you already have ccdash installed by hand (real files, not symlinks, under
> `~/.claude/scripts/`), **don't run `install.sh`** — replace those files with symlinks
> yourself, or keep what you have. The script is meant for a fresh clone.

Removal: `./uninstall.sh` — your log and config are kept.

### Configuration

`~/.claude/ccdash.config.json`, sample in
[`examples/ccdash.config.json`](examples/ccdash.config.json). Every field is optional.

| Field | Effect |
|---|---|
| `projectRoots` | Directories whose subdirectories are projects. **List every tree** where your projects live. **Order matters** — see below. |
| `remoteHost` | Host name you reach this machine through (`ssh <host>`). Adds the `ssh -t <host> "tmux attach …"` variant to the session picker. Omit it and only the local command is offered. |
| `peers` | Other machines that also run Claude Code, as ssh host aliases (`["mini"]`). Their `~/.claude/projects` is mirrored here and counted — see **Multiple machines** below. |
| `timezone` | Day-boundary grouping. Defaults to the system zone. |
| `thresholds.monthEur` | Monthly warning threshold shown on the dashboard (EUR). |
| `thresholds.dayUsd` / `monthUsd` | Thresholds for the daily logger's macOS notification (USD). |

**Order matters when a name appears in two roots.** The session picker lists the
subdirectories of every root, and a project name can legitimately exist twice — say
`~/dev/thing` (the repo) and `~/Projects/thing` (its notes). Two identical rows would be a
riddle, and one of them would open a terminal in the wrong place, so **duplicates are
dropped and the first root wins**. Put the tree you actually work in first. Cost
attribution is unaffected: both paths report the same project name, which is correct —
they are the same project.

**Multiple machines (`peers`).** If Claude Code runs on more than one computer, each one
writes its own `~/.claude/projects`, and ccusage only reads the local tree — so a dashboard
on either machine shows a fraction of the truth (measured: two machines, almost exactly
half each). With `peers` set, ccdash rsyncs every listed host's `~/.claude/projects` into
`~/.claude/peers/<host>/projects/` every 5 minutes and hands ccusage both trees through a
comma-separated `CLAUDE_CONFIG_DIR`. ccusage deduplicates sessions, so nothing is counted
twice. Requirements: passwordless `ssh <host>` (the alias from your `~/.ssh/config`) and
`rsync` on both ends. The copy is persistent: if the peer is unreachable, the last copy
stays in use and the dashboard shows how old it is (yellow card after 30 minutes). Set
`peers` on both machines pointing at each other and whichever dashboard you open shows the
total. The daily logger refreshes the copy too, so the archive holds the combined figure.
Nothing is deleted from the copy — a session removed on the peer stays here as archive.

**Why `projectRoots` has to be listed by hand.** Claude Code stores transcripts in a
directory named after a slug of the working path: `/Users/x/Projects/web` becomes
`-Users-x-Projects-web`. That transform is **not reversible** — `.` and `@` also become
hyphens, so a hyphen could have been `/`, `.`, `@` or a real hyphen. ccdash therefore reads
the project from the transcript's `cwd` field, which requires knowing which paths are
project roots. Deriving them from the home directory does not work.

### Usage

```bash
~/.claude/scripts/ccdash                 # server + browser
~/.claude/scripts/ccdash-open            # Chrome app window (separate profile)
CCDASH_DEMO=1 ~/.claude/scripts/ccdash   # demo mode, see below
python3 src/token_usage_daily.py --dry-run   # print today's log line without writing
```

The server is lazy: if nobody polls it, it sleeps and never invokes `ccusage`. That's why
it is safe to keep it under launchd permanently.

**Demo mode.** `CCDASH_DEMO=1` replaces project names with `demo1…demoN` (ordered by cost,
so numbering is stable) and session titles with generic ones. Costs, tokens and timestamps
stay real. Meant for screenshots and presentations — session titles come from the `aiTitle`
field and are free-form sentences about real work.

**Daily logger** (launchd, 09:00) appends a line to
`~/.claude/logs/token-usage-daily.log`. That file is the **only durable usage history** —
`ccusage` loses old days and they cannot be reconstructed. Don't delete it, and don't try
to "rebuild it from `ccusage`".

### Sessions you cannot otherwise see

Claude Code has two kinds of session that a terminal tab does not show you, and both cost
money while you are not looking. ccdash surfaces them in the header.

**Background jobs** (`claude --bg`) run in a daemon with no terminal at all. They appear in
neither the editor's tab bar nor `/tasks`; the only places they exist are `claude agents`
and `~/.claude/jobs/<id>/state.json`. Four of them once sat blocked for weeks.

- A **waiting bar** appears at the top of the page — and *only* when something is actually
  waiting. It shows the id, what the job is waiting for, how many days it has been stuck,
  and a copyable `claude attach <id>`.
- Existing session rows carry `bg`, `no tty`, `⏳ waiting` and `web ↗` badges.
- The state is read from `state.json` on every refresh (~33 ms) but `claude agents --json`
  is the **authority** on `state` and is consulted at most every 5 minutes — a job's
  `state.json` can be stale (measured: "working" in the file, "blocked" per the CLI).
- A job counts as waiting when `state == blocked` **or** `needs` is set. `needs` alone is
  not enough: blocked jobs have been observed whose `needs` had gone empty.

**The session picker** (`Sessions ▾`) lists the tmux sessions running on this machine —
click one to copy its `tmux attach` line — then every project under `projectRoots`, then
`+ new chat` for work that belongs to no project. Clicking a project **starts** the session
here (`tmux new-session` in the project directory, running `claude` through a login shell)
and hands you the attach command. An already-running session is never touched: no keys are
sent into a live Claude prompt.

The toggle at the top of the menu decides whether the copied line is prefixed with
`ssh -t <remoteHost>`. Turn it on when your terminal is already on this machine (an
editor's remote-SSH terminal), off when it is somewhere else. The choice is remembered.

> **This is the one endpoint that starts a process,** so it is worth knowing how it is
> fenced. The browser never sends a path — it sends a project *name*, and the path is
> looked up server-side from the configured roots. The session name is derived on the
> server and must pass a strict allowlist. `POST` requires `Content-Type: application/json`
> (which forces a CORS preflight that is never answered) plus `Origin` and `Host`
> allowlists. Demo mode disables the picker entirely. **What none of this stops** is a
> browser extension with broad permissions: extensions bypass CORS, and no localhost
> server can prevent that.

### What it does not do

- **No percentage of your limit.** Anthropic does not publish the limit anywhere
  machine-readable (checked: transcripts, logs, caches, `~/.claude.json`). ccdash shows
  actual volumes and reset times instead. For the percentage: claude.ai → Settings → Usage.
- **Not cross-platform.** launchd, `osascript` notifications and the Chrome app window are
  macOS-specific. The server itself (`python3 src/ccdash.py --port 8787`) runs anywhere.
- **Sends nothing anywhere.** Everything stays on your machine; the network is touched only
  for the ECB exchange rate, the LiteLLM price table and `npx`.

### Who made this

[vibetec.eu](https://vibetec.eu) — a mini-accelerator: an idea owner without a dev team
gets a working product in 2–4 weeks, built by an AI-orchestrated team. ccdash is a
by-product of measuring what that actually costs.

MIT.

---

## Eesti

Milline projekt sinu kvooti sööb, kui suureks on sessioonid paisunud ja kui palju kuu seni
maksnud on. Üks Pythoni fail serveerib kogu HTML-i; sõltuvusi peale standardteegi ja
`npx ccusage` ei ole.

### Miks see olemas on

Dashboard ise on kaunistus. Väärtus on [`src/ccusage_lib.py`](src/ccusage_lib.py) päises:
**kolm viisi, kuidas `ccusage` vaikselt valesid numbreid annab** — kõik kolm mõõdetud,
mitte teoreetilised.

**1. Hinnakiri kaob.** Claude Code'i `.jsonl`-id ei sisalda tellimuskasutuse puhul
`costUSD` välja, seega kulu arvutatakse **alati** võrgust tõmmatud hinnatabelist. Tõmbe
ebaõnnestumisel ei tule viga — tuleb `totalCost: 0` ja täiesti normaalsed tokeninumbrid.
Nii tekkis logisse rida `$0.00 / 129 759 914 tok` päeva kohta, mis päriselt maksis $99.26.
→ `fetch()` valideerib: tokeneid on, aga kulu null = katki. Proovib `--offline`
(vahemälust) ja alles siis annab vea.

**2. Hinnakiri võib kaduda OSALISELT** — ja see on salakavalam. `ccusage` 20.0.19
sisseehitatud offline-tabelis puudus `claude-opus-5`: `--offline` andis $1955.45, õige oli
$3266.19. Rea kogusumma kontroll ei päästa, sest segapäeval (`claude-opus-5` $0.00 +
`claude-sonnet-5` $16.16) on `totalCost` nullist erinev — kontroll läheb läbi ja $237 jääb
hinnastamata.
→ `_rows_look_priced()` kontrollib **mudelite kaupa** (`modelBreakdowns`), mitte rea summat.
→ `~/.claude/ccusage.json` annab puuduvad hinnad `pricingOverrides` kaudu; kontrollitud, et
offline+config ühtib online'iga sendi täpsusega, sh vahemälu lisatasud.

**3. Ajalugu kustub.** Claude Code koristab vanu transkripte, nii et ajaloolised summad
**kahanevad ajas** — üks kuu oli logi järgi $2606 ja paar nädalat hiljem näitas `ccusage`
$2187. Kuu summat ei tohi seepärast `ccusage`'ist arvutada; see tuleb liita oma logist.
→ vt `month_total_from_log()` ja päevalogijat allpool.

Lisaks võrdleb `check_pricing_drift()` sinu hinnaülekirjutusi LiteLLM-i tabeliga ja
hoiatab, kui need lahku lähevad. Võrdlus käib mudeli **enda** kirje vastu; ankrut
kasutatakse ainult mudelil, mida LiteLLM veel ei tunne.

### Paigaldus

Nõuded: macOS, Python 3.11+, Node (`npx`), soovi korral Google Chrome (äpiaken).

```bash
git clone https://github.com/vibetec-eu/ccdash.git ~/dev/ccdash
cd ~/dev/ccdash && ./install.sh
```

`install.sh` teeb symlingid `~/.claude/scripts/` alla, paigaldab kaks launchd-jobi
(`eu.vibetec.ccdash`, `eu.vibetec.token-usage-daily`) ja käivitab serveri. **Ta ei kirjuta
midagi üle** — kui mõni fail on juba olemas ja ei ole symlink sellesse repo, ta peatub ja
ütleb, mis takistab.

> ⚠️ Kui sul on ccdash juba käsitsi paigaldatud (päris failid, mitte symlingid
> `~/.claude/scripts/` all), **ära jooksuta `install.sh`** — asenda failid ise
> symlinkidega või jäta vanad alles. Skript on värske klooni jaoks.

Eemaldus: `./uninstall.sh` — logi ja seadistus jäävad alles.

### Seadistus

`~/.claude/ccdash.config.json`, näidis
[`examples/ccdash.config.json`](examples/ccdash.config.json). Kõik väljad on valikulised.

| Väli | Mida teeb |
|---|---|
| `projectRoots` | Kaustad, mille alamkaustad on projektid. **Loetle kõik puud**, kus projektid elavad. **Järjekord loeb** — vt allpool. |
| `remoteHost` | Masinanimi, mille kaudu sa selle masinani jõuad (`ssh <host>`). Lisab sessioonivalijasse variandi `ssh -t <host> "tmux attach …"`. Puudumisel pakutakse ainult kohalikku käsku. |
| `peers` | Teised masinad, kus Claude Code samuti jookseb, ssh-aliastena (`["mini"]`). Nende `~/.claude/projects` peegeldatakse siia ja loetakse kokku — vt **Mitu masinat** allpool. |
| `timezone` | Päevade grupeerimine. Puudumisel süsteemi oma. |
| `thresholds.monthEur` | Kuu hoiatuslävi dashboardil (EUR). |
| `thresholds.dayUsd` / `monthUsd` | Päevalogija macOS-teate läved (USD). |

**Järjekord loeb, kui sama nimi on kahes juures.** Sessioonivalija loetleb iga juure
alamkaustad ja projektinimi võib ausalt esineda kaks korda — näiteks `~/dev/asi` (repo) ja
`~/Projects/asi` (selle märkmed). Kaks ühesugust rida oleks kasutajale mõistatus ja üks
neist avaks terminali vales kohas, seega **kordused visatakse välja ja esimene juur
võidab**. Pane ettepoole see puu, kus sa päriselt töötad. Kulude omistamist projektidele
see ei puuduta: mõlemad teed annavad sama projektinime, mis ongi õige — tegu on ühe
projektiga.

**Mitu masinat (`peers`).** Kui Claude Code jookseb rohkem kui ühes arvutis, kirjutab
igaüks oma `~/.claude/projects` ja ccusage loeb ainult kohalikku puud — kummagi masina
dashboard näitab siis osa tõest (mõõdetud: kaks masinat, peaaegu täpselt pool kumbki).
`peers` seadistusega rsync'ib ccdash iga loetletud hosti `~/.claude/projects` kausta
`~/.claude/peers/<host>/projects/` iga 5 minuti järel ja annab ccusage'ile mõlemad puud
komaga eraldatud `CLAUDE_CONFIG_DIR`-is. ccusage dedupib sessioonid, midagi ei loeta
topelt. Eeldused: paroolita `ssh <host>` (alias sinu `~/.ssh/config`-ist) ja `rsync`
mõlemas otsas. Koopia on püsiv: kui teine masin ei vasta, jääb viimane koopia kasutusse ja
dashboard näitab, kui vana see on (kollane kaart alates 30 minutist). Sea `peers` mõlemas
masinas teineteise peale ja kumb dashboard sa ka avad, näitab see kogusummat. Ka
päevalogija värskendab koopia, seega arhiivis on koondsumma. Koopiast ei kustutata
midagi — teises masinas kustutatud sessioon jääb siia arhiivina alles.

**Miks `projectRoots` tuleb käsitsi loetleda.** Claude Code hoiab transkripte kaustanime
järgi, kus teest on tehtud slug: `/Users/x/Projects/veeb` → `-Users-x-Projects-veeb`.
Teisendus **ei ole pööratav** — sidekriipsuks muutuvad ka `.` ja `@`, seega tagasi ei saa.
Seepärast loeb ccdash projekti transkripti `cwd`-väljast ja peab teadma, millised teed on
projektijuured. Nende tuletamine kodukaustast ei tööta.

### Kasutus

```bash
~/.claude/scripts/ccdash                 # server + brauser
~/.claude/scripts/ccdash-open            # Chrome'i äpiaken (eraldi profiil)
CCDASH_DEMO=1 ~/.claude/scripts/ccdash   # demo-režiim, vt allpool
python3 src/token_usage_daily.py --dry-run   # päevarida, ilma kirjutamata
```

Server on laisk: kui keegi ei polli, ta magab ega jooksuta `ccusage`'it. Just seetõttu on
turvaline hoida teda launchd all kogu aeg.

**Demo-režiim.** `CCDASH_DEMO=1` asendab projektinimed `demo1…demoN` (kulu järjekorras,
seega nummerdus on stabiilne) ja sessioonipealkirjad üldistega. Kulud, tokenid ja ajad
jäävad päris. Mõeldud ekraanipildi või esitluse jaoks — sessioonipealkirjad tulevad
`aiTitle` väljast ja on vabas vormis laused päris tööst.

**Päevalogi.** launchd kirjutab iga päev kell 9 ühe rea faili
`~/.claude/logs/token-usage-daily.log`.
See on **ainus püsiv kasutusajalugu** — `ccusage` kaotab vanad päevad ja neid ei saa
taastada. Ära kustuta seda faili ega "ehita uuesti üles".

### Sessioonid, mida sa mujalt ei näe

Claude Code'il on kaht sorti sessioone, mida terminali tabiriba ei näita, ja mõlemad
maksavad raha ajal, mil sa neid ei vaata. ccdash toob nad päisesse.

**Taustatööd** (`claude --bg`) jooksevad daemonis, ilma igasuguse terminalita. Neid ei ole
ei redaktori tabiribal ega `/tasks` all; ainsad kohad, kus nad eksisteerivad, on
`claude agents` ja `~/.claude/jobs/<id>/state.json`. Neli sellist seisis kord nädalaid
blokeerituna.

- Lehe tippu ilmub **ootajate riba** — ja ainult siis, kui keegi päriselt ootab. Seal on
  id, mida töö ootab, mitu päeva ta on seisnud, ja kopeeritav `claude attach <id>`.
- Olemasolevatel sessiooniridadel on märgid `bg`, `tabita`, `⏳ ootab` ja `veeb ↗`.
- Seisu loetakse `state.json`-ist igal värskendusel (~33 ms), aga `state` välja
  **autoriteet** on `claude agents --json`, mida küsitakse maksimaalselt iga 5 minuti
  tagant — töö `state.json` võib olla aegunud (mõõdetud: failis „working", CLI järgi
  „blocked").
- Töö on ootaja, kui `state == blocked` **või** `needs` on täidetud. Ainult `needs`-ist ei
  piisa: nähtud on blokeeritud töid, mille `needs` oli vahepeal tühjaks läinud.

**Sessioonivalija** (`Sessioonid ▾`) loetleb selles masinas jooksvad tmux-sessioonid —
klikk kopeerib nende `tmux attach` rea —, siis kõik projektid `projectRoots` alt, siis
`+ uus chat` töö jaoks, mis ei kuulu ühessegi projekti. Klikk projektil **käivitab**
sessiooni siin (`tmux new-session` projektikaustas, `claude` login-shelli kaudu) ja annab
attach-käsu. Juba jooksvat sessiooni ei puututa kunagi: ühtegi klahvi ei saadeta elava
Claude'i sisendisse.

Menüü ülaosa lüliti otsustab, kas kopeeritava rea ees on `ssh -t <remoteHost>`. Pane sisse,
kui su terminal on juba selles masinas (redaktori remote-SSH terminal), välja siis, kui ta
on mujal. Valik jääb meelde.

> **See on ainus endpoint, mis käivitab protsessi,** seega tasub teada, kuidas ta on
> piiratud. Brauser ei saada kunagi teed — ta saadab projekti *nime* ja tee otsitakse
> serveris seadistatud juurte hulgast. Sessiooninimi tuletatakse serveris ja peab läbima
> range valge nimekirja. `POST` nõuab `Content-Type: application/json` (mis sunnib
> CORS-preflighti, millele kunagi ei vastata) ning `Origin` ja `Host` valget nimekirja.
> Demo-režiimis on valija täielikult väljas. **Mida see kõik EI peata:** laia õigusega
> brauserilaiendus — laiendused lähevad CORS-ist mööda ja ükski localhost-server ei saa
> seda takistada.

### Mida see EI tee

- **Ei näita protsenti limiidist.** Anthropic ei avalda limiiti üheski masinloetavas kohas
  (kontrollitud: transkriptid, logid, vahemälu, `~/.claude.json`). ccdash näitab tegelikke
  mahtusid ja lähtestamisaegu. Protsent: claude.ai → Settings → Usage.
- **Ei ole mitmeplatvormiline.** launchd, `osascript`-teated ja Chrome'i äpiaken on macOS-i
  omad. Server ise (`python3 src/ccdash.py --port 8787`) töötab igal pool.
- **Ei saada andmeid kuhugi.** Kõik jääb masinasse; võrku läheb ccdash ainult EKP kursi,
  LiteLLM-i hinnatabeli ja `npx` pärast.

### Kes tegi

[vibetec.eu](https://vibetec.eu) — minikiirendi: idee omanik ilma dev-tiimita saab töötava
toote 2–4 nädalaga, selle ehitab AI-orkestreeritud tiim. ccdash sündis kõrvalsaadusena
selle mõõtmisest, mis see päriselt maksab.

MIT.
