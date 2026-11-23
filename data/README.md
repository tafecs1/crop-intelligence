# Optional input files

Put these here only if you have them (the run works without them):

| File | What it adds |
|---|---|
| `apy_district.csv` | DES district Area-Production-Yield (Govt crop statistics) → names crops other than rice/wheat/maize |
| `field_observations.csv` | Ground truth: `tehsil_lgd,crop,stage,observation_date,planting_date` → real accuracy in the Validation sheet |
| `*harvested_area*.zip` | IFPRI SPAM 2020 harvested-area file (GitHub refuses files over 100 MB – keep it out if larger) |

Never put keys or credentials in this folder – the repo is public.
