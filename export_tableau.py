import yaml
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
from sklearn.neighbors import BallTree
import pantab
import os

def load_config(config_path="config.yaml"):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def get_db_engine(db_config):
    conn_str = f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
    return create_engine(conn_str)

def main():
    print("Starting Tableau Export Process...")
    config = load_config()
    db_config = config['db']
    search_radius_meters = config['search_parameters']['radius_meters']
    surfactant_uwis = config['surfactant_uwis']
    
    engine = get_db_engine(db_config)
    
    # 1. Fetch Spatial Headers to Identify Neighbors
    print("Fetching spatial headers...")
    spatial_query = """
    SELECT uwi, bottom_hole_latitude, bottom_hole_longitude, primary_zone,
           spud_date, profile_type, heel_latitude, heel_longitude, license_substance,
           lateral_length, operator
    FROM corporate.master_well_ticket
    WHERE bottom_hole_latitude IS NOT NULL AND bottom_hole_longitude IS NOT NULL
    """
    df_spatial = pd.read_sql(spatial_query, engine)
    
    # Filter by spud_date
    df_spatial['spud_date'] = pd.to_datetime(df_spatial['spud_date'], errors='coerce')
    df_spatial = df_spatial[df_spatial['spud_date'].dt.year >= 2011]
    
    df_spatial['lat_rad'] = np.radians(df_spatial['bottom_hole_latitude'])
    df_spatial['lon_rad'] = np.radians(df_spatial['bottom_hole_longitude'])
    
    tree = BallTree(df_spatial[['lat_rad', 'lon_rad']].values, metric='haversine')
    EARTH_RADIUS_METERS = 6371000
    radius_rad = search_radius_meters / EARTH_RADIUS_METERS
    
    df_surfactant = df_spatial[df_spatial['uwi'].isin(surfactant_uwis)]
    if df_surfactant.empty:
        print("No surfactant wells found with coordinates.")
        return
        
    indices_within_radius = tree.query_radius(df_surfactant[['lat_rad', 'lon_rad']].values, r=radius_rad)
    
    valid_neighbor_labels = []
    for i, (_, s_row) in enumerate(df_surfactant.iterrows()):
        s_zone = s_row['primary_zone']
        neighbors_df = df_spatial.iloc[indices_within_radius[i]]
        if pd.notna(s_zone):
            valid_neighbor_labels.extend(neighbors_df[neighbors_df['primary_zone'] == s_zone].index.tolist())
        else:
            valid_neighbor_labels.extend(neighbors_df.index.tolist())
            
    unique_neighbor_labels = np.unique(valid_neighbor_labels)
    all_target_uwis = df_spatial.loc[unique_neighbor_labels, 'uwi'].tolist()
    
    print(f"Identified {len(all_target_uwis)} total wells (surfactant + neighbors).")
    
    # 2. Fetch Full Well Ticket Data for Target Wells
    print("Fetching full well ticket metadata...")
    uwi_list_str = "', '".join(all_target_uwis)
    full_header_query = f"""
    SELECT *
    FROM corporate.master_well_ticket
    WHERE uwi IN ('{uwi_list_str}')
    """
    df_full_headers = pd.read_sql(full_header_query, engine)
    
    # Add well_type
    df_full_headers['well_type'] = np.where(df_full_headers['uwi'].isin(surfactant_uwis), 'Surfactant', 'Neighbor')
    
    # 3. Fetch Production Data
    print("Fetching production data...")
    prod_query = f"""
    SELECT 
        uwi, volume_date, days_in_month, hours_on_prod,
        oil_volume_m3, gas_volume_e3m3, water_volume_m3
    FROM production.gdc_well_production_monthly_flat
    WHERE uwi IN ('{uwi_list_str}')
    """
    df_prod = pd.read_sql(prod_query, engine)
    
    if df_prod.empty:
        print("No production data found for these wells.")
        return
        
    # 4. Calculate Metrics
    print("Calculating metrics (cumulative volumes, rates, months online)...")
    df_prod['volume_date'] = pd.to_datetime(df_prod['volume_date'])
    df_prod = df_prod.sort_values(by=['uwi', 'volume_date'])
    
    # Remove leading zeros
    df_prod['total_vol'] = df_prod['oil_volume_m3'].fillna(0) + df_prod['gas_volume_e3m3'].fillna(0) + df_prod['water_volume_m3'].fillna(0)
    first_prod = df_prod[df_prod['total_vol'] > 0].groupby('uwi')['volume_date'].min().reset_index()
    first_prod.rename(columns={'volume_date': 'first_prod_date'}, inplace=True)
    
    df_prod = df_prod.merge(first_prod, on='uwi', how='inner')
    df_prod = df_prod[df_prod['volume_date'] >= df_prod['first_prod_date']]
    df_prod.drop(columns=['total_vol', 'first_prod_date'], inplace=True)
    
    df_prod['months_online'] = df_prod.groupby('uwi').cumcount() + 1
    
    df_prod['cum_oil_m3'] = df_prod.groupby('uwi')['oil_volume_m3'].cumsum()
    df_prod['cum_gas_e3m3'] = df_prod.groupby('uwi')['gas_volume_e3m3'].cumsum()
    df_prod['cum_water_m3'] = df_prod.groupby('uwi')['water_volume_m3'].cumsum()
    
    df_prod['hours_on_prod'] = df_prod['hours_on_prod'].fillna(0)
    df_prod['days_in_month'] = df_prod['days_in_month'].fillna(30)
    df_prod['effective_days'] = np.where(df_prod['hours_on_prod'] > 0, df_prod['hours_on_prod'] / 24, df_prod['days_in_month'])
    df_prod['effective_days'] = df_prod['effective_days'].replace(0, np.nan)
    
    df_prod['oil_rate_m3_d'] = df_prod['oil_volume_m3'] / df_prod['effective_days']
    df_prod['gas_rate_e3m3_d'] = df_prod['gas_volume_e3m3'] / df_prod['effective_days']
    df_prod['water_rate_m3_d'] = df_prod['water_volume_m3'] / df_prod['effective_days']
    df_prod.drop(columns=['effective_days'], inplace=True)
    
    # 5. Merge into Massive Flat Table
    print("Merging well tickets with production data...")
    df_final = df_prod.merge(df_full_headers, on='uwi', how='left')
    
    # 6. Export to Tableau Hyper
    print("Exporting to Tableau Hyper file...")
    os.makedirs('results', exist_ok=True)
    export_path = os.path.join('results', 'tableau_analysis_extract.hyper')
    
    # Drop completely empty columns which cause Arrow 'na' type errors
    df_final = df_final.dropna(axis=1, how='all')
    
    # Ensure all datetime columns are timezone-naive (pantab requirement)
    for col in df_final.select_dtypes(include=['datetimetz']).columns:
        df_final[col] = df_final[col].dt.tz_localize(None)
        
    # Convert remaining object columns with missing values to string to avoid inference issues
    for col in df_final.select_dtypes(include=['object']).columns:
        df_final[col] = df_final[col].fillna('')
        df_final[col] = df_final[col].astype(str)
        
    pantab.frame_to_hyper(df_final, export_path, table="well_data")
    
    print(f"✅ Success! Exported {len(df_final)} records to {export_path}")

if __name__ == "__main__":
    main()
