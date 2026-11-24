# CropIntel – All-India tehsil crop intelligence

Weekly GitHub Actions run of CropIntel v4.0 that writes 8 tabs to a Google Sheet:
`Crop_Intelligence_Current`, `NDVI_MoM`, `EVI_MoM`, `NDWI_MoM`, `NDRE_MoM`, `SAVI_MoM`, `BSI_MoM`, `Ideal_Crop_Window`.
An Excel copy (plus Area_Balance, Data_Sources, References, Validation) is attached to every run as an artifact.

## How a run works
1. **setup** – fixes one run date (IST) for all jobs.
2. **extract** – N parallel jobs; each takes a set of whole states and pulls Earth Engine data into `work/cache`.
3. **build** – merges all caches, runs the full pipeline (no repeat Earth Engine calls for cached batches) and writes the Sheet.

Schedule: every Monday 02:00 IST. Manual run: Actions → CropIntel weekly → Run workflow (optional states / shard count).

## Secrets (Settings → Secrets and variables → Actions)
| Secret | Value |
|---|---|
| `EE_KEY_JSON` | full JSON key of the service account registered for Earth Engine |
| `EE_PROJECT` | Google Cloud project ID used for Earth Engine |
| `CROP_SHEET_ID` | Google Sheet ID (or full link) |
| `SHEETS_KEY_JSON` | optional – a different service-account key for Sheets; default is `EE_KEY_JSON` |

Share the Google Sheet with the service-account e-mail as **Editor**.

## Optional inputs
v4.0: every stage is satellite-observed, model-inferred from an OBSERVED planting date, field-verified, or an explicit
"Stage unresolved – …" status with its reason and next action. Crop statistics (CROPGRIDS 2020, optional SPAM / APY) are only listed as
candidates. Tests: `pytest -q tests` (also run by the workflow before extraction). Details: `docs/CORRECTION_REPORT.md`.
Optional extras: see `data/README.md` (Govt APY file, field observations, SPAM 2020).

## Join key for dashboards
`Tehsil LGD Code` (text) from the LGD sub-district file on bharatlas.com – use it, not names, to join with the Agri, Weather and Groundwater sheets.
