# batchfiles

Windows batch and Unix shell helpers for **scheduled incremental sync**: register a task or `crontab` line that runs `uv run onelap2strava sync --incremental` from the repository root.

Prefer **`uv run onelap2strava auto-sync install`** from the repo root (`--mode` / `--every` / `--at`); it delegates to scripts in this folder. You can also run `install-scheduled-sync-*.cmd` / `.sh` directly.

See the main [README.md](../README.md) (scheduled sync) and [contexts/phase4-scheduled-sync.md](../contexts/phase4-scheduled-sync.md).
