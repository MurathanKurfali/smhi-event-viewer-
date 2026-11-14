"""Nordic weather events: compact browser maps and paginated SQLite details."""

import hashlib
import math
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import db

st.set_page_config(page_title="Nordic Weather Events", page_icon="🌦️", layout="wide")

event_map = components.declare_component("event_map", path=str(Path(__file__).parent / "map_component"))
PALETTE = {
    "Lightning": {"color": "#b08800", "emoji": "⚡", "dot": "🟡"},
    "Rain": {"color": "#2875b9", "emoji": "🌧️", "dot": "🔵"},
    "Snow": {"color": "#789cad", "emoji": "❄️", "dot": "⚪"},
    "Temperature": {"color": "#d54b48", "emoji": "🌡️", "dot": "🔴"},
    "Wind": {"color": "#318768", "emoji": "💨", "dot": "🟢"},
}


def scope_key(*values):
    return hashlib.sha256(repr(values).encode()).hexdigest()[:16]


def show_event(record):
    fields = record["fields"]
    st.write(f"**{record['location']}** · {record['country']} · {record['event_type']} · {record['date'] or 'Date unavailable'}")
    if record["information"]:
        st.write(record["information"])
    measurements = {name: value for name, value in fields.items() if value is not None and (
        "(" in name or "discharges" in name.casefold()
    )}
    if measurements:
        st.dataframe(pd.DataFrame({"Measurement": measurements.keys(), "Value": [str(v) for v in measurements.values()]}),
                     hide_index=True, width="stretch")
    with st.expander("All event fields and source information"):
        st.json({"Event ID": record["id"], "Country": record["country"], "Category": record["category"],
                 **fields, "Source file": record["source_file"], "Source sheet": record["source_sheet"],
                 "Source row": record["source_row"]})


@st.fragment
def map_panel(filters, marker_style, cluster, basemap):
    revision = db.version()
    points = db.map_points(revision, filters)
    data_key = scope_key(revision, filters)
    map_slot = st.container()
    selection = st.session_state.get("weather_map")
    details = None
    if selection and selection.get("data_key") == data_key:
        selected = db.event(revision, selection["id"])
        if selected:
            location = (selected["latitude"], selected["longitude"], selected["category"])
            group_key = scope_key(data_key, location)
            requested = st.session_state.get(f"location_page_{group_key}", 1)
            records, count, page = db.table_page(revision, filters, requested, 25, location=location)
            with st.expander(f"Events at {selected['location']} · {count:,} matching events", expanded=True):
                if count > 25:
                    st.number_input("Location page", min_value=1, max_value=math.ceil(count / 25),
                                    value=page, key=f"location_page_{group_key}")
                choices = {r["Event ID"]: r for r in records}
                chosen = st.selectbox("Event at this location", list(choices),
                    format_func=lambda i: f"{choices[i].get('Date', 'Undated')} · {choices[i].get('Type of Event', selected['category'])} · #{i}",
                    key=f"location_event_{group_key}_{page}")
                if chosen is not None:
                    current = db.event(revision, chosen)
                    show_event(current)
                    details = {"marker_id": selection["id"], "country": current["country"],
                               "category": current["category"], "fields": current["fields"]}
    with map_slot:
        event_map(points=points, data_key=data_key, marker_style=marker_style, cluster=cluster, basemap=basemap,
                  categories=PALETTE, details=details, height=590, key="weather_map", default=None)
    if points:
        st.caption("Marker and cluster counts represent events. Select a marker to browse every event at its location.")
    else:
        st.info("This selection has no events with valid coordinates. All matching records remain in the table.")


def display_frame(records):
    frame = pd.DataFrame(records)
    # Preserve range-valued measurements (e.g. '18-22') alongside numbers in Arrow.
    for column in frame.columns:
        values = frame[column].dropna()
        if any(isinstance(value, str) for value in values) and any(isinstance(value, (int, float)) for value in values):
            frame[column] = frame[column].map(lambda value: str(value) if pd.notna(value) else None)
    return frame


@st.fragment
def table_panel(filters):
    revision = db.version()
    scope = scope_key(revision, filters)
    st.subheader("Event details")
    search_col, sort_col, size_col, direction_col = st.columns([3, 1.5, 1, 1])
    search = search_col.text_input("Search events", key=f"search_{scope}",
        help="Search all matching locations, event types, notes and measurement IDs.")
    sort = sort_col.selectbox("Sort all results by", list(db.SORT_COLUMNS), key="table_sort")
    page_size = size_col.selectbox("Rows per page", [50, 100, 250], index=1, key="table_page_size")
    descending = direction_col.checkbox("Descending", key="table_descending")
    page_key = f"page_{scope_key(scope, search, sort, page_size, descending)}"
    requested = st.session_state.get(page_key, 1)
    records, count, page = db.table_page(revision, filters, requested, page_size, sort, descending, search)
    nav, export = st.columns([1, 3])
    nav.number_input("Page", min_value=1, max_value=max(1, math.ceil(count / page_size)), value=page, key=page_key)
    with export:
        st.download_button("Download all matching events", data=lambda: db.export_csv(filters, search),
                           file_name="nordic_weather_events.csv", mime="text/csv", on_click="ignore")
    if not records:
        st.info("No events match this search.")
        return
    st.caption(f"{(page - 1) * page_size + 1:,}–{min(page * page_size, count):,} of {count:,} events. "
               "Use the sort control above to order the complete result. Select a row for its full details.")
    frame = display_frame(records)
    core = ["Country", "Category", "Type of Event", "Date", "Location", "Latitude", "Longitude"]
    measurements = [c for c in frame if "(" in c or "discharges" in c.casefold()]
    order = [c for c in [*core, *measurements, "Additional Information", "Event ID"] if c in frame]
    selection = st.dataframe(frame, hide_index=True, width="stretch", height=420,
        column_order=order, key=f"events_{page_key}_{page}", on_select="rerun", selection_mode="single-row")
    if selection.selection.rows:
        show_event(db.event(revision, records[selection.selection.rows[0]]["Event ID"]))


def main():
    st.title("Nordic Weather Events")
    try:
        revision = db.version()
        meta = db.metadata(revision)
    except (FileNotFoundError, OSError):
        st.error("The event database is unavailable. Build it with `python build_database.py` before starting the app.")
        st.stop()
    if not meta["rows"]:
        st.info("The source workbooks contain no event records.")
        st.stop()
    st.caption("Explore reported weather events across Denmark, Finland, Norway and Sweden.")
    with st.sidebar:
        st.header("Explore")
        countries = st.multiselect("Countries", meta["countries"], default=meta["countries"], key="countries")
        categories = st.multiselect("Event categories", meta["categories"], default=meta["categories"], key="categories")
        st.caption("An empty selection includes all options.")
        st.divider()
        marker_style = st.selectbox("Marker style", ["Colored dots", "Weather icons"])
        cluster = st.checkbox("Cluster markers", value=True)
        basemap = st.selectbox("Basemap", ["Light", "Dark", "OpenStreetMap"])
        st.divider()
        st.caption(f"Dataset: {meta['rows']:,} events · {min(meta['years'])}–{max(meta['years'])}" if meta["years"] else f"Dataset: {meta['rows']:,} undated events")
    view = st.radio("View", ["Map", "Time-lapse"], horizontal=True, key="view_mode")
    years = meta["years"]
    include_undated = True
    if view == "Time-lapse" and years:
        timeline, cumulative_col = st.columns([3, 1])
        year = timeline.slider("Year", min_value=min(years), max_value=max(years), value=min(years),
                               step=1, key="timeline_year") if len(years) > 1 else years[0]
        cumulative = cumulative_col.checkbox("Cumulative up to year", value=False, key="cumulative")
        selected_years = tuple(y for y in years if y <= year) if cumulative else (year,)
        include_undated = False
    else:
        decades = sorted({year // 10 * 10 for year in years})
        labels = {f"{decade}s": [year for year in years if decade <= year < decade + 10] for decade in decades}
        labels.update({str(year): [year] for year in years})
        selected = st.multiselect("Years / decades", list(labels), default=[f"{d}s" for d in decades], key="years")
        selected_years = tuple(sorted({year for label in selected for year in labels[label]})) if selected else ()
        include_undated = not selected or set(selected_years) == set(years)
    filters = db.Filters(tuple(sorted(countries)), tuple(sorted(categories)), selected_years, include_undated)
    counts = db.summary(revision, filters)
    total_col, mapped_col, unmapped_col = st.columns(3)
    total_col.metric("Matching events", f"{counts['total']:,}")
    mapped_col.metric("On the map", f"{counts['mappable']:,}")
    unmapped_col.metric("Without valid coordinates", f"{counts['total'] - counts['mappable']:,}")
    map_panel(filters, marker_style, cluster, basemap)
    if view == "Time-lapse" and counts["categories"]:
        st.bar_chart(pd.Series(counts["categories"], name="Events"), height=200, color="#2875b9")
    table_panel(filters)


if __name__ == "__main__":
    main()
