# CropIntel v4.0 – audit and correction report

Baseline: `Crop Intelligence (1).xlsx`, run RUN-20261124-073719, model CropIntel-v3.5, Tamil Nadu (291 tehsils, 5,036 crop rows).
Corrected code: `crop_intel.py` (built from the Colab source by `build_github.py`), workflow `.github/workflows/cropintel.yml`, tests in `tests/`.

**Status:** the code changes are done and the 17 automated tests pass on synthetic data. **The pipeline has NOT been re-run on Earth Engine.**
The Earth Engine service-account key exists only as the GitHub secret `EE_KEY_JSON`, so a live run and the regenerated workbook have to come from
GitHub Actions (see *Reproduce*). Nothing below that depends on satellite data is a measured result.

## 1. Repository map (dependency order)

| Cell / file | Role | Depends on |
|---|---|---|
| Cell 2 – CONFIG | thresholds, paths, env overrides | – |
| Cell 3 (`c3_github.txt`) | service-account auth (EE + Sheets), `EXTRACT_ONLY` | secrets `EE_KEY_JSON`, `EE_PROJECT`, `CROP_SHEET_ID` |
| Cell 4 | run date, season, 24 months, 14-day trend ends | 2 |
| Cell 5 / 5b | optional DES APY file; crop knowledge base (`REFS`, `RICE_CLASSES`, `CROP_MODELS`, `TN_WINDOWS`, `INDIA_WINDOWS`, machinery) | 4 |
| Cell 6 | Earth Engine layers: S2 masking/compositing, indices, S1 collection, rice rule, trend/maps/current/obs images | 4, 5b |
| Cell 7 (+ `c7_batches.txt`) | LGD tehsil boundaries, cached EE reductions per batch (sharded in GitHub) | 6 |
| Cell 7b / 7b2 / 7c | crop-mix priors (SPAM optional, CROPGRIDS auto), GGCMI calendars | 7 |
| Cell 8 | monthly index series, gap-fill, district areas | 7 |
| Cell 9 | phenology engine: `curve_phase`, `stages_at`, `decide_stage`, rice context/durations | 5b, 8 |
| Cell 10 | crop rows (`crops_v3`), sowing dates, stage resolution, Tab 1 + Tab 8 rows, dominant crop | 8, 9 |
| Cell 11 | Tabs 2-7, **acceptance tests**, QC summary, stage report | 10 |
| Cell 11b | validation (field data), reference area checks, rice calibration print | 10 |
| Cell 12 | Google Sheet (8 tabs) + Excel artifact with extra sheets | 11, 11b |
| Workflow | `setup → tests → extract (6 shards) → build` | all |

## 2. Issue → root cause → fix

| ID | Symptom in baseline | Location | Root cause | Fix (v4.0) | Test |
|---|---|---|---|---|---|
| I-1 | `Stage Observation Date` = 1970-01-01 in all 5,036 rows; "last clear S2 ≈ 1970-01-01" | Cell 6 `_mask_s2`, `current_image` | `img.select().updateMask().divide()` returns a new image **without properties**, so `system:time_start` was missing and `ee.Algorithms.If(..., 0)` produced epoch 0 | `_mask_s2` copies `system:time_start` etc.; new `obs_image` reduces per-pixel latest acquisition day with **max** (latest real scene) and **median**; `valid_obs_date` rejects any date before 2014-04-03 | T1, T11 |
| I-2 | 3,247 rows had a generic "(cropland curve estimate)" stage; 919 "(calendar estimate)" | Cell 10 `calendar_estimate` (v3.5 Option A) | design decision of v3.5 to fill every cell | removed; `resolve_unconfirmed` gives a crop-specific stage only from an **observed** planting date + that crop's table (labelled "(model-inferred)"), otherwise one of 9 explicit `Stage unresolved – …` statuses with reason and next action | T2, T5, T6 |
| I-3 | historical crop mix created crop rows (Garlic in 269 tehsils, Cauliflower 258 …) and named the dominant crop | Cell 10 `crops_v3` | CROPGRIDS/SPAM rows labelled "Probable" with scaled areas | priors never create a crop row; they are listed in **Alternative Crop Candidates** on the unidentified-cropland row (≥ 100 ha and ≥ 3 % only) | T5 |
| I-4 | whole perennial area named after one crop (Sugarcane 364,170 ha; Mango in Nagapattinam) | `crops_v3` perennial block | largest-prior naming | one row "Perennial / plantation crops (type not separable)" with prior proportions as candidates; stage `U_PERENNIAL` | T4 |
| I-5 | calendar window midpoint shown as `Sowing Date Estimated` | Cell 10 sowing block / T1 row | calendar-only S written like an observed date | calendar-only S never displayed as sowing date, its confidence blanked, harvest text prefixed "IF sown within the recommended window"; Tab 8 actual sowing / expected harvest / deviations only from an observed date | T7 |
| I-6 | rice in rainfed black-soil tehsils (Tuticorin 79,055 ha, Vilathikulam 19,195 ha) | Cell 6 `rice_layers` | radar flood rule alone; rain-wetted vertisol darkens VH/VV like puddling; no speckle filter | (a) 3×3 focal-median speckle filter on every S1 scene; (b) **optical water check** – pixel must be open water / flooded vegetation once (Dynamic World ≥ 0.3 or S2 MNDWI ≥ 0); (c) calibration now reports areas with and without (b) | needs live run |
| I-7 | delta rice too low (Thanjavur 30,043 ha vs 76,900 ha reference) | `rice_layers` | late samba flooded 35-75 d ago without the 5 dB canopy rise is dropped silently | new **Candidate – flooded, no canopy rise yet** row (reported, never counted as rice); thresholds NOT lowered – choose them from the calibration report | needs live run |
| I-8 | false wheat (Tenkasi 1,124 ha) | `crops_v3` wheat corroboration | CROPGRIDS spreads small national totals | prior corroboration also needs wheat/barley ≥ 1 % of the state crop mix | harness |
| I-9 | % MoM / YoY on negative baselines (BSI: 117 tehsils with \|MoM\| > 100 %) | Cell 11 `pct` | % of a negative or near-zero index | `pct_idx` only for baselines ≥ 0.05; NDWI/BSI % left empty; new Δ (index-point) columns | T9, T10 |
| I-10 | Machinery empty / generic in 81 % of Tab 8 | Cell 10 Tab 8 | bucket → category only | operation engine `MACHINERY[(crop family, next-stage bucket)]` (TNAU Agritech / ICAR-CIAE), 3 new columns; no machine for unresolved stages | T13 |
| I-11 | S1 backscatter curves mixed ascending + descending passes | `s1_collection` | no orbit consistency | `single_pass=True` (dominant pass) for VH trend and current S1 means; flood detection keeps both passes for revisit density | – |
| I-12 | no freshness / acquisition transparency | T1 | – | Latest S2 / S1 / usable date, days since, freshness class; optical-only stage on stale (> 20 d) imagery downgraded to model-inferred | T11 |
| I-13 | "High-confidence satellite identification" without validation data | presence labels | wording implied validated accuracy | renamed "Detected – current-season satellite evidence"; scores remain labelled heuristic rubric scores | – |
| I-14 | dominant crop claimed at 10-12 % of active cropland | Cell 10 DOM | largest named crop always "dominant" | dominant only if current-season evidence covers ≥ 25 % of active cropland, else "Not established – …" | harness |

## 3. Files changed / added

* `crop_intel.py` – all changes above (MODEL_VERSION `CropIntel-v4.0`).
* `.github/workflows/cropintel.yml` – new `tests` job (extraction only runs if tests pass); stage report, phenology DB and validation file uploaded with the Excel artifact.
* `tests/harness.py`, `tests/test_cropintel.py`, `tests/baseline_headers.json` – 17 tests (15 mandatory + baseline regression + in-pipeline acceptance).
* `requirements.txt` – `tabulate` (markdown report).
* `docs/CORRECTION_REPORT.md` – this file.

## 4. Output changes

* All 8 tabs and all existing columns are unchanged (Looker fields keep working). Appended columns:
  * Tab 1 (+13): Crop Stage Start / End Estimate, Latest S2 / S1 Acquisition Date, Latest Usable Observation Date, Days Since Observation,
    Satellite Freshness, Stage Estimation Method, Stage Verification Status, Alternative Stage Candidates,
    Stage Unresolved Reason / Next Action, Alternative Crop Candidates, Crop Area Basis.
  * Tabs 2-7 (+2): Latest MoM Δ (index points), YoY Δ (index points).
  * Tab 8 (+4): Next Stage Operation, Machinery Recommendation (crop & operation), Machinery Basis, Sowing Reference Basis.
* Excel artifact extra sheets: `QC_Summary`, `Stage_Report` (crop × stage, district × stage, evidence shares by rows and area, confidence
  distributions, latest observation), `Crop_Phenology_DB`; also `stage_report.md` and `crop_phenology_db.csv`.
* The run **stops before writing the Sheet** if any acceptance test (T1-T15) fails on the real output.

### Expected effect versus the baseline (to be confirmed by the live run)

| Measure | Baseline v3.5 | v4.0 (by construction) |
|---|---|---|
| Rows with 1970-01-01 observation date | 5,036 | 0 (test-enforced) |
| Generic curve / calendar stages as final stage | 4,166 | 0 (test-enforced) |
| Crop rows created from historical priors | ~3,400 | 0 – listed as candidates |
| Tab 1 rows (TN) | 5,036 | far fewer (≈ 3-5 per tehsil) |
| Share of rows "unresolved" | 0 % (filled) | will rise – this is the honest coverage |
| Rice area Tuticorin / Thanjavur | 79,055 / 30,043 ha | to be measured – water check and calibration |

## 5. Remaining gaps and external dependencies

1. **Live run** – needs the GitHub secrets; regenerate with the workflow (below), then compare with the baseline using `Stage_Report` / `QC_Summary`.
2. **Rice thresholds** – choose from the calibration print (`rice_calibrate=true`), now with and without the optical water check. Not lowered silently.
3. **Crop identity for non-rice field crops** – no current-season crop-type map exists for Tamil Nadu in Earth Engine; groundnut, pulses,
   millets, cotton and vegetables stay "crop identity not established" unless field / DCS (AgriStack) records are supplied
   (`data/field_observations.csv`) or a trained classifier with labelled samples is added.
4. **Stage models not added** for coconut, banana, mango, tea, rubber, pearl / finger millet, sorghum, red gram: no verified TNAU/ICAR stage
   table was compiled in this change; they are listed as *unsupported* in `Crop_Phenology_DB` and always reported as unresolved.
5. **Accuracy metrics** (precision / recall / F1 / confusion, date and area error) are produced by Cell 11b only when field observations exist.
   All confidence values are heuristic evidence scores, not accuracy.
6. Agro-climatic-zone-specific calendars beyond the existing TNAU season windows are not yet encoded.

## 6. Reproduce

```bash
# local tests (no Earth Engine)
pip install -r requirements.txt pytest
pytest -q tests

# live run: GitHub → Actions → CropIntel weekly → Run workflow
#   state_filter = Tamil Nadu, rice_calibrate = true, shards = 6
# outputs: Google Sheet (8 tabs) + artifact CropIntel-Excel-<date> (workbook, stage_report.md, crop_phenology_db.csv)
```
