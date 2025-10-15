import os
import asyncio
import sqlite3
import time
from typing import Optional, Callable

try:
    import dropbox
    from dropbox.exceptions import ApiError, AuthError
    from dropbox.files import WriteMode
except Exception:
    dropbox = None
    ApiError = AuthError = WriteMode = None

try:
    import requests
except Exception:
    requests = None


def _log(msg: str):
    print(f"[Backup] {msg}")


def safe_sqlite_copy(src_path: str, dst_path: str) -> bool:
    """Create a consistent copy of a SQLite database.

    Uses the SQLite backup API to avoid corrupt or partial copies.
    Returns True on success, False otherwise.
    """
    try:
        os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
        src_conn = sqlite3.connect(src_path)
        dst_conn = sqlite3.connect(dst_path)
        with dst_conn:
            src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        return True
    except Exception as e:
        _log(f"safe_sqlite_copy failed: {e}")
        return False


class DropboxBackupClient:
    def __init__(self, token: str, remote_path: str):
        if not dropbox:
            raise RuntimeError("dropbox SDK not available; ensure 'dropbox' is in requirements")
        if not token:
            raise ValueError("DROPBOX_ACCESS_TOKEN is required")
        if not remote_path:
            raise ValueError("DROPBOX_FILE_PATH is required")
        self.dbx = dropbox.Dropbox(token, timeout=20)
        self.remote_path = remote_path

    def exists(self) -> bool:
        try:
            self.dbx.files_get_metadata(self.remote_path)
            return True
        except ApiError as e:
            # Not found
            if getattr(e, 'error', None) and str(e.error).find('path/not_found') != -1:
                return False
            return False
        except Exception:
            return False

    def download_to(self, local_path: str) -> bool:
        try:
            md, res = self.dbx.files_download(self.remote_path)
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            with open(local_path, 'wb') as f:
                f.write(res.content)
            return True
        except Exception as e:
            _log(f"Dropbox download failed: {e}")
            return False

    def upload_from(self, local_path: str) -> bool:
        try:
            with open(local_path, 'rb') as f:
                data = f.read()
            self.dbx.files_upload(data, self.remote_path, mode=WriteMode('overwrite'))
            return True
        except Exception as e:
            _log(f"Dropbox upload failed: {e}")
            return False


class GitHubBackupClient:
    """Backup client using GitHub REST API to store file in a repository.

    Uploads use PUT /repos/{owner}/{repo}/contents/{path} with base64 content.
    Downloads use GET /repos/{owner}/{repo}/contents/{path} with Accept raw.
    """

    def __init__(self, token: str, repo: str, branch: str, remote_path: str):
        if not requests:
            raise RuntimeError("requests library not available; ensure 'requests' is in requirements")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        if not repo or "/" not in repo:
            raise ValueError("GITHUB_REPO must be in the form 'owner/repo'")
        owner, repo_name = repo.split("/", 1)
        self.owner = owner
        self.repo = repo_name
        self.branch = branch or "main"
        # GitHub API expects path without leading '/'
        self.remote_path = remote_path.lstrip("/") if remote_path else None
        if not self.remote_path:
            raise ValueError("GITHUB_PATH is required")
        self.token = token
        self.base = "https://api.github.com"

    def _headers(self, accept_raw: bool = False):
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        if accept_raw:
            h["Accept"] = "application/vnd.github.v3.raw"
        return h

    def _content_url(self):
        return f"{self.base}/repos/{self.owner}/{self.repo}/contents/{self.remote_path}?ref={self.branch}"

    def exists(self) -> bool:
        try:
            resp = requests.get(self._content_url(), headers=self._headers(), timeout=20)
            return resp.status_code == 200
        except Exception:
            return False

    def _get_sha_if_exists(self) -> Optional[str]:
        try:
            resp = requests.get(self._content_url(), headers=self._headers(), timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("sha")
            return None
        except Exception:
            return None

    def download_to(self, local_path: str) -> bool:
        try:
            resp = requests.get(self._content_url(), headers=self._headers(accept_raw=True), timeout=30)
            if resp.status_code != 200:
                _log(f"GitHub download failed: HTTP {resp.status_code}")
                return False
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as e:
            _log(f"GitHub download error: {e}")
            return False

    def upload_from(self, local_path: str) -> bool:
        try:
            if not os.path.exists(local_path):
                _log("Local DB missing; skipping upload.")
                return True
            with open(local_path, "rb") as f:
                data_bytes = f.read()
            import base64
            content_b64 = base64.b64encode(data_bytes).decode("ascii")
            sha = self._get_sha_if_exists()
            payload = {
                "message": f"Backup database {os.path.basename(local_path)} at {time.strftime('%Y-%m-%d %H:%M:%S')}",
                "content": content_b64,
                "branch": self.branch,
            }
            if sha:
                payload["sha"] = sha
            url = f"{self.base}/repos/{self.owner}/{self.repo}/contents/{self.remote_path}"
            resp = requests.put(url, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code in (200, 201):
                return True
            else:
                _log(f"GitHub upload failed: HTTP {resp.status_code} - {resp.text[:200]}")
                return False
        except Exception as e:
            _log(f"GitHub upload error: {e}")
            return False


async def _retry_async(fn: Callable[[], bool], attempts: int = 3, base_delay: float = 1.0) -> bool:
    """Retry helper for async-context around blocking functions."""
    for i in range(attempts):
        ok = await asyncio.get_event_loop().run_in_executor(None, fn)
        if ok:
            return True
        await asyncio.sleep(base_delay * (2 ** i))
    return False


def configure_backup_hooks(
    app,
    db_path: str,
    post_restore: Optional[Callable[[], None]] = None,
):
    """Attach startup/shutdown hooks to FastAPI app for DB backup/restore.

    Environment variables (set in Render):
      - BACKUP_PROVIDER: 'dropbox' (default)
      - DROPBOX_ACCESS_TOKEN: required for Dropbox
      - DROPBOX_FILE_PATH: remote path in Dropbox (e.g., '/apps/myapp/database.db')
      - BACKUP_INTERVAL_SECONDS: optional periodic backup interval (default 600)

    Args:
      app: FastAPI application instance
      db_path: local SQLite file path
      post_restore: callable to run after restore (e.g., init_db)
    """

    provider = os.getenv('BACKUP_PROVIDER', 'dropbox').lower().strip()
    backup_interval = int(os.getenv('BACKUP_INTERVAL_SECONDS', '600') or '600')
    client = None
    if provider == 'dropbox':
        remote_path = os.getenv('DROPBOX_FILE_PATH', f"/db_backups/{os.path.basename(db_path)}")
        token = os.getenv('DROPBOX_ACCESS_TOKEN', '')
        try:
            client = DropboxBackupClient(token=token, remote_path=remote_path)
        except Exception as e:
            _log(f"Dropbox client init failed: {e}")
            client = None
    elif provider == 'github':
        token = os.getenv('GITHUB_TOKEN', '')
        repo = os.getenv('GITHUB_REPO', '')  # e.g., 'yourname/db-backups'
        branch = os.getenv('GITHUB_BRANCH', 'main')
        gh_path = os.getenv('GITHUB_PATH', f"db-backups/{os.path.basename(db_path)}")
        try:
            client = GitHubBackupClient(token=token, repo=repo, branch=branch, remote_path=gh_path)
        except Exception as e:
            _log(f"GitHub client init failed: {e}")
            client = None

    async def do_restore_if_available():
        if not client:
            _log("No backup client configured; skipping restore.")
            return
        # Download to temp and replace atomically
        temp_dir = os.path.join(os.path.dirname(db_path) or ".", "_tmp_backup")
        os.makedirs(temp_dir, exist_ok=True)
        temp_dl = os.path.join(temp_dir, os.path.basename(db_path) + ".download")

        def _restore_once() -> bool:
            try:
                if not client.exists():
                    _log("Remote backup missing; starting with local DB.")
                    return True  # Not an error; allow startup
                if not client.download_to(temp_dl):
                    return False
                # Replace local DB atomically
                try:
                    os.replace(temp_dl, db_path)
                except Exception as e:
                    _log(f"Atomic replace failed: {e}")
                    # fallback to copy
                    try:
                        import shutil
                        shutil.copy2(temp_dl, db_path)
                    except Exception as e2:
                        _log(f"Copy fallback failed: {e2}")
                        return False
                _log("Restore completed from remote backup.")
                return True
            except Exception as e:
                _log(f"Restore attempt failed: {e}")
                return False

        ok = await _retry_async(_restore_once, attempts=3, base_delay=1.0)
        if not ok:
            _log("Restore failed after retries; proceeding with local DB if present.")

    async def do_backup_once():
        if not client:
            return False
        # Create a consistent temp copy, then upload
        temp_dir = os.path.join(os.path.dirname(db_path) or ".", "_tmp_backup")
        os.makedirs(temp_dir, exist_ok=True)
        temp_copy = os.path.join(temp_dir, os.path.basename(db_path) + ".copy")

        def _backup_once() -> bool:
            try:
                if not os.path.exists(db_path):
                    _log("Local DB missing; skipping backup.")
                    return True
                if not safe_sqlite_copy(db_path, temp_copy):
                    return False
                if not client.upload_from(temp_copy):
                    return False
                _log("Backup uploaded to remote storage.")
                return True
            except Exception as e:
                _log(f"Backup attempt failed: {e}")
                return False

        return await _retry_async(_backup_once, attempts=3, base_delay=1.0)

    async def periodic_backup_task():
        if backup_interval <= 0:
            return
        _log(f"Starting periodic backup every {backup_interval}s.")
        try:
            while True:
                await asyncio.sleep(backup_interval)
                try:
                    ok = await do_backup_once()
                    if not ok:
                        _log("Periodic backup failed.")
                except Exception as e:
                    _log(f"Periodic backup error: {e}")
        except asyncio.CancelledError:
            _log("Periodic backup task cancelled.")

    @app.on_event("startup")
    async def _on_startup():
        # Restore if possible
        await do_restore_if_available()
        # Initialize DB schema if needed
        try:
            if callable(post_restore):
                post_restore()
        except Exception as e:
            _log(f"post_restore failed: {e}")
        # Start periodic backup task
        app.state._backup_task = asyncio.create_task(periodic_backup_task())

    @app.on_event("shutdown")
    async def _on_shutdown():
        # Stop periodic backup
        task = getattr(app.state, "_backup_task", None)
        if task:
            with contextlib_suppress():
                task.cancel()
                await task
        # Backup latest DB
        ok = await do_backup_once()
        if not ok:
            _log("Shutdown backup failed after retries.")


class contextlib_suppress:
    def __enter__(self):
        return None
    def __exit__(self, exc_type, exc_val, exc_tb):
        return True