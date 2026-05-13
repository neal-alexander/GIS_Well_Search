import yaml
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.neighbors import BallTree
import os
import warnings

# Suppress seaborn palette warning
warnings.filterwarnings("ignore", category=UserWarning, module="seaborn")
warnings.filterwarnings("ignore", category=FutureWarning, module="seaborn")

def load_config(config_path="config.yaml"):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def get_db_engine(db_config):
    # Construct connection string
    conn_str = f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}"
    return create_engine(conn_str)

def main():
    # 1. Load Configuration
    config = load_config()
    db_config = config['db']
    search_radius_meters = config['search_parameters']['radius_meters']
    surfactant_uwis = config['surfactant_uwis']
    
    print(f"Loaded {len(surfactant_uwis)} surfactant UWIs.")
    print(f"Search Radius: {search_radius_meters} meters.")
    
    # 2. Connect to Database
    engine = get_db_engine(db_config)
    
    # 3. Query Well Header Data
    print("Querying master well ticket for coordinates and primary zone...")
    header_query = """
    SELECT uwi, bottom_hole_latitude, bottom_hole_longitude, primary_zone
    FROM corporate.master_well_ticket
    WHERE bottom_hole_latitude IS NOT NULL AND bottom_hole_longitude IS NOT NULL
    """
    df_headers = pd.read_sql(header_query, engine)
    
    # 4. Perform Spatial Search
    print("Performing spatial search...")
    # Convert lat/long to radians for BallTree (Haversine formula requires radians)
    df_headers['lat_rad'] = np.radians(df_headers['bottom_hole_latitude'])
    df_headers['lon_rad'] = np.radians(df_headers['bottom_hole_longitude'])
    
    # Create BallTree
    tree = BallTree(df_headers[['lat_rad', 'lon_rad']].values, metric='haversine')
    
    # Earth radius in meters
    EARTH_RADIUS_METERS = 6371000
    radius_rad = search_radius_meters / EARTH_RADIUS_METERS
    
    # Find indices of surfactant wells in the dataframe
    df_surfactant = df_headers[df_headers['uwi'].isin(surfactant_uwis)]
    
    if df_surfactant.empty:
        print("No surfactant wells found in the database with valid coordinates.")
        return
        
    # Query the tree for points within the radius for each surfactant well
    indices_within_radius = tree.query_radius(df_surfactant[['lat_rad', 'lon_rad']].values, r=radius_rad)
    
    # Filter neighbors to only those sharing the same primary_zone as the surfactant well
    valid_neighbor_labels = []
    for i, (_, s_row) in enumerate(df_surfactant.iterrows()):
        s_zone = s_row['primary_zone']
        neighbor_idx_array = indices_within_radius[i]
        
        # Get the neighbor rows using positional indices
        neighbors_df = df_headers.iloc[neighbor_idx_array]
        
        if pd.notna(s_zone):
            # Keep neighbors with the same primary_zone
            same_zone_neighbors = neighbors_df[neighbors_df['primary_zone'] == s_zone]
            valid_neighbor_labels.extend(same_zone_neighbors.index.tolist())
        else:
            # If the surfactant well has no primary_zone, default to keeping all its spatial neighbors
            valid_neighbor_labels.extend(neighbors_df.index.tolist())
            
    # Flatten and get unique pandas index labels
    unique_neighbor_labels = np.unique(valid_neighbor_labels)
    
    # Extract the UWIs for all filtered neighbors
    df_filtered_headers = df_headers.loc[unique_neighbor_labels]
    all_target_uwis = df_filtered_headers['uwi'].tolist()
    
    print(f"Found {len(all_target_uwis)} wells within {search_radius_meters}m of the surfactant wells that match primary_zone.")
    
    # 5. Query Production Data
    print("Querying production data...")
    uwi_list_str = "', '".join(all_target_uwis)
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
    df_prod = pd.read_sql(prod_query, engine)
    
    print(f"Loaded {len(df_prod)} production records.")
    
    if df_prod.empty:
        print("No production data found.")
        return
        
    # Merge primary zone info into production data
    df_prod = df_prod.merge(df_filtered_headers[['uwi', 'primary_zone']], on='uwi', how='left')
    
    # 6. Pre-calculate Rates and Cumulative Volumes for Time Series
    print("Calculating rates and cumulative volumes...")
    # Sort by uwi and date for rolling calculations
    df_prod['volume_date'] = pd.to_datetime(df_prod['volume_date'])
    df_prod = df_prod.sort_values(by=['uwi', 'volume_date'])
    
    # Calculate Cumulative Production
    df_prod['cum_oil'] = df_prod.groupby('uwi')['oil_volume_m3'].cumsum()
    df_prod['cum_gas'] = df_prod.groupby('uwi')['gas_volume_e3m3'].cumsum()
    df_prod['cum_water'] = df_prod.groupby('uwi')['water_volume_m3'].cumsum()
    
    # Calculate Rates
    # Using volume / (hours_on_prod/24) if hours_on_prod > 0, else volume / days_in_month
    # Handle NaNs and zeros
    df_prod['hours_on_prod'] = df_prod['hours_on_prod'].fillna(0)
    df_prod['days_in_month'] = df_prod['days_in_month'].fillna(30) # Default to 30 if missing
    
    # Define effective days: use hours_on_prod/24 if > 0, else days_in_month
    df_prod['effective_days'] = np.where(df_prod['hours_on_prod'] > 0, df_prod['hours_on_prod'] / 24, df_prod['days_in_month'])
    # Avoid division by zero
    df_prod['effective_days'] = df_prod['effective_days'].replace(0, np.nan)
    
    df_prod['oil_rate'] = df_prod['oil_volume_m3'] / df_prod['effective_days']
    df_prod['gas_rate'] = df_prod['gas_volume_e3m3'] / df_prod['effective_days']
    df_prod['water_rate'] = df_prod['water_volume_m3'] / df_prod['effective_days']
    
    # Add a column indicating if it's a surfactant well
    df_prod['is_surfactant'] = df_prod['uwi'].isin(surfactant_uwis).astype(str)
    
    # Aggregate Total Cumulative Production for spatial map & boxplots
    df_cum = df_prod.groupby('uwi').agg({
        'oil_volume_m3': 'sum',
        'gas_volume_e3m3': 'sum',
        'water_volume_m3': 'sum',
        'primary_zone': 'first'
    }).reset_index()
    
    df_cum = df_cum.merge(df_filtered_headers[['uwi', 'bottom_hole_latitude', 'bottom_hole_longitude']], on='uwi', how='left')
    df_cum['is_surfactant'] = df_cum['uwi'].isin(surfactant_uwis).astype(str)
    df_cum = df_cum.sort_values('is_surfactant', ascending=False)
    
    # 7. Visualization per Zone
    print("Generating plots per zone...")
    sns.set_theme(style="whitegrid")
    palette = {'True': '#E74C3C', 'False': '#3498DB'} # Red for surfactant, Blue for others
    
    # Get unique valid zones
    unique_zones = df_prod['primary_zone'].dropna().unique()
    
    # Create base results directory
    os.makedirs('results', exist_ok=True)
    
    for zone in unique_zones:
        # Sanitize zone name for folder
        safe_zone = "".join([c for c in zone if c.isalpha() or c.isdigit() or c==' ']).rstrip().replace(" ", "_")
        zone_dir = os.path.join('results', safe_zone)
        os.makedirs(zone_dir, exist_ok=True)
        print(f"Processing Zone: {zone} -> {zone_dir}")
        
        # Filter data for this zone
        z_cum = df_cum[df_cum['primary_zone'] == zone]
        z_prod = df_prod[df_prod['primary_zone'] == zone]
        
        if z_cum.empty or z_prod.empty:
            continue
            
        # 7a. Cumulative Production Map
        fig, axes = plt.subplots(1, 3, figsize=(18, 6))
        sns.scatterplot(data=z_cum, x='bottom_hole_longitude', y='bottom_hole_latitude', size='oil_volume_m3', hue='is_surfactant', palette=palette, sizes=(20, 400), alpha=0.7, ax=axes[0])
        axes[0].set_title(f'{zone} - Cumulative Oil (m3)')
        sns.scatterplot(data=z_cum, x='bottom_hole_longitude', y='bottom_hole_latitude', size='gas_volume_e3m3', hue='is_surfactant', palette=palette, sizes=(20, 400), alpha=0.7, ax=axes[1])
        axes[1].set_title(f'{zone} - Cumulative Gas (e3m3)')
        sns.scatterplot(data=z_cum, x='bottom_hole_longitude', y='bottom_hole_latitude', size='water_volume_m3', hue='is_surfactant', palette=palette, sizes=(20, 400), alpha=0.7, ax=axes[2])
        axes[2].set_title(f'{zone} - Cumulative Water (m3)')
        plt.tight_layout()
        plt.savefig(os.path.join(zone_dir, 'cumulative_production_map.png'), dpi=300)
        plt.close(fig)
        
        # 7b. Boxplots
        fig2, axes2 = plt.subplots(1, 3, figsize=(18, 6))
        sns.boxplot(data=z_cum, x='is_surfactant', y='oil_volume_m3', hue='is_surfactant', palette=palette, legend=False, ax=axes2[0])
        axes2[0].set_title(f'{zone} - Cumulative Oil Dist')
        sns.boxplot(data=z_cum, x='is_surfactant', y='gas_volume_e3m3', hue='is_surfactant', palette=palette, legend=False, ax=axes2[1])
        axes2[1].set_title(f'{zone} - Cumulative Gas Dist')
        sns.boxplot(data=z_cum, x='is_surfactant', y='water_volume_m3', hue='is_surfactant', palette=palette, legend=False, ax=axes2[2])
        axes2[2].set_title(f'{zone} - Cumulative Water Dist')
        plt.tight_layout()
        plt.savefig(os.path.join(zone_dir, 'cumulative_production_distribution.png'), dpi=300)
        plt.close(fig2)
        
        # 7c. Rate vs Cum Plots
        # Oil Rate vs Cum
        fig3, ax3 = plt.subplots(figsize=(10, 6))
        sns.scatterplot(data=z_prod, x='cum_oil', y='oil_rate', hue='is_surfactant', palette=palette, alpha=0.3, edgecolor=None, ax=ax3)
        ax3.set_title(f'{zone} - Oil Rate vs Cumulative Oil')
        ax3.set_xlabel('Cumulative Oil (m3)')
        ax3.set_ylabel('Daily Oil Rate (m3/d)')
        plt.tight_layout()
        plt.savefig(os.path.join(zone_dir, 'rate_vs_cum_oil.png'), dpi=300)
        plt.close(fig3)
        
        # Gas Rate vs Cum
        fig4, ax4 = plt.subplots(figsize=(10, 6))
        sns.scatterplot(data=z_prod, x='cum_gas', y='gas_rate', hue='is_surfactant', palette=palette, alpha=0.3, edgecolor=None, ax=ax4)
        ax4.set_title(f'{zone} - Gas Rate vs Cumulative Gas')
        ax4.set_xlabel('Cumulative Gas (e3m3)')
        ax4.set_ylabel('Daily Gas Rate (e3m3/d)')
        plt.tight_layout()
        plt.savefig(os.path.join(zone_dir, 'rate_vs_cum_gas.png'), dpi=300)
        plt.close(fig4)
        
        # Water Rate vs Cum
        fig5, ax5 = plt.subplots(figsize=(10, 6))
        sns.scatterplot(data=z_prod, x='cum_water', y='water_rate', hue='is_surfactant', palette=palette, alpha=0.3, edgecolor=None, ax=ax5)
        ax5.set_title(f'{zone} - Water Rate vs Cumulative Water')
        ax5.set_xlabel('Cumulative Water (m3)')
        ax5.set_ylabel('Daily Water Rate (m3/d)')
        plt.tight_layout()
        plt.savefig(os.path.join(zone_dir, 'rate_vs_cum_water.png'), dpi=300)
        plt.close(fig5)
        
    print("Analysis complete.")

if __name__ == "__main__":
    main()
