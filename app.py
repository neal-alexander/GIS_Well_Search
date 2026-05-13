import streamlit as st
import yaml
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import plotly.express as px
import plotly.graph_objects as go
from sklearn.neighbors import BallTree

# Set Page Config
st.set_page_config(page_title="Well Performance Dashboard", layout="wide", initial_sidebar_state="expanded")

# --- CSS Styling for Premium Feel ---
st.markdown("""
<style>
    .main .block-container {
        padding-top: 2rem;
    }
    .stMetric {
        background-color: var(--secondary-background-color);
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.2);
    }
</style>
""", unsafe_allow_html=True)

# --- Data Loading and Processing Functions ---

@st.cache_data
def load_config():
    with open("config.yaml", 'r') as f:
        return yaml.safe_load(f)

@st.cache_resource
def get_db_engine():
    config = load_config()
    db_config = config['db']
    conn_str = f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
    return create_engine(conn_str)

@st.cache_data(show_spinner=False)
def fetch_well_headers_filtered():
    engine = get_db_engine()
    header_query = """
    SELECT uwi, bottom_hole_latitude, bottom_hole_longitude, primary_zone, field_name,
           spud_date, profile_type, heel_latitude, heel_longitude, license_substance,
           lateral_length, operator
    FROM corporate.master_well_ticket
    WHERE bottom_hole_latitude IS NOT NULL AND bottom_hole_longitude IS NOT NULL
    """
    df = pd.read_sql(header_query, engine)
    
    # Hard filter for Spud Date >= 2011
    df['spud_date'] = pd.to_datetime(df['spud_date'], errors='coerce')
    df = df[df['spud_date'].dt.year >= 2011]
    
    return df

@st.cache_data(show_spinner=False)
def process_spatial_data(df_headers, search_radius_meters, surfactant_uwis):
    df_headers['lat_rad'] = np.radians(df_headers['bottom_hole_latitude'])
    df_headers['lon_rad'] = np.radians(df_headers['bottom_hole_longitude'])
    
    tree = BallTree(df_headers[['lat_rad', 'lon_rad']].values, metric='haversine')
    EARTH_RADIUS_METERS = 6371000
    radius_rad = search_radius_meters / EARTH_RADIUS_METERS
    
    df_surfactant = df_headers[df_headers['uwi'].isin(surfactant_uwis)]
    
    if df_surfactant.empty:
        return pd.DataFrame(), []
        
    indices_within_radius = tree.query_radius(df_surfactant[['lat_rad', 'lon_rad']].values, r=radius_rad)
    
    valid_neighbor_labels = []
    for i, (_, s_row) in enumerate(df_surfactant.iterrows()):
        s_zone = s_row['primary_zone']
        neighbor_idx_array = indices_within_radius[i]
        neighbors_df = df_headers.iloc[neighbor_idx_array]
        
        if pd.notna(s_zone):
            same_zone_neighbors = neighbors_df[neighbors_df['primary_zone'] == s_zone]
            valid_neighbor_labels.extend(same_zone_neighbors.index.tolist())
        else:
            valid_neighbor_labels.extend(neighbors_df.index.tolist())
            
    unique_neighbor_labels = np.unique(valid_neighbor_labels)
    df_filtered_headers = df_headers.loc[unique_neighbor_labels].copy()
    
    # Add a well type column
    df_filtered_headers['well_type'] = np.where(df_filtered_headers['uwi'].isin(surfactant_uwis), 'Surfactant', 'Neighbor')
    
    all_target_uwis = df_filtered_headers['uwi'].tolist()
    return df_filtered_headers, all_target_uwis

@st.cache_data(show_spinner=False)
def fetch_production_data(uwi_list):
    if not uwi_list:
        return pd.DataFrame()
        
    engine = get_db_engine()
    uwi_list_str = "', '".join(uwi_list)
    prod_query = f"""
    SELECT 
        uwi,
        volume_date,
        days_in_month,
        hours_on_prod,
        oil_volume_m3,
        gas_volume_e3m3,
        water_volume_m3
    FROM production.gdc_well_production_monthly_flat
    WHERE uwi IN ('{uwi_list_str}')
    """
    return pd.read_sql(prod_query, engine)

@st.cache_data(show_spinner=False)
def calculate_metrics(df_prod, df_filtered_headers):
    if df_prod.empty:
        return pd.DataFrame(), pd.DataFrame()
        
    df_prod = df_prod.merge(df_filtered_headers[['uwi', 'primary_zone', 'field_name', 'well_type', 'profile_type', 'license_substance', 'lateral_length', 'operator']], on='uwi', how='left')
    
    df_prod['volume_date'] = pd.to_datetime(df_prod['volume_date'])
    df_prod = df_prod.sort_values(by=['uwi', 'volume_date'])
    
    # Remove leading zeros before a well's first production
    df_prod['total_vol'] = df_prod['oil_volume_m3'].fillna(0) + df_prod['gas_volume_e3m3'].fillna(0) + df_prod['water_volume_m3'].fillna(0)
    first_prod = df_prod[df_prod['total_vol'] > 0].groupby('uwi')['volume_date'].min().reset_index()
    first_prod.rename(columns={'volume_date': 'first_prod_date'}, inplace=True)
    
    df_prod = df_prod.merge(first_prod, on='uwi', how='inner')
    df_prod = df_prod[df_prod['volume_date'] >= df_prod['first_prod_date']]
    df_prod = df_prod.drop(columns=['total_vol', 'first_prod_date'])
    
    # Calculate running months online for each well
    df_prod['months_online'] = df_prod.groupby('uwi').cumcount() + 1
    
    if df_prod.empty:
        return pd.DataFrame(), pd.DataFrame()
        
    df_prod['cum_oil'] = df_prod.groupby('uwi')['oil_volume_m3'].cumsum()
    df_prod['cum_gas'] = df_prod.groupby('uwi')['gas_volume_e3m3'].cumsum()
    df_prod['cum_water'] = df_prod.groupby('uwi')['water_volume_m3'].cumsum()
    
    df_prod['hours_on_prod'] = df_prod['hours_on_prod'].fillna(0)
    df_prod['days_in_month'] = df_prod['days_in_month'].fillna(30)
    
    df_prod['effective_days'] = np.where(df_prod['hours_on_prod'] > 0, df_prod['hours_on_prod'] / 24, df_prod['days_in_month'])
    df_prod['effective_days'] = df_prod['effective_days'].replace(0, np.nan)
    
    df_prod['oil_rate'] = df_prod['oil_volume_m3'] / df_prod['effective_days']
    df_prod['gas_rate'] = df_prod['gas_volume_e3m3'] / df_prod['effective_days']
    df_prod['water_rate'] = df_prod['water_volume_m3'] / df_prod['effective_days']
    
    # Calculate 12-Month Cumulative
    df_cum_12mo = df_prod[df_prod['months_online'] <= 12]
    df_cum = df_cum_12mo.groupby('uwi').agg({
        'oil_volume_m3': 'sum',
        'gas_volume_e3m3': 'sum',
        'water_volume_m3': 'sum',
        'primary_zone': 'first',
        'field_name': 'first',
        'well_type': 'first',
        'profile_type': 'first',
        'license_substance': 'first',
        'operator': 'first',
        'lateral_length': 'first'
    }).reset_index()
    
    df_cum = df_cum.merge(df_filtered_headers[['uwi', 'bottom_hole_latitude', 'bottom_hole_longitude', 'heel_latitude', 'heel_longitude']], on='uwi', how='left')
    
    # Sort so Surfactant is plotted on top/last
    df_cum = df_cum.sort_values('well_type', ascending=True)
    
    return df_prod, df_cum

# --- Main Application Logic ---

def main():
    st.title("🛢️ GIS Well Performance Dashboard")
    
    # Load config and initial data
    with st.spinner("Loading configuration and master well data..."):
        config = load_config()
        search_radius_meters = config['search_parameters']['radius_meters']
        surfactant_uwis = config['surfactant_uwis']
        df_headers = fetch_well_headers_filtered()
        
    with st.spinner("Calculating spatial relationships..."):
        df_filtered_headers, all_target_uwis = process_spatial_data(df_headers, search_radius_meters, surfactant_uwis)
        
    if df_filtered_headers.empty:
        st.error("No surfactant wells found in the database with valid coordinates.")
        return
        
    with st.spinner("Fetching production data..."):
        df_prod = fetch_production_data(all_target_uwis)
        
    with st.spinner("Calculating performance metrics..."):
        df_prod, df_cum = calculate_metrics(df_prod, df_filtered_headers)
        
    if df_cum.empty:
        st.warning("No production data available for the targeted wells.")
        return

    # --- Sidebar Filtering ---
    st.sidebar.header("Filter Options")
    
    # Zone selector
    unique_zones = sorted([z for z in df_cum['primary_zone'].unique() if pd.notna(z)])
    selected_zone = st.sidebar.selectbox("Select Formation/Zone", ["All Formations"] + unique_zones)
    
    # Fluid selector
    fluid_type = st.sidebar.radio("Select Primary Fluid for Maps/Plots", ["Oil", "Gas", "Water"])
    
    # Profile Type selector
    unique_profiles = sorted([p for p in df_cum['profile_type'].unique() if pd.notna(p)])
    selected_profile = st.sidebar.selectbox("Select Profile Type", ["All Profiles"] + unique_profiles)
    
    # License Substance selector
    unique_substances = sorted([s for s in df_cum['license_substance'].unique() if pd.notna(s)])
    selected_substance = st.sidebar.selectbox("Select License Substance", ["All Substances"] + unique_substances)
    
    # Operator selector
    unique_operators = sorted([o for o in df_cum['operator'].unique() if pd.notna(o)])
    selected_operator = st.sidebar.selectbox("Select Operator", ["All Operators"] + unique_operators)
    
    st.sidebar.markdown("---")
    normalize_length = st.sidebar.checkbox("Normalize metrics to 1000m (Hz Wells)", value=False)
    
    # Filter Dataframes
    df_cum_filtered = df_cum.copy()
    df_prod_filtered = df_prod.copy()
    
    if selected_zone != "All Formations":
        df_cum_filtered = df_cum_filtered[df_cum_filtered['primary_zone'] == selected_zone]
        df_prod_filtered = df_prod_filtered[df_prod_filtered['primary_zone'] == selected_zone]
        
    if selected_profile != "All Profiles":
        df_cum_filtered = df_cum_filtered[df_cum_filtered['profile_type'] == selected_profile]
        df_prod_filtered = df_prod_filtered[df_prod_filtered['profile_type'] == selected_profile]
        
    if selected_substance != "All Substances":
        df_cum_filtered = df_cum_filtered[df_cum_filtered['license_substance'] == selected_substance]
        df_prod_filtered = df_prod_filtered[df_prod_filtered['license_substance'] == selected_substance]
        
    if selected_operator != "All Operators":
        df_cum_filtered = df_cum_filtered[df_cum_filtered['operator'] == selected_operator]
        df_prod_filtered = df_prod_filtered[df_prod_filtered['operator'] == selected_operator]
        
    # Apply Length Normalization
    if normalize_length:
        for df in [df_cum_filtered, df_prod_filtered]:
            df['scaling_factor'] = 1.0
            hz_mask = (df['profile_type'] == 'HZ') & (df['lateral_length'] > 0)
            df.loc[hz_mask, 'scaling_factor'] = 1000.0 / df.loc[hz_mask, 'lateral_length']
            
            # Apply to volumes and rates
            if 'oil_volume_m3' in df.columns:
                df['oil_volume_m3'] *= df['scaling_factor']
                df['gas_volume_e3m3'] *= df['scaling_factor']
                df['water_volume_m3'] *= df['scaling_factor']
            if 'oil_rate' in df.columns:
                df['oil_rate'] *= df['scaling_factor']
                df['gas_rate'] *= df['scaling_factor']
                df['water_rate'] *= df['scaling_factor']
                df['cum_oil'] *= df['scaling_factor']
                df['cum_gas'] *= df['scaling_factor']
                df['cum_water'] *= df['scaling_factor']
        
    # --- Top KPIs ---
    surfactant_count = len(df_cum_filtered[df_cum_filtered['well_type'] == 'Surfactant'])
    neighbor_count = len(df_cum_filtered[df_cum_filtered['well_type'] == 'Neighbor'])
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Surfactant Wells", surfactant_count)
    col2.metric("Neighbor Wells", neighbor_count)
    
    # Cumulative metric based on fluid
    if fluid_type == "Oil":
        total_fluid = df_cum_filtered['oil_volume_m3'].sum()
        col3.metric(f"Total 12-Month Cum. {fluid_type} (m³)", f"{total_fluid:,.0f}")
    elif fluid_type == "Gas":
        total_fluid = df_cum_filtered['gas_volume_e3m3'].sum()
        col3.metric(f"Total 12-Month Cum. {fluid_type} (e³m³)", f"{total_fluid:,.0f}")
    else:
        total_fluid = df_cum_filtered['water_volume_m3'].sum()
        col3.metric(f"Total 12-Month Cum. {fluid_type} (m³)", f"{total_fluid:,.0f}")
        
    # Average Months Online
    if not df_prod_filtered.empty:
        avg_months = df_prod_filtered.groupby('uwi')['months_online'].max().mean()
        col4.metric("Avg Months Online", f"{avg_months:.1f}")
    else:
        col4.metric("Avg Months Online", "0.0")

    st.markdown("---")

    # --- Geospatial Mapping (Mapbox) ---
    st.subheader(f"Geospatial Distribution ({selected_zone})")
    
    color_discrete_map = {'Surfactant': '#E74C3C', 'Neighbor': '#3498DB'}
    
    # Determine size column based on fluid
    size_col = 'oil_volume_m3' if fluid_type == "Oil" else 'gas_volume_e3m3' if fluid_type == "Gas" else 'water_volume_m3'
    max_size = df_cum_filtered[size_col].max()
    
    # Build Traces for Mapbox
    traces = []
    
    # 1. Horizontal Well Lines
    for well_type, color in color_discrete_map.items():
        type_df = df_cum_filtered[df_cum_filtered['well_type'] == well_type]
        lats = []
        lons = []
        for _, row in type_df.iterrows():
            if pd.notna(row['heel_latitude']) and pd.notna(row['heel_longitude']):
                lats.extend([row['heel_latitude'], row['bottom_hole_latitude'], None])
                lons.extend([row['heel_longitude'], row['bottom_hole_longitude'], None])
        
        if lats:
            traces.append(go.Scattermapbox(
                mode="lines",
                lat=lats,
                lon=lons,
                line=dict(width=2, color=color),
                hoverinfo="skip",
                showlegend=False
            ))
            
    # 2. Well Points (Bottom Hole) for Interaction and Sizing
    for well_type, color in color_discrete_map.items():
        type_df = df_cum_filtered[df_cum_filtered['well_type'] == well_type]
        if type_df.empty: continue
        
        hover_texts = []
        for _, row in type_df.iterrows():
            text = (
                f"<b>UWI:</b> {row['uwi']}<br>"
                f"<b>Formation:</b> {row['primary_zone']}<br>"
                f"<b>Field:</b> {row['field_name']}<br>"
                f"<b>Operator:</b> {row['operator']}<br>"
                f"<b>12-Month Cum {fluid_type}:</b> {row[size_col]:,.0f}"
            )
            hover_texts.append(text)
            
        traces.append(go.Scattermapbox(
            mode="markers",
            lat=type_df["bottom_hole_latitude"],
            lon=type_df["bottom_hole_longitude"],
            marker=dict(
                size=type_df[size_col],
                sizemode="area",
                sizeref=max_size / (40**2) if max_size > 0 else 1,
                sizemin=5,
                color=color,
                opacity=0.7
            ),
            hovertext=hover_texts,
            hoverinfo="text",
            name=well_type,
            customdata=type_df[["uwi"]].values
        ))
        
    fig_map = go.Figure(data=traces)
    
    # Default center
    center_lat = df_cum_filtered["bottom_hole_latitude"].mean() if not df_cum_filtered.empty else 55.0
    center_lon = df_cum_filtered["bottom_hole_longitude"].mean() if not df_cum_filtered.empty else -115.0
    
    fig_map.update_layout(
        mapbox_style="carto-positron",
        mapbox=dict(center=dict(lat=center_lat, lon=center_lon), zoom=7),
        margin={"r":0,"t":40,"l":0,"b":0},
        legend_title_text='Well Type',
        title=f"Well Map Scaled by 12-Month Cumulative {fluid_type}"
    )
    
    # Adding key="well_map" persists the selection state across widget interactions
    map_selection = st.plotly_chart(
        fig_map, 
        use_container_width=True, 
        on_select="rerun", 
        selection_mode=("points", "box", "lasso"),
        config={'scrollZoom': True, 'displayModeBar': True},
        key="well_map"
    )
    
    st.markdown("---")
    
    # Process Map Selection
    if "persistent_selection" not in st.session_state:
        st.session_state.persistent_selection = set()
        
    if map_selection and "selection" in map_selection:
        points = map_selection["selection"].get("points", [])
        if points:
            current_selection = set()
            for pt in points:
                if "customdata" in pt and len(pt["customdata"]) > 0:
                    current_selection.add(pt["customdata"][0])
                elif "hovertext" in pt:
                    current_selection.add(pt["hovertext"])
            st.session_state.persistent_selection = current_selection
            
    selected_uwis = st.session_state.persistent_selection
    
    col_a, col_b = st.columns([1, 5])
    with col_a:
        if st.button("Clear Selection", use_container_width=True):
            st.session_state.persistent_selection = set()
            selected_uwis = set()
            # Note: The map will still physically show the lasso until another map interaction, 
            # but the plots below will immediately reset.
            
    # Filter for plots if any wells are selected
    if selected_uwis:
        df_prod_filtered = df_prod_filtered[df_prod_filtered['uwi'].isin(selected_uwis)]
        df_cum_filtered = df_cum_filtered[df_cum_filtered['uwi'].isin(selected_uwis)]
        with col_b:
            st.success(f"Filtered plots to {len(selected_uwis)} selected well(s).")
    else:
        with col_b:
            st.info("💡 **Tip:** Use the Box Select or Lasso Select tool on the map above to filter the plots below! You can also use your mouse wheel to zoom.")
    
    
    # --- Performance Plots ---
    st.subheader("Performance Analysis")
    
    tab1, tab2, tab3 = st.tabs(["Rate vs Cumulative", "Months Online vs Cumulative", "Cumulative Distribution"])
    
    with tab1:
        st.markdown(f"#### {fluid_type} Rate vs. Cumulative Production")
        
        suffix = " / 1000m" if normalize_length else ""
        if fluid_type == "Oil":
            x_col, y_col = 'cum_oil', 'oil_rate'
            x_title, y_title = f'Cumulative Oil (m³{suffix})', f'Daily Oil Rate (m³/d{suffix})'
        elif fluid_type == "Gas":
            x_col, y_col = 'cum_gas', 'gas_rate'
            x_title, y_title = f'Cumulative Gas (e³m³{suffix})', f'Daily Gas Rate (e³m³/d{suffix})'
        else:
            x_col, y_col = 'cum_water', 'water_rate'
            x_title, y_title = f'Cumulative Water (m³{suffix})', f'Daily Water Rate (m³/d{suffix})'
            
        fig_rate = px.line(
            df_prod_filtered,
            x=x_col,
            y=y_col,
            line_group="uwi",
            color="well_type",
            color_discrete_map=color_discrete_map,
            hover_name="uwi",
            hover_data=['volume_date', 'primary_zone', 'field_name', 'operator'],
            title=f"{fluid_type} Rate Decline (Rate vs. Cumulative)"
        )
        fig_rate.update_traces(opacity=0.6, line=dict(width=1.5))
        fig_rate.update_layout(xaxis_title=x_title, yaxis_title=y_title, legend_title_text='Well Type')
        # Add slight log scale option for rate
        use_log = st.checkbox(f"Log Scale for {fluid_type} Rate", value=False)
        if use_log:
            fig_rate.update_yaxes(type="log")
            
        st.plotly_chart(fig_rate, use_container_width=True)
        
    with tab2:
        st.markdown(f"#### Cumulative {fluid_type} over Months Online")
        
        fig_time_cum = px.line(
            df_prod_filtered,
            x='months_online',
            y=x_col, # x_col holds the cumulative volume column name based on fluid_type
            line_group="uwi",
            color="well_type",
            color_discrete_map=color_discrete_map,
            hover_name="uwi",
            hover_data=['volume_date', 'primary_zone', 'field_name', 'operator'],
            title=f"{fluid_type} Type Curve (Months Online vs. Cumulative)"
        )
        fig_time_cum.update_traces(opacity=0.6, line=dict(width=1.5))
        fig_time_cum.update_layout(xaxis_title="Months Online", yaxis_title=x_title, legend_title_text='Well Type')
        
        st.plotly_chart(fig_time_cum, use_container_width=True)
        
    with tab3:
        st.markdown(f"#### Distribution of 12-Month Cumulative {fluid_type}")
        
        fig_box = px.box(
            df_cum_filtered,
            x="well_type",
            y=size_col,
            color="well_type",
            color_discrete_map=color_discrete_map,
            hover_name="uwi",
            hover_data=['primary_zone', 'field_name', 'operator'],
            points="all",
            title=f"12-Month Cumulative {fluid_type} Distribution by Well Type"
        )
        y_axis_label = f'12-Month Cumulative Oil (m³{suffix})' if fluid_type == "Oil" else f'12-Month Cumulative Gas (e³m³{suffix})' if fluid_type == "Gas" else f'12-Month Cumulative Water (m³{suffix})'
        fig_box.update_layout(xaxis_title="Well Type", yaxis_title=y_axis_label, showlegend=False)
        
        st.plotly_chart(fig_box, use_container_width=True)

if __name__ == "__main__":
    main()
