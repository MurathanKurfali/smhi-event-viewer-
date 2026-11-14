"""Import the copied geocoded workbooks without discarding or deduplicating rows."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, time
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    country TEXT NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    date TEXT,
    year INTEGER,
    location TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    mappable INTEGER NOT NULL CHECK (mappable IN (0, 1)),
    measurement_id TEXT,
    information TEXT,
    source_file TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    raw TEXT NOT NULL,
    UNIQUE (source_file, source_sheet, source_row)
);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def json_value(value):
    """Keep numbers, strings, nulls and booleans typed; serialize Excel dates as ISO."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).strip().replace(",", "."))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def normalize_date(value):
    """Keep year/month precision instead of inventing a day or dropping the event."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d{4}(?:-\d{2})?", value):
            try:
                date(int(value[:4]), int(value[5:7]) if len(value) == 7 else 1, 1)
                return value
            except ValueError:
                return None
        try:
            # Accept full ISO dates/timestamps, not ambiguous month/year-only dates.
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.date().isoformat()
        except ValueError:
            pass
    return None


def core_value(raw, *names):
    """Match core headers case-insensitively; retain original headers in raw JSON."""
    lookup = {str(k).strip().casefold(): v for k, v in raw.items()}
    for name in names:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return None


def import_workbook(connection, path, country):
    workbook = load_workbook(path, read_only=True, data_only=False)
    sheets = {}
    try:
        for sheet in workbook:
            values = sheet.iter_rows(values_only=True)
            header = next(values, ())
            if not header:
                sheets[sheet.title] = {"rows": 0, "mappable": 0, "undated": 0, "columns": []}
                continue
            names = [str(name) if name is not None else f"Column {i}" for i, name in enumerate(header, 1)]
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate column names in {path.name}: {sheet.title}")
            stats = {"rows": 0, "mappable": 0, "undated": 0, "columns": names}
            batch = []
            for row_number, row in enumerate(values, 2):
                if all(value is None for value in row):
                    continue
                raw = dict(zip(names, map(json_value, row)))
                latitude = number(core_value(raw, "Latitude", "Lat"))
                longitude = number(core_value(raw, "Longitude", "Lon", "Lng"))
                mappable = int(latitude is not None and longitude is not None
                               and -90 <= latitude <= 90 and -180 <= longitude <= 180)
                event_date = normalize_date(core_value(raw, "Date", "Event Date"))
                category = core_value(raw, "Category", "Sheet") or sheet.title
                event_type = core_value(raw, "Type of Event", "Event Type") or category
                batch.append((
                    country, str(category), str(event_type), event_date,
                    int(event_date[:4]) if event_date else None,
                    str(core_value(raw, "Location", "Place") or ""), latitude, longitude, mappable,
                    core_value(raw, "Measurement ID"), core_value(raw, "Additional Information"),
                    path.name, sheet.title, row_number,
                    json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                ))
                stats["rows"] += 1
                stats["mappable"] += mappable
                stats["undated"] += event_date is None
                if len(batch) == 1000:
                    insert_rows(connection, batch)
                    batch.clear()
            insert_rows(connection, batch)
            sheets[sheet.title] = stats
    finally:
        workbook.close()
    return sheets


def insert_rows(connection, rows):
    connection.executemany("""
        INSERT INTO events (country, category, event_type, date, year, location,
            latitude, longitude, mappable, measurement_id, information,
            source_file, source_sheet, source_row, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)


def copy_sources(root, destination):
    """Copy one chosen geocoded workbook per country, including a checksum manifest."""
    root, destination = Path(root), Path(destination)
    paths = sorted(root.rglob("events_geocoded.xlsx"))
    if not paths:
        raise FileNotFoundError(f"No events_geocoded.xlsx workbooks in {root}")
    manifest, chosen = {}, {}
    for path in paths:
        country = path.relative_to(root).parts[0].lower()
        if country in chosen:
            raise ValueError(f"Multiple workbook variants for {country}; provide only the intended variant")
        chosen[country] = path
    destination.mkdir(parents=True, exist_ok=True)
    for country, path in chosen.items():
        name = f"{country}.xlsx"
        shutil.copy2(path, destination / name)
        manifest[name] = {"country": country.title(), "origin": path.relative_to(root).as_posix(),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def build_database(source_dir=ROOT / "data/sources", output=ROOT / "data/events.sqlite"):
    source_dir, output = Path(source_dir), Path(output)
    manifest_path = source_dir / "manifest.json"
    supplied = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    files = [source_dir / name for name in sorted(supplied)] if manifest_path.exists() else sorted(source_dir.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No Excel sources in {source_dir}")
    sources = {}
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entry = supplied.get(path.name, {})
        if entry.get("sha256", digest) != digest:
            raise ValueError(f"Source checksum changed: {path.name}; update manifest.json intentionally")
        sources[path.name] = {**entry, "country": entry.get("country", path.stem.title()), "sha256": digest}
    revision = hashlib.sha256(json.dumps([SCHEMA_VERSION, sources], sort_keys=True).encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".events-", suffix=".sqlite", dir=output.parent)
    os.close(descriptor)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(SCHEMA)
            for path in files:
                entry = sources[path.name]
                entry["sheets"] = import_workbook(connection, path, entry["country"])
            connection.executescript("""
                CREATE INDEX events_map ON events
                    (mappable, country, category, year, latitude, longitude);
                CREATE INDEX events_date ON events (date, id);
                CREATE INDEX events_location ON events (latitude, longitude, category, date, id);
                ANALYZE;
            """)
            total, mappable, undated = connection.execute(
                "SELECT count(*), coalesce(sum(mappable), 0), sum(year IS NULL) FROM events"
            ).fetchone()
            metadata = {"revision": revision, "schema_version": SCHEMA_VERSION, "sources": sources,
                        "rows": total, "mappable": mappable, "undated": undated,
                        "source_columns": list(dict.fromkeys(column for source in sources.values()
                            for sheet in source["sheets"].values() for column in sheet["columns"]))}
            connection.executemany("INSERT INTO metadata VALUES (?, ?)",
                                   [(key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()])
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
        finally:
            connection.close()
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"database": str(output), "rows": total, "mappable": mappable,
                      "unmapped": total - mappable, "undated": undated, "revision": revision}, indent=2))
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "data/sources")
    parser.add_argument("--output", type=Path, default=ROOT / "data/events.sqlite")
    parser.add_argument("--import-from", type=Path, help="Copy geocoded country workbooks from this directory first")
    args = parser.parse_args()
    if args.import_from:
        copy_sources(args.import_from, args.sources)
    build_database(args.sources, args.output)
