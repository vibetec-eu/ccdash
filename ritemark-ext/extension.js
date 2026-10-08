// ccdash → editor terminal. ccdash's server opens
//   <scheme>://heikki.ccdash-terminal/attach?session=<name>
// and this extension opens a terminal running `ssh -t <host> tmux attach -t <name>`.
//
// The host is a SETTING, never read from the URI: any web page can open a
// custom-scheme link, so the URI may only pick which session, not where to ssh.
const vscode = require('vscode');

// Same rule as tmux_lib._NAME_RE in ccdash — the name goes into a remote shell line.
const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,31}$/;
const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

function attach(session) {
  if (!NAME_RE.test(session)) {
    vscode.window.showErrorMessage(`ccdash: lubamatu sessiooninimi „${session}"`);
    return;
  }
  const host = vscode.workspace.getConfiguration('ccdashTerminal').get('host', 'mini');
  if (!HOST_RE.test(host)) {
    vscode.window.showErrorMessage(`ccdash: lubamatu host „${host}"`);
    return;
  }
  const name = `${session} @${host}`;
  // Already open and alive → just focus it. A terminal whose ssh died stays in
  // the list with exitStatus set; that one is replaced, not focused.
  const live = vscode.window.terminals.find(t => t.name === name && t.exitStatus === undefined);
  if (live) { live.show(); return; }
  // ssh joins its arguments into ONE line for the remote login shell (zsh on
  // the Mini), so the tmux part is a single string — and without the `=name`
  // exact-match prefix, which zsh would expand as a command path.
  const term = vscode.window.createTerminal({
    name,
    shellPath: '/usr/bin/ssh',
    shellArgs: ['-t', host, `tmux attach -t ${session}`],
    location: vscode.TerminalLocation.Editor,
  });
  term.show();
}

function activate(context) {
  context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri) {
      if (uri.path !== '/attach') {
        vscode.window.showErrorMessage(`ccdash: tundmatu käsk ${uri.path}`);
        return;
      }
      attach(new URLSearchParams(uri.query).get('session') || '');
    },
  }));
}

module.exports = { activate, deactivate() {} };
