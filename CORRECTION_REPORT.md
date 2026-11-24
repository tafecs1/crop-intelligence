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

## 7. v4.0.1 – fixes from the independent audit of RUN-20261124-073719

The submitted `Crop_Intelligence (2).xlsx` was still the v3.5 output (identical content to the baseline), so it was audited as-is
(`tools/audit_workbook.py`, report `Exception_Report_RUN-20261124-073719.xlsx`). Issues not yet covered by v4.0 and now fixed:

| ID | Finding | Fix | Test |
|---|---|---|---|
| I-15 | 92 tehsils: crop rows sum to more than active cropland – newly transplanted paddy was left out of the subtraction, so the unidentified row counted it again | every satellite row subtracted once, unidentified floored at 0 | test_20, in-pipeline T8 |
| I-16 | curve-picked stage far from the days-after-planting model (maize at 51 DAS reported "Silking – grain filling") | stage must be within ±1 stage of the duration-model stage at the point planting date, otherwise `Stage unresolved – satellite evidence contradicts…` | test_18 |
| I-17 | expected harvest already in the past while the crop is still at pod / grain filling (36 rows) | harvest window moved to (start of observed stage + table time to harvest), QC19 | test_21, in-pipeline T17 |
| I-18 | 766 duplicate (tehsil, crop) records – three paddy cohorts shared one crop name | `Rice (paddy)`, `Rice (paddy) – newly transplanted`, `Rice (paddy) – harvested this season` (dominant crop aggregates them) | test_19, in-pipeline T16 |

Audit re-run on a v4.0.1 synthetic workbook: 0 exceptions. Run `python3 tools/audit_workbook.py <workbook.xlsx> crop_intel.py <report.xlsx>`
on every regenerated workbook before releasing it.

## 8. v4.1 – Ideal_Crop_Window rebuilt at DISTRICT level

* **Why:** TNAU publishes crop calendars per district / agro-climatic zone. The tehsil-level Tab 8 repeated the same statewide windows for
  every tehsil and mixed in calendar-only sowing dates, which made it look more precise than its sources.
* **New source table:** `data/tn_district_crop_calendar.csv` (113 rows, 14 crops). Every row has its season (pattam), sowing months, districts,
  duration, varieties, source URL and a confidence:
  * **High:** copied from the TNAU Agritech "Seasons & Varieties" pages (rice, cotton, sugarcane, sorghum, ragi, cumbu, red gram,
    Bengal gram, sesame, sunflower).
  * **Medium:** extracted from the text of the TNAU Crop Production Guide PDF (black gram, green gram, groundnut, maize). The PDF table
    layout was partly garbled.
  * **Low:** rows whose alignment in the PDF text was ambiguous, each with its reason, plus the North-Western / Western maize zones,
    where the statewide pattams are shown.
* **Districts created after the source tables** (Chengalpattu, Tenkasi, Kallakurichi, Ranipet, Tirupathur, Mayiladuthurai) inherit
  their parent district's row, flagged in QC. Krishnagiri and Tiruppur rice rows come from the TNAU zone table.
* **Tab 8 rows:** one per district × crop × season (Tamil Nadu). States outside Tamil Nadu get GGCMI / national-window rows marked Low.
* **Observed columns** (area, district median planting date, deviation, timing, current stage, expected harvest) come only from tehsils with
  an OBSERVED planting date. Each observation is assigned to exactly one season row, so overlapping seasons never double count.
  Observations outside every TNAU window are listed in QC_Summary.
* **Dashboard:** join Tab 8 on **District LGD Code** (it no longer has Tehsil columns).
* **Tests:** 23 tests pass (25 as of v4.3), including calendar file integrity, district coverage, observation assignment and harvest = sowing + duration.

## 9. v4.2 – audit of RUN-20261124-130006 (Crop_Intelligence (3).xlsx)

| ID | Finding | Fix |
|---|---|---|
| I-19 | All 291 Tamil Nadu tehsils published as "Fallow – no active crop" (0 ha). QC14 on every row: the crop-map (MAPS) and 14-day trend (TREND) Earth Engine reductions returned no data for any tehsil, while monthly / current / weather layers worked. The run still wrote the Sheet. | **Extraction completeness gate** after Cell 7: coverage of monthly, current, maps and trend must be ≥ 95 % of cropland tehsils, otherwise the run stops before writing anything and prints the missing tehsils. A tehsil whose crop maps are missing is labelled "Satellite crop layers missing – not assessed", never fallow. Test 22. **Root cause (extract log of RUN-20261124-123259):** `Expected a homogeneous image collection … band 'm': Float<-1, 1>` – the v4.0 optical water check merged Sentinel-2 MNDWI (`normalizedDifference`, range-typed float) with a plain-float placeholder image; Earth Engine rejected the collection, so every TREND and MAPS batch failed (trend 0 · maps 0). Fixed: no merge for MNDWI (the S2 collection already has a masked placeholder) and Dynamic World uses `If(size > 0)`. |
| I-20 | Tab 8 rice "Sowing" stage columns used 18-40 d seedling age for every rice season | seedling age by duration class (TNAU: short 18-22, medium 25-30, long 35-40 d); QC note that the rice sowing window is nursery sowing and the stage columns are transplanting |
| I-21 | Tab 8 "Expected Stage by Calendar" listed stages late → early ("Physiological maturity … Grain filling – dough") | listed early → late |

## 10. v4.3 – audit of RUN-20261124-143636 (Crop_Intelligence (4).xlsx)

Extraction was complete (291/291 on every layer) and all acceptance tests passed, but paddy was badly under-detected.

| ID | Finding | Fix |
|---|---|---|
| I-22 | Paddy 52,834 ha state-wide; Thanjavur 7,152 ha vs 76,900 ha reference (9 %). Every looser threshold set raised Thanjavur sharply (9 → 25 → 50 %), which is the signature of mixed pixels rather than of wrong thresholds. Cause: `reduceRegions(scale=100)` made Earth Engine average each Sentinel-1 scene to 100 m **before** the flood test, and the 3×3 speckle filter then spanned 300 m – delta paddy mixed with bunds, roads and coconut rows rarely reached VH < −20 dB. | `RICE_SCALE_M = 20`: every paddy layer (flood test, drop/rise tests, speckle filter, optical water check, harvested and calibration layers) is forced onto a 20 m grid; tehsil fractions are still read at 100 m spacing, i.e. an even systematic sample of the 20 m result (no area bias). `RICE_SIG` includes the scale, so old 100 m caches are not reused. `tileScale` 16. Thresholds unchanged – to be chosen from the 20 m calibration grid. Test 24. |
| I-23 | State check printed "within reference" for 52,834 ha against a 2.0 M ha all-season target (3 %) – an upper bound cannot detect an undercount | verdict now reads "below upper bound (x % of it) – this check cannot detect an undercount" |
| Open | "Active cropland – crop unknown" 4.1 M ha = 95 % of WorldCover cropland: "active" = max NDVI ≥ 0.35 since 1 Oct, which weedy fallows also pass in the NE monsoon | next step: require a green-up (rise from a bare-soil low) |
| Open | Maize 221 rows but 5,274 ha, 94 rows "evidence contradicts" – WorldCereal 2021 maize map weak for Tamil Nadu | treat maize as low confidence; revisit after paddy |
| Open | Direct-seeded samba (no standing water) is invisible to any flood rule | later step (optical green-up route for delta tehsils) |
