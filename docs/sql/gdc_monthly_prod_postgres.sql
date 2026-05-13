SELECT 
    uwi,
    volume_date,
    days_in_month,
    hours_on_prod,
    oil_volume_m3,
    oil_sales_volume_m3,
    gas_volume_e3m3,
    gas_sales_volume_e3m3,
    water_volume_m3,
    water_load_recovery_volume_m3,
    cond_volume_m3,
    pentane_plus_sales_volume_m3,
    field_cond_sales_volume_m3, --this is the sum of conde and pentane plus
    co2_volume_e3m3,
    ngl_sales_volume_m3,
    gas_injection_volume_e3m3,
    water_injection_volume_m3
FROM production.gdc_well_production_monthly_flat
WHERE "uwi" = '100151006003W600' -- Replace with your specific UWI
ORDER BY uwi, volume_date;