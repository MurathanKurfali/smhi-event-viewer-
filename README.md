# Nordic Weather Events

A lightweight Streamlit and SQLite explorer for Denmark, Finland, Norway and
Sweden. The included database contains 38,502 source records. All records are
retained, including 2,603 without coordinates and 305 with month-only dates.

## Run

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open http://localhost:8501. Stop the server with Ctrl+C.
No API keys or JavaScript build tools are required. OpenStreetMap tiles require
internet access; light and dark styles use browser filters on those tiles.

## Update the data

The app reads only `data/events.sqlite`. Original workbooks and checksums are
kept in `data/sources/`. To rebuild from these copies:

```bash
python -m pip install -r requirements-build.txt
python build_database.py
```

To copy updated upstream workbooks and rebuild:

```bash
python build_database.py --import-from /path/to/data/geocoding
```

Provide one chosen `country/.../events_geocoded.xlsx` per country. The importer
preserves every non-empty row and source field, checks source hashes, and
atomically replaces the database. Runtime access is read-only. Reload the page
after updating to refresh filter options.

## Use

- Filter by country, category and years/decades. Empty selections include all.
- Markers group exact coordinates and categories; counts represent all matching
  records. Select a marker to browse its events, or use **Fit events** to fit
  the selection. Pan, zoom and cluster expansion stay in the browser.
- Time-lapse displays one year or a cumulative period, retaining month precision.
- The table loads one page. Its sort and search controls apply to the full
  result. Select a row for details. **Download all matching events** exports
  the complete filtered result; the table toolbar exports the current page.
- `Date Basis` and `Measurement ID` are omitted only from map popups. Original
  fields remain available in tables, detailed source information and exports.

## Check correctness

```bash
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -v
```

Tests cover source-cell preservation, counts, dates, measurements, filtering,
pagination, exports, read-only access and Streamlit workflows.
