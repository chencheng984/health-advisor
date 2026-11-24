"""Build and inspect the local cache through a shared import path."""

import hashlib
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from adapters import adapter, sniff
from catalog import METRIC_KEYS
from import_records import CHUNK_BYTES, HealthError, source_fingerprint


APPLICATION_ID = 0x41484144
SCHEMA_VERSION = 5
READABLE_VERSIONS = (1, 2, 3, 4, SCHEMA_VERSION)
MERGEABLE_VERSIONS = (2, 3, 4, SCHEMA_VERSION)
SOURCE_PURGE_KEY = "source_purge_pending"
INSERT_BATCH = 4096
SOURCE_LABELS = {"watch": "Apple Watch", "phone": "iPhone", "garmin": "Garmin", "huawei": "华为", "other": "其他来源"}

CREATE_SOURCES = """
CREATE TABLE sources (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT 'apple'
)"""

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
{CREATE_SOURCES};
CREATE TABLE inventory (
    identifier TEXT PRIMARY KEY, records INTEGER NOT NULL,
    first_day TEXT, last_day TEXT, skipped INTEGER NOT NULL, excluded INTEGER NOT NULL
);
CREATE TABLE samples (
    metric TEXT NOT NULL, day TEXT NOT NULL, source_id INTEGER NOT NULL,
    start_utc REAL NOT NULL, end_utc REAL NOT NULL,
    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
    value REAL NOT NULL, state TEXT NOT NULL,
    UNIQUE (metric, source_id, start_utc, end_utc, value, state, start_offset, end_offset)
);
CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    first_day TEXT,
    last_day TEXT,
    samples_added INTEGER NOT NULL,
    samples_total INTEGER NOT NULL,
    format TEXT NOT NULL DEFAULT 'apple'
);
CREATE TABLE ecg (
    recorded_utc REAL PRIMARY KEY,
    classification TEXT NOT NULL,
    symptoms TEXT NOT NULL,
    device TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    samples INTEGER NOT NULL
);
CREATE TABLE routes (
    start_utc REAL PRIMARY KEY,
    name TEXT NOT NULL,
    points INTEGER NOT NULL,
    distance_m REAL NOT NULL,
    duration_s REAL NOT NULL,
    ascent_m REAL NOT NULL
);
"""


@dataclass
class Inventory:
    records: int = 0
    first_day: str | None = None
    last_day: str | None = None
    skipped: int = 0
    excluded: int = 0

    def observe(self, day, records):
        self.records += records
        if day is not None:
            self.first_day = day if self.first_day is None else min(self.first_day, day)
            self.last_day = day if self.last_day is None else max(self.last_day, day)


def cache_version(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def open_cache(path):
    if not path.is_file():
        raise HealthError("找不到缓存。请先导入健康数据，或检查 --db 路径。")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        number = connection.execute("PRAGMA application_id").fetchone()[0]
        if number != APPLICATION_ID or cache_version(connection) not in READABLE_VERSIONS:
            raise HealthError("缓存格式与当前版本不兼容。请指定一个新的 --db 路径后重新导入。")
        yield connection
    finally:
        connection.close()


def write_observations(connection, observations):
    inventory = {}
    sources = {(kind, brand, fingerprint): source_id for source_id, kind, brand, fingerprint in
               connection.execute("SELECT id, kind, brand, fingerprint FROM sources")}
    pending = []
    statement = "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
    for observation in observations:
        item = inventory.get(observation.identifier)
        if item is None:
            item = inventory[observation.identifier] = Inventory()
        item.observe(observation.day, observation.records)
        item.skipped += observation.skipped
        item.excluded += observation.excluded
        for sample in observation.samples:
            identity = (sample.source_kind, sample.brand, sample.fingerprint)
            if identity not in sources:
                source = connection.execute("INSERT INTO sources(kind, brand, fingerprint) VALUES (?, ?, ?)", identity)
                sources[identity] = source.lastrowid
            pending.append((sample.metric, sample.day, sources[identity], sample.start_utc, sample.end_utc,
                            sample.start_offset, sample.end_offset, sample.value, sample.state))
            if len(pending) >= INSERT_BATCH:
                connection.executemany(statement, pending)
                pending.clear()
    connection.executemany(statement, pending)
    connection.executemany("INSERT OR REPLACE INTO inventory VALUES (?, ?, ?, ?, ?, ?)", (
        (code, item.records, item.first_day, item.last_day, item.skipped, item.excluded)
        for code, item in inventory.items()
    ))
    days = [item for item in inventory.values() if item.first_day is not None]
    return (min(item.first_day for item in days), max(item.last_day for item in days)) if days else (None, None)


def snapshot_info(connection, show_source_names=False):
    metadata = dict(connection.execute("SELECT key, value FROM meta"))
    counts = {
        row["metric"]: {
            "stored_rows": row["n"], "first_indexed_date": row["first_day"], "last_indexed_date": row["last_day"]
        }
        for row in connection.execute("SELECT * FROM metric_coverage")
    }
    inventory = []
    for row in connection.execute("SELECT * FROM inventory ORDER BY identifier"):
        metric = METRIC_KEYS.get(row["identifier"])
        inventory.append({
            "identifier": row["identifier"], "metric": metric, "records_seen": row["records"],
            "first_date": row["first_day"], "last_date": row["last_day"],
            "skipped": row["skipped"], "excluded_by_definition": row["excluded"],
            "status": "indexed" if metric in counts else ("no_usable_rows" if metric else "inventory_only"),
            **counts.get(metric, {}),
        })
    starts = [item["first_indexed_date"] for item in counts.values()]
    ends = [item["last_indexed_date"] for item in counts.values()]
    result = {
        "schema_version": cache_version(connection),
        "first_indexed_date": min(starts, default=None), "last_indexed_date": max(ends, default=None),
        "sources": read_sources(connection, show_source_names),
        "inventory": inventory,
        "archive": {
            "ecg_csv": int(metadata["ecg_csv"]), "gpx": int(metadata["gpx"]), "cda_xml": int(metadata["cda_xml"])
        },
    }
    if table_exists(connection, "imports"):
        import_format = "format" if cache_version(connection) >= 5 else "'apple' AS format"
        result["imports"] = [
            dict(row) for row in connection.execute(
                "SELECT id, substr(fingerprint, 1, 12) AS fingerprint, imported_at, mode,"
                f" first_day, last_day, samples_added, samples_total, {import_format}"
                " FROM imports ORDER BY id DESC LIMIT 10"
            )
        ]
    if table_exists(connection, "ecg") or table_exists(connection, "routes"):
        from archive import archive_summary
        result["archive_detail"] = archive_summary(connection)
    return result


def table_exists(connection, name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def read_sources(connection, include_names=False):
    sources = []
    brand_column = "brand" if cache_version(connection) >= 5 else "'apple' AS brand"
    for source_id, kind, brand in connection.execute(f"SELECT id, kind, {brand_column} FROM sources ORDER BY id"):
        label = f"{SOURCE_LABELS[kind]} #{source_id}"
        item = {"id": source_id, "kind": kind, "brand": brand, "label": label}
        if include_names:
            item["name"] = label
        sources.append(item)
    return sources


def record_import(connection, digest, mode, first_day, last_day, added, total, format_name):
    connection.execute(
        "INSERT INTO imports(fingerprint, imported_at, mode, first_day, last_day, samples_added, samples_total, format)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (digest, datetime.now(timezone.utc).isoformat(timespec="seconds"), mode, first_day, last_day, added, total,
         format_name),
    )


def apply_import(connection, path, importer, format_name, digest, metadata, merge):
    before = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] if merge else 0
    with closing(importer.iter_samples(path)) as records:
        first_day, last_day = write_observations(connection, records)
    if format_name == "apple":
        from archive import load_archive
        with ZipFile(path) as archive:
            load_archive(archive, connection)
    connection.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        {"fingerprint": digest, "import_format": format_name, **metadata}.items(),
    )
    connection.execute("DROP TABLE IF EXISTS metric_coverage")
    connection.execute("""
        CREATE TABLE metric_coverage AS
        SELECT metric, COUNT(*) AS n, MIN(day) AS first_day, MAX(day) AS last_day FROM samples GROUP BY metric
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS sample_days ON samples(metric, day, source_id, start_utc)")
    total = connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    record_import(connection, digest, "merge" if merge else "replace", first_day, last_day, total - before, total,
                  format_name)


def build_snapshot(path, database, importer, format_name, digest, metadata):
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(SCHEMA)
        with connection:
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            counts = dict.fromkeys(("ecg_csv", "gpx", "cda_xml"), 0)
            apply_import(connection, path, importer, format_name, digest, {**counts, **metadata}, False)
        connection.row_factory = sqlite3.Row
        return snapshot_info(connection)


UPGRADE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS imports (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        first_day TEXT,
        last_day TEXT,
        samples_added INTEGER NOT NULL,
        samples_total INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ecg (
        recorded_utc REAL PRIMARY KEY,
        classification TEXT NOT NULL,
        symptoms TEXT NOT NULL,
        device TEXT NOT NULL,
        sample_rate INTEGER NOT NULL,
        samples INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routes (
        start_utc REAL PRIMARY KEY,
        name TEXT NOT NULL,
        points INTEGER NOT NULL,
        distance_m REAL NOT NULL,
        duration_s REAL NOT NULL,
        ascent_m REAL NOT NULL
    )
    """,
)


@contextmanager
def write_cache(path):
    if not path.is_file():
        raise HealthError("找不到缓存。请检查 --db 路径。")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete = ON")
        yield connection
    finally:
        connection.close()


def upgrade_cache(connection):
    """Upgrade under the caller's write transaction, retaining sample source IDs."""
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise HealthError("目标文件不是本工具的缓存。请检查 --db 路径。")
    version = cache_version(connection)
    if version not in MERGEABLE_VERSIONS:
        raise HealthError("此缓存版本不支持迁移或合并。请用不带 --merge 的导入重建缓存。")
    if version == SCHEMA_VERSION:
        return False
    if version == 2:
        # executescript would commit before running the DDL.
        for statement in UPGRADE_STATEMENTS:
            connection.execute(statement)
    if version == 4:
        connection.execute("ALTER TABLE sources ADD COLUMN brand TEXT NOT NULL DEFAULT 'apple'")
    else:
        connection.execute("ALTER TABLE sources RENAME TO sources_legacy")
        connection.execute(CREATE_SOURCES)
        connection.executemany(
            "INSERT INTO sources(id, kind, fingerprint) VALUES (?, ?, ?)",
            ((source_id, kind, source_fingerprint(name, hardware)) for source_id, kind, name, hardware in
             connection.execute("SELECT id, kind, name, hardware FROM sources_legacy")),
        )
        connection.execute("DROP TABLE sources_legacy")
        connection.execute("INSERT INTO meta VALUES (?, '1')", (SOURCE_PURGE_KEY,))
    connection.execute("ALTER TABLE imports ADD COLUMN format TEXT NOT NULL DEFAULT 'apple'")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return True


def source_purge_pending(connection):
    return connection.execute("SELECT 1 FROM meta WHERE key = ?", (SOURCE_PURGE_KEY,)).fetchone() is not None


def finish_source_purge(connection):
    if not source_purge_pending(connection):
        return False
    # VACUUM also removes stale copies in pages freed before secure_delete was enabled.
    # Leave the marker in place until the main file and any WAL have been scrubbed.
    connection.execute("VACUUM")
    busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise HealthError("缓存仍被占用。请关闭占用缓存的程序后重新运行 migrate。")
    with connection:
        connection.execute("DELETE FROM meta WHERE key = ?", (SOURCE_PURGE_KEY,))
    return True


def migrate_cache(database):
    with write_cache(database) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            upgraded = upgrade_cache(connection)
        purged = finish_source_purge(connection)
        state = "migrated" if upgraded or purged else "reused"
        return {"state": state, "schema_version": cache_version(connection)}


def merge_snapshot(path, database, importer, format_name, digest, metadata):
    with write_cache(database) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            upgrade_cache(connection)
            apply_import(connection, path, importer, format_name, digest, metadata, True)
        finish_source_purge(connection)
        return snapshot_info(connection)


def import_export(export, database, merge=False, format_name="auto"):
    export = Path(export).resolve()
    database = Path(database).resolve()
    if export == database or (database.exists() and export.samefile(database)):
        raise HealthError("缓存路径必须与原始导出路径分开。")
    with export.open("rb") as stream:
        fingerprint = hashlib.sha256()
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            fingerprint.update(block)
    digest = fingerprint.hexdigest()
    format_name = sniff(export) if format_name == "auto" else format_name
    if database.exists():
        with open_cache(database) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM meta"))
            same_snapshot = (cache_version(connection) == SCHEMA_VERSION and metadata.get("fingerprint") == digest
                             and metadata.get("import_format", "apple") == format_name)
            if same_snapshot and not source_purge_pending(connection):
                return {"state": "reused", **snapshot_info(connection)}
        if same_snapshot:
            migrate_cache(database)
            with open_cache(database) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM meta"))
                if metadata.get("fingerprint") == digest and metadata.get("import_format", "apple") == format_name:
                    return {"state": "reused", **snapshot_info(connection)}
    importer = adapter(format_name)
    metadata = importer.inspect(export)
    if merge and database.exists():
        return {"state": "merged", **merge_snapshot(export, database, importer, format_name, digest, metadata)}
    database.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".health-", suffix=".sqlite3", dir=database.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        result = build_snapshot(export, temporary, importer, format_name, digest, metadata)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return {"state": "imported", **result}


def import_zip(export, database, merge=False):
    return import_export(export, database, merge, format_name="apple")
