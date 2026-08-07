import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path


SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_TABLES = {"settings", "web_users"}
FULL_BACKUP_EXCLUDED_DIRS = {"hf_cache", "restore_backups", "restore_staging"}


def validate_database_backup(path: str) -> dict:
    """Validate an uploaded SQLite backup without modifying it."""
    if not os.path.isfile(path):
        raise ValueError("The uploaded backup file is missing.")
    if os.path.getsize(path) < 100:
        raise ValueError("The uploaded backup is too small to be a SQLite database.")
    with open(path, "rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError("The uploaded file is not a SQLite database.")

    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            detail = result[0] if result else "no result"
            raise ValueError(f"SQLite integrity check failed: {detail}")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise ValueError(
                "The backup is not a Corax database. Missing table(s): "
                + ", ".join(missing)
            )
        return {
            "size": os.path.getsize(path),
            "tables": len(tables),
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"The SQLite backup is invalid: {exc}") from exc
    finally:
        connection.close()


def create_full_data_archive(
    data_dir: str,
    database_path: str,
    database_snapshot: str,
    archive_path: str,
) -> dict:
    """Archive persistent app data with a consistent database snapshot."""
    data_dir = os.path.realpath(data_dir)
    database_path = os.path.realpath(database_path)
    database_sidecars = {database_path, database_path + "-wal", database_path + "-shm"}
    database_arcname = os.path.relpath(database_path, data_dir)
    file_count = 0

    manifest = {
        "format": "corax-full-data-backup-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": database_arcname,
        "excluded_directories": sorted(FULL_BACKUP_EXCLUDED_DIRS),
    }

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(database_snapshot, database_arcname)
        file_count += 1
        for root, dirs, files in os.walk(data_dir, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if name not in FULL_BACKUP_EXCLUDED_DIRS
                and not os.path.islink(os.path.join(root, name))
            ]
            for filename in files:
                candidate = os.path.join(root, filename)
                if os.path.islink(candidate):
                    continue
                source = os.path.realpath(candidate)
                if source in database_sidecars:
                    continue
                if not source.startswith(data_dir + os.sep):
                    continue
                arcname = os.path.relpath(source, data_dir)
                if arcname == "backup-manifest.json":
                    continue
                try:
                    archive.write(source, arcname)
                    file_count += 1
                except FileNotFoundError:
                    # Runtime media may disappear while an online archive is built.
                    continue
        archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2))

    return {
        "files": file_count,
        "size": os.path.getsize(archive_path),
    }


def prune_restore_backups(directory: str, keep: int = 5) -> None:
    if not os.path.isdir(directory):
        return
    candidates = sorted(
        (
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith((".db", ".sqlite", ".sqlite3"))
            and os.path.isfile(os.path.join(directory, name))
        ),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in candidates[max(1, keep):]:
        try:
            os.remove(path)
        except OSError:
            pass
