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
  windows/tray.ps1    -> pymonitor/tray.py (native rewrite, not a wrapper)
"""

__version__ = "0.1.0"
