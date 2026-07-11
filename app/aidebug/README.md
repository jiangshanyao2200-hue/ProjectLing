# AITermux aidebug

`aidebug` is the shared runtime debug chain for AITermux shell integration.

Scope:
- `motd` startup and launcher stability
- `zshrc` source stability
- `aitermux-bootstrap`
- `projectling` integration and terminal collaboration logs
- Windows/WSL launcher compatibility checks for ProjectLing desktop builds

`projectying` is intentionally excluded because it owns a separate `Aidebug` chain.

Layout:
- `logs/startup.log` - shared chronological startup stream
- `logs/motd.log` - motd and launcher events
- `logs/motd-zshrc-smoke.jsonl` - automated motd/zshrc smoke summaries
- `logs/zshrc.log` - zshrc source events
- `logs/bootstrap.log` - bootstrap events
- `logs/projectling.log` - projectling runner events
- `logs/aidebug-health.json` - latest chain health report
- `logs/aidebug-health.jsonl` - historical chain health reports
- `logs/aidebug-windows.json` - latest Windows adapter and CLI text layout report
- `logs/ui-screenshot-*.png` - optional Windows UI screenshots captured by `aidebug windows --screenshot`
- `projectling/terminal output/` - projectling collaborative terminal logs
- `legacy/` - old logs moved from previous scattered locations

Commands:
- `aidebug windows` - check WSL/runtime compatibility, Windows launcher EXE freshness, and narrow-width CLI text layout
- `aidebug windows --repair` - safely refresh WSL compatibility symlinks and remove lowercase `projectling` only when it is a log-only residue
- `aidebug windows --screenshot` - additionally capture a UI screenshot when visual inspection is needed
- `aidebug/aidebug.ps1 windows` - run the Windows-native adapter check without WSL
- `aidebug motd-zshrc-smoke` - run non-TTY motd, zshrc hook, and PTY launcher smoke tests
- `aidebug projectling-auto` - run the Project Ling toolchain regression loop
- `aidebug health` - score each debug/runtime chain and write `notes/aidebug-health.md`
- `../run.sh cleanup` - force runtime housekeeping for ProjectLing temp archives and bounded logs
- `../run.sh cleanup --deep` - additionally remove Python bytecode caches when storage cleanup matters more than startup speed

Retention:
- Keep recent logs and state for debugging.
- Treat `tmp/`, Python caches, and downloaded package archives as disposable runtime output.
