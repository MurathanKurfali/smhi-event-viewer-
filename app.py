import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

from geocode_events import OUTPUT_DB

DB_PATH = OUTPUT_DB
TABLE_NAME = "events"

# Basemap options (only the three you want)
BASEMAPS = {
    "OSM Standard": "OpenStreetMap",
    "CartoDB Positron (light)": "CartoDB positron",
    "CartoDB Dark Matter (dark)": "CartoDB dark_matter",
}


@st.cache_data
def load_events(db_path: str) -> pd.DataFrame:
    """Load events once and cache the result (SQLite is not your bottleneck)."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Could not find {db_path}")
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(f"SELECT * FROM {TABLE_NAME}", conn)
    conn.close()

    # Parse dates
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

    # Clean numeric lat/lon
    for col in ["Latitude", "Longitude"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop rows without coordinates
    df = df.dropna(subset=["Latitude", "Longitude"])

    # Add year column for filtering
    if "Date" in df.columns:
        df["year"] = df["Date"].dt.year
    else:
        df["year"] = None

    return df


def emoji_for_key(key: str, use_colored: bool, default_emoji: str) -> str:
    """Return emoji based on sheet-name key and chosen style."""
    key = key.strip().lower()
    if use_colored:
        # Simple colored circles (emoji)
        if "rain" in key:
            return "🔵"  # rain = blue circle
        elif "snow" in key:
            return "⚪️"  # snow = white circle
        elif "wind" in key:
            return "🟢"  # wind = green circle
        elif "lightning" in key or "thunder" in key or "storm" in key:
            return "🟡"  # lightning/storm = yellow circle
        elif "temp" in key or "heat" in key:
            return "🔴"  # temperature = red circle
        else:
            return default_emoji
    else:
        # Weather symbol emojis
        if "rain" in key:
            return "🌧️"
        elif "snow" in key:
            return "❄️"
        elif "wind" in key:
            return "💨"
        elif "lightning" in key or "thunder" in key or "storm" in key:
            return "⚡"
        elif "temp" in key or "heat" in key:
            return "🌡️"
        else:
            return default_emoji


# -------------------------------------------------------------------
# App setup and global data
# -------------------------------------------------------------------

st.set_page_config(page_title="Weather Event Explorer", layout="wide")
st.title("Weather Event Explorer (SMHI-style events)")

df = load_events(DB_PATH)

if df.empty:
    st.error("No events found in the database. Run the geocoding script first.")
    st.stop()

# Precompute year info
all_years = sorted(int(y) for y in df["year"].dropna().unique())
min_year_data = min(all_years)
max_year_data = max(all_years)

# Build decade labels and mapping year/decade -> list of years
decades = sorted({(y // 10) * 10 for y in all_years})
decade_labels = [f"{d}s" for d in decades]
year_labels = [str(y) for y in all_years]

label_to_years: dict[str, list[int]] = {}
for y in all_years:
    label_to_years[str(y)] = [y]
for d in decades:
    label = f"{d}s"
    label_to_years[label] = [y for y in all_years if d <= y <= d + 9]

year_options = decade_labels + year_labels  # decades first, then individual years

# Numeric columns (once, globally)
numeric_cols_all: list[str] = []
for col in df.columns:
    if col in ["Latitude", "Longitude", "year"]:
        continue
    if col.lower().startswith("unnamed"):
        continue
    if pd.api.types.is_numeric_dtype(df[col]):
        numeric_cols_all.append(col)

# Color palette for fast CircleMarkers
if "Sheet" in df.columns:
    all_sheets = sorted(df["Sheet"].dropna().unique())
else:
    all_sheets = []
color_palette = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
]
sheet_color_map = {
    sheet: color_palette[i % len(color_palette)]
    for i, sheet in enumerate(all_sheets)
}

default_emoji = "⚫️"  # fallback


# -------------------------------------------------------------------
# Sidebar (global controls)
# -------------------------------------------------------------------

with st.sidebar:
    st.header("Filters")

    # Multi-select for years/decades (MAP MODE)
    selected_year_labels = st.multiselect(
        "Years / decades",
        options=year_options,
        default=decade_labels,  # default: all decades -> all years
        key="year_labels_main",
    )

    # Resolve selected labels -> concrete years
    if selected_year_labels:
        selected_years = sorted(
            {
                yr
                for label in selected_year_labels
                for yr in label_to_years.get(label, [])
            }
        )
    else:
        # If user deselects everything, fall back to all years
        selected_years = all_years

    # "Type of Event" = sheet names (do NOT say "sheet" in the UI)
    if "Sheet" in df.columns:
        type_options = sorted(df["Sheet"].dropna().unique())
        selected_types = st.multiselect(
            "Type of Event",
            options=type_options,
            default=type_options,
            key="types_main",
        )
    else:
        selected_types = None

    # Emoji markers toggle (slower, but you still can use them)
    use_emoji_markers = st.checkbox(
        "Use emoji markers (slower)",
        value=False,  # default OFF for performance
        key="use_emoji_markers",
    )

    # If using emojis, you can choose style (colored dots vs weather icons)
    if use_emoji_markers:
        emoji_style = st.radio(
            "Emoji style",
            options=["Colored dots", "Weather icons"],
            index=0,
            key="emoji_style",
        )
        use_colored_emojis = emoji_style == "Colored dots"
    else:
        use_colored_emojis = True  # unused when emojis are off

    # Clustering toggle
    use_clustering = st.checkbox("Cluster markers", value=True, key="cluster_main")

    st.markdown("---")
    basemap_label = st.selectbox(
        "Basemap style",
        options=list(BASEMAPS.keys()),
        index=1,  # default: CartoDB Positron (light)
        key="basemap_main",
    )
    tiles_style = BASEMAPS[basemap_label]

# Choose mode: only ONE map is built per rerun (big performance win)
view_mode = st.radio(
    "View",
    options=["Map", "Time-lapse"],
    horizontal=True,
    key="view_mode",
)

# -------------------------------------------------------------------
# MAP MODE
# -------------------------------------------------------------------
if view_mode == "Map":
    # Filter for selected years and types
    mask = df["year"].isin(selected_years)
    if selected_types:
        mask &= df["Sheet"].isin(selected_types)
    filtered = df[mask].copy()

    if selected_years:
        year_text = (
            f"{min(selected_years)}–{max(selected_years)}"
            if len(selected_years) > 1
            else f"{selected_years[0]}"
        )
    else:
        year_text = "all years"

    st.subheader(f"Showing {len(filtered)} events for {year_text}")

    if filtered.empty:
        st.info("No events match the current filters.")
    else:
        st.write("### Map of events")

        mid_lat = float(filtered["Latitude"].mean())
        mid_lon = float(filtered["Longitude"].mean())

        m = folium.Map(
            location=[mid_lat, mid_lon],
            zoom_start=4,
            tiles=tiles_style,
        )

        # Cluster or not
        if use_clustering:
            marker_group = MarkerCluster().add_to(m)
        else:
            marker_group = m

        # Build markers
        for _, row in filtered.iterrows():
            event_type = row.get("Type of Event", "")
            name = row.get("Name of Event", "")
            location = row.get("Location", "")
            date_val = row.get("Date", "")
            info = row.get("Additional Information", "")

            # Clean name
            name_str = ""
            if pd.notna(name) and str(name).strip():
                name_str = str(name).strip()

            # Date without time
            if pd.isna(date_val):
                date_str = ""
            else:
                date_str = date_val.strftime("%Y-%m-%d")

            # Numeric metrics
            metric_lines = []
            for col in numeric_cols_all:
                if col in filtered.columns:
                    val = row.get(col)
                    if pd.notna(val):
                        metric_lines.append(f"{col}: {val:g}")
            metrics_html = "<br>".join(metric_lines)

            # Popup content
            if event_type:
                first_line = f"<b>{event_type}</b>"
            else:
                first_line = "<b>Event</b>"

            if name_str:
                first_line += f" – {name_str}"

            popup_parts = [first_line]

            if location:
                popup_parts.append(str(location))
            if date_str:
                popup_parts.append(date_str)
            if metrics_html:
                popup_parts.append(metrics_html)
            if info:
                popup_parts.append(f"<small>{info}</small>")

            popup_html = "<br>".join(popup_parts)
            popup_html_wrapped = (
                f'<div style="font-size: 11px; line-height: 1.2;">{popup_html}</div>'
            )
            popup = folium.Popup(popup_html_wrapped, max_width=300)

            sheet_name = row.get("Sheet", "")
            color = sheet_color_map.get(sheet_name, "#000000")

            if use_emoji_markers:
                # Emoji marker via DivIcon (heavier)
                emoji = emoji_for_key(str(sheet_name), use_colored_emojis, default_emoji)
                icon_html = f"""
                <div style="
                    font-size: 14px;
                    line-height: 14px;
                    text-align: center;
                    text-shadow:
                        -1px -1px 2px #fff,
                         1px -1px 2px #fff,
                        -1px  1px 2px #fff,
                         1px  1px 2px #fff;
                ">{emoji}</div>
                """
                folium.Marker(
                    location=[row["Latitude"], row["Longitude"]],
                    icon=folium.DivIcon(html=icon_html),
                    popup=popup,
                    tooltip=(
                        f"{event_type} – {location}"
                        if event_type or location
                        else "Event"
                    ),
                ).add_to(marker_group)
            else:
                # Fast CircleMarker (recommended for performance)
                folium.CircleMarker(
                    location=[row["Latitude"], row["Longitude"]],
                    radius=4,
                    color=color,
                    weight=1,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.8,
                    popup=popup,
                    tooltip=(
                        f"{event_type} – {location}"
                        if event_type or location
                        else "Event"
                    ),
                ).add_to(marker_group)

        st_folium(m, width=None, height=800)

        # Table
        st.write("### Event details")

        table_df = filtered.copy()

        for col in ["Sheet", "year"]:
            if col in table_df.columns:
                table_df = table_df.drop(columns=[col])

        if "Date" in table_df.columns:
            table_df["Date"] = table_df["Date"].dt.date

        core_order = [
            "Type of Event",
            "Name of Event",
            "Date",
            "Location",
            "Latitude",
            "Longitude",
        ]
        columns_order = [c for c in core_order if c in table_df.columns]

        for col in numeric_cols_all:
            if col in table_df.columns and col not in columns_order:
                columns_order.append(col)

        if "Additional Information" in table_df.columns:
            if "Additional Information" in columns_order:
                columns_order.remove("Additional Information")
            columns_order.append("Additional Information")

        missing_cols = [c for c in table_df.columns if c not in columns_order]
        columns_order.extend(missing_cols)

        table_df = table_df[columns_order]

        st.dataframe(
            table_df.sort_values("Date") if "Date" in table_df.columns else table_df,
            use_container_width=True,
        )

# -------------------------------------------------------------------
# TIME-LAPSE MODE
# -------------------------------------------------------------------
else:  # view_mode == "Time-lapse"
    st.subheader("Time-lapse: distribution over time")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        # Spacer to push the slider down closer to the vertical center of the map
        st.markdown("<div style='height: 240px;'></div>", unsafe_allow_html=True)

        year_tl = st.slider(
            "Year (time-lapse)",
            min_value=min_year_data,
            max_value=max_year_data,
            value=min_year_data,
            step=1,
            key="year_timelapse",
        )
        cumulative = st.checkbox(
            "Cumulative up to year",
            value=False,
            key="cumulative_timelapse",
        )

        if "Sheet" in df.columns:
            tl_type_options = sorted(df["Sheet"].dropna().unique())
            selected_types_tl = st.multiselect(
                "Type of Event (time-lapse)",
                options=tl_type_options,
                default=tl_type_options,
                key="types_timelapse",
            )
        else:
            selected_types_tl = None

    # Build mask for time-lapse
    if cumulative:
        tl_mask = df["year"] <= year_tl
    else:
        tl_mask = df["year"] == year_tl

    if selected_types_tl:
        tl_mask &= df["Sheet"].isin(selected_types_tl)

    df_tl = df[tl_mask].copy()

    with col_right:
        st.write(
            f"Showing {len(df_tl)} events "
            + ("up to" if cumulative else "in")
            + f" year {year_tl}"
        )

        if df_tl.empty:
            st.info("No events for this selection.")
        else:
            st.write("### Time-lapse map")

            mid_lat_tl = float(df_tl["Latitude"].mean())
            mid_lon_tl = float(df_tl["Longitude"].mean())

            m2 = folium.Map(
                location=[mid_lat_tl, mid_lon_tl],
                zoom_start=4,
                tiles=tiles_style,
            )

            if use_clustering:
                tl_marker_group = MarkerCluster().add_to(m2)
            else:
                tl_marker_group = m2

            for _, row in df_tl.iterrows():
                event_type = row.get("Type of Event", "")
                name = row.get("Name of Event", "")
                location = row.get("Location", "")
                date_val = row.get("Date", "")
                info = row.get("Additional Information", "")

                name_str = ""
                if pd.notna(name) and str(name).strip():
                    name_str = str(name).strip()

                if pd.isna(date_val):
                    date_str = ""
                else:
                    date_str = date_val.strftime("%Y-%m-%d")

                metric_lines = []
                for col in numeric_cols_all:
                    if col in df_tl.columns:
                        val = row.get(col)
                        if pd.notna(val):
                            metric_lines.append(f"{col}: {val:g}")
                metrics_html = "<br>".join(metric_lines)

                if event_type:
                    first_line = f"<b>{event_type}</b>"
                else:
                    first_line = "<b>Event</b>"

                if name_str:
                    first_line += f" – {name_str}"

                popup_parts = [first_line]

                if location:
                    popup_parts.append(str(location))
                if date_str:
                    popup_parts.append(date_str)
                if metrics_html:
                    popup_parts.append(metrics_html)
                if info:
                    popup_parts.append(f"<small>{info}</small>")

                popup_html = "<br>".join(popup_parts)
                popup_html_wrapped = (
                    f'<div style="font-size: 11px; line-height: 1.2;">{popup_html}</div>'
                )
                popup = folium.Popup(popup_html_wrapped, max_width=300)

                sheet_name = row.get("Sheet", "")
                color = sheet_color_map.get(sheet_name, "#000000")

                if use_emoji_markers:
                    emoji = emoji_for_key(
                        str(sheet_name), use_colored_emojis, default_emoji
                    )
                    icon_html = f"""
                    <div style="
                        font-size: 14px;
                        line-height: 14px;
                        text-align: center;
                        text-shadow:
                            -1px -1px 2px #fff,
                             1px -1px 2px #fff,
                            -1px  1px 2px #fff,
                             1px  1px 2px #fff;
                    ">{emoji}</div>
                    """
                    folium.Marker(
                        location=[row["Latitude"], row["Longitude"]],
                        icon=folium.DivIcon(html=icon_html),
                        popup=popup,
                        tooltip=(
                            f"{event_type} – {location}"
                            if event_type or location
                            else "Event"
                        ),
                    ).add_to(tl_marker_group)
                else:
                    folium.CircleMarker(
                        location=[row["Latitude"], row["Longitude"]],
                        radius=4,
                        color=color,
                        weight=1,
                        fill=True,
                        fill_color=color,
                        fill_opacity=0.8,
                        popup=popup,
                        tooltip=(
                            f"{event_type} – {location}"
                            if event_type or location
                            else "Event"
                        ),
                    ).add_to(tl_marker_group)

            st_folium(m2, width=None, height=650)

    # Type distribution below
    if not df_tl.empty and "Sheet" in df_tl.columns:
        st.write("### Type distribution for this selection")
        type_counts = (
            df_tl["Sheet"].value_counts().sort_index().rename("Count").to_frame()
        )
        st.bar_chart(type_counts)
