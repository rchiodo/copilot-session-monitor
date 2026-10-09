"""Copilot Session Monitor -- Python implementation.

Package layout mirrors the original Node.js ``src/*.mjs`` modules 1:1 so the
port can be reviewed file-by-file against the original implementation:

  events.mjs         -> pymonitor/events.py
  background.mjs     -> pymonitor/background.py
  engine.mjs          -> pymonitor/engine.py
  hierarchy.mjs       -> pymonitor/hierarchy.py
  families.mjs        -> pymonitor/families.py
  source.mjs          -> pymonitor/source.py
  local-report.mjs    -> pymonitor/local_report.py
  protocol.mjs        -> pymonitor/protocol.py
  configuration.mjs   -> pymonitor/configuration.py
  collector.mjs       -> pymonitor/collector.py
  reporter.mjs        -> pymonitor/reporter.py
  watcher.mjs         -> pymonitor/watcher.py
  server.mjs          -> pymonitor/server.py
  actions.mjs         -> pymonitor/actions.py
  windows/tray.ps1    -> pymonitor/console_bridge.py (console rewrite; an
                          earlier native-tray rewrite, tray_native.py, was
                          later replaced -- see docs/porting-notes.md)

``pymonitor/launcher.py`` has no ``.mjs`` equivalent: it is the testable
start/stop/live-detection logic absorbed from the PowerShell-only
``scripts/Start-Role.ps1``/``scripts/Stop-Role.ps1``, which the Node app
never had a module for. The root-level ``*.py`` PEP 723 scripts (e.g.
``start-host.py``) are its thin CLI wrappers; see ``docs/porting-notes.md``.
"""

__version__ = "0.1.0"
