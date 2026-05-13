# GIS Well Performance Dashboard

An interactive Streamlit dashboard for evaluating surfactant-treated well performance against offset neighbors using geospatial proximity analysis and production data from a PostgreSQL (GDC) database.

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-1.30+-FF4B4B?logo=streamlit&logoColor=white)
![Plotly](https://img.shields.io/badge/Plotly-Mapbox-3F4F75?logo=plotly&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-GDC-4169E1?logo=postgresql&logoColor=white)

---

## Features

### Interactive Map
- **Mapbox GIS visualization** with well markers scaled by 12-month cumulative production.
- **Horizontal lateral trajectories** drawn as lines from Heel to Bottom Hole coordinates.
- **Lasso / Box Select** to spatially filter all downstream performance charts.
- Rich hover tooltips showing UWI, Formation, Field, Operator, and cumulative volume.

### Performance Charts
- **Rate vs. Cumulative** decline curves (with optional log-scale toggle).
- **Months Online vs. Cumulative** type curves for benchmarking well maturity.
- **12-Month Cumulative Distribution** box plots comparing Surfactant vs. Neighbor populations.

### Sidebar Filters
| Filter | Description |
|---|---|
| **Formation / Zone** | Filter by producing formation (e.g., `KCARD_SS`). |
| **Fluid Type** | Toggle the primary fluid for all charts (Oil / Gas / Water). |
| **Profile Type** | Horizontal (`HZ`), Vertical (`V`), Directional (`D`), or All. |
| **License Substance** | Filter by licensed substance (Oil vs. Gas fluid system). |
| **Operator** | Filter by producing operator. |
| **Normalize to 1000m** | Dynamically scale all Hz well metrics to a standard 1000m lateral length. |

### Tableau Export
- Standalone script (`export_tableau.py`) generates a `.hyper` file containing a flattened, production-normalized dataset with full well ticket metadata — ready for drag-and-drop analysis in Tableau.

---

## Project Structure

```
GIS_Well_Search/
├── app.py                  # Main Streamlit dashboard
├── analysis.py             # Standalone batch analysis (matplotlib output)
├── export_tableau.py       # Tableau .hyper file generator
├── config.example.yaml     # Template configuration (copy to config.yaml)
├── requirements.txt        # Python dependencies
├── docs/
│   └── sql/                # Reference SQL schemas
│       ├── corporate_master_well_ticket.sql
│       ├── fdc_full_production_uwi_daily.sql
│       ├── gdc_deviation_survey_station.sql
│       ├── gdc_monthly_prod_postgres.sql
│       └── master_well_ticket_shortened.sql
├── results/                # Generated outputs (gitignored)
├── venv/                   # Local virtual environment (gitignored)
└── .gitignore
```

---

## Getting Started

### Prerequisites
- **Python 3.10+**
- Network access to the PostgreSQL (GDC) database.

### 1. Clone the Repository

```bash
git clone https://github.com/<your-username>/GIS_Well_Search.git
cd GIS_Well_Search
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
```

Activate it:

```powershell
# Windows (PowerShell)
.\venv\Scripts\Activate.ps1
```

> **Note:** If you normally use Conda, make sure to deactivate it first:
> ```powershell
> conda deactivate
> .\venv\Scripts\Activate.ps1
> ```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Database Credentials

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your actual database host, credentials, and surfactant UWI list.

### 5. Run the Dashboard

```bash
streamlit run app.py
```

The dashboard will open at `http://localhost:8501`.

### 6. Export to Tableau (Optional)

```bash
python export_tableau.py
```

This generates `results/tableau_analysis_extract.hyper`.

---

## How It Works

1. **Spatial Search**: For each surfactant well in `config.yaml`, a BallTree radius search identifies all neighboring wells within the configured `radius_meters` that share the same producing formation.
2. **Data Pipeline**: Monthly production data is fetched, leading zero-production months are stripped, and cumulative volumes / daily rates are calculated.
3. **12-Month Benchmark**: The map bubble sizes and distribution statistics are locked to 12-month cumulative volumes for fair comparison across wells of different ages.
4. **Length Normalization**: When enabled, horizontal well metrics are scaled by `1000 / lateral_length` to normalize performance to a standard 1000m lateral.

---

## Configuration

All runtime parameters are controlled via `config.yaml`:

```yaml
db:
  host: "your-db-host"
  port: 5444
  database: "your-database"
  user: "your-username"
  password: "your-password"

search_parameters:
  radius_meters: 2000      # Neighbor search radius in meters

surfactant_uwis:
  - "100012104009W500"      # List of surfactant-treated well UWIs
  - "102012104009W500"
```

After editing `config.yaml`, clear the Streamlit cache to pick up changes:
- In the browser: click the **⋮** menu (top-right) → **Clear cache**, then press `R` to rerun.

---

## License

This project is intended for internal use. Contact the repository owner for access and usage terms.
