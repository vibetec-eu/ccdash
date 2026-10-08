# ccdash terminal (Ritemark / VS Code extension)

Lets ccdash open an editor terminal attached to a tmux session on another
machine — one click in the session menu instead of `ssh` + `tmux attach` by hand.

ccdash's server opens `<scheme>://heikki.ccdash-terminal/attach?session=<name>`
(set `terminalUri` in `ccdash.config.json`); the extension runs
`ssh -t <host> "tmux attach -t <name>"`. The host is the setting
`ccdashTerminal.host` (default `mini`), never taken from the link.

Install: `npx @vscode/vsce package --allow-missing-repository` → `<editor>/bin/code --install-extension ccdash-terminal-0.1.1.vsix`.

---

**ET:** laiendus, mille kaudu ccdash avab redaktoris terminali, mis on juba teise masina
tmux-sessiooniga ühendatud. Host on seadistus `ccdashTerminal.host`, lingist seda ei loeta.
