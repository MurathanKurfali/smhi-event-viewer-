#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Geocode SMHI-style event Excel and build a SQLite DB.

- Reads all sheets (rain, snow, wind, lightning, temperature, etc.)
- Fills missing Latitude/Longitude using OpenStreetMap Nominatim.
- Writes:
    - events_geocoded.xlsx
    - events.db (table: events)

This version is more verbose:
- Prints sheet info and row counts.
- Reports how many rows need geocoding.
- Shows progress while geocoding (cache hits and queries).
- Summarizes how many locations were successfully geocoded vs unresolved.
"""

from pathlib import Path
import sqlite3
from typing import Dict, Tuple, Optional

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter


# --- CONFIG -------------------------------------------------------------

INPUT_XLSX = "/Users/murathanku/PycharmProjects/SMHI_event_extraction/output_swe/gpt-5-mini_events.xlsx"
OUTPUT_XLSX = "gpt-5.1-mini_events_geocoded.xlsx"
OUTPUT_DB = "gpt-5.1-mini.db"

COUNTRY_SUFFIX = ", Sweden"  # helps disambiguate place names

# How often to print progress during geocoding (in number of API queries)
PROGRESS_EVERY_N_QUERIES = 25


# --- HELPERS ------------------------------------------------------------

def clean_number(val):
    """
    Convert Swedish-style numbers ("48,5") or floats to Python float.
    Returns None if not parseable.
    """
    if pd.isna(val) or val == "":
        return None
    if isinstance(val, str):
        val = val.strip().replace(",", ".")
    try:
        return float(val)
    except Exception:
        return None


def main():
    print("=== GEOCODING PIPELINE STARTED ===")
    print(f"Input Excel: {INPUT_XLSX}")
    print(f"Output Excel: {OUTPUT_XLSX}")
    print(f"Output DB: {OUTPUT_DB}")
    print("")

    input_path = Path(INPUT_XLSX)
    if not input_path.exists():
        raise FileNotFoundError(f"Could not find {input_path.resolve()}")

    # 1) Read all sheets and tag by sheet name
    print("Step 1/5: Reading Excel file and concatenating sheets...")
    xls = pd.ExcelFile(input_path)
    frames = []

    print(f"  Found {len(xls.sheet_names)} sheet(s): {', '.join(xls.sheet_names)}")

    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet)
        n_rows = len(df)
        print(f"    - Sheet '{sheet}': {n_rows} row(s)")
        df["Sheet"] = sheet  # keep origin info
        frames.append(df)

    events = pd.concat(frames, ignore_index=True)
    print(f"  Total rows after concatenation: {len(events)}")
    print("")

    # Ensure Latitude/Longitude columns exist
    print("Step 2/5: Normalizing Latitude/Longitude columns...")
    for col in ["Latitude", "Longitude"]:
        if col not in events.columns:
            print(f"  Column '{col}' not found, creating empty column.")
            events[col] = None

    # Normalize existing lat/lon
    events["Latitude"] = events["Latitude"].apply(clean_number)
    events["Longitude"] = events["Longitude"].apply(clean_number)

    # Count rows needing geocoding
    missing_mask = events["Latitude"].isna() | events["Longitude"].isna()
    n_missing = missing_mask.sum()
    print(f"  Rows with missing coordinates: {n_missing} / {len(events)}")
    print("")

    # 3) Set up Nominatim geocoder with rate limiting
    print("Step 3/5: Setting up Nominatim geocoder with rate limiting...")
    geolocator = Nominatim(user_agent="smhi_event_mapper_2025")  # custom UA per policy
    geocode = RateLimiter(
        geolocator.geocode,
        min_delay_seconds=1,     # 1 request/sec per Nominatim policy
        max_retries=2,
        error_wait_seconds=5.0,
    )
    print("  Geocoder ready.")
    print("")

    # Cache so we only geocode each place name once
    geo_cache: Dict[str, Tuple[Optional[float], Optional[float]]] = {}

    # Stats for verbosity
    stats = {
        "rows_total": len(events),
        "rows_with_missing": int(n_missing),
        "cache_hits": 0,
        "api_queries": 0,
        "locations_geocoded": 0,
        "locations_unresolved": 0,
    }

    print("Step 4/5: Geocoding missing coordinates...")
    if n_missing == 0:
        print("  No rows need geocoding, skipping this step.")
    else:
        print("  This may take a while (Nominatim = 1 req/sec).")
        print("  Progress will be printed every "
              f"{PROGRESS_EVERY_N_QUERIES} new API queries.")
        print("")

    # Precompute set of unique locations needing geocoding (for info only)
    locations_to_geocode = (
        events.loc[missing_mask, "Location"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
    )
    print(f"  Unique non-empty locations among missing coords: "
          f"{len(locations_to_geocode)}")
    print("")

    def fill_lat_lon(row):
        lat = row["Latitude"]
        lon = row["Longitude"]
        place = row.get("Location")

        # If we already have coordinates, keep them
        if pd.notna(lat) and pd.notna(lon):
            return lat, lon

        # No usable place name -> nothing we can do
        if not isinstance(place, str) or not place.strip():
            return lat, lon

        key = place.strip().lower()

        # Cache hit
        if key in geo_cache:
            stats["cache_hits"] += 1
            return geo_cache[key]

        # Need to query API
        stats["api_queries"] += 1
        query = place + COUNTRY_SUFFIX

        try:
            loc = geocode(query)
        except Exception:
            loc = None

        if loc is not None:
            result = (float(loc.latitude), float(loc.longitude))
            stats["locations_geocoded"] += 1
        else:
            result = (lat, lon)
            stats["locations_unresolved"] += 1

        geo_cache[key] = result

        # Periodic progress print
        if stats["api_queries"] % PROGRESS_EVERY_N_QUERIES == 0:
            print(
                f"  [Progress] API queries: {stats['api_queries']}, "
                f"cache hits: {stats['cache_hits']}, "
                f"geocoded locations: {stats['locations_geocoded']}, "
                f"unresolved: {stats['locations_unresolved']}"
            )

        return result

    # 4) Apply geocoding for rows missing lat/lon
    if n_missing > 0:
        events[["Latitude", "Longitude"]] = events.apply(
            lambda r: pd.Series(fill_lat_lon(r)),
            axis=1,
        )

        # Recount missing coordinates after geocoding
        missing_after = (events["Latitude"].isna() | events["Longitude"].isna()).sum()
        print("")
        print("Geocoding done.")
        print(f"  API queries performed: {stats['api_queries']}")
        print(f"  Cache hits: {stats['cache_hits']}")
        print(f"  Locations successfully geocoded: {stats['locations_geocoded']}")
        print(f"  Locations unresolved (no result from Nominatim): "
              f"{stats['locations_unresolved']}")
        print(f"  Rows still missing coordinates after geocoding: "
              f"{missing_after} / {len(events)}")
    else:
        missing_after = 0

    print("")

    # Ensure Date is datetime for later use in Streamlit
    print("Step 5/5: Final normalization and saving outputs...")
    if "Date" in events.columns:
        events["Date"] = pd.to_datetime(events["Date"], errors="coerce")
        invalid_dates = events["Date"].isna().sum()
        print(f"  Parsed 'Date' column to datetime. Unparseable entries: "
              f"{invalid_dates}")

    # 5a) Save to Excel (for inspection)
    events.to_excel(OUTPUT_XLSX, index=False)
    print(f"  Wrote geocoded data to Excel: {OUTPUT_XLSX}")

    # 5b) Save to SQLite
    conn = sqlite3.connect(OUTPUT_DB)
    events.to_sql("events", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  Wrote SQLite DB: {OUTPUT_DB} (table: 'events')")

    print("")
    print("=== GEOCODING PIPELINE FINISHED ===")
    print(f"  Total rows: {stats['rows_total']}")
    print(f"  Initial rows with missing coords: {stats['rows_with_missing']}")
    print(f"  Final rows with missing coords: {missing_after}")
    print("")


if __name__ == "__main__":
    main()
