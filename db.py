"""Small, read-only SQLite queries. Only map groups and one table page are loaded."""

import csv
import io
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

DB_PATH = Path(__file__).resolve().parent / "data/events.sqlite"


@dataclass(frozen=True)
class Filters:
    countries: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    include_undated: bool = True


def version():
    stat = DB_PATH.stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def connect():
    connection = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.create_function("casefold", 1, lambda text: text.casefold(), deterministic=True)
    return connection


def where(filters, *, search="", location=None, mappable=False):
    clauses, parameters = [], []
    for column, values in [("country", filters.countries), ("category", filters.categories), ("year", filters.years)]:
        if values:
            clause = f"{column} IN ({','.join('?' for _ in values)})"
            if column == "year" and filters.include_undated:
                clause = f"({clause} OR year IS NULL)"
            clauses.append(clause)
            parameters.extend(values)
    if not filters.years and not filters.include_undated:
        clauses.append("year IS NOT NULL")
    if mappable:
        clauses.append("mappable = 1")
    if location:
        clauses.append("latitude = ? AND longitude = ? AND category = ?")
        parameters.extend(location)
    if search.strip():
        # instr is literal: %, _, quotes and Unicode cannot become SQL wildcards.
        clauses.append("instr(casefold(location || ' ' || event_type || ' ' || coalesce(information, '') || ' ' || coalesce(measurement_id, '')), casefold(?)) > 0")
        parameters.append(search.strip())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters


@st.cache_data(max_entries=4, show_spinner=False)
def metadata(revision):
    with closing(connect()) as connection:
        result = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT * FROM metadata")}
        for key, column in [("countries", "country"), ("categories", "category"), ("years", "year")]:
            result[key] = [row[0] for row in connection.execute(
                f"SELECT DISTINCT {column} FROM events WHERE {column} IS NOT NULL ORDER BY {column}"
            )]
        return result


@st.cache_data(max_entries=64, show_spinner=False)
def summary(revision, filters):
    clause, parameters = where(filters)
    with closing(connect()) as connection:
        counts = connection.execute(
            "SELECT count(*), coalesce(sum(mappable), 0) FROM events" + clause, parameters
        ).fetchone()
        categories = {row[0]: row[1] for row in connection.execute(
            "SELECT category, count(*) FROM events" + clause + " GROUP BY category ORDER BY category", parameters
        )}
    return {"total": counts[0], "mappable": counts[1], "categories": categories}


@st.cache_data(max_entries=32, show_spinner=False)
def map_points(revision, filters):
    clause, parameters = where(filters, mappable=True)
    with closing(connect()) as connection:
        # Coincident events share a weighted marker. No rounding, sampling, or data loss.
        points = [list(row) for row in connection.execute(
            "SELECT latitude, longitude, category, count(*), min(id) FROM events"
            + clause + " GROUP BY latitude, longitude, category", parameters
        )]
    return points


SORT_COLUMNS = {"Date": "date", "Country": "country", "Category": "category",
                "Location": "location", "Type of Event": "event_type"}


@st.cache_data(max_entries=64, show_spinner=False)
def table_page(revision, filters, page=1, page_size=100, sort="Date", descending=False, search="", location=None):
    if not 1 <= page_size <= 250:
        raise ValueError("Page size must be between 1 and 250")
    column = SORT_COLUMNS[sort]
    clause, parameters = where(filters, search=search, location=location)
    with closing(connect()) as connection:
        count = connection.execute("SELECT count(*) FROM events" + clause, parameters).fetchone()[0]
        page = max(1, min(int(page), max(1, (count + page_size - 1) // page_size)))
        rows = connection.execute(
            "SELECT id, country, category, source_file, source_sheet, source_row, raw FROM events"
            + clause + f" ORDER BY {column} {'DESC' if descending else 'ASC'} NULLS LAST, id"
            + " LIMIT ? OFFSET ?", [*parameters, page_size, (page - 1) * page_size]
        ).fetchall()
    records = []
    for row in rows:
        records.append({"Event ID": row["id"], "Country": row["country"], "Category": row["category"],
                        **json.loads(row["raw"]), "Source file": row["source_file"],
                        "Source sheet": row["source_sheet"], "Source row": row["source_row"]})
    return records, count, page


@st.cache_data(max_entries=128, show_spinner=False)
def event(revision, event_id):
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["fields"] = json.loads(result.pop("raw"))
    return result


def export_csv(filters, search="", location=None):
    """Prepare the complete result only when explicitly requested, fetching in batches."""
    clause, parameters = where(filters, search=search, location=location)
    with closing(connect()) as connection:
        # Header discovery uses source schemas, not a second full scan of event JSON.
        info = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT * FROM metadata")}
        fields = info.get("source_columns", [])
        cursor = connection.execute(
            "SELECT id, country, category, raw, source_file, source_sheet, source_row FROM events"
            + clause + " ORDER BY date, id", parameters
        )
        output = io.StringIO(newline="")
        header = ["Event ID", "Country", "Category", *fields, "Source file", "Source sheet", "Source row"]
        writer = csv.DictWriter(output, fieldnames=header)
        writer.writeheader()
        while rows := cursor.fetchmany(1000):
            for row in rows:
                writer.writerow({"Event ID": row["id"], "Country": row["country"], "Category": row["category"],
                                 **json.loads(row["raw"]), "Source file": row["source_file"],
                                 "Source sheet": row["source_sheet"], "Source row": row["source_row"]})
    return output.getvalue().encode("utf-8-sig")
