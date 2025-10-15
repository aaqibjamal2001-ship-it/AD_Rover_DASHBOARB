# adrover-scripts

## SQLite Backup/Restore on Render (Free plan)

This app keeps your local SQLite data safe across Render restarts by restoring from a remote backup on startup and uploading a fresh backup on shutdown (and periodically while running).

### How it works
- On app startup: attempts to download the latest backup and replace the local database.
- After restore: initializes the schema if needed.
- While running: performs periodic backups (configurable interval).
- On app shutdown: uploads the latest backup.

### Providers
- Dropbox (simple, token-based)
- GitHub (requests-based, commits file into a repository)

### Environment variables (Render → Settings → Environment)
- Common:
  - `BACKUP_PROVIDER` = `dropbox` or `github`
  - `BACKUP_INTERVAL_SECONDS` = backup interval in seconds (default `600`)
  - `DB_FILE_PATH` = local SQLite file path (default `qr_counter.db`)

- Dropbox:
  - `DROPBOX_ACCESS_TOKEN` = your Dropbox access token
  - `DROPBOX_FILE_PATH` = remote path in Dropbox, e.g. `/db_backups/qr_counter.db`

- GitHub:
  - `GITHUB_TOKEN` = a Personal Access Token (PAT) with `repo` scope
  - `GITHUB_REPO` = `owner/repo` (e.g., `yourname/db-backups`)
  - `GITHUB_BRANCH` = branch to use (default `main`)
  - `GITHUB_PATH` = path in the repo (e.g., `db-backups/qr_counter.db`)

Do not hardcode secrets; always set them as Render environment variables.

### Files changed/added
- `backup_manager.py` – backup/restore logic, retries, periodic backups, FastAPI lifecycle integration.
- `Counter.py` – hooks the lifecycle events via `configure_backup_hooks`, and reads DB path from `DB_FILE_PATH` env var.
- `requirements.txt` – adds `dropbox`, `requests`, and Postgres deps.

### Edge cases handled
- No internet on startup: logs and continues with local DB if present.
- Missing remote backup: logs and continues; creates new DB if needed.
- Failed uploads/downloads: retries with exponential backoff.
- Consistent backup copy: uses SQLite backup API to avoid partial/corrupt uploads.

### Render setup steps
1. In your service, open Settings → Environment and add the variables above.
2. Redeploy. On first run, a local DB is created; on subsequent runs, the app restores from Dropbox automatically.
3. Verify logs show `[Backup] Restore completed from remote backup.` or appropriate warnings.

### Local DB path
By default, the app stores data in `qr_counter.db`. To change it, set `DB_FILE_PATH` and ensure `DROPBOX_FILE_PATH` points to the corresponding remote file.

## GitHub Setup Guide

1. Create a repository for backups (e.g., `db-backups`).
2. In GitHub → Settings → Developer settings → Personal access tokens, create a classic or fine‑grained PAT with permissions:
   - Classic: select `repo` scope.
   - Fine‑grained: grant read/write content access to your backups repo.
3. In Render → Settings → Environment, add:
   - `BACKUP_PROVIDER=github`
   - `GITHUB_TOKEN=<paste PAT>`
   - `GITHUB_REPO=<owner/repo>`
   - `GITHUB_BRANCH=main` (or your default)
   - `GITHUB_PATH=db-backups/<your db file name>`
   - `DB_FILE_PATH=<your local db file name>`
4. Redeploy your service.

Behavior:
- Startup: fetches raw file via GitHub API if present and restores locally.
- Runtime/shutdown: commits updated file to the repo path (creates or updates). Logs show `[Backup]` messages for status.

## Postgres (Neon) Setup Guide

If you prefer fully persistent storage without filesystem backups, you can use Neon (Postgres) instead of local SQLite.

### What changes
- `Counter.py` now detects `DATABASE_URL`. When set, it uses Postgres via SQLAlchemy and skips Dropbox/GitHub backups.
- When `DATABASE_URL` is not set, it defaults to local SQLite with backup hooks enabled.

### Steps
1. In Neon, copy your connection string (e.g., `postgresql://<user>:<password>@<host>/<db>?sslmode=require`).
2. In Render → your service → Settings → Environment:
   - Add `DATABASE_URL=<your Neon connection string>`
   - (Optional) Remove `BACKUP_PROVIDER`, `DROPBOX_*` and `GITHUB_*` vars if not using SQLite backups.
3. Redeploy your service.

### Notes
- Tables are created automatically on first run.
- Existing local SQLite data won’t be auto-migrated; if you need it, export from `qr_counter.db` and import to Neon, or ask to add a simple migration script.
- Local development can still use SQLite by unsetting `DATABASE_URL`.