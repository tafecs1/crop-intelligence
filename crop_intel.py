#!/usr/bin/env python3
"""CropIntel – All-India tehsil crop intelligence → Google Sheet (8 tabs).

GitHub Actions port of the Colab notebook Crop_Intelligence_FINAL v3.4. The crop / stage LOGIC IS UNCHANGED.
Only the Colab-specific parts differ:
  * secrets come from environment variables (EE_KEY_JSON, CROP_SHEET_ID, EE_PROJECT, optional SHEETS_KEY_JSON);
  * files live in ./work (cache, downloads, Excel) and ./data (optional inputs you commit);
  * Google sign-in is service-account only (share the Sheet with it as Editor);
  * Earth Engine extraction can run as parallel shards (EXTRACT_ONLY=1, SHARD_INDEX, SHARD_COUNT) that fill the cache,
    then one final job runs the whole pipeline from that cache and writes the Sheet.
"""
import json, os, re, sys, time, math, hashlib, datetime as dt
from collections import Counter
import numpy as np, pandas as pd
import ee, gspread
from google.oauth2.service_account import Credentials

# ===== CELL 2: CONFIG (edit this cell only) =====
EE_SECRET_NAME = 'EE_KEY_JSON'
SHEET_SECRET   = 'CROP_SHEET_ID'
SHEET_ID       = os.environ.get('CROP_SHEET_ID', '')               # GitHub secret CROP_SHEET_ID (Sheet ID or link)
EE_PROJECT     = os.environ.get('EE_PROJECT', '')                  # GitHub secret EE_PROJECT
SHEETS_AUTH    = 'auto'                     # 'auto' = try the Sheets JSON key, then your own Google login (recommended)
                                            # 'upload_key' | 'same_key' | 'google_login' to force one method

# Tehsil boundaries: LGD sub-districts (direct download, no Earth Engine asset needed)
BHARATLAS         = "https://bharatlas.com/api/v1"
TEHSIL_SOURCE_URL = "https://bharatlas.com/api/dl/admin/subdistricts/LGD_Subdistricts.parquet"
SIMPLIFY_DEG      = 0.002                   # boundary simplification (~200 m) so polygons can be sent to Earth Engine
BOUNDARY_UNIT = 'Tehsil / Sub-district (LGD)'
STATE_FILTER  = [x.strip() for x in os.environ.get('STATE_FILTER', '').split(',') if x.strip()]   # empty = ALL INDIA; e.g. 'Tamil Nadu,Kerala'

WORK_DIR     = os.path.abspath(os.environ.get('CROP_WORK_DIR', 'work'))   # run folder (cache, downloads, Excel)
INPUT_DIR    = os.path.abspath(os.environ.get('CROP_INPUT_DIR', 'data'))  # optional input files committed to the repo
DRIVE_DIR    = WORK_DIR
GOVT_APY_CSV = f'{INPUT_DIR}/apy_district.csv'   # OPTIONAL DES district APY file (skipped if absent)
USE_SPAM     = True                         # auto-download IFPRI SPAM 2020 crop-mix maps (Harvard Dataverse) for crops satellites can't separate
APY_YEARS    = 3                            # average of the latest N years per district
CACHE_DIR    = f'{DRIVE_DIR}/cache'

BATCH_SIZE      = 100                       # tehsils per Earth Engine request (auto-splits on memory errors)
SCALE_M         = 100
MONTHS_BACK     = 24
TREND_STEPS     = 12                        # 14-day NDVI steps (12 = 168 days)
MIN_VALID_SHARE = 0.10                      # min clear-cropland share for a monthly value to count
GAP_FILL        = True                      # fill cloud gaps in index tabs from measured values (marked grey)
# ---- Sentinel-1 paddy rule (v3.1). v3.0 used VH < -18 dB / +3 dB over a 5-month window → rice on 3.7 Mha (≈3× official) – see audit
RICE_VH_FLOOD_DB = float(os.environ.get('RICE_VH_FLOOD_DB') or -20.0)                    # flooded field: VH below this (dB)            – REFS['S1_RICE']
RICE_VV_FLOOD_DB = float(os.environ.get('RICE_VV_FLOOD_DB') or -15.0)                    # … AND VV below this on the same date (open water); dry soil rarely meets both
RICE_VH_DROP_DB  = float(os.environ.get('RICE_VH_DROP_DB') or 3.0)                      # field must DROP into the flood (max VH 5-45 d before ≥ flood VH + this) – excludes always-dark soil/tank beds
RICE_VH_RISE_DB  = float(os.environ.get('RICE_VH_RISE_DB') or 5.0)                      # canopy growth after flooding: max VH ≥ 14 d after flood ≥ flood VH + this
RICE_FLOOD_LEAD_DAYS = 120                  # transplanting searched from SEASON_START − this (≈ early June for Rabi) so late-kharif rice still standing in
                                            # Nov (Odisha, WB, AP …) is captured; TN samba still uses the LATEST flood episode
RICE_RECENT_DAYS = 35                       # flooded within this many days and no rise yet → "recently puddled / transplanted" (Probable)
RICE_HARVEST_NDVI = 0.30                    # rice pixel ≥ 80 d after transplanting whose max NDVI in the last 30 d is below this → harvested
# v3.4: wheat/barley from the 2021 winter-cereal map is reported ONLY with Govt APY or SPAM corroboration (the old "≥25 % of
#       cropland" rule reported delta thaladi rice as wheat in Thanjavur)
RICE_CALIBRATE   = os.environ.get('RICE_CALIBRATE', 'false').lower() == 'true' or False                    # True = also compute paddy area under RICE_CAL_SETS (one extra comparison table, more EE compute)
RICE_CAL_SETS    = [(-20.0, -15.0, 3.0, 5.0), (-19.0, -14.0, 2.0, 4.0), (-18.0, -13.0, 2.0, 3.0)]   # (VH flood, VV flood, drop, rise) dB
GGCMI_DIR        = f'{DRIVE_DIR}/ggcmi'   # GGCMI Phase 3 crop calendar (auto-downloaded from Zenodo, CC BY 4.0)
GGCMI_URL        = 'https://zenodo.org/records/5062513/files/{f}?download=1'
GGCMI_WINDOW_DAYS = 30                      # ± days around the GGCMI (multi-year average) planting day accepted as the sowing window
# v3.5: CROPGRIDS 2020 (Tang et al. 2024, Scientific Data, CC BY 4.0) – 173 crops at 0.05° (~5.6 km); per-crop NetCDF files download
#       automatically (no form, no login). Used to NAME the crops satellites cannot separate (replaces the manual APY/SPAM step).
USE_CROPGRIDS   = True
CROPGRIDS_DIR   = f'{DRIVE_DIR}/cropgrids'
CROPGRIDS_URLS  = ['https://geodata.ucdavis.edu/geodata/crops/cropgrids/CROPGRIDSv1.07_{c}.nc']   # per-crop files (UC Davis mirror)
CROPGRIDS_ZIP   = 'https://ndownloader.figshare.com/files/44950942'   # fallback: official figshare v1.08 zip (807 MB, all crops)
OTHER_CURVE_SHARE = 0.60                    # a modelled crop that is ≥ this share of the tehsil's modelled seasonal crop area may use the
                                            # measured curve of the remaining annual cropland (not rice / maize / winter cereal / perennial)
CALENDAR_ESTIMATE = True                    # Option A: when evidence cannot confirm a stage, publish the crop-calendar stage labelled
                                            # "(calendar estimate)" with a low confidence instead of "Stage not determined"
STRICT_FILL     = True                      # stop the run (instead of writing placeholders) if no crop-mix source could be loaded
# Official reference areas used ONLY to validate (never to set) satellite estimates. Add rows as figures are published.
REFERENCE_AREAS = [
    dict(scope='Tamil Nadu (state)', state='Tamil Nadu', district=None, crop='Rice', what='2026-27 paddy area target, all seasons', ha=2_000_000, kind='upper_bound',
         date='2026', source='TN Agriculture Dept via Deccan Chronicle: "paddy … area of 20 lakh hectares in the 2026–27"'),
    dict(scope='Thanjavur (district)', state='Tamil Nadu', district='Thanjavur', crop='Rice', what='samba + thaladi planted so far (1.90 lakh acres)', ha=76_900, kind='point',
         date='2026-11-14', source='newkerala.com, 14-Nov-2026 – also reports crops "planted about a month ago"'),
]
FIELD_OBS_CSV    = f'{INPUT_DIR}/field_observations.csv'  # OPTIONAL ground truth: tehsil_lgd,crop,stage,observation_date,planting_date
MODEL_VERSION   = 'CropIntel-v3.5'
RUN_DATE        = os.environ.get('RUN_DATE') or None   # set once per workflow run so all jobs share the same date


# ===== CELL 3: Authenticate (GitHub secrets – service account only) =====
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
EXTRACT_ONLY = os.environ.get('EXTRACT_ONLY') == '1'      # shard jobs: Earth Engine extraction only, no Sheet
os.makedirs(WORK_DIR, exist_ok=True)
DRIVE_OK, GOOGLE_GC = True, None

key_info = json.loads(os.environ['EE_KEY_JSON'])
ee.Initialize(ee.ServiceAccountCredentials(key_info['client_email'], key_data=json.dumps(key_info)),
              project=EE_PROJECT or key_info.get('project_id'))
print('✅ Earth Engine connected as', key_info['client_email'])

def can_edit(client, sid):
    """Real write test: re-saves the Sheet's own title (no visible change)."""
    try:
        s_ = client.open_by_key(sid)
        s_.batch_update({'requests': [{'updateSpreadsheetProperties': {'properties': {'title': s_.title}, 'fields': 'title'}}]})
        return True, s_.title
    except Exception as e:
        return False, str(e)[:160]

gc = SHEETS_USER = None
if not EXTRACT_ONLY:
    _m = re.search(r'/d/([A-Za-z0-9_-]+)', SHEET_ID)
    SHEET_ID = _m.group(1) if _m else SHEET_ID.strip()
    assert SHEET_ID, 'Secret CROP_SHEET_ID is empty'
    _sk = json.loads(os.environ.get('SHEETS_KEY_JSON') or os.environ['EE_KEY_JSON'])
    from gspread.http_client import BackOffHTTPClient     # retries 'quota exceeded' (429) with exponential back-off
    gc = gspread.authorize(Credentials.from_service_account_info(_sk, scopes=SCOPES), http_client=BackOffHTTPClient)
    SHEETS_USER = _sk['client_email']
    _ok, _info = can_edit(gc, SHEET_ID)
    if not _ok:
        raise SystemExit(f'⛔ {SHEETS_USER} cannot edit the Sheet ({_info}). Open the Sheet → Share → add this e-mail as Editor.')
    print(f'✅ Google Sheet writer: {SHEETS_USER} → https://docs.google.com/spreadsheets/d/{SHEET_ID} ("{_info}")')


# ===== CELL 4: Dates, season, labels =====
TODAY  = dt.date.today() if RUN_DATE is None else dt.date.fromisoformat(RUN_DATE)
RUN_ID = 'RUN-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S')

def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)

def add_years(d, n):
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)

CUR_MONTH    = dt.date(TODAY.year, TODAY.month, 1)
MONTHS       = [add_months(CUR_MONTH, -i) for i in range(MONTHS_BACK - 1, -1, -1)]
MONTH_LABELS = [m.strftime('%b-%Y') for m in MONTHS]
YM, YM_DASH  = [m.strftime('%Y%m') for m in MONTHS], [m.strftime('%Y-%m') for m in MONTHS]

def current_season(d):
    if d.month in (10, 11, 12, 1, 2):
        return 'Rabi', dt.date(d.year if d.month >= 10 else d.year - 1, 10, 1)
    if d.month in (3, 4, 5):
        return 'Zaid', dt.date(d.year, 3, 1)
    return 'Kharif', dt.date(d.year, 6, 1)

SEASON, SEASON_START = current_season(TODAY)
SEASON_SPAN = {'Kharif': (6, 7), 'Rabi': (10, 7), 'Zaid': (2, 6)}     # start month, number of months (incl. harvest)
TREND_ENDS  = [TODAY - dt.timedelta(days=14 * i) for i in range(TREND_STEPS - 1, -1, -1)]
TREND_MID   = [e - dt.timedelta(days=7) for e in TREND_ENDS]
RICE_FLOOD_START = SEASON_START - dt.timedelta(days=RICE_FLOOD_LEAD_DAYS)   # current-season transplanting window starts here
S1_START    = RICE_FLOOD_START - dt.timedelta(days=50)   # 50 d earlier so the 'drop into flood' test has prior observations
V30_START   = SEASON_START - dt.timedelta(days=90)       # v3.0 window – kept only for the before/after comparison
RICE_BINS   = []                                         # 15-day transplanting cohorts (day offsets from S1_START)
_b = (RICE_FLOOD_START - S1_START).days
while _b <= (TODAY - S1_START).days:
    RICE_BINS.append((_b, _b + 15)); _b += 15

SAT, GOVT, FILL, NA = 'Satellite (GEE)', 'Govt', 'Gap-filled (Estimated)', 'Not available'
UNC = 'Stage not determined'          # used whenever the evidence cannot support a crop-specific stage
MODEL = 'Modelled (IFPRI SPAM 2020)'
STAGES = ['Land Prep', 'Sowing', 'Germination', 'Vegetative', 'Flowering', 'Grain Fill/Maturity', 'Harvest']
ORDER  = ['Not Sown / Fallow'] + STAGES + ['Harvested']
STAGE_CODE = {s: f'P{i}' for i, s in enumerate(ORDER)}
MACHINERY = {'Land Prep': 'Tillage', 'Sowing': 'Seeding', 'Germination': 'None', 'Vegetative': 'Spraying',
             'Flowering': 'Spraying', 'Grain Fill/Maturity': 'None', 'Harvest': 'Harvesting'}
print(f'Run {TODAY} | Season {SEASON} from {SEASON_START} | Months {MONTH_LABELS[0]} → {MONTH_LABELS[-1]} | {RUN_ID}')

def norm_state(s):
    s = ' '.join(str(s or '').upper().replace('&', 'AND').split())
    return {'ORISSA': 'ODISHA', 'UTTARANCHAL': 'UTTARAKHAND', 'JAMMU KASHMIR': 'JAMMU AND KASHMIR'}.get(s, s)

def norm_name(s):
    return re.sub(r'[^A-Z0-9]', '', str(s or '').upper())

def norm_code(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ''
    try:
        return str(int(float(x)))
    except Exception:
        return str(x).strip()


# ===== CELL 5: Govt – DES district Area/Production/Yield (APY) =====
AGG_RE = r'total|food ?grain|nine oilseed|^oilseeds$|^pulses$|^cereals|^coarse|^all '

def norm_season(s):
    s = str(s).strip().lower()
    if 'kharif' in s or 'autumn' in s:
        return 'Kharif'
    if 'rabi' in s or 'winter' in s:
        return 'Rabi'
    if 'summer' in s or 'zaid' in s:
        return 'Zaid'
    if 'whole' in s or 'annual' in s:
        return 'Annual'
    return None

def _pick(cols, *keys, exclude=()):
    for c in cols:
        lc = c.lower()
        if all(k in lc for k in keys) and not any(x in lc for x in exclude):
            return c
    return None

APY = None
if os.path.exists(GOVT_APY_CSV):
    _df = pd.read_csv(GOVT_APY_CSV, low_memory=False)
    _cols = list(_df.columns)
    C = dict(year=_pick(_cols, 'year'),
             state=_pick(_cols, 'state', 'name') or _pick(_cols, 'state', exclude=('code', 'lgd')),
             dname=_pick(_cols, 'district', 'name') or _pick(_cols, 'district', exclude=('code', 'lgd')),
             dcode=_pick(_cols, 'district', 'lgd') or _pick(_cols, 'district', 'code'),
             crop=_pick(_cols, 'crop', 'name') or _pick(_cols, 'crop', exclude=('code', 'type', 'year')),
             season=_pick(_cols, 'season'), area=_pick(_cols, 'area', exclude=('unit',)), unit=_pick(_cols, 'area', 'unit'))
    print('APY columns used:', C)
    assert all(C[k] for k in ('year', 'state', 'dname', 'crop', 'season', 'area')), 'APY file columns not recognised – send me the header row'
    APY = pd.DataFrame({
        'year': pd.to_numeric(_df[C['year']].astype(str).str[:4], errors='coerce'),
        'state': _df[C['state']], 'dname': _df[C['dname']],
        'dcode': _df[C['dcode']].map(norm_code) if C['dcode'] else '',
        'crop': _df[C['crop']].astype(str).str.strip(),
        'season': _df[C['season']].map(norm_season),
        'area': pd.to_numeric(_df[C['area']], errors='coerce')})
    if C['unit']:
        _u = _df[C['unit']].astype(str).str.lower()
        APY['area'] *= np.where(_u.str.contains('lakh'), 1e5, np.where(_u.str.contains('000|thousand'), 1e3, 1))
    APY = APY[APY.season.notna() & APY.area.gt(0) & APY.year.notna() & ~APY.crop.str.lower().str.contains(AGG_RE)]
    print(f'✅ APY loaded: {len(APY):,} rows, years {int(APY.year.min())}–{int(APY.year.max())}, {APY.crop.nunique()} crops')
else:
    print(f'ℹ️ No DES APY file at {GOVT_APY_CSV} – using satellite crop maps + IFPRI SPAM 2020 crop mix (Cell 7b)')

def sat_group(crop):
    c = crop.lower()
    if 'rice' in c or 'paddy' in c:
        return 'RICE'
    if 'wheat' in c or 'barley' in c:
        return 'WC'
    if 'maize' in c:
        return 'MZ'
    if 'sugarcane' in c:
        return 'SC'
    return None


# ===== CELL 5b: Crop knowledge base (every number below is sourced – see REFS) =====
# Rules: (1) no stage duration is used unless it is published; (2) where sources only give a broad phase,
# the model reports the broad phase instead of inventing sub-stages; (3) documented assumptions are named
# ASSUMPTION_* and shown in the output evidence.
REFS = {
    'TNAU_RICE_SEASONS': 'TNAU Agritech – Planting seasons in Tamil Nadu (Navarai Dec-Jan <120 d; Sornavari Apr-May <120 d; '
                         'Kuruvai Jun-Jul <120 d; Early Samba Jul-Aug 130-135 d; Samba Aug 130-135 & >150 d; '
                         'Late Samba/Thaladi/Pishanam Sep-Oct 130-135 d; Late Thaladi Oct-Nov 120-140 d) '
                         'https://agritech.tnau.ac.in/expert_system/paddy/season.html',
    'TNAU_RICE_NURSERY': 'TNAU Agritech – optimum seedling age: short 18-22 d, medium 25-30 d, long 35-40 d '
                         'https://agritech.tnau.ac.in/agriculture/agri_Main_Field_preparation_Rice.html',
    'RICE_SHORT_RANGE': 'Short-duration rice 90-120 d (TN season classification) https://agroscribles.blogspot.com/2022/01/rice-growing-seasons-and-important-rice.html',
    'RICE_PHASES': 'PROSEA: panicle initiation to flowering ≈35 d; flowering to full ripening ≈30 d; vegetative stage varies by cultivar '
                   'https://prosea.prota4u.org/view.aspx?id=59 ; Yoshida (IRRI): panicle primordia initiate ≈30 d before heading',
    'RICE_MILK_DOUGH': 'LSU AgCenter: milk stage 7-10 d after heading, dough ≈1 week later '
                       'https://www.lsuagcenter.com/portals/our_offices/research_stations/rice/features/publications/knowledge-of-rice-growth-stages-important',
    'S1_RICE': 'Sentinel-1 VH V-shaped flooding/transplanting signature (Auto-RMVPF, Remote Sensing 2023, 15(11), 2785); '
               'TN Samba VH rice curve -19.25 to -15.15 dB (ISPRS Archives XLII-3-W6, 2019, Tiruvarur)',
    'TNAU_SUGARCANE': 'Sugarcane phases used in TNAU-based irrigation schedules: germination 1-35, tillering 36-100, '
                      'grand growth 101-270, maturity 271 DAP to harvest',
    'MAIZE': 'Maize (TN): 50% tasseling 47-56 DAS, 50% silking 50.5-60 DAS by season (Madras Agric. J., TNAU); physiological maturity '
             '30-35 d after 50% flowering (TNAU Agritech hybrid seed production); harvest 95-105 DAS (ICAR Seed Research, TNAU CO 6)',
    'GROUNDNUT': 'Groundnut: flowering phase 26-60 DAS, maturity phase 61-105 DAS (TNAU irrigation mgmt); flowering from 25-30 DAS '
                 '(Legume Research LR-4366); earthing-up/pegging 40-45 DAS, pod development 45 DAS (TNAU); Spanish/Valencia mature '
                 '110-130 DAS (TNAU organic farming)',
    'COTTON': 'Cotton: first squares 35-47 DAS; squaring 40-45 DAS & flowering/early boll formation 70-75 DAS (ICAR-CICR HDPS bulletin); '
              'flowering starts 55-65 DAS (CRDA); boll bursting ≈51-55 d (ICAR-CICR Nagpur study)',
    'PULSES': 'TNAU varietal data: blackgram 50% flowering 30-35 (up to 40-45) DAS, maturity 60-80 DAS; greengram 50% flowering 26-50 DAS, '
              'duration 56-90 DAS https://agritech.tnau.ac.in/agriculture/pulses_blackgram.html',
    'TN_SEASONS_OTHER': 'TNAU: blackgram Adipattam Jun-Aug, Puratasipattam Sep-Nov; greengram Adipattam Jun-Jul; groundnut Adipattam Jun-Jul, '
                        'Karthikaipattam Oct-Nov, Margazhipattam Dec-Jan, Thaipattam Jan-Feb, Masipattam Feb-Mar; cotton winter irrigated '
                        'Aug-Sep, rainfed Sep-Oct, summer irrigated Feb-Mar; Cauvery delta rice-fallow pulses Jan-Apr',
    'GDD_BASE': 'GDD base temperature: rice 10 °C (Thomas 1957, used in Indian rice GDD studies), maize 10 °C (Madras Agric. J. maize GDD '
                'study); cotton 15 °C and sugarcane 15 °C (crop minimum temperatures, agrometeorology tables); groundnut 15 °C (TNAU '
                'groundnut climate requirement T.min)',
    'IMD_HEAVY': 'IMD rainfall intensity terminology: heavy rainfall ≥ 64.5 mm/day',
    'WORLDCEREAL': 'ESA WorldCereal 2021 10 m (Earth Engine ESA/WorldCereal/2021/MODELS/v100) – historical map, 2021 season',
    'WORLDCOVER': 'ESA WorldCover v200 (2021) 10 m',
    'CROPGRIDS': 'CROPGRIDS v1.07/1.08 – Tang F.H.M. et al. (2024) Scientific Data 11:413, doi:10.1038/s41597-024-03247-7; 173 crops, '
                 'harvested area circa 2020 at 0.05°, CC BY 4.0 (modelled from FAOSTAT 2020 + sub-national statistics + 27 gridded datasets)',
    'INDIA_CAL': 'Broad all-India sowing windows of the kharif / rabi / summer seasons (DES Agricultural Statistics at a Glance, crop calendar of '
                 'major crops; ICAR package of practices). Used ONLY when no TNAU or GGCMI window exists for the tehsil',
    'SPAM': 'IFPRI SPAM 2020 V2r2 harvested area, 5-arcmin (~10 km) modelled crop allocation, doi:10.7910/DVN/SWPENT',
    'ERA5': 'ECMWF ERA5-Land daily aggregates (ECMWF/ERA5_LAND/DAILY_AGGR), 11.1 km, reanalysis',
    'CHIRPS': 'UCSB-CHG CHIRPS daily rainfall, ~5.5 km',
    'S2': 'Copernicus Sentinel-2 L2A harmonized (COPERNICUS/S2_SR_HARMONIZED), 10-20 m, SCL cloud/shadow mask',
    'S1': 'Copernicus Sentinel-1 GRD IW (COPERNICUS/S1_GRD), 10 m, VV/VH dB',
    'LGD': 'LGD sub-district boundaries via bharatlas.com (LGD_Subdistricts.parquet)',
    'TNAU_MAIZE_SEASON': 'TNAU Agritech maize hybrids COH(M) 6/8/11: irrigated Jun-Jul (Aadi), Nov-Dec (Karthigai), Dec-Jan (Thai), '
                         'Apr-May (Chittirai); rainfed Sep-Oct (Puratassi); duration 100-110 d '
                         'https://agritech.tnau.ac.in/agriculture/agri_maize_Characteristics_hybrids.html',
    'TNAU_GROUNDNUT_SEASON': 'TNAU Agritech groundnut seasons of Tamil Nadu: Thaipattam Jan-Feb, Adipattam Jul-Aug, Purattasipattam Sep-Oct, '
                             'Ayppasipattam Oct-Nov, Chithiraipattam Apr-May https://agritech.tnau.ac.in/agriculture/Crop-improvement%20-1.html',
    'DYNAMIC_WORLD': 'Google/WRI Dynamic World V1 (GOOGLE/DYNAMICWORLD/V1): near-real-time 10 m land-cover labels per Sentinel-2 scene; '
                     '"crops" and "flooded_vegetation" classes – generic classes, NOT crop types; cloud-limited',
    'WHEAT': 'Wheat (North India) stages: germination 5-7, CRI 20-25, tillering to 45, jointing 45-60, boot leaf 70-75, flowering 85-90, '
             'milk 100-105, dough 105-110, maturity 115-120 DAS (TNAU/ANGRAU eAgri AGRO301 lecture 2); ICAR irrigation schedule CRI 22, '
             'tillering 45, jointing 65, heading 85, milk 105, dough 120 DAS',
    'MUSTARD': 'Indian mustard: flowering commences 46-48 DAS and physiological maturity 89-90 d in early varieties (Indian J. Agric. Sci., '
               'ICAR); pre-flowering 40-45 DAS and pod filling 70-80 DAS critical stages; DRMR varieties mature 86-151 d',
    'CHICKPEA': 'Chickpea: 50 % flowering 41-57 DAS, pod initiation ≈59 DAS, harvest/maturity 98-111 DAS (Plant Archives; Legume Research '
                'LR-5223; IJARe A-6054); later in the north with longer duration',
    'GGCMI': 'GGCMI Phase 3 crop calendar (Jägermeyr, Müller, Minoli, Ray, Siebert 2021), doi:10.5281/zenodo.5062513 – 0.5° grid, '
             'multi-year average planting & maturity day for 18 crops, irrigated/rainfed; gap-filled/extrapolated outside cultivated areas',
    'S1_FLOOD_VV': 'Open water / flooded paddy: very low co-polarised (VV) and cross-polarised (VH) backscatter at transplanting; VV+VH '
                   'rule reduces confusion with dry bare soil (S1 rice-mapping literature, e.g. Nelson et al. 2014 RS 6:10773; '
                   'Auto-RMVPF 2023)',
}
ASSUMPTION_GREENUP_LAG = (7, 25)   # days between planting and satellite-detectable green-up (10 % of cycle amplitude).
                                   # Not published for TN crops → disclosed assumption; widens the planting-date range.
ASSUMPTION_RICE_LONG_MAX = 160     # TNAU gives ">145/>150 d" for long-duration rice without an upper bound.
ASSUMPTION_RICE_ESTAB = 10         # days of post-transplanting establishment (recovery) before active tillering – disclosed assumption.
ASSUMPTION_PUDDLE_TO_TRANSPLANT = (0, 7)   # days from the start of puddling flood (seen by S1) to transplanting – disclosed assumption.
ASSUMPTION_RICE_GREENUP_LAG = (10, 35)     # days from transplanting to a sustained optical green-up of paddy pixels – disclosed assumption.

STAGE_BUCKETS = ['Land Prep', 'Sowing', 'Germination', 'Vegetative', 'Flowering', 'Grain Fill/Maturity', 'Harvest']
PHASES = {'pre': 'bare / pre-green-up', 'rising': 'green-up / canopy development', 'peak': 'canopy peak',
          'declining': 'senescence / decline', 'post': 'bare after decline', 'flat': 'steady green (no seasonal cycle)'}

# ---- Rice (transplanted) – stage table built per duration class, in days after transplanting (DAT)
RICE_SEASONS_TN = [  # (season, nursery-sowing months, duration class)  – REFS['TNAU_RICE_SEASONS']
    ('Navarai', (12, 1), 'short'), ('Sornavari', (4, 5), 'short'), ('Kuruvai', (6, 7), 'short'),
    ('Early Samba', (7, 8), 'medium'), ('Samba', (8,), 'medium_long'),
    ('Late Samba / Thaladi / Pishanam', (9, 10), 'medium'), ('Late Thaladi / Late Pishanam', (10, 11), 'late')]
RICE_CLASSES = {   # total duration (days from nursery sowing) and nursery age – REFS TNAU_RICE_SEASONS / TNAU_RICE_NURSERY
    'short':       dict(total=(90, 120), nursery=(18, 22)),
    'medium':      dict(total=(130, 135), nursery=(25, 30)),
    'medium_long': dict(total=(130, ASSUMPTION_RICE_LONG_MAX), nursery=(25, 40)),
    'late':        dict(total=(120, 140), nursery=(25, 30)),
}

def rice_stage_table(field_dur):
    """Stages in DAT from field duration F (transplanting → grain maturity). Heading H = F − 30 (ripening ≈30 d),
    panicle initiation = H − 35 … H − 30 (REFS RICE_PHASES); milk H+7, dough H+14 (RICE_MILK_DOUGH)."""
    H = field_dur - 30
    return [  # (stage, start_DAT, bucket, allowed S2 phases) – rice only; never reused for another crop
        ('Land preparation', -30, 'Land Prep', {'pre', 'post'}),
        ('Puddling and transplanting', 0, 'Sowing', {'pre'}),
        ('Establishment (recovery)', 1, 'Germination', {'pre', 'rising'}),
        ('Tillering', ASSUMPTION_RICE_ESTAB, 'Vegetative', {'pre', 'rising'}),
        ('Panicle initiation – booting', H - 35, 'Vegetative', {'rising', 'peak'}),
        ('Heading / flowering', H, 'Flowering', {'peak'}),
        ('Grain filling – milk', H + 7, 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Grain filling – dough', H + 14, 'Grain Fill/Maturity', {'declining', 'peak'}),
        ('Physiological maturity – harvest-ready', H + 30, 'Harvest', {'declining', 'post'}),
    ]
# Sentinel-1 VH phase each rice stage is compatible with (canopy volume scattering rises to ~heading, then levels/declines)
RICE_VH_ALLOWED = {'Land preparation': {'pre'}, 'Puddling and transplanting': {'pre'}, 'Establishment (recovery)': {'pre', 'rising'},
                   'Tillering': {'rising'}, 'Panicle initiation – booting': {'rising', 'peak'}, 'Heading / flowering': {'peak', 'rising'},
                   'Grain filling – milk': {'peak', 'declining'}, 'Grain filling – dough': {'peak', 'declining'},
                   'Physiological maturity – harvest-ready': {'declining', 'peak', 'post'}}

# ---- Other crops: stage start in days after sowing/planting as (earliest, latest) – sources in REFS
CROP_MODELS = {
    'Maize': dict(group='Cereals', ref='MAIZE', tbase=10, harvest=(95, 110), stages=[
        ('Germination – vegetative (V6/V12 not separable by satellite)', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Tasseling', (47, 56), 'Flowering', {'rising', 'peak'}),
        ('Silking – grain filling (blister to dent)', (50, 60), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Physiological maturity', (80, 95), 'Grain Fill/Maturity', {'declining'}),
        ('Harvest', (95, 110), 'Harvest', {'declining', 'post'})]),
    'Groundnut': dict(group='Oilseeds', ref='GROUNDNUT', tbase=15, harvest=(105, 130), stages=[
        ('Emergence – vegetative growth', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Flowering', (25, 30), 'Flowering', {'rising', 'peak'}),
        ('Pegging', (40, 45), 'Flowering', {'rising', 'peak'}),
        ('Pod development – pod filling', (45, 61), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Maturity – harvest', (105, 130), 'Harvest', {'declining', 'post'})]),
    'Cotton': dict(group='Fibre crops', ref='COTTON', tbase=15, harvest=(106, 120), stages=[
        ('Emergence – vegetative growth', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Squaring', (35, 47), 'Vegetative', {'rising', 'peak'}),
        ('Flowering – boll formation', (55, 65), 'Flowering', {'rising', 'peak'}),
        ('Boll development', (70, 80), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Boll opening – picking', (106, 120), 'Harvest', {'declining', 'peak'})]),
    'Black gram': dict(group='Pulses', ref='PULSES', tbase=10, harvest=(60, 80), stages=[
        ('Emergence – vegetative growth', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Flowering – pod formation – pod filling', (30, 45), 'Flowering', {'rising', 'peak', 'declining'}),
        ('Maturity – harvest', (60, 80), 'Harvest', {'declining', 'post'})]),
    'Green gram': dict(group='Pulses', ref='PULSES', tbase=10, harvest=(56, 90), stages=[
        ('Emergence – vegetative growth', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Flowering – pod formation – pod filling', (26, 50), 'Flowering', {'rising', 'peak', 'declining'}),
        ('Maturity – harvest', (56, 90), 'Harvest', {'declining', 'post'})]),
    'Sugarcane': dict(group='Sugar crops (perennial)', ref='TNAU_SUGARCANE', tbase=15, harvest=(330, 365), stages=[
        ('Land preparation and planting', (0, 0), 'Sowing', {'pre'}),
        ('Germination / sprouting', (1, 1), 'Germination', {'pre', 'rising'}),
        ('Tillering', (36, 36), 'Vegetative', {'rising'}),
        ('Grand growth / stalk elongation', (101, 101), 'Vegetative', {'rising', 'peak', 'flat'}),
        ('Maturity (early maturity → maturity)', (271, 271), 'Grain Fill/Maturity', {'flat', 'peak', 'declining'}),
        ('Harvest (ratoon follows)', (330, 365), 'Harvest', {'post', 'declining'})]),
}
CROP_MODELS.update({
    'Wheat': dict(group='Cereals', ref='WHEAT', tbase=0, harvest=(115, 150), stages=[
        ('Germination – emergence', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Crown root initiation (CRI)', (20, 25), 'Germination', {'pre', 'rising'}),
        ('Tillering', (26, 30), 'Vegetative', {'rising'}),
        ('Jointing', (45, 60), 'Vegetative', {'rising', 'peak'}),
        ('Booting', (70, 75), 'Vegetative', {'peak', 'rising'}),
        ('Heading / flowering (anthesis)', (85, 90), 'Flowering', {'peak'}),
        ('Grain filling – milk', (100, 105), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Grain filling – dough', (105, 120), 'Grain Fill/Maturity', {'declining'}),
        ('Physiological maturity – harvest', (115, 150), 'Harvest', {'declining', 'post'})]),
    'Rapeseed & Mustard': dict(group='Oilseeds', ref='MUSTARD', tbase=5, harvest=(90, 150), stages=[
        ('Emergence – rosette / vegetative', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Pre-flowering – bud formation', (40, 45), 'Vegetative', {'rising'}),
        ('Flowering', (46, 50), 'Flowering', {'rising', 'peak'}),
        ('Siliqua formation – pod filling', (70, 80), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Physiological maturity – harvest', (89, 150), 'Harvest', {'declining', 'post'})]),
    'Chickpea': dict(group='Pulses', ref='CHICKPEA', tbase=5, harvest=(98, 150), stages=[
        ('Emergence – vegetative growth', (0, 0), 'Germination', {'pre', 'rising'}),
        ('Flowering (50 %)', (41, 57), 'Flowering', {'rising', 'peak'}),
        ('Pod initiation – pod filling', (55, 65), 'Grain Fill/Maturity', {'peak', 'declining'}),
        ('Physiological maturity – harvest', (98, 150), 'Harvest', {'declining', 'post'})]),
})
# Crop → GGCMI Phase 3 calendar codes (all-India sowing windows / durations); crops not listed have no gridded calendar
GGCMI_CODES = {'RICE': ['ri1', 'ri2'], 'Wheat': ['wwh', 'swh'], 'Maize': ['mai'], 'Groundnut': ['nut'], 'Cotton': ['cot'],
               'Sugarcane': ['sgc'], 'Rapeseed & Mustard': ['rap'], 'Sorghum': ['sor'], 'Millet': ['mil'], 'Soybean': ['soy'], 'Potato': ['pot']}
RICE_MODEL = dict(group='Cereals', ref='RICE_PHASES', tbase=10)
SPAM_TO_MODEL = {'maiz': 'Maize', 'grou': 'Groundnut', 'cott': 'Cotton', 'sugc': 'Sugarcane', 'whea': 'Wheat', 'rape': 'Rapeseed & Mustard',
                 'chic': 'Chickpea'}   # blackgram/greengram sit in SPAM "opul"

# TN sowing windows (month numbers) – REFS TN_SEASONS_OTHER; used ONLY as supporting evidence, never as proof
TN_WINDOWS = {
    'Maize': [('Aadi pattam – irrigated', (6, 7)), ('Puratassi pattam – rainfed', (9, 10)), ('Karthigai pattam – irrigated', (11, 12)),
              ('Thai pattam – irrigated', (12, 1)), ('Chittirai pattam – irrigated', (4, 5))],
    'Groundnut': [('Adipattam', (6, 8)), ('Purattasipattam', (9, 10)), ('Ayppasi/Karthikaipattam', (10, 11)), ('Margazhipattam', (12, 1)),
                  ('Thaipattam', (1, 2)), ('Masipattam', (2, 3)), ('Chithiraipattam', (4, 5))],
    'Cotton': [('Winter irrigated', (8, 9)), ('Rainfed', (9, 10)), ('Summer irrigated', (2, 3))],
    'Black gram': [('Adipattam', (6, 8)), ('Puratasipattam', (9, 11)), ('Rice fallow (Cauvery delta)', (1, 2))],
    'Green gram': [('Adipattam', (6, 7)), ('Rice fallow (Cauvery delta)', (1, 2))],
}
# v3.5 all-India fallback sowing windows (month numbers) – REFS['INDIA_CAL']; broad national windows, lowest calendar weight
INDIA_WINDOWS = {
    'Maize': [('India kharif', (6, 7)), ('India rabi', (10, 11)), ('India spring', (1, 2))],
    'Groundnut': [('India kharif', (6, 7)), ('India rabi / summer', (11, 1))],
    'Cotton': [('India irrigated (north)', (4, 5)), ('India rainfed (central / south)', (6, 7))],
    'Black gram': [('India kharif', (6, 7)), ('India rabi', (10, 11))],
    'Green gram': [('India kharif', (6, 7)), ('India summer', (3, 4))],
    'Sugarcane': [('India spring planting', (2, 3)), ('India adsali', (7, 8)), ('India autumn planting', (10, 10))],
    'Wheat': [('India rabi', (11, 12))],
    'Rapeseed & Mustard': [('India rabi', (10, 11))],
    'Chickpea': [('India rabi', (10, 11))],
}
MACHINERY_BY_BUCKET = {'Land Prep': 'Tillage', 'Sowing': 'Seeding / transplanting', 'Germination': 'None',
                       'Vegetative': 'Spraying', 'Flowering': 'Spraying', 'Grain Fill/Maturity': 'None', 'Harvest': 'Harvesting'}

def month_window_dates(months, ref):
    """Most recent occurrence (start, end) of a sowing-month window at or before ref (+30 d look-ahead)."""
    m0, m1 = months[0], months[-1]
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        a = dt.date(y, m0, 1)
        b_y = y if m1 >= m0 else y + 1
        b = add_months(dt.date(b_y, m1, 1), 1) - dt.timedelta(days=1)
        if a <= ref + dt.timedelta(days=30):
            best = (a, b)
    return best

print('✅ Crop knowledge base loaded:', ', '.join(['Rice'] + list(CROP_MODELS)))


# ===== CELL 6: Earth Engine layers =====
S2_BANDS  = ['B2', 'B3', 'B4', 'B5', 'B8', 'B11']
CROPLAND  = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map').eq(40)
WATER     = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').select('occurrence').unmask(0).gt(50)
EMPTY_S2  = ee.Image.constant([0] * 6).rename(S2_BANDS).toFloat().updateMask(ee.Image.constant(0))
EMPTY_1   = lambda name: ee.Image.constant(0).rename(name).toFloat().updateMask(ee.Image.constant(0))
CHIRPS    = ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY')
WCER      = ee.ImageCollection('ESA/WorldCereal/2021/MODELS/v100')

def wc_layer(product, band, season=None):
    col = WCER.filter(ee.Filter.eq('product', product))
    if season:
        col = col.filter(ee.Filter.eq('season', season))
    return col.select(band).map(lambda i: i.toFloat()).merge(ee.ImageCollection([EMPTY_1(band)])).max()

WC_WINTER = wc_layer('wintercereals', 'classification').eq(100).unmask(0).And(CROPLAND)
WC_CONF   = wc_layer('wintercereals', 'confidence').updateMask(WC_WINTER)
WC_MZ_K   = wc_layer('maize', 'classification', 'tc-maize-main').eq(100).unmask(0).And(CROPLAND)
WC_MZ_R   = wc_layer('maize', 'classification', 'tc-maize-second').eq(100).unmask(0).And(CROPLAND)
WC_MAIZE  = WC_MZ_R if SEASON == 'Rabi' else WC_MZ_K

def _mask_s2(img):
    scl = img.select('SCL')
    good = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
    return img.select(S2_BANDS).updateMask(good).divide(10000).toFloat()

def s2_collection(start, end, region):
    return (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').filterBounds(region)
            .filterDate(str(start), str(end)).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 85))
            .map(_mask_s2).merge(ee.ImageCollection([EMPTY_S2])))

def indices(img):
    N, R, B, G, RE, S = [img.select(b) for b in ['B8', 'B4', 'B2', 'B3', 'B5', 'B11']]
    return ee.Image.cat([
        N.subtract(R).divide(N.add(R)).rename('NDVI'),
        N.subtract(R).multiply(2.5).divide(N.add(R.multiply(6)).subtract(B.multiply(7.5)).add(1)).rename('EVI'),
        G.subtract(N).divide(G.add(N)).rename('NDWI'),
        N.subtract(RE).divide(N.add(RE)).rename('NDRE'),
        N.subtract(R).multiply(1.5).divide(N.add(R).add(0.5)).rename('SAVI'),
        S.add(R).subtract(N.add(B)).divide(S.add(R).add(N).add(B)).rename('BSI'),
        N.subtract(S).divide(N.add(S)).rename('NDMI')])

def s2_composite(start, end, region, bands, suffix):
    idx = indices(s2_collection(start, end, region).median()).select(bands).updateMask(CROPLAND)
    valid = idx.select(bands[0]).mask().gt(0).toFloat().rename('valid').updateMask(CROPLAND)
    return ee.Image.cat([idx, valid]).rename([f'{b}_{suffix}' for b in bands] + [f'valid_{suffix}'])

def s1_collection(start, end, region, pols=('VV', 'VH')):
    col = (ee.ImageCollection('COPERNICUS/S1_GRD').filterBounds(region).filterDate(str(start), str(end))
           .filter(ee.Filter.eq('instrumentMode', 'IW')))
    for p in pols:
        col = col.filter(ee.Filter.listContains('transmitterReceiverPolarisation', p))
    return col.select(list(pols))

def rice_layers(region, vh_th=None, vv_th=None, drop_db=None, rise_db=None, core_only=False):
    """v3.1 paddy rule, per pixel, Sentinel-1 VV+VH, CURRENT SEASON ONLY:
    1) flooded acquisition = VH < RICE_VH_FLOOD_DB AND VV < RICE_VV_FLOOD_DB, on/after RICE_FLOOD_START;
    2) planting = START of the LATEST flooding episode (first flooded acquisition after the last non-flooded one). Standing water
       stays for weeks after transplanting, so neither the lowest VH (v3.0 → July/Aug dates) nor the last flooded date is used;
    3) drop test: max VH 5-45 d before the episode ≥ flood VH + RICE_VH_DROP_DB (field became water; rejects always-dark soil / tank beds);
    4) established rice = max VH ≥ 14 d after the flood ≥ flood VH + RICE_VH_RISE_DB (canopy growth);
       recent = flooded within RICE_RECENT_DAYS, drop passed, rise not yet seen (puddled / just transplanted → Probable only).
    Permanent water (JRC occurrence > 50 %) and non-cropland (WorldCover) excluded."""
    vh_th = RICE_VH_FLOOD_DB if vh_th is None else vh_th
    vv_th = RICE_VV_FLOOD_DB if vv_th is None else vv_th
    drop_db = RICE_VH_DROP_DB if drop_db is None else drop_db
    rise_db = RICE_VH_RISE_DB if rise_db is None else rise_db
    t0 = ee.Date(str(S1_START))
    fs, fe = (RICE_FLOOD_START - S1_START).days, (TODAY - S1_START).days
    def prep(i):
        t = ee.Image.constant(i.date().difference(t0, 'day')).toFloat().rename('t')
        return ee.Image.cat([i.select('VV').toFloat(), i.select('VH').toFloat(), t])
    empty = ee.Image.constant([0, 0, 0]).rename(['VV', 'VH', 't']).toFloat().updateMask(ee.Image.constant(0))
    col = s1_collection(S1_START, TODAY + dt.timedelta(days=1), region, ('VV', 'VH')).map(prep).merge(ee.ImageCollection([empty]))
    def flooded(i):
        return i.updateMask(i.select('VH').lt(vh_th).And(i.select('VV').lt(vv_th)).And(i.select('t').gte(fs)))
    fl = col.map(flooded)
    t_last = fl.select('t').max()
    t_dry = (col.map(lambda i: i.updateMask(i.select('VH').gte(vh_th).Or(i.select('VV').gte(vv_th))
                                       .And(i.select('t').lt(t_last)))).select('t').max().unmask(-1))
    ep = fl.map(lambda i: i.updateMask(i.select('t').gt(t_dry)))
    ft, fvh = ep.select('t').min(), ep.select('VH').min()
    prior_max = col.map(lambda i: i.updateMask(i.select('t').lt(ft.subtract(5)).And(i.select('t').gte(ft.subtract(45))))).select('VH').max()
    rise = col.map(lambda i: i.updateMask(i.select('t').gte(ft.add(14)))).select('VH').max().subtract(fvh)
    base = prior_max.subtract(fvh).gte(drop_db).And(CROPLAND).And(WATER.Not())
    est_all = base.And(rise.gte(rise_db)).unmask(0)
    if core_only:                                            # calibration: paddy this season (standing + harvested) + recently flooded
        return est_all.Or(base.And(ft.gte(fe - RICE_RECENT_DAYS)).unmask(0))
    recent = base.And(ft.gte(fe - RICE_RECENT_DAYS)).And(rise.lt(rise_db).unmask(1)).unmask(0).And(est_all.Not())
    # harvested this season: paddy cycle ≥ 80 d old AND max NDVI in the last 30 d below RICE_HARVEST_NDVI (cloud-masked → treated as standing)
    nd30 = (s2_collection(TODAY - dt.timedelta(days=30), TODAY + dt.timedelta(days=1), region)
            .map(lambda i: i.normalizedDifference(['B8', 'B4']).rename('NDVI')).max())
    harv = est_all.And(nd30.lt(RICE_HARVEST_NDVI)).And(ft.lte(fe - 80)).unmask(0)
    est = est_all.And(harv.Not())
    # v3.0 rule (lowest VH since SEASON_START−90 d < −18 dB, any later +3 dB) – computed ONLY for the before/after comparison
    c30 = col.map(lambda i: i.updateMask(i.select('t').gte((V30_START - S1_START).days)).addBands(i.select('VH').multiply(-1).rename('neg')))
    lo30, hi30 = c30.qualityMosaic('neg'), c30.qualityMosaic('VH')
    v30 = (lo30.select('VH').lt(-18).And(hi30.select('VH').subtract(lo30.select('VH')).gte(3)).And(hi30.select('t').gt(lo30.select('t')))
           .And(CROPLAND).And(WATER.Not())).unmask(0)
    bins = [est.And(ft.gte(a)).And(ft.lt(b)).unmask(0) for a, b in RICE_BINS]
    return dict(est=est, recent=recent, harv=harv, ft=ft.updateMask(est), ft_recent=ft.updateMask(recent), ft_harv=ft.updateMask(harv),
                obs=col.select('VH').count().toFloat(), v30=v30, bins=bins)

IDX6 = ['NDVI', 'EVI', 'NDWI', 'NDRE', 'SAVI', 'BSI']

def monthly_image_fn(months):
    return lambda region: ee.Image.cat([s2_composite(m, add_months(m, 1), region, IDX6, m.strftime('%Y%m')) for m in months])

def trend_image(region):
    """14-day steps: NDVI for all cropland; NDVI + EVI on crop-specific pixels; Sentinel-1 VH (cloud-independent) on rice pixels."""
    R = rice_layers(region)
    rice = R['est']
    mz = WC_MAIZE.And(rice.Not()).And(R['recent'].Not())
    wcm = WC_WINTER.And(rice.Not()).And(R['recent'].Not()).And(mz.Not())
    # v3.5 'OTH' = remaining ANNUAL cropland active this season (not paddy, maize map, winter-cereal map or perennially green):
    #      the measured curve of the field crops that satellites cannot name (used only for a crop that dominates that area)
    s2y = s2_collection(TODAY - dt.timedelta(days=365), TODAY + dt.timedelta(days=1), region).map(
        lambda i: i.normalizedDifference(['B8', 'B4']).rename('NDVI'))
    perennial = s2y.map(lambda i: i.gte(0.4)).mean().gte(0.85).And(s2y.count().gte(8)).unmask(0)
    ndmax = (s2_collection(SEASON_START, TODAY + dt.timedelta(days=1), region)
             .map(lambda i: i.normalizedDifference(['B8', 'B4']).rename('NDVI')).max())
    oth = (ndmax.gte(0.35).unmask(0).And(CROPLAND).And(rice.Not()).And(R['recent'].Not()).And(R['harv'].Not())
           .And(mz.Not()).And(wcm.Not()).And(perennial.Not()))
    masks = {'': None, 'RICE': rice, 'MZ': mz, 'WC': wcm, 'OTH': oth}
    s1 = s1_collection(TREND_ENDS[0] - dt.timedelta(days=14), TODAY + dt.timedelta(days=1), region, ('VH',))
    bands = []
    for i, e in enumerate(TREND_ENDS):
        a, b = e - dt.timedelta(days=14), e + dt.timedelta(days=1)
        ix = indices(s2_collection(a, b, region).median()).updateMask(CROPLAND)
        nd, ev = ix.select('NDVI'), ix.select('EVI')
        for k, m in masks.items():
            bands.append((nd if m is None else nd.updateMask(m)).rename(f'NDVI{k}_T{i:02d}'))
            if m is not None:
                bands.append(ev.updateMask(m).rename(f'EVI{k}_T{i:02d}'))
        vh = (s1.filterDate(str(a), str(b)).map(lambda im: ee.Image.constant(10).pow(im.select('VH').divide(10)).rename('VH').toFloat())
              .merge(ee.ImageCollection([EMPTY_1('VH')])).mean())
        bands.append(vh.log10().multiply(10).updateMask(rice).rename(f'VHRICE_T{i:02d}'))
    return ee.Image.cat(bands)

DW = ee.ImageCollection('GOOGLE/DYNAMICWORLD/V1')

def maps_image(region):
    """Mutually exclusive crop masks (rice → recent rice → perennial → maize → winter-cereal map) + Dynamic World corroboration."""
    R = rice_layers(region)
    rice, recent = R['est'], R['recent']
    g_sum, v_sum = ee.Image(0), ee.Image(0)
    for k in range(1, 13):
        m = add_months(CUR_MONTH, -k)
        nd = indices(s2_collection(m, add_months(m, 1), region).median()).select('NDVI')
        g_sum = g_sum.add(nd.gte(0.4).unmask(0))
        v_sum = v_sum.add(nd.mask().gt(0).unmask(0))
    perennial = v_sum.gte(8).And(g_sum.divide(v_sum.max(1)).gte(0.85)).And(CROPLAND)
    per = perennial.And(rice.Not()).And(recent.Not())
    ndmax = (s2_collection(SEASON_START, TODAY + dt.timedelta(days=1), region)
             .map(lambda i: i.normalizedDifference(['B8', 'B4']).rename('NDVI')).max())
    active = ndmax.gte(0.35).unmask(0).And(CROPLAND)
    mz = WC_MAIZE.And(active).And(rice.Not()).And(recent.Not()).And(per.Not())
    wcl = WC_WINTER.And(active).And(rice.Not()).And(recent.Not()).And(per.Not()).And(mz.Not())
    wc = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
    noncrop_veg = wc.eq(10).Or(wc.eq(20)).Or(wc.eq(30)).Or(wc.eq(95))
    water = wc.eq(80).Or(wc.eq(90))
    dwc = (DW.filterBounds(region).filterDate(str(TODAY - dt.timedelta(days=60)), str(TODAY + dt.timedelta(days=1))).select('label')
           .merge(ee.ImageCollection([ee.Image.constant(0).rename('label').toByte().updateMask(ee.Image.constant(0))])))
    dw_mode = dwc.mode()
    dw_crop = dw_mode.eq(4).Or(dw_mode.eq(3))               # 4 = crops, 3 = flooded vegetation
    cohorts = [b.toFloat().rename(f'rice_coh_{k:02d}') for k, b in enumerate(R['bins'])]
    if RICE_CALIBRATE:
        cohorts += [rice_layers(region, *cs, core_only=True).toFloat().rename(f'rice_cal_{k}') for k, cs in enumerate(RICE_CAL_SETS)]
    return ee.Image.cat([
        rice.toFloat().rename('rice_frac'), recent.toFloat().rename('rice_recent_frac'), R['v30'].toFloat().rename('rice_frac_v30'),
        R['harv'].toFloat().rename('rice_harv_frac'), R['ft_harv'].rename('rice_harv_t'),
        R['ft'].rename('rice_flood_t'), R['ft_recent'].rename('rice_recent_t'), R['obs'].updateMask(rice).rename('rice_s1_obs'),
        rice.And(dw_crop).unmask(0).toFloat().rename('rice_dw_frac'), mz.And(dw_crop).unmask(0).toFloat().rename('mz_dw_frac'),
        WC_WINTER.toFloat().rename('wc_frac'), wcl.toFloat().rename('wc_active_frac'), WC_CONF.rename('wc_conf'),
        WC_MAIZE.toFloat().rename('mz_frac'), mz.toFloat().rename('mz_active_frac'),
        per.toFloat().rename('per_frac'), active.toFloat().rename('active_frac'),
        dw_crop.And(CROPLAND).unmask(0).toFloat().rename('dw_crop_frac'), dwc.count().toFloat().updateMask(CROPLAND).rename('dw_obs'),
        noncrop_veg.toFloat().rename('noncrop_veg_frac'), water.toFloat().rename('water_frac')] + cohorts)

ERA5D = ee.ImageCollection('ECMWF/ERA5_LAND/DAILY_AGGR')

def _last_date(col):
    v = col.filterDate(str(SEASON_START), str(TODAY + dt.timedelta(days=1))).aggregate_max('system:time_start').getInfo()
    return dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).date() if v else None

_ch_last, _era_last = _last_date(CHIRPS), _last_date(ERA5D)
if _ch_last and (TODAY - _ch_last).days <= 20:
    RAIN_COL, RAIN_MULT, RAIN_LAST, RAIN_LABEL = CHIRPS.select('precipitation'), 1, _ch_last, SAT
    _rname = 'CHIRPS satellite-gauge rainfall'
elif _era_last:
    RAIN_COL, RAIN_MULT, RAIN_LAST, RAIN_LABEL = ERA5D.select('total_precipitation_sum'), 1000, _era_last, 'Reanalysis (ERA5-Land, GEE)'
    _rname = 'ERA5-Land reanalysis rainfall (CHIRPS not yet updated for this season)'
elif _ch_last:
    RAIN_COL, RAIN_MULT, RAIN_LAST, RAIN_LABEL = CHIRPS.select('precipitation'), 1, _ch_last, SAT
    _rname = 'CHIRPS satellite-gauge rainfall (partial season)'
else:
    RAIN_COL = RAIN_LAST = None
    RAIN_LABEL, _rname = NA, 'none'
CHIRPS_LAST = RAIN_LAST                                  # kept name used later
RAIN_PERIOD = f'{SEASON_START} to {RAIN_LAST} – {_rname}' if RAIN_LAST else NA
print('Rainfall:', RAIN_PERIOD)

def current_image(region):
    start, end = TODAY - dt.timedelta(days=30), TODAY + dt.timedelta(days=1)
    cur = s2_composite(start, end, region, ['NDVI', 'EVI', 'NDRE', 'NDMI', 'NDWI', 'BSI'], 'CUR')
    s2n = s2_collection(start, end, region).select('B4').count().toFloat().rename('s2_scenes').updateMask(CROPLAND)
    s1c = s1_collection(start, end, region).merge(ee.ImageCollection(
        [ee.Image.constant([0, 0]).rename(['VV', 'VH']).toFloat().updateMask(ee.Image.constant(0))]))
    s1 = s1c.map(lambda i: ee.Image(10).pow(i.divide(10)).copyProperties(i)).mean().log10().multiply(10).updateMask(CROPLAND)  # linear mean → dB
    s1n = s1c.select('VV').count().toFloat().rename('s1_scenes').updateMask(CROPLAND)
    d0 = ee.Date(str(start))
    s1_last = (s1_collection(start, end, region, ('VV',)).map(lambda i: ee.Image.constant(i.date().difference(d0, 'day')).toFloat()
               .rename('s1_last_t').updateMask(i.select('VV').mask())).merge(ee.ImageCollection([EMPTY_1('s1_last_t')])).max()
               .updateMask(CROPLAND))
    s2_last = (s2_collection(start, end, region).map(lambda i: ee.Image.constant(ee.Date(ee.Algorithms.If(i.get('system:time_start'),
               i.get('system:time_start'), 0)).difference(d0, 'day')).toFloat().rename('s2_last_t').updateMask(i.select('B4').mask()))
               .max().updateMask(CROPLAND))
    if CHIRPS_LAST:
        r_end = CHIRPS_LAST + dt.timedelta(days=1)
        rain = RAIN_COL.filterDate(str(SEASON_START), str(r_end)).sum().multiply(RAIN_MULT)
        rain_ly = RAIN_COL.filterDate(str(add_years(SEASON_START, -1)), str(add_years(r_end, -1))).sum().multiply(RAIN_MULT)
    else:
        rain, rain_ly = EMPTY_1('precipitation'), EMPTY_1('precipitation')
    lst = (ee.ImageCollection('MODIS/061/MOD11A2').filterDate(str(TODAY - dt.timedelta(days=32)), str(end))
           .select('LST_Day_1km').map(lambda i: i.toFloat()).merge(ee.ImageCollection([EMPTY_1('LST_Day_1km')])).mean()
           .multiply(0.02).subtract(273.15).rename('lst_c'))
    return ee.Image.cat([cur, s2n, s1.select('VV').rename('s1_VV'), s1.select('VH').rename('s1_VH'),
                         s1.select('VV').subtract(s1.select('VH')).rename('VV_VH'), s1n,
                         rain.rename('rain_mm'), rain_ly.rename('rain_ly_mm'), lst,
                         CROPLAND.unmask(0).toFloat().rename('crop_share'), s1_last, s2_last])

# ---- ERA5-Land weather: season rain vs last year AND vs 1991-2020 normal, 30-day air temperature + anomaly,
#      heavy-rain days (IMD ≥ 64.5 mm/day), daily Tmin/Tmax series for growing-degree-days
GDD_DAYS = 240
_era_end = (_era_last + dt.timedelta(days=1)) if _era_last else None

def weather_image(region):
    if not _era_end:
        return ee.Image.constant(0).rename('no_era5').updateMask(0)
    P = ERA5D.select('total_precipitation_sum')
    T = ERA5D.select('temperature_2m')
    rain = P.filterDate(str(SEASON_START), str(_era_end)).sum().multiply(1000).rename('era_rain')
    rain_ly = P.filterDate(str(add_years(SEASON_START, -1)), str(add_years(_era_end, -1))).sum().multiply(1000).rename('era_rain_ly')
    norm = ee.ImageCollection([P.filterDate(str(add_years(SEASON_START, y - _era_end.year)), str(add_years(_era_end, y - _era_end.year)))
                               .sum().multiply(1000) for y in range(1991, 2021)]).mean().rename('era_rain_norm')
    heavy = P.filterDate(str(SEASON_START), str(_era_end)).map(lambda i: i.multiply(1000).gte(64.5)).sum().rename('heavy_rain_days')
    t0 = _era_end - dt.timedelta(days=30)
    t30 = T.filterDate(str(t0), str(_era_end)).mean().subtract(273.15).rename('t30')
    t30n = ee.ImageCollection([T.filterDate(str(add_years(t0, y - _era_end.year)), str(add_years(_era_end, y - _era_end.year))).mean()
                               for y in range(1991, 2021)]).mean().subtract(273.15).rename('t30_norm')
    g0 = _era_end - dt.timedelta(days=GDD_DAYS)
    daily = ERA5D.filterDate(str(g0), str(_era_end)).select(['temperature_2m_min', 'temperature_2m_max'])
    gd = daily.map(lambda i: i.subtract(273.15).rename(['tmin', 'tmax'])
                   .set('d', ee.Date(i.get('system:time_start')).format('YYYYMMdd'))).toBands()
    return ee.Image.cat([rain, rain_ly, norm, heavy, t30, t30n, gd])


# ===== CELL 7: LGD tehsil list + boundaries, then Earth Engine extraction (cached in Drive, resumable) =====
import requests, geopandas as gpd
from shapely.geometry import mapping

def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def clean_name(s):
    return re.sub(r"\s+", " ", str(s)).strip()

def get_json(url, params=None, tries=4, timeout=60):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            time.sleep(8 * (i + 1) if r.status_code == 429 else 2 * (i + 1))
        except (requests.RequestException, ValueError):
            time.sleep(2 * (i + 1))
    return None

def find_urls(obj, found=None):
    found = [] if found is None else found
    if isinstance(obj, dict):
        for v in obj.values():
            find_urls(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_urls(v, found)
    elif isinstance(obj, str) and obj.startswith("http") and re.search(r"\.(parquet|geojson|json|gpkg|zip)(\?|$)", obj, re.I):
        found.append(obj)
    return found

def discover_tehsil_url():
    data = get_json(f"{BHARATLAS}/layers")
    if data is None:
        return None
    layers = data if isinstance(data, list) else (data.get("layers") or data.get("data") or data.get("items") or [])
    cands = []
    for L in layers:
        text = json.dumps(L).lower()
        lid = str(L.get("id") or L.get("layer_id") or L.get("slug") or L.get("name") or "")
        if re.search(r"sub.?district", text) and "village" not in lid.lower() and "block" not in lid.lower():
            cands.append((0 if lid.lower().startswith("lgd") else 1, lid, L))
    cands.sort(key=lambda x: x[0])
    for _, lid, L in cands:
        urls = sorted(find_urls(L), key=lambda u: (".parquet" not in u.lower(), u))
        if urls:
            print("Using layer:", lid, "->", urls[0])
            return urls[0]
    return None

def download(url, path):
    with requests.get(url, stream=True, timeout=600, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path

def read_geo(path):
    if path.lower().endswith(".parquet"):
        try:
            return gpd.read_parquet(path)
        except Exception:
            from shapely import wkb
            df = pd.read_parquet(path)
            gcol = next((c for c in df.columns if norm(c) in ("geometry", "geom", "wkb", "wkbgeometry")), None)
            geom = df[gcol].map(lambda b_: wkb.loads(bytes(b_)) if b_ is not None else None)
            return gpd.GeoDataFrame(df.drop(columns=[gcol]), geometry=geom, crs=4326)
    return gpd.read_file(path)

def pick(cols, *must, exclude=()):
    for c in cols:
        n = norm(c)
        if all(m in n for m in must) and not any(e in n for e in exclude):
            return c
    return None

_lgd_local = f'{WORK_DIR}/LGD_Subdistricts' + ('.parquet' if '.parquet' in (TEHSIL_SOURCE_URL or '').lower() else '.geojson')
_url = TEHSIL_SOURCE_URL
try:
    download(_url, _lgd_local)
except Exception as e:
    print('Direct LGD link failed (', str(e)[:120], ') – discovering layer on bharatlas.com …')
    _url = discover_tehsil_url()
    assert _url, "Could not find the LGD sub-district file on bharatlas.com – set TEHSIL_SOURCE_URL in Cell 2"
    _lgd_local = f'{WORK_DIR}/LGD_Subdistricts' + ('.parquet' if '.parquet' in _url.lower() else '.geojson')
    download(_url, _lgd_local)
g_raw = read_geo(_lgd_local)
if g_raw.crs is None:
    g_raw = g_raw.set_crs(4326)
g_raw = g_raw.to_crs(4326)
_cols = [c for c in g_raw.columns if c != g_raw.geometry.name]
print('LGD layer:', _url, '| rows:', len(g_raw), '| columns:', _cols)

C_STATE = pick(_cols, "stname") or pick(_cols, "state", "name") or pick(_cols, "state", exclude=("code", "id", "lgd"))
C_DIST  = pick(_cols, "dtname") or pick(_cols, "district", "name") or pick(_cols, "district", exclude=("code", "id", "lgd"))
C_TEH   = (pick(_cols, "sdtname") or pick(_cols, "subdistrict", "name") or pick(_cols, "tehsil")
           or pick(_cols, "taluk") or pick(_cols, "subdistrict", exclude=("code", "id")))
# LGD codes first (subdt_lgd / dist_lgd / state_lgd); Census-2011 codes (sdtcode11 …) only if no LGD column exists
C_TCODE = (pick(_cols, "subdt", "lgd") or pick(_cols, "subdistrict", "lgd") or pick(_cols, "tehsil", "lgd")
           or pick(_cols, "sdtcode") or pick(_cols, "subdistrict", "code", exclude=("census",)))
C_SCODE = pick(_cols, "state", "lgd") or pick(_cols, "st", "lgd", exclude=("dist", "subdt")) or pick(_cols, "stcode")
C_DCODE = pick(_cols, "dist", "lgd") or pick(_cols, "dtcode")
print('Columns used → state:', C_STATE, '| state code:', C_SCODE, '| district:', C_DIST, '| district code:', C_DCODE,
      '| tehsil:', C_TEH, '| tehsil LGD code:', C_TCODE)
assert C_STATE and C_DIST and C_TEH and C_TCODE, 'LGD columns not recognised – send me the column list printed above'

TEH = pd.DataFrame({
    'state': g_raw[C_STATE].map(clean_name), 'state_lgd': g_raw[C_SCODE] if C_SCODE else None,
    'district': g_raw[C_DIST].map(clean_name), 'district_lgd': g_raw[C_DCODE] if C_DCODE else None,
    'tehsil': g_raw[C_TEH].map(clean_name), 'tehsil_lgd': g_raw[C_TCODE]})
TEH['geometry'] = g_raw.geometry.values
if STATE_FILTER:
    _want = {norm(x) for x in STATE_FILTER}
    TEH = TEH[TEH.state.map(norm).isin(_want)]
    assert len(TEH), f'No tehsils for STATE_FILTER={STATE_FILTER}. States in LGD file: {sorted(g_raw[C_STATE].map(clean_name).unique())}'
TEH['lgd'], TEH['slgd'], TEH['dlgd'] = TEH.tehsil_lgd.map(norm_code), TEH.state_lgd.map(norm_code), TEH.district_lgd.map(norm_code)
TEH['dkey'] = TEH.state.map(norm_state) + '|' + TEH.district.map(norm_name)
_bad = TEH.lgd.eq('').sum()
TEH = TEH[TEH.lgd.ne('') & TEH.geometry.notna()].drop_duplicates('lgd').reset_index(drop=True)
TEH['tehsil_lgd'] = TEH.lgd                                   # LGD code as text = join key everywhere
print(f'✅ LGD tehsils: {len(TEH):,} in {TEH.state.nunique()} states, {TEH.dkey.nunique()} districts (dropped {_bad} without code)')

# Earth Engine features built from the LGD polygons (simplified only for transfer)
LGD_F = 'tehsil_lgd'
_gs = gpd.GeoSeries(TEH.geometry, crs=4326).buffer(0).simplify(SIMPLIFY_DEG, preserve_topology=True)
FEAT = {lgd: ee.Feature(ee.Geometry(mapping(geom), None, False), {LGD_F: lgd})
        for lgd, geom in zip(TEH.lgd, _gs) if geom is not None and not geom.is_empty}
TEH = TEH[TEH.lgd.isin(FEAT)].reset_index(drop=True)
TEH_GDF = gpd.GeoDataFrame(TEH[['lgd']], geometry=gpd.GeoSeries(TEH.geometry, crs=4326))

def fc_for(codes):
    return ee.FeatureCollection([FEAT[c] for c in codes if c in FEAT])

os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_path(kind, codes):
    return f"{CACHE_DIR}/{kind}_{hashlib.md5(json.dumps(sorted(map(str, codes))).encode()).hexdigest()[:12]}.json"

def reduce_batch(img, fc_b, scale=None):
    out = img.reduceRegions(collection=fc_b, reducer=ee.Reducer.mean(), scale=scale or SCALE_M, tileScale=8)
    return [x['properties'] for x in out.map(lambda f: ee.Feature(None, f.toDictionary())).getInfo()['features']]

def safe_reduce(kind, codes, img_fn, add_area=False, scale=None):
    p = _cache_path(kind, codes)
    if os.path.exists(p):
        return json.load(open(p))
    def run(cs, depth=0):
        for attempt in range(4):
            try:
                fc_b = fc_for(cs)
                if add_area:
                    fc_b = fc_b.map(lambda f: f.set('area_ha', f.geometry().area(100).divide(1e4)))
                return reduce_batch(img_fn(fc_b), fc_b, scale)
            except ee.EEException as e:
                msg = str(e).lower()
                if any(s in msg for s in ['memory', 'timed out', 'too large', 'limit exceeded']) and len(cs) > 3 and depth < 6:
                    h = len(cs) // 2
                    return run(cs[:h], depth + 1) + run(cs[h:], depth + 1)
                print(f'   retry {attempt + 1} [{kind}] {str(e)[:150]}')
                time.sleep(15 * (attempt + 1))
        print(f'   ⚠️ gave up on {len(cs)} tehsils for {kind}')
        return []
    res = run(list(codes))
    if len(res) >= len(codes):
        json.dump(res, open(p, 'w'))
    return res

SAT_MONTHLY, SAT_TREND, SAT_CUR, SAT_MAPS, SAT_WX = {}, {}, {}, {}, {}
RICE_SIG = hashlib.md5(json.dumps([RICE_VH_FLOOD_DB, RICE_VV_FLOOD_DB, RICE_VH_DROP_DB, RICE_VH_RISE_DB, RICE_FLOOD_LEAD_DAYS,
                                   RICE_RECENT_DAYS, RICE_HARVEST_NDVI, RICE_CALIBRATE, RICE_CAL_SETS]).encode()).hexdigest()[:8]
GEO_AREA_HA = dict(zip(TEH.lgd, (gpd.GeoSeries(TEH.geometry, crs=4326).to_crs(6933).area / 1e4).round(1)))  # full-res, equal-area
# GitHub: batches are built per state, so a shard job and the final job create identical batches (= identical cache files)
SHARD_COUNT, SHARD_INDEX = int(os.environ.get('SHARD_COUNT', '1')), int(os.environ.get('SHARD_INDEX', '0'))
_by_state = TEH.groupby('state', sort=True).lgd.apply(list)
_scope = list(_by_state.index)
if SHARD_COUNT > 1:
    _load, _own = [0] * SHARD_COUNT, {}
    for st in sorted(_scope, key=lambda s_: (-len(_by_state[s_]), s_)):      # greedy, deterministic balancing by tehsil count
        k_ = _load.index(min(_load)); _own[st] = k_; _load[k_] += len(_by_state[st])
    _scope = [s_ for s_ in _scope if _own[s_] == SHARD_INDEX]
    print(f'Shard {SHARD_INDEX + 1}/{SHARD_COUNT}: {len(_scope)} states, {sum(len(_by_state[s_]) for s_ in _scope)} tehsils → {_scope}')
_batches = [codes_[i:i + BATCH_SIZE] for st in _scope for codes_ in [_by_state[st]] for i in range(0, len(codes_), BATCH_SIZE)]
_mgroups = [MONTHS[i:i + 6] for i in range(0, len(MONTHS), 6)]
_t0 = time.time()
for bi, codes in enumerate(_batches, 1):
    for mg in _mgroups:
        kind = f"M_{mg[0]:%Y%m}_{mg[-1]:%Y%m}" + (f'_{TODAY}' if CUR_MONTH in mg else '')
        for pr in safe_reduce(kind, codes, monthly_image_fn(mg)):
            SAT_MONTHLY.setdefault(norm_code(pr.get(LGD_F)), {}).update(pr)
    for pr in safe_reduce(f'TREND6_{TODAY}_{RICE_SIG}', codes, trend_image):
        SAT_TREND[norm_code(pr.get(LGD_F))] = pr
    for pr in safe_reduce(f'CUR4_{TODAY}', codes, current_image, add_area=True):
        SAT_CUR[norm_code(pr.get(LGD_F))] = pr
    for pr in safe_reduce(f'MAPS5_{TODAY}_{RICE_SIG}', codes, maps_image):
        SAT_MAPS[norm_code(pr.get(LGD_F))] = pr
    for pr in safe_reduce(f'WX_{TODAY}', codes, weather_image, scale=11132):
        SAT_WX[norm_code(pr.get(LGD_F))] = pr
    el = time.time() - _t0
    print(f'Batch {bi}/{len(_batches)} | {el / 60:.1f} min | ETA {el / bi * (len(_batches) - bi) / 60:.1f} min')
print(f'✅ Extraction done: monthly {len(SAT_MONTHLY)} · trend {len(SAT_TREND)} · current {len(SAT_CUR)} · maps {len(SAT_MAPS)} · weather {len(SAT_WX)}')
if EXTRACT_ONLY:
    print('Extraction-only shard finished – cache saved in', CACHE_DIR)
    sys.exit(0)


# ===== CELL 7b: IFPRI SPAM 2020 crop mix (auto-download, cached in Drive) =====
# Modelled crop harvested area on a ~10 km grid, built by IFPRI from national/sub-national crop statistics (circa 2019-21).
# Used ONLY for crops the satellite layers cannot separate. Labelled "Modelled (IFPRI SPAM 2020)".
import zipfile, glob
SPAM_NAMES = {'whea': 'Wheat', 'rice': 'Rice', 'maiz': 'Maize', 'barl': 'Barley', 'pmil': 'Pearl millet (Bajra)',
    'smil': 'Small millets', 'sorg': 'Sorghum (Jowar)', 'ocer': 'Other cereals', 'pota': 'Potato', 'swpo': 'Sweet potato',
    'yams': 'Yams', 'cass': 'Cassava', 'orts': 'Other roots', 'bean': 'Beans', 'chic': 'Chickpea (Gram)', 'cowp': 'Cowpea',
    'pige': 'Pigeonpea (Tur/Arhar)', 'lent': 'Lentil', 'opul': 'Other pulses', 'soyb': 'Soybean', 'grou': 'Groundnut',
    'cnut': 'Coconut', 'oilp': 'Oil palm', 'sunf': 'Sunflower', 'rape': 'Rapeseed & Mustard', 'sesa': 'Sesame',
    'ooil': 'Other oilseeds', 'sugc': 'Sugarcane', 'sugb': 'Sugar beet', 'cott': 'Cotton', 'ofib': 'Other fibre crops',
    'acof': 'Arabica coffee', 'rcof': 'Robusta coffee', 'coco': 'Cocoa', 'teas': 'Tea', 'toba': 'Tobacco', 'bana': 'Banana',
    'plnt': 'Plantain', 'trof': 'Tropical fruit', 'temf': 'Temperate fruit', 'vege': 'Vegetables', 'rest': 'Other crops'}
# Typical Indian growing season of each crop (agronomic knowledge – flagged as Intelligence in QC Flags)
SPAM_SEASON = {'Rabi': {'whea', 'barl', 'chic', 'lent', 'rape', 'pota', 'sunf', 'sugb', 'toba', 'temf'},
               'Kharif': {'pmil', 'smil', 'soyb', 'cott', 'pige', 'sesa', 'cowp', 'bean', 'opul', 'ocer', 'ofib'},
               'Both': {'rice', 'maiz', 'sorg', 'grou', 'vege', 'ooil', 'orts', 'swpo'},
               'Annual': {'sugc', 'bana', 'plnt', 'cass', 'yams', 'trof', 'cnut', 'oilp', 'teas', 'acof', 'rcof', 'coco'}}
DV = 'https://dataverse.harvard.edu'
SPAM_TEH, SPAM_INFO = {}, NA

SPAM_DOI = 'doi:10.7910/DVN/SWPENT'          # IFPRI "Global Spatially-Disaggregated Crop Production Statistics Data for 2020"
UA = {'User-Agent': 'Mozilla/5.0 (crop-intel colab)', 'Accept': 'application/json'}

def _json(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=90)
    if r.status_code != 200 or 'json' not in r.headers.get('content-type', ''):
        raise RuntimeError(f'Dataverse {r.status_code} {r.headers.get("content-type")} for {r.url}: {r.text[:200]}')
    return r.json()

def _dv_find():
    return SPAM_DOI, 'SPAM 2020 (IFPRI)'

def _dv_files(pid):
    d = _json(f'{DV}/api/datasets/:persistentId/versions/:latest-published/files', {'persistentId': pid})['data']
    return [(f['dataFile']['id'], f['dataFile']['filename']) for f in d]

def _download(fid, path):
    with requests.get(f'{DV}/api/access/datafile/{fid}', stream=True, timeout=900, headers={'User-Agent': UA['User-Agent']}) as r:
        r.raise_for_status()
        with open(path, 'wb') as fh:
            for chunk in r.iter_content(1 << 22):
                fh.write(chunk)

def _bbox():
    x0, y0, x1, y1 = TEH_GDF.total_bounds
    return x0 - 0.1, y0 - 0.1, x1 + 0.1, y1 + 0.1

def spam_cells():
    """India grid cells: x, y + harvested area (ha) per SPAM crop code (all technologies)."""
    cache = f'{CACHE_DIR}/spam2020_india_cells.csv'
    for _c in (cache, f'{globals().get("INPUT_DIR", WORK_DIR)}/spam2020_india_cells.csv', f'{WORK_DIR}/spam2020_india_cells.csv'):
        if os.path.exists(_c):
            print('Using SPAM India cells file:', _c)
            return pd.read_csv(_c)
    local = sorted(glob.glob(f'{INPUT_DIR}/*harvested_area*.zip') + glob.glob(f'{WORK_DIR}/*harvested_area*.zip'))
    local_csv = [f for f in local if 'csv' in f.lower()]
    if local_csv or local:
        path = (local_csv or local)[0]
        print('Using SPAM file from Drive:', path)
        os.makedirs(f'{WORK_DIR}/spam', exist_ok=True)
        zipfile.ZipFile(path).extractall(f'{WORK_DIR}/spam')
        return _spam_parse(cache)
    pid, name = _dv_find()
    files = _dv_files(pid)
    print('SPAM dataset:', name, pid, '| files:', [f[1] for f in files][:30])
    harv = [f for f in files if 'harvest' in f[1].lower() or '_h_' in f[1].lower() or '_h.' in f[1].lower()]
    pick = next((f for f in harv if 'csv' in f[1].lower()), None) or next((f for f in harv if 'tif' in f[1].lower()), None)
    assert pick, f'No harvested-area file found in {[f[1] for f in files]}'
    os.makedirs(f'{WORK_DIR}/spam', exist_ok=True)
    path = f'{WORK_DIR}/spam/{pick[1]}'
    print('Downloading', pick[1], '…')
    try:
        _download(pick[0], path)
    except requests.HTTPError as e:
        raise RuntimeError('Harvard Dataverse needs a guestbook form for this file. Download '
                           f'"{pick[1]}" once in your browser from https://doi.org/10.7910/DVN/SWPENT and commit it to the repo '
                           'folder data/ (only if under 100 MB), then re-run.') from e
    if path.endswith('.zip'):
        zipfile.ZipFile(path).extractall(f'{WORK_DIR}/spam')
    return _spam_parse(cache)

def _spam_parse(cache):
    csvs = [f for f in glob.glob(f'{WORK_DIR}/spam/**/*.csv', recursive=True)]
    if csvs:
        df = None
        for chunk in pd.read_csv(csvs[0], chunksize=200000, low_memory=False):
            chunk.columns = [c.lower() for c in chunk.columns]
            chunk = chunk[chunk['iso3'].astype(str).str.upper() == 'IND']
            df = chunk if df is None else pd.concat([df, chunk])
        keep = ['x', 'y'] + [c for c in df.columns if c.endswith('_a') and len(c) == 6]
        assert len(keep) > 2, f'SPAM CSV columns not recognised: {list(df.columns)[:40]}' 
        df = df[keep].rename(columns={c: c[:-2] for c in keep if c.endswith('_a')})
    else:
        import rasterio
        from rasterio.windows import from_bounds
        tifs = [f for f in glob.glob(f'{WORK_DIR}/spam/**/*.tif', recursive=True) if re.search(r'_a\.tif$', f.lower())]
        assert tifs, 'No SPAM CSV or "_A.tif" rasters found'
        df = None
        for t in tifs:
            code = re.search(r'_([a-z]{4})_a\.tif$', t.lower()).group(1)
            with rasterio.open(t) as src:
                w = from_bounds(68, 6, 98, 38, src.transform)
                a = src.read(1, window=w).astype(float)
                tr = src.window_transform(w)
            a[(a < 0) | ~np.isfinite(a)] = 0
            rr, cc = np.nonzero(a > 0)
            xs, ys = rasterio.transform.xy(tr, rr, cc)
            part = pd.DataFrame({'x': np.round(xs, 5), 'y': np.round(ys, 5), code: a[rr, cc]})
            df = part if df is None else df.merge(part, on=['x', 'y'], how='outer')
        df = df.fillna(0)
    df.to_csv(cache, index=False)
    print(f'SPAM India cells cached: {len(df):,}')
    return df

if USE_SPAM:
    try:
        cells = spam_cells()
        x0, y0, x1, y1 = _bbox()
        cells = cells[(cells.x >= x0) & (cells.x <= x1) & (cells.y >= y0) & (cells.y <= y1)].reset_index(drop=True)
        _pts = gpd.GeoDataFrame({'cid': cells.index}, geometry=gpd.points_from_xy(cells.x, cells.y), crs=4326)
        _j = gpd.sjoin(_pts, TEH_GDF, how='inner', predicate='within')
        cid2lgd = dict(zip(_j.cid, _j.lgd))
        codes = [c for c in cells.columns if c not in ('x', 'y')]
        cells['lgd'] = cells.index.map(cid2lgd)
        agg = cells.dropna(subset=['lgd']).groupby('lgd')[codes].sum()
        for lgd, row in agg.iterrows():
            SPAM_TEH[lgd] = {c: float(v) for c, v in row.items() if v > 0}
        SPAM_INFO = 'IFPRI SPAM 2020 harvested area (~10 km grid, modelled from crop statistics circa 2019-21)'
        print(f'✅ SPAM crop mix for {len(SPAM_TEH)} of {len(TEH)} tehsils (others use their district mix)')
    except Exception as e:
        print('⚠️ SPAM not loaded – crops satellites cannot separate will show as "Other crops – not identified":', str(e)[:300])


# ===== CELL 7b2: CROPGRIDS 2020 crop mix – automatic download, no form / login (v3.5) =====
# 173 crops, harvested area circa 2020 at 0.05° (~5.6 km). Used ONLY to NAME crops that the satellite layers cannot separate,
# and always labelled "Modelled (CROPGRIDS 2020)" – it is a statistical crop mix, not a current-season observation.
import xarray as xr
CG_CROPS = {  # CROPGRIDS file name → pipeline crop code (IFPRI SPAM codes where one exists)
    'rice': 'rice', 'wheat': 'whea', 'maize': 'maiz', 'barley': 'barl', 'millet': 'mill', 'sorghum': 'sorg', 'potato': 'pota',
    'sweetpotato': 'swpo', 'cassava': 'cass', 'yam': 'yams', 'bean': 'bean', 'chickpea': 'chic', 'cowpea': 'cowp', 'pigeonpea': 'pige',
    'lentil': 'lent', 'pulsenes': 'opul', 'pea': 'peas', 'greenpea': 'peas', 'soybean': 'soyb', 'groundnut': 'grou', 'coconut': 'cnut',
    'oilpalm': 'oilp', 'sunflower': 'sunf', 'rapeseed': 'rape', 'mustard': 'rape', 'sesame': 'sesa', 'castor': 'cast',
    'safflower': 'saff', 'linseed': 'lins', 'sugarcane': 'sugc', 'cotton': 'cott', 'jute': 'jute', 'coffee': 'coff', 'tea': 'teas',
    'tobacco': 'toba', 'banana': 'bana', 'plantain': 'plnt', 'mango': 'mang', 'cashew': 'cash', 'areca': 'arec', 'rubber': 'rubb',
    'pepper': 'pepp', 'ginger': 'ging', 'spicenes': 'spic', 'chilleetc': 'chil', 'onion': 'onio', 'garlic': 'garl', 'tomato': 'toma',
    'eggplant': 'brin', 'okra': 'okra', 'cauliflower': 'caul', 'cabbage': 'cabb', 'vegetablenes': 'vege', 'cucumberetc': 'vege',
    'pumpkinetc': 'vege', 'orange': 'citr', 'lemonlime': 'citr', 'citrusnes': 'citr', 'grape': 'grap', 'papaya': 'papa',
    'pineapple': 'pine', 'tropicalnes': 'trof', 'fruitnes': 'trof'}
SPAM_NAMES.update({'mill': 'Millets (bajra / ragi / small millets)', 'peas': 'Peas', 'cast': 'Castor', 'saff': 'Safflower',
    'lins': 'Linseed', 'jute': 'Jute', 'coff': 'Coffee', 'mang': 'Mango', 'cash': 'Cashew', 'arec': 'Arecanut', 'rubb': 'Rubber',
    'pepp': 'Black pepper', 'ging': 'Ginger', 'spic': 'Spices (turmeric, coriander, cumin …)', 'chil': 'Chillies', 'onio': 'Onion',
    'garl': 'Garlic', 'toma': 'Tomato', 'brin': 'Brinjal', 'okra': 'Okra (bhindi)', 'caul': 'Cauliflower', 'cabb': 'Cabbage',
    'citr': 'Citrus', 'grap': 'Grapes', 'papa': 'Papaya', 'pine': 'Pineapple'})
SPAM_SEASON['Rabi'] |= {'peas', 'saff', 'lins', 'garl', 'caul', 'cabb'}
SPAM_SEASON['Kharif'] |= {'mill', 'cast', 'jute'}
SPAM_SEASON['Both'] |= {'chil', 'onio', 'toma', 'brin', 'okra'}
SPAM_SEASON['Annual'] |= {'coff', 'mang', 'cash', 'arec', 'rubb', 'pepp', 'ging', 'spic', 'citr', 'grap', 'papa', 'pine'}
MIX_SRC, MIX_NAME, MIX_REF = 'Modelled (IFPRI SPAM 2020)', 'IFPRI SPAM 2020', 'SPAM'

def _cg_file(c):
    pth = f'{CROPGRIDS_DIR}/{c}.nc'
    if os.path.exists(pth) and os.path.getsize(pth) > 10_000:
        return pth
    for u in CROPGRIDS_URLS:
        for attempt in range(3):
            try:
                rq = requests.get(u.format(c=c), timeout=300, headers={'User-Agent': 'CropIntel/3.5 (+https://github.com/tafecs1/crop-intelligence)'})
                if rq.status_code == 200 and rq.content[:4] in (b'\x89HDF', b'CDF\x01', b'CDF\x02'):
                    open(pth, 'wb').write(rq.content)
                    return pth
            except Exception:
                pass
            time.sleep(5 * (attempt + 1))
    return None

def _cg_zip_fallback(missing):
    """Official figshare zip (807 MB) – used only if the per-crop mirror failed; extracts just the missing crops."""
    zp = f'{CROPGRIDS_DIR}/CROPGRIDS_NC_maps.zip'
    if not os.path.exists(zp):
        with requests.get(CROPGRIDS_ZIP, stream=True, timeout=1800, headers={'User-Agent': 'CropIntel/3.5'}) as r:
            r.raise_for_status()
            with open(zp, 'wb') as fh:
                for chunk in r.iter_content(1 << 22):
                    fh.write(chunk)
    with zipfile.ZipFile(zp) as z:
        for nm in z.namelist():
            m = re.search(r'CROPGRIDSv[\d.]+_([a-z]+)\.nc$', nm)
            if m and m.group(1) in missing:
                open(f'{CROPGRIDS_DIR}/{m.group(1)}.nc', 'wb').write(z.read(nm))
    os.remove(zp)

def cropgrids_cells():
    cache = f'{CROPGRIDS_DIR}/cropgrids_india_cells.csv'
    if os.path.exists(cache):
        return pd.read_csv(cache)
    os.makedirs(CROPGRIDS_DIR, exist_ok=True)
    got = {c: _cg_file(c) for c in CG_CROPS}
    missing = [c for c, p in got.items() if not p]
    if missing:
        print(f'   CROPGRIDS mirror missing {len(missing)} crops – trying the official figshare zip …')
        try:
            _cg_zip_fallback(set(missing))
        except Exception as e:
            print('   figshare fallback failed:', str(e)[:200])
        got = {c: (f'{CROPGRIDS_DIR}/{c}.nc' if os.path.exists(f'{CROPGRIDS_DIR}/{c}.nc') else None) for c in CG_CROPS}
    ok = {c: p for c, p in got.items() if p}
    assert ok.get('rice') and ok.get('wheat'), 'CROPGRIDS core files (rice, wheat) could not be downloaded'
    grid, acc = None, {}
    for c, p in ok.items():
        with xr.open_dataset(p) as d:
            v = 'harvarea' if 'harvarea' in d.data_vars else next(k for k in d.data_vars if 'harv' in k.lower())
            la = next(k for k in d.coords if k.lower().startswith('lat'))
            lo = next(k for k in d.coords if k.lower().startswith('lon'))
            lat, lon = d[la].values, d[lo].values
            iy, ix = np.where((lat >= 6) & (lat <= 38))[0], np.where((lon >= 68) & (lon <= 98))[0]
            a = np.asarray(d[v].isel({la: iy, lo: ix}).squeeze().values, dtype=float)
            if a.shape != (len(iy), len(ix)):
                a = a.T
            if grid is None:
                grid = (lat[iy], lon[ix])
        a[~np.isfinite(a) | (a < 0)] = 0
        code = CG_CROPS[c]
        acc[code] = acc[code] + a if code in acc else a
    tot = sum(acc.values())
    rr, cc = np.nonzero(tot > 0)
    df = pd.DataFrame({'x': np.round(grid[1][cc], 4), 'y': np.round(grid[0][rr], 4), **{k: v[rr, cc] for k, v in acc.items()}})
    df.to_csv(cache, index=False)
    for p_ in ok.values():                      # the India table is all the pipeline needs – drop the global files
        os.remove(p_)
    print(f'CROPGRIDS India cells cached: {len(df):,} cells, {len(ok)} crop files')
    return df

if USE_CROPGRIDS and not SPAM_TEH:
    try:
        cells = cropgrids_cells()
        x0, y0, x1, y1 = _bbox()
        cells = cells[(cells.x >= x0) & (cells.x <= x1) & (cells.y >= y0) & (cells.y <= y1)].reset_index(drop=True)
        _pts = gpd.GeoDataFrame({'cid': cells.index}, geometry=gpd.points_from_xy(cells.x, cells.y), crs=4326)
        _j = gpd.sjoin(_pts, TEH_GDF, how='inner', predicate='within')
        cells['lgd'] = cells.index.map(dict(zip(_j.cid, _j.lgd)))
        codes = [c for c in cells.columns if c not in ('x', 'y', 'lgd')]
        for lgd, row in cells.dropna(subset=['lgd']).groupby('lgd')[codes].sum().iterrows():
            SPAM_TEH[lgd] = {c: float(v) for c, v in row.items() if v > 0}
        MIX_SRC, MIX_NAME, MIX_REF = 'Modelled (CROPGRIDS 2020)', 'CROPGRIDS 2020', 'CROPGRIDS'
        SPAM_INFO = 'CROPGRIDS 2020 harvested area (0.05° grid, modelled from FAOSTAT 2020 + sub-national statistics)'
        _ri = sum(m.get('rice', 0) for m in SPAM_TEH.values()); _wh = sum(m.get('whea', 0) for m in SPAM_TEH.values())
        print(f'✅ CROPGRIDS crop mix for {len(SPAM_TEH)} of {len(TEH)} tehsils (others use their district mix) | '
              f'run-area totals: rice {_ri:,.0f} ha, wheat {_wh:,.0f} ha (all seasons, c. 2020)')
    except Exception as e:
        print('⚠️ CROPGRIDS not loaded:', str(e)[:300])
if STRICT_FILL and not SPAM_TEH and globals().get('APY') is None:
    raise RuntimeError('No crop-mix source loaded (CROPGRIDS download failed, no SPAM / APY file). Stopping instead of writing '
                       'placeholder crop names – re-run the workflow; set STRICT_FILL = False to publish anyway.')


# ===== CELL 7c: GGCMI Phase 3 crop calendar – all-India sowing windows & durations (auto-download, cached in Drive) =====
# Static multi-year average planting / maturity day per 0.5° cell, irrigated (ir) and rainfed (rf). Used as CALENDAR EVIDENCE only
# (like TNAU in Tamil Nadu): it never proves that a crop is present, and it is labelled in every row that uses it.
import xarray as xr
GG = {}
try:
    os.makedirs(GGCMI_DIR, exist_ok=True)
    _pts = TEH_GDF.geometry.representative_point()
    _lon, _lat = _pts.x.values, _pts.y.values
    for code in sorted({c for v in GGCMI_CODES.values() for c in v}):
        for sysn in ('ir', 'rf'):
            fn = f'{code}_{sysn}_ggcmi_crop_calendar_phase3_v1.01.nc4'
            pth = f'{GGCMI_DIR}/{fn}'
            if not os.path.exists(pth) and os.path.exists(f'{INPUT_DIR}/ggcmi/{fn}'):
                pth = f'{INPUT_DIR}/ggcmi/{fn}'                       # files committed to the repo (data/ggcmi/)
            if not os.path.exists(pth):
                for url in (f'https://zenodo.org/api/records/5062513/files/{fn}/content', GGCMI_URL.format(f=fn)):
                    for attempt in range(3):
                        rq = requests.get(url, timeout=180, headers={'User-Agent': 'CropIntel/3.3 (+https://github.com/tafecs1/crop-intelligence)'})
                        if rq.status_code == 200:
                            break
                        time.sleep(5 * (attempt + 1))
                    if rq.status_code == 200:
                        break
                rq.raise_for_status()
                open(pth, 'wb').write(rq.content)
            with xr.open_dataset(pth) as dsx:
                pv = next(v for v in dsx.data_vars if 'plant' in v.lower())
                mv = next(v for v in dsx.data_vars if 'matur' in v.lower())
                la = next(c for c in dsx.coords if c.lower().startswith('lat'))
                lo = next(c for c in dsx.coords if c.lower().startswith('lon'))
                sel = {la: xr.DataArray(_lat, dims='pt'), lo: xr.DataArray(_lon, dims='pt')}
                P = np.asarray(dsx[pv].squeeze().sel(sel, method='nearest').values, dtype=float)
                M = np.asarray(dsx[mv].squeeze().sel(sel, method='nearest').values, dtype=float)
            for lgd, p_, m_ in zip(TEH.lgd, P, M):
                if np.isfinite(p_) and np.isfinite(m_) and 1 <= p_ <= 366 and 1 <= m_ <= 366:
                    GG.setdefault(lgd, {}).setdefault(code, []).append((sysn, int(p_), int(m_)))
    print(f'✅ GGCMI crop calendar: {len(GG)} of {len(TEH)} tehsils, crops {sorted({c for v in GG.values() for c in v})}')
except Exception as e:
    print('⚠️ GGCMI calendar not loaded – outside Tamil Nadu sowing windows will be missing (stages mostly "Stage not determined"):', str(e)[:300])

def gg_windows(lgd, model, ref):
    """GGCMI sowing windows for this tehsil and crop: most recent planting day ≤ ref + window, ± GGCMI_WINDOW_DAYS; duration = maturity − planting."""
    out, W = [], dt.timedelta(days=GGCMI_WINDOW_DAYS)
    for code in GGCMI_CODES.get(model, []):
        for sysn, p_, m_ in GG.get(lgd, {}).get(code, []):
            dur = (m_ - p_) % 365 or 365
            occ = [dt.date(y, 1, 1) + dt.timedelta(days=p_ - 1) for y in (ref.year - 1, ref.year, ref.year + 1)]
            occ = [o for o in occ if o - W <= ref + dt.timedelta(days=30)]
            if occ:
                o = max(occ)
                out.append(dict(label=f"GGCMI {code} ({'irrigated' if sysn == 'ir' else 'rainfed'})", win=(o - W, o + W), dur=dur))
    return out

def sow_windows(model, state, lgd, ref):
    """Sowing windows: TNAU (Tamil Nadu, finer) else GGCMI (all India). Returns [(label, (start, end), duration or None)]."""
    if norm_state(state) == 'TAMIL NADU' and model in TN_WINDOWS:
        return [(f'TNAU {nm}', month_window_dates(mo, ref), None) for nm, mo in TN_WINDOWS[model]]
    out = [(w['label'], w['win'], w['dur']) for w in gg_windows(lgd, model, ref)]
    if not out and model in INDIA_WINDOWS:        # v3.5: broad national window only when TNAU / GGCMI have nothing for this tehsil
        out = [(f'{nm} (broad national window)', month_window_dates(mo, ref), None) for nm, mo in INDIA_WINDOWS[model]]
    return out


# ===== CELL 8: Monthly index series + Govt district crop areas =====
def fnum(x):
    try:
        x = float(x)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except Exception:
        return None

def pct(a, b):
    if a is None or b is None or abs(b) < 0.02:
        return None
    return round((a - b) / abs(b) * 100, 1)

def r4(x):
    return None if x is None else round(x, 4)

DIST_OF, STATE_OF = dict(zip(TEH.lgd, TEH.dlgd)), dict(zip(TEH.lgd, TEH.slgd))

SERIES = {idx: {} for idx in IDX6}
for lgd in TEH.lgd:
    p = SAT_MONTHLY.get(lgd, {})
    for idx in IDX6:
        vals, srcs = [], []
        for ym in YM:
            v, share = fnum(p.get(f'{idx}_{ym}')), fnum(p.get(f'valid_{ym}')) or 0
            ok = v is not None and share >= MIN_VALID_SHARE
            vals.append(v if ok else None); srcs.append(SAT if ok else None)
        SERIES[idx][lgd] = [vals, srcs]
SAT_ONLY_NDVI = {lgd: list(SERIES['NDVI'][lgd][0]) for lgd in TEH.lgd}     # measured values only (for Tab 8 history)

if GAP_FILL:
    for idx in IDX6:
        td, ts = {}, {}
        for lgd, (vals, _) in SERIES[idx].items():
            for i, v in enumerate(vals):
                if v is not None:
                    td.setdefault((DIST_OF[lgd], i), []).append(v); ts.setdefault((STATE_OF[lgd], i), []).append(v)
        dmed = {k: float(np.median(v)) for k, v in td.items()}
        smed = {k: float(np.median(v)) for k, v in ts.items()}
        for lgd, (vals, srcs) in SERIES[idx].items():
            orig = list(vals)
            for i in range(len(vals)):
                if orig[i] is not None:
                    continue
                j = next((j for j in range(i - 1, -1, -1) if orig[j] is not None), None)
                k = next((k for k in range(i + 1, len(orig)) if orig[k] is not None), None)
                if j is not None and k is not None and k - j <= 3:
                    fill = orig[j] + (orig[k] - orig[j]) * (i - j) / (k - j)          # interpolation (measured neighbours)
                else:
                    fill = dmed.get((DIST_OF[lgd], i), smed.get((STATE_OF[lgd], i)))  # measured district/state median, same month
                if fill is not None:
                    vals[i], srcs[i] = fill, FILL

# Govt district crop areas (latest APY_YEARS years, mean per year)
GOVT_AREA, APY_MODE = {}, 'not loaded'
if APY is not None:
    code_hit = TEH.dlgd.isin(set(APY.dcode)).mean() if APY.dcode.ne('').any() else 0
    if code_hit >= 0.5:
        APY['key'], TEH['gkey'], APY_MODE = APY.dcode, TEH.dlgd, f'district LGD code ({code_hit:.0%} matched)'
    else:
        APY['key'] = APY.state.map(norm_state) + '|' + APY.dname.map(norm_name)
        TEH['gkey'], APY_MODE = TEH.dkey, f'district name ({TEH.dkey.isin(set(APY.key)).mean():.0%} matched)'
    latest = APY.groupby('key').year.max()
    _a = APY[APY.year > APY.key.map(latest) - APY_YEARS]
    nyears = _a.groupby('key').year.nunique()
    _g = _a.groupby(['key', 'crop', 'season']).agg(area=('area', 'sum'), y0=('year', 'min'), y1=('year', 'max')).reset_index()
    for r in _g.itertuples():
        GOVT_AREA.setdefault(r.key, []).append(dict(crop=r.crop, season=r.season, area=r.area / nyears[r.key],
                                                    years=f'{int(r.y0)}–{int(r.y1)}'))
    _miss = TEH[~TEH.gkey.isin(GOVT_AREA)].district.unique()
    print(f'✅ Govt APY matched by {APY_MODE}; districts without APY data: {len(_miss)} {list(_miss)[:10]}')
else:
    TEH['gkey'] = TEH.dlgd
print('✅ Monthly series ready')


# ===== CELL 9: Crop-specific phenology engine (measured curves + sourced calendars) =====
def crossings(dates, vals, ref_amp=None, min_amp=0.12):
    """Current growth cycle of a measured NDVI curve: rising crossings at 10/30/80 % and falling at 80/20 % of cycle amplitude."""
    pts = [(d, v) for d, v in zip(dates, vals) if v is not None]
    if len(pts) < 4:
        return None
    vs = [v for _, v in pts]
    pk = vs.index(max(vs))
    start = vs[:pk + 1].index(min(vs[:pk + 1]))
    post_min = pk + vs[pk:].index(min(vs[pk:]))
    if post_min < len(vs) - 1 and max(vs[post_min:]) - vs[post_min] >= min_amp:
        start = post_min
        pk = start + vs[start:].index(max(vs[start:]))
    mn = vs[start]
    rise = vs[pk] - mn
    ref = max(rise, ref_amp or 0)
    if ref < min_amp:
        return None
    peaked = pk < len(vs) - 1 and rise >= 0.8 * ref and vs[-1] < vs[pk] - min_amp / 6
    out = {'peak': pts[pk][0] if peaked else None, 'amp': ref, 'min': mn, 'peaked': peaked}
    for key, f in (('c10', .1), ('c30', .3), ('c80r', .8)):
        # v3.4: a crossing counts only if the curve then STAYS above the level up to the peak (noise blips in a flat baseline ignored)
        out[key] = next((pts[i][0] for i in range(start + 1, pk + 1) if all(v >= mn + f * ref for v in vs[i:pk + 1])), None)
    for key, f in (('c80f', .8), ('c20f', .2)):
        out[key] = next((pts[i][0] for i in range(pk + 1, len(pts)) if vs[i] <= mn + f * ref), None) if peaked else None
    return out

def curve_phase(trend, ref_amp=None, name='NDVI', min_amp=0.12, low_level=0.3, unit=''):
    """Phase of a MULTI-DATE measured curve (never a single value). Returns dict(phase, q, slope, obs, cx, n, reason).
    phase=None → fewer than 4 valid 14-day composites (insufficient). Also requires one of the last two steps to be valid."""
    pts = [(i, v) for i, v in enumerate(trend or []) if v is not None]
    if len(pts) < 4 or pts[-1][0] < len(trend) - 2:
        why = (f'{len(pts)}/{TREND_STEPS} valid 14-day {name} composites' if len(pts) < 4 else
               f'no valid {name} composite in the last 28 days (latest valid step ends {TREND_ENDS[pts[-1][0]]})')
        return dict(phase=None, q=0, slope=None, obs=TREND_ENDS[pts[-1][0]] if pts else None, cx=None, n=len(pts),
                    reason=why + ' – insufficient')
    vs = [v for _, v in pts]
    latest = vs[-1]
    slope = (vs[-1] - vs[-2]) / (pts[-1][0] - pts[-2][0])
    cx = crossings(TREND_MID, trend, ref_amp, min_amp)
    tol = min_amp / 12
    if not cx:
        ph = 'pre' if latest < low_level else 'flat'
        rel = None
    else:
        rel = (latest - cx['min']) / cx['amp']
        if not cx['peaked']:
            # a curve whose latest composite is its maximum and still climbing is RISING, not at peak
            ph = 'pre' if (rel < 0.1 and slope <= tol) else ('peak' if (rel >= 0.8 and slope <= 2 * tol) else 'rising')
        else:
            ph = 'peak' if rel >= 0.8 else ('declining' if (rel >= 0.2 or slope < -tol) else 'post')
    q = round(min(1, len(pts) / TREND_STEPS) * (min(1, cx['amp'] / (2.5 * min_amp)) if cx else 0.4), 2)
    why = (f'{name} {latest:.2f}{unit}, {slope:+.3f}{unit}/14 d over {len(pts)} composites'
           + (f', {rel:.0%} of cycle amplitude {cx["amp"]:.2f}{unit}' if cx else ', no seasonal cycle'))
    return dict(phase=ph, q=q, slope=slope, obs=TREND_ENDS[pts[-1][0]], cx=cx, n=len(pts), reason=f'{why} → {PHASES[ph]}')

def stages_at(table, d_lo, d_hi):
    """Crop stages possible for days-after-planting in [d_lo, d_hi], using both the early and late stage-start bounds.
    Returned IN SEQUENCE ORDER as (name, bucket, allowed_phases, index) – v3.0 returned them unordered (→ backward ranges)."""
    hit = set()
    step = max(1, (int(d_hi) - int(d_lo)) // 20 or 1)
    for bound in (0, 1):
        for d in list(range(int(d_lo), int(d_hi) + 1, step)) + [int(d_hi)]:
            cur = None
            for k, (name, start, bucket, allowed) in enumerate(table):
                if d >= (start[bound] if isinstance(start, tuple) else start):
                    cur = k
            if cur is not None:
                hit.add(cur)
    return [(table[k][0], table[k][2], table[k][3], k) for k in sorted(hit)]

def stage_names(table):
    return [t[0] for t in table]

def rice_context(T, state, lgd=None):
    """Rice season + duration class consistent with a detected transplanting date T.
    Tamil Nadu: TNAU seasons (nursery sowing = T − nursery age). Elsewhere: GGCMI ri1/ri2 windows (±GGCMI_WINDOW_DAYS) with
    duration = GGCMI maturity − planting ± 15 d; nursery 0-25 d allowed because GGCMI does not state whether 'planting' is sowing or transplanting."""
    cands = []
    if norm_state(state) == 'TAMIL NADU':
        for season, months, cls in RICE_SEASONS_TN:
            c = RICE_CLASSES[cls]
            for nur in range(c['nursery'][0], c['nursery'][1] + 1):
                if (T - dt.timedelta(days=nur)).month in months:
                    cands.append((f'TNAU {season}', cls)); break
    elif lgd is not None:
        for w in gg_windows(lgd, 'RICE', T):
            if w['win'][0] - dt.timedelta(days=25) <= T <= w['win'][1] + dt.timedelta(days=25):
                key = f"gg_{w['dur']}"
                RICE_CLASSES.setdefault(key, dict(total=(max(80, w['dur'] - 15), w['dur'] + 15), nursery=(0, 25)))
                cands.append((w['label'], key))
    if not cands:
        return None, [('No matching rice calendar', k) for k in ('short', 'medium', 'medium_long', 'late')]
    return ', '.join(sorted({c[0] for c in cands})), cands

def rice_field_duration(cands):
    lo = min(RICE_CLASSES[c]['total'][0] - RICE_CLASSES[c]['nursery'][1] for _, c in cands)
    hi = max(RICE_CLASSES[c]['total'][1] - RICE_CLASSES[c]['nursery'][0] for _, c in cands)
    return lo, hi

def gdd_since(wx, start, tbase):
    """Growing degree days from `start` to the last ERA5-Land day: Σ max(0, (Tmax+Tmin)/2 − Tbase). None if no data."""
    if not wx or start is None:
        return None, None
    tot, n, last = 0.0, 0, None
    for k, v in wx.items():
        m = re.match(r'(\d{8})_tmax$', k)
        if not m:
            continue
        d = dt.datetime.strptime(m.group(1), '%Y%m%d').date()
        tmin = fnum(wx.get(f'{m.group(1)}_tmin')); tmax = fnum(v)
        if d >= start and tmin is not None and tmax is not None:
            tot += max(0.0, (tmax + tmin) / 2 - tbase); n += 1; last = max(last or d, d)
    return (round(tot) if n else None), last

def decide_stage(cands, ph, vh=None, vh_allowed=None):
    """Combine the crop-specific calendar candidates (from the planting date) with INDEPENDENT measured evidence:
    the Sentinel-2 curve phase (multi-date) and, for rice, the Sentinel-1 VH curve phase (cloud-independent).
    Returns (final, conf_factor, note). Never guesses: contradictions or non-adjacent candidates → 'Stage not determined'."""
    if not cands:
        return UNC, 0.0, 'no crop-specific calendar stage for this planting date'
    s2 = ph['phase'] if ph and ph.get('phase') else None
    s1 = vh['phase'] if vh and vh.get('phase') else None
    if not s2 and not s1:
        return UNC, 0.0, 'insufficient recent clear satellite observations to confirm the calendar stage'
    ok = [c for c in cands if (s2 is None or s2 in c[2]) and (s1 is None or s1 in (vh_allowed or {}).get(c[0], {s1}))]
    ev = ' + '.join(x for x in [f'S2 curve: {PHASES[s2]}' if s2 else None, f'S1 VH curve: {PHASES[s1]}' if s1 else None] if x)
    both = bool(s2 and s1)
    if len(ok) == 1:
        return ok[0][0], (1.0 if both or not vh_allowed else 0.8), f'calendar stage confirmed by measured {ev}'
    if len(ok) == 2 and ok[1][3] - ok[0][3] == 1:
        return f'{ok[0][0]} → {ok[1][0]} (transition)', 0.55, f'{ev} fits two consecutive stages – reported as a transition'
    if len(ok) > 2 or (len(ok) == 2 and ok[1][3] - ok[0][3] > 1):
        return UNC, 0.0, (f'{ev} fits {len(ok)} stages ({", ".join(c[0] for c in ok)}) – planting-date / duration uncertainty too wide')
    if s2 and s1 and any(s2 in c[2] for c in cands) and any(s1 in (vh_allowed or {}).get(c[0], set()) for c in cands):
        return UNC, 0.0, f'optical and radar evidence disagree ({ev}) – not resolved'
    early = all(c[1] in ('Land Prep', 'Sowing', 'Germination', 'Vegetative') for c in cands)
    if s2 in ('declining', 'post') and early:
        return UNC, 0.0, ('POSSIBLE STRESS / CROP FAILURE: vegetation declining while the calendar expects '
                          f'{cands[0][0]} – requires field inspection (not classified as harvest)')
    return UNC, 0.0, f'measured {ev} contradicts calendar stage(s) {", ".join(c[0] for c in cands)}'

def history_windows(lgd, season):
    """Fallback 'ideal' timing: this tehsil's past same-season Sentinel-2 monthly NDVI (measured only)."""
    if season not in SEASON_SPAN:
        return None
    sm, n = SEASON_SPAN[season]
    vals = SAT_ONLY_NDVI[lgd]
    cur_y = SEASON_START.year if season == SEASON else (TODAY.year if dt.date(TODAY.year, sm, 1) <= TODAY else TODAY.year - 1)
    found = []
    for y in range(MONTHS[0].year - 1, TODAY.year + 1):
        if y == cur_y:
            continue
        months = [add_months(dt.date(y, sm, 1), k) for k in range(n)]
        if months[-1] > MONTHS[-1] or months[0] < add_months(MONTHS[0], -2):
            continue
        sv = [vals[MONTHS.index(m)] if m in MONTHS else None for m in months]
        cx = crossings([m + dt.timedelta(days=14) for m in months], sv)
        if cx:
            found.append({k: (add_years(v, cur_y - y) if isinstance(v, dt.date) else v) for k, v in cx.items()})
    if not found:
        return None
    w = {}
    for key in ('c10', 'c30', 'c80r', 'c80f', 'c20f'):
        ds_ = [f[key] for f in found if f.get(key)]
        if ds_:
            w[key] = (min(ds_) - dt.timedelta(days=15), max(ds_) + dt.timedelta(days=15), min(ds_) + (max(ds_) - min(ds_)) / 2)
    w['amp'] = float(np.median([f['amp'] for f in found])); w['n'] = len(found)
    return w

print('✅ Phenology engine ready')


# ===== CELL 10: Tab 1 – Crop_Intelligence_Current (57 cols)  +  Tab 8 – Ideal_Crop_Window (52 cols) =====
T1_HEADER = ['State', 'State LGD Code', 'District', 'District LGD Code', 'Tehsil', 'Tehsil LGD Code', 'Boundary Unit',
    'Crop', 'Crop Season', 'Crop Presence Status', 'Crop Identification Method', 'Crop Identification Confidence',
    'Estimated Crop Area Ha', 'Cropland Area Ha (WorldCover)', 'District Crop Area Ha (Govt/ISRO)', 'Crop Area Evidence',
    'Sowing Date Estimated', 'Sowing Date Earliest', 'Sowing Date Latest', 'Sowing Date Confidence', 'Sowing Date Evidence',
    'Satellite Crop Stage', 'Estimated Crop Stage', 'Final Crop Stage', 'Stage Phase Code', 'Stage Source', 'Stage Observation Date',
    'Growth Stage Confidence', 'Cropland Phase Reason', 'Expected Harvest Window',
    'Crop Health Status', 'Weather Signal', 'Satellite Signal', 'Key Insight',
    'NDVI', 'EVI', 'NDRE (red-edge)', 'NDMI', 'NDWI', 'Sentinel-1 VV', 'Sentinel-1 VH', 'S1 VV-VH (dB)',
    'NDVI Trend (14-day steps, oldest to latest)',
    'Rainfall mm', 'Rainfall Observation Period', 'Temperature',
    'Valid S2 Pixel Share', 'S2 Scenes / S1 Scenes', 'Field Verification Status', 'Geography Validation', 'Evidence Classification',
    'QC Flags', 'Overall Confidence Score', 'Model Version', 'Source', 'Processing Date', 'Run ID']
T8_HEADER = ['State', 'State LGD Code', 'District', 'District LGD Code', 'Tehsil', 'Tehsil LGD Code', 'Crop', 'Crop Season',
    'Final Crop Stage (current)', 'Ideal Window Source', 'Ideal Window Confidence', 'Ideal Sowing Window', 'Ideal Harvest Window',
    'Ideal Crop Duration Days', 'Actual Sowing Date (this season)', 'Sowing Deviation Days (+ late / − early)',
    'Overall Timing Status (Early / On Time / Late)', 'Expected Harvest Date (adjusted)', 'Days to Harvest', 'Next Stage',
    'Next Stage Ideal Start', 'Machinery Need – Next Stage (Tillage / Seeding / Spraying / Harvesting / None)']
for st in STAGE_BUCKETS:
    T8_HEADER += [f'{st} – Ideal Start', f'{st} – Ideal End', f'{st} – Actual Start', f'{st} – Deviation Days']
T8_HEADER += ['QC Flags', 'Run ID']
assert len(T1_HEADER) == 57 and len(T8_HEADER) == 52, (len(T1_HEADER), len(T8_HEADER))

ds = lambda d: d.isoformat() if isinstance(d, dt.date) else ''
wtxt = lambda a, b: f'{a:%d-%b-%Y} to {b:%d-%b-%Y}' if (a and b) else NA
UNC = 'Stage not determined'
PROVISIONAL = 'PROVISIONAL – rubric score, not measured accuracy (no ground-truth validation)'

# ---- optional ground truth
FIELD = {}
if os.path.exists(FIELD_OBS_CSV):
    _fo = pd.read_csv(FIELD_OBS_CSV)
    _fo.columns = [c.strip().lower() for c in _fo.columns]
    for rr in _fo.itertuples():
        FIELD.setdefault(norm_code(rr.tehsil_lgd), []).append(rr._asdict())
    print(f'✅ Field observations loaded: {len(_fo)} records')

def pr_(x, d=1):
    return None if x is None else round(x, d)

def model_for(name):
    n = name.lower()
    for k in CROP_MODELS:
        if k.lower() in n or (k == 'Black gram' and 'urad' in n) or (k == 'Green gram' and ('moong' in n or 'mung' in n)) or \
           (k == 'Rapeseed & Mustard' and ('mustard' in n or 'rapeseed' in n)) or (k == 'Chickpea' and (n.strip() == 'gram' or 'chickpea' in n)):
            return k
    if 'rice' in n or 'paddy' in n:
        return 'RICE'
    return None

def mix_for(r):
    """Crop mix (ha by crop code) for this tehsil: its own CROPGRIDS/SPAM cells, else district mix × cropland share,
    else state mix × cropland share (tehsils smaller than one 5.6 km grid cell)."""
    m = SPAM_TEH.get(r.lgd)
    if m:
        return m
    cl = CL_HA.get(r.lgd, 0)
    for key, mixes, cls in ((r.gkey, DIST_SPAM, DIST_CL), (STATE_OF.get(r.lgd), STATE_SPAM, STATE_CL)):
        if mixes.get(key):
            fac = cl / (cls.get(key) or cl or 1)
            return {k: v * fac for k, v in mixes[key].items()}
    return {}

def mix_how(r):
    if SPAM_TEH.get(r.lgd):
        return f'{MIX_NAME} cells inside the tehsil'
    return f'{MIX_NAME} ' + ('district' if DIST_SPAM.get(r.gkey) else 'state') + ' mix × tehsil cropland share'

def crops_v3(r, geo_ha, cl_ha, mp, cur_share):
    """Crop rows with the 7 presence labels. Area totals are kept within active cropland (overlap prevention)."""
    out = []
    f = lambda k: fnum(mp.get(k))
    active_ha = (f('active_frac') or 0) * geo_ha
    rice_ha = (f('rice_frac') or 0) * geo_ha
    recent_ha = (f('rice_recent_frac') or 0) * geo_ha
    mz_ha = (f('mz_active_frac') or 0) * geo_ha if SEASON in ('Kharif', 'Rabi') else 0
    wc_ha = (f('wc_active_frac') or 0) * geo_ha if SEASON == 'Rabi' else 0
    per_ha = (f('per_frac') or 0) * geo_ha
    rule = (f'Sentinel-1: flooded (VH < {RICE_VH_FLOOD_DB:.0f} dB and VV < {RICE_VV_FLOOD_DB:.0f} dB) after a ≥ {RICE_VH_DROP_DB:.0f} dB drop, '
            f'searched from {RICE_FLOOD_START:%d-%b-%Y}; permanent water excluded')
    if rice_ha >= 1:
        out.append(dict(crop='Rice (paddy)', group='Cereals', model='RICE', trend='RICE', area=rice_ha,
            src='Satellite (current season)', presence=None,   # decided after the optical / Dynamic World checks
            method=rule + f'; then ≥ {RICE_VH_RISE_DB:.0f} dB VH rise ≥ 14 d later (canopy growth). Confirmed when Sentinel-2 NDVI/EVI on '
                   'the same pixels rises after flooding and Dynamic World labels them crops / flooded vegetation',
            alts='Flooded fallow, aquaculture ponds, wetlands, waterlogged dryland after heavy rain',
            ev=f'{rice_ha:,.0f} ha of current-season Sentinel-1 paddy pixels (flood → canopy growth)'))
    harv_ha = (f('rice_harv_frac') or 0) * geo_ha
    if harv_ha >= 1:
        out.append(dict(crop='Rice (paddy)', group='Cereals', model='RICE_HARVESTED', trend=None, area=harv_ha,
            src='Satellite (current season)', presence='High-confidence satellite identification',
            method=rule + f'; ≥ {RICE_VH_RISE_DB:.0f} dB VH rise (crop grew), cycle ≥ 80 d old, and max Sentinel-2 NDVI in the last 30 d '
                   f'< {RICE_HARVEST_NDVI} → HARVESTED this season (the field may now be fallow or sown to the next crop)',
            alts='Crop failure / lodging (verify)', ev=f'{harv_ha:,.0f} ha of this season\'s paddy pixels now harvested'))
    if recent_ha >= 1:
        out.append(dict(crop='Rice (paddy)', group='Cereals', model='RICE_RECENT', trend=None, area=recent_ha,
            src='Satellite (current season)', presence='Probable crop identification',
            method=rule + f'; flooded within the last {RICE_RECENT_DAYS} d, canopy growth NOT yet observed → puddled / newly transplanted '
                   'field (or rain-flooded fallow – cannot yet be excluded)',
            alts='Rain-flooded fallow, nursery beds, aquaculture', ev=f'{recent_ha:,.0f} ha of recently flooded cropland pixels (no rise yet)'))
    if mz_ha >= 1:
        out.append(dict(crop='Maize', group='Cereals', model='Maize', trend='MZ', area=mz_ha, src='Historical map (2021) + current activity',
            presence='Probable crop identification', method='ESA WorldCereal 2021 maize pixels that are green this season (NDVI ≥ 0.35)',
            alts='Sorghum, millets, other cereals sown on former maize fields', ev=f'{mz_ha:,.0f} ha of 2021 maize-map pixels active this season'))
    gl_all = [g for g in GOVT_AREA.get(r.gkey, []) if g['season'] in (SEASON, 'Annual')]
    wc_govt = sum(g['area'] for g in gl_all if sat_group(g['crop']) == 'WC')
    wc_note = ''
    wc_share = (f('wc_frac') or 0) * geo_ha / cl_ha if cl_ha else 0
    wc_spam = (SPAM_TEH.get(r.lgd) or {}).get('whea', 0) + (SPAM_TEH.get(r.lgd) or {}).get('barl', 0)
    _mix_tot = sum((SPAM_TEH.get(r.lgd) or {}).values())
    wc_why = ('Govt APY reports wheat/barley in the district' if wc_govt > 0 else
              f'{MIX_NAME} shows {wc_spam:,.0f} ha wheat/barley' if wc_spam >= max(100, 0.05 * _mix_tot) else None)
    if wc_ha >= 1 and wc_why and SEASON == 'Rabi':
        out.append(dict(crop='Wheat (winter cereal – wheat/barley not separable by map)', group='Cereals', model='Wheat', trend='WC', area=wc_ha,
            src='Historical map (2021) + current activity', presence='Probable crop identification',
            method=f'ESA WorldCereal 2021 winter-cereal pixels (not rice/maize this season) green this season; corroboration: {wc_why}. '
                   'Stage uses the wheat model (barley is similar but shorter)',
            alts='Barley, oats, mustard or other rabi crops on former wheat fields', ev=f'{wc_ha:,.0f} ha of 2021 winter-cereal pixels active this season'))
    elif wc_ha >= 1:
        wc_note = (f'; includes {wc_ha:,.0f} ha that the 2021 WorldCereal map labels winter cereals – NOT reported as wheat/barley: '
                   'no current-season or Govt evidence (old map ≠ current crop)')
    if per_ha >= 1:
        mix = mix_for(r)
        perennial_alts = sorted([(SPAM_NAMES.get(k, k), v, k) for k, v in mix.items() if k in SPAM_SEASON['Annual']], key=lambda x: -x[1])[:5]
        if perennial_alts:     # v3.5: named after the largest perennial crop of the crop mix; the satellite only proves "perennial"
            p_name, p_ha, p_code = perennial_alts[0]
            p_share = p_ha / sum(v for _, v, _ in perennial_alts)
            out.append(dict(crop=p_name, group='Perennial crops', model=SPAM_TO_MODEL.get(p_code), trend=None, area=per_ha,
                src='Satellite (12-month greenness)', presence='Probable crop identification', perennial=True,
                method=(f'Cropland green (NDVI ≥ 0.4) in ≥ 85 % of clear months over 12 months (satellite: perennial / long-duration crop); '
                        f'named {p_name} = largest perennial crop in the {MIX_NAME} crop mix ({p_share:.0%} of its perennial area) – '
                        'the satellite cannot separate sugarcane, banana, coconut or orchards'),
                alts=', '.join(f'{n} ({MIX_NAME} {v:,.0f} ha)' for n, v, _ in perennial_alts[1:]) or '—',
                ev=f'{per_ha:,.0f} ha of perennially green cropland pixels'))
        else:
            out.append(dict(crop='Perennial / long-duration crops', group='Perennial crops', model=None, trend=None, area=per_ha,
                src='Satellite (12-month greenness)', presence='Crop group identified, exact crop unknown', perennial=True,
                method='Cropland green (NDVI ≥ 0.4) in ≥ 85 % of clear months over 12 months; no crop-mix data for this tehsil',
                alts='Sugarcane, banana, coconut, orchards, fodder', ev=f'{per_ha:,.0f} ha of perennially green cropland pixels'))
    identified = sum(c['area'] for c in out)
    room = max(0.0, active_ha - identified)
    gl = gl_all
    extra = []
    if gl:
        dcl = DIST_CL.get(r.gkey) or cl_ha
        for g in gl:
            grp = sat_group(g['crop'])
            if grp in ('RICE', 'MZ', 'WC', 'SC'):
                continue
            extra.append(dict(crop=g['crop'], group='Govt-reported crop', model=model_for(g['crop']), trend=None,
                area=g['area'] * cl_ha / dcl, src='Govt district statistics (apportioned)', presence='Probable crop identification',
                method=f"DES district APY (avg {g['years']}) apportioned by cropland share; not a current-season observation",
                alts='—', ev=f"District {g['crop']} {g['area']:,.0f} ha × tehsil cropland share", darea=g['area']))
    else:
        mix = mix_for(r)
        how = mix_how(r)
        for code, a in (mix or {}).items():
            name = SPAM_NAMES.get(code, f'SPAM crop {code.upper()}')
            if code in SPAM_SEASON['Annual'] or sat_group(name) in ('RICE', 'MZ', 'WC'):
                continue
            if not (code in SPAM_SEASON['Both'] or code in SPAM_SEASON.get(SEASON, set())):
                continue
            extra.append(dict(crop=name, group='Modelled crop mix', model=SPAM_TO_MODEL.get(code), trend=None, area=a,
                src=MIX_SRC, presence='Probable crop identification',
                method=f"{MIX_NAME} modelled crop mix (from crop statistics c. 2020); season by crop type; "
                       "NOT a current-season observation", alts='Other crops of the same season',
                ev=f'{a:,.0f} ha {MIX_NAME} harvested area ({how})' + ('; all-season total' if code in SPAM_SEASON['Both'] else '')))
    tot = sum(c['area'] for c in extra)
    # v3.5 Phase 2: a crop that dominates the unexplained annual cropland uses that area's MEASURED curve ('OTH' pixels)
    if tot > 0:
        top = max(extra, key=lambda c: c['area'])
        if top['area'] / tot >= OTHER_CURVE_SHARE and top['model'] in CROP_MODELS:
            top['trend'] = 'OTH'
            top['method'] += (f'; stage evidence = measured Sentinel-2 curve of the remaining annual cropland (not paddy / maize / '
                              f'winter-cereal / perennial pixels), where {top["crop"]} is {top["area"] / tot:.0%} of the modelled seasonal area')
    for c in extra:
        if tot > room:
            c['area'] = c['area'] * room / tot if tot else 0
            c['ev'] += ' – scaled to fit unexplained active cropland'
    out += [c for c in extra if c['area'] >= 1]
    unknown = active_ha - sum(c['area'] for c in out if c['group'] != 'Perennial crops' and c['model'] != 'RICE_RECENT') - per_ha
    if unknown >= 1:
        out.append(dict(crop='Active cropland – crop unknown', group='Unidentified', model=None, trend=None, area=unknown,
            src='Satellite (current season)', presence='Active cropland, crop unknown',
            method='Cropland green this season (NDVI ≥ 0.35) not explained by identified crops' + (
                '' if (GOVT_AREA or SPAM_TEH) else ' – no crop-mix source loaded'),
            alts='Groundnut, pulses, millets, cotton, vegetables, maize …', ev=f'{unknown:,.0f} ha active cropland not attributed to any crop' + wc_note))
    if not out:
        out.append(dict(crop='Fallow – no active crop' if cur_share >= 0.2 else '—', group='—', model=None, trend=None, area=0.0,
            src='Satellite (current season)',
            presence='No active crop detected' if cur_share >= 0.2 else 'Insufficient data',
            method='No cropland pixel was green this season' if cur_share >= 0.2 else 'Too few clear observations', alts='—', ev='—'))
    for c in out:
        c.setdefault('darea', None)
    return out, dict(active=active_ha, identified=sum(c['area'] for c in out if c['group'] not in ('Unidentified', '—')))

def dmed(key, src):
    out = {}
    for lgd in TEH.lgd:
        v = fnum(src.get(lgd, {}).get(key))
        if v is not None:
            out.setdefault(DIST_OF[lgd], []).append(v)
    return {k: float(np.median(v)) for k, v in out.items()}

CL_HA = {lgd: (fnum(SAT_CUR.get(lgd, {}).get('crop_share')) or 0) * GEO_AREA_HA.get(lgd, 0) for lgd in TEH.lgd}
DIST_CL, _GK = {}, dict(zip(TEH.lgd, TEH.gkey))
for lgd, v in CL_HA.items():
    DIST_CL[_GK[lgd]] = DIST_CL.get(_GK[lgd], 0) + v
DIST_SPAM, STATE_SPAM, STATE_CL = {}, {}, {}
for lgd, v in CL_HA.items():
    STATE_CL[STATE_OF[lgd]] = STATE_CL.get(STATE_OF[lgd], 0) + v
for lgd, mix in SPAM_TEH.items():
    k = _GK.get(lgd)
    if k is not None:
        for code, a in mix.items():
            DIST_SPAM.setdefault(k, {})[code] = DIST_SPAM.get(k, {}).get(code, 0) + a
    for code, a in mix.items():
        STATE_SPAM.setdefault(STATE_OF[lgd], {})[code] = STATE_SPAM.get(STATE_OF[lgd], {}).get(code, 0) + a
_dup_names = TEH.groupby(['dkey', 'tehsil']).lgd.transform('count')
DUP_NAME = dict(zip(TEH.lgd, _dup_names > 1))

EST_SOURCE = {'calendar': 'Calendar estimate (Option A): planting date + published crop stage durations – NOT confirmed by satellite',
              'curve': 'Cropland-curve estimate (Option A): measured S2 curve phase mapped to a generic stage – no crop-specific model',
              'season': 'Season-calendar estimate (Option A): days since season start – no clear satellite curve',
              'perennial': 'Satellite: perennially green for 12 months (standing perennial crop)',
              'fallow': 'Satellite: no cropland green this season'}
GENERIC_BY_PHASE = {'pre': 'Land Prep / Sowing', 'rising': 'Vegetative', 'peak': 'Flowering', 'declining': 'Grain Fill/Maturity',
                    'post': 'Harvest / post-harvest', 'flat': 'Vegetative (steady green canopy)'}

def stage_index_at(table, dap):
    """Index of the stage whose (mid) start day is the last one ≤ days-after-planting."""
    cur = None
    for k, t in enumerate(table):
        st = t[1]
        if dap >= ((st[0] + st[1]) / 2 if isinstance(st, tuple) else st):
            cur = k
    return 0 if cur is None else cur

def calendar_estimate(c, model, table, S, s_w, dur_hi, prefix, ph, ph_all, r, lgd, s_txt=''):
    """Option A. Returns (stage text, code, kind, confidence, next stage, next window, explanation). Always returns a stage."""
    lab = ' (calendar estimate)'
    if c.get('presence') == 'No active crop detected':
        return 'Fallow – no active crop', 'FALLOW', 'fallow', 50, '— (next sowing)', None, 'no cropland pixel green this season'
    if table and S:
        dap = (TODAY - S).days
        src_txt = s_txt or f'planting ~{S:%d-%b-%Y}'
        if dap < 0:
            return ('Land Prep' + lab, f'{prefix}-S00-EST', 'calendar', 20, table[0][0], None,
                    f'{src_txt} is ahead – field preparation expected')
        if dur_hi and dap > dur_hi + 15:
            return ('Harvested / post-harvest' + lab, f'{prefix}-HARV-EST', 'calendar', 25, '— (next crop / fallow)', None,
                    f'{dap} days since {src_txt} exceeds the crop duration ({dur_hi} d)')
        k = stage_index_at(table, dap)
        nxt = table[k + 1][0] if k + 1 < len(table) else '— (crop cycle complete)'
        st = table[k + 1][1] if k + 1 < len(table) else None
        nd = ((S + dt.timedelta(days=st[0]), S + dt.timedelta(days=st[1])) if isinstance(st, tuple) else
              (S + dt.timedelta(days=st), S + dt.timedelta(days=st))) if st is not None else None
        conf = int(min(45, round(15 + 35 * s_w)))
        return (table[k][0] + lab, f'{prefix}-S{k + 1:02d}-EST', 'calendar', conf, nxt, nd,
                f'{dap} days after {src_txt} → {table[k][0]} by the published {model} stage durations; satellite evidence could not '
                'confirm it, so it is published as a calendar estimate')
    if model in CROP_MODELS:                                 # crop with a stage model but no sowing window open now
        table = CROP_MODELS[model]['stages']
        wins = [w for _, w, _ in sow_windows(model, r.state, lgd, TODAY + dt.timedelta(days=60)) if w and w[0] > TODAY]
        nxt_w = min(wins, key=lambda w: w[0]) if wins else None
        return ('Land Prep' + lab, f'{model.upper().replace(" ", "")[:6]}-S00-EST', 'calendar', 15, table[0][0], nxt_w,
                f'no {model} sowing window is open now' + (f'; next window opens {nxt_w[0]:%d-%b-%Y}' if nxt_w else ''))
    if c.get('perennial'):
        return ('Standing perennial crop (no seasonal stage)', 'PERENNIAL', 'perennial', 50, '—', None,
                'perennially green for 12 months – perennial crops have no seasonal stage sequence')
    pp = ph if (ph and ph.get('phase')) else (ph_all if ph_all and ph_all.get('phase') else None)
    if pp:
        return (GENERIC_BY_PHASE[pp['phase']] + ' (cropland curve estimate)', f"GEN-{pp['phase'].upper()}-EST", 'curve', 25, '—', None,
                f"generic stage from the measured {'crop-pixel' if pp is ph else 'tehsil cropland'} curve: {PHASES[pp['phase']]}")
    d = (TODAY - SEASON_START).days
    g = ('Sowing' if d < 25 else 'Vegetative' if d < 70 else 'Flowering' if d < 95 else 'Grain Fill/Maturity' if d < 125 else 'Harvest')
    return (g + ' (season calendar estimate)', 'GEN-SEASON-EST', 'season', 10, '—', None,
            f'{d} days since the {SEASON} season start ({SEASON_START:%d-%b}); no clear satellite curve – lowest confidence')

T1_ROWS, T8_ROWS, DOM, DOM_NOTE, AREA_ROWS = [], [], {}, {}, []
for r in TEH.itertuples():
    lgd = r.lgd
    geo_ha, cl_ha = GEO_AREA_HA.get(lgd, 0), CL_HA.get(lgd, 0)
    cur, tr, mp, wx = SAT_CUR.get(lgd, {}), SAT_TREND.get(lgd, {}), SAT_MAPS.get(lgd, {}), SAT_WX.get(lgd, {})
    g = lambda k: fnum(cur.get(k))
    share = g('valid_CUR') or 0
    hist = history_windows(lgd, SEASON)
    trend_all = [fnum(tr.get(f'NDVI_T{i:02d}')) for i in range(TREND_STEPS)]
    ph_all = curve_phase(trend_all, hist['amp'] if hist else None)

    # ---- weather (ERA5-Land) with explicit comparison periods
    rain, rain_ly, rain_n = fnum(wx.get('era_rain')), fnum(wx.get('era_rain_ly')), fnum(wx.get('era_rain_norm'))
    t30, t30n, heavy = fnum(wx.get('t30')), fnum(wx.get('t30_norm')), fnum(wx.get('heavy_rain_days'))
    d_ly, d_n = pct(rain, rain_ly), pct(rain, rain_n)
    t_anom = (t30 - t30n) if (t30 is not None and t30n is not None) else None
    rain_period = (f'{SEASON_START} to {_era_last} | ERA5-Land daily (11.1 km reanalysis), mm; normal = 1991-2020 mean of the same '
                   f'calendar window' if _era_last else NA)
    wsig = ('; '.join(x for x in [
        f'Season rain {rain:.0f} mm' if rain is not None else 'Season rain not available',
        f'vs same period last year {d_ly:+.0f}%' if d_ly is not None else None,
        f'vs 1991-2020 normal {d_n:+.0f}%' if d_n is not None else None,
        f'heavy-rain days (≥64.5 mm, IMD) {heavy:.0f}' if heavy is not None else None,
        f'air temp (30 d) {t30:.1f} °C, anomaly {t_anom:+.1f} °C vs 1991-2020' if t_anom is not None else None] if x))
    nd_vals, nd_src = SERIES['NDVI'][lgd]
    ndvi_yoy = pct(nd_vals[-1], nd_vals[-13]) if nd_src[-1] == SAT and nd_src[-13] == SAT else None

    crops, area_bal = crops_v3(r, geo_ha, cl_ha, mp, share)
    over = area_bal['identified'] - area_bal['active']
    nonveg = fnum(mp.get('noncrop_veg_frac')); water = fnum(mp.get('water_frac'))
    area_text = (f"Tehsil {geo_ha:,.0f} ha | cropland {cl_ha:,.0f} | active this season {area_bal['active']:,.0f} | "
                 f"identified crops {area_bal['identified']:,.0f} | unidentified active {max(0, area_bal['active'] - area_bal['identified']):,.0f} | "
                 f"non-crop vegetation {((nonveg or 0) * geo_ha):,.0f} | water/wetland {((water or 0) * geo_ha):,.0f}")
    AREA_ROWS.append(dict(State=r.state, District=r.district, Tehsil=r.tehsil, **{'Tehsil LGD Code': lgd},
        **{'Total geographic area ha': geo_ha, 'Cropland ha (WorldCover)': round(cl_ha, 1), 'Active cultivated ha': round(area_bal['active'], 1),
           'Crop-specific identified ha': round(area_bal['identified'], 1),
           'Unidentified active cropland ha': round(max(0, area_bal['active'] - area_bal['identified']), 1),
           'Non-crop vegetation ha': round((nonveg or 0) * geo_ha, 1), 'Water / wetland ha': round((water or 0) * geo_ha, 1),
           'Overlap check': 'OK' if over <= 1 else f'Identified exceeds active by {over:,.0f} ha (perennial overlap)'}))

    for ci, c in enumerate(crops):
        qc, unc_notes = [], []
        field = next((fo for fo in FIELD.get(lgd, []) if str(fo.get('crop', '')).lower() in c['crop'].lower()), None)
        crop_trend = ([fnum(tr.get(f"NDVI{c['trend']}_T{i:02d}")) for i in range(TREND_STEPS)] if c['trend'] else None)
        ph = curve_phase(crop_trend, hist['amp'] if hist else None) if crop_trend else None
        dq = min(1, (ph['n'] if ph else 0) / TREND_STEPS) * min(1, share / 0.5)

        model = c['model']
        vh_trend = ([fnum(tr.get(f'VHRICE_T{i:02d}')) for i in range(TREND_STEPS)] if model == 'RICE' else None)
        vh = curve_phase(vh_trend, None, name='S1 VH', min_amp=2.0, low_level=-20.0, unit=' dB') if vh_trend else None
        ev_trend = ([fnum(tr.get(f"EVI{c['trend']}_T{i:02d}")) for i in range(TREND_STEPS)] if c['trend'] else None)
        ph_evi = curve_phase(ev_trend, None, name='EVI', min_amp=0.10) if ev_trend else None
        if ph and ph_evi and ph['phase'] and ph_evi['phase'] and ph['phase'] != ph_evi['phase']:
            unc_notes.append(f"NDVI and EVI curves disagree ({PHASES[ph['phase']]} vs {PHASES[ph_evi['phase']]}) – stage confidence reduced")

        # ---- planting / transplanting date (never the raw green-up)
        S = e1 = e2 = None; s_method, s_w = NA, 0.0
        table = None; cohort_txt = ''
        if field and field.get('planting_date') and str(field['planting_date']) != 'nan':
            S = e1 = e2 = pd.to_datetime(field['planting_date']).date(); s_method, s_w = 'Field-recorded planting date', 1.0
        elif model == 'RICE':
            rf = fnum(mp.get('rice_frac')) or 0
            coh = [(k, (fnum(mp.get(f'rice_coh_{k:02d}')) or 0) / rf if rf else 0) for k in range(len(RICE_BINS))]
            coh = [(k, sh_) for k, sh_ in coh if sh_ > 0]
            G = ph['cx'].get('c10') if (ph and ph.get('cx')) else None      # sustained optical green-up of the paddy pixels
            bin_a = lambda k: S1_START + dt.timedelta(days=RICE_BINS[k][0])
            bin_b = lambda k: S1_START + dt.timedelta(days=RICE_BINS[k][1] - 1)
            if coh:
                cohort_txt = 'Flooding-episode cohorts (share of paddy pixels): ' + ', '.join(
                    f'{bin_a(k):%d-%b}–{bin_b(k):%d-%b} {sh_:.0%}' for k, sh_ in coh if sh_ >= 0.05)
            # v3.4 fix 1: a radar flooding date is accepted only if the optical green-up follows it within 0-45 d
            bin_m = lambda k: bin_a(k) + (bin_b(k) - bin_a(k)) / 2
            # green-up is read from 14-day composites, so it can appear up to ~14 d before the true transplanting date
            use = [(k, sh_) for k, sh_ in coh if -14 <= (G - bin_m(k)).days <= 45] if G else coh
            tot = sum(sh_ for _, sh_ in use)
            if G and coh and tot < sum(sh_ for _, sh_ in coh):
                unc_notes.append(f'{1 - tot / sum(sh_ for _, sh_ in coh):.0%} of paddy pixels have flood episodes > 45 d before the optical '
                                 f'green-up ({G:%d-%b}) – fields kept flooded before transplanting or an earlier crop; those dates ignored')
            if use and tot >= 0.3 * sum(sh_ for _, sh_ in coh):
                k1, sh1 = max(use, key=lambda x: x[1])
                k2 = max((x for x in use if abs(x[0] - k1) == 1), key=lambda x: x[1], default=(k1, 0))
                ks = sorted({k1, k2[0]}) if sh1 / tot < 0.5 and (sh1 + k2[1]) / tot >= 0.5 else [k1]
                main_share = sum(sh_ for k, sh_ in use if k in ks) / tot
                if main_share >= 0.5:
                    a, b = bin_a(ks[0]), bin_b(ks[-1])
                    e1, e2 = a - dt.timedelta(days=6), b + dt.timedelta(days=ASSUMPTION_PUDDLE_TO_TRANSPLANT[1])
                    S = a + (b - a) / 2
                    s_method = (f'Sentinel-1: start of the latest flooding episode (puddling) of the main cohort ({main_share:.0%} of '
                                f'paddy pixels whose flooding is followed by optical green-up within 45 d); transplanting 0-'
                                f'{ASSUMPTION_PUDDLE_TO_TRANSPLANT[1]} d later (ASSUMPTION); ±6 d S1 revisit')
                    s_w = 0.8 if main_share >= 0.7 else 0.6
            if S is None and G:
                e1, e2 = G - dt.timedelta(days=ASSUMPTION_RICE_GREENUP_LAG[1]), G - dt.timedelta(days=ASSUMPTION_RICE_GREENUP_LAG[0])
                S = e1 + (e2 - e1) / 2
                s_method = (f'Optical green-up of the radar-confirmed paddy pixels ({G:%d-%b}) minus transplanting-to-green-up lag '
                            f'{ASSUMPTION_RICE_GREENUP_LAG[0]}-{ASSUMPTION_RICE_GREENUP_LAG[1]} d (ASSUMPTION); radar flooding dates were '
                            'spread or earlier than the crop cycle')
                s_w = 0.5
            if S is None and coh:
                unc_notes.append('transplanting dates spread over several cohorts and no sustained optical green-up – one stage cannot '
                                 'represent the tehsil')
        elif model == 'RICE_HARVESTED' and fnum(mp.get('rice_harv_t')) is not None:
            S = S1_START + dt.timedelta(days=int(fnum(mp['rice_harv_t'])))
            e1, e2 = S - dt.timedelta(days=6), S + dt.timedelta(days=ASSUMPTION_PUDDLE_TO_TRANSPLANT[1])
            s_method, s_w = 'Sentinel-1: start of the flooding episode of this season\'s (now harvested) paddy crop', 0.6
        elif model == 'RICE_RECENT' and fnum(mp.get('rice_recent_t')) is not None:
            S = S1_START + dt.timedelta(days=int(fnum(mp['rice_recent_t'])))
            e1, e2 = S - dt.timedelta(days=6), S + dt.timedelta(days=ASSUMPTION_PUDDLE_TO_TRANSPLANT[1])
            s_method, s_w = 'Sentinel-1: start of recent flooding episode (puddling); transplanting 0-7 d later (ASSUMPTION)', 0.5
        elif model in CROP_MODELS and ph and ph['cx'] and ph['cx'].get('c10') and not (
                sow_windows(model, r.state, lgd, TODAY) and ph['cx']['c10'] < min(w[0] for _, w, _ in sow_windows(model, r.state, lgd, TODAY) if w)
                - dt.timedelta(days=30) - dt.timedelta(days=ASSUMPTION_GREENUP_LAG[0])):
            G = ph['cx']['c10']             # (a green-up before this crop's sowing window belongs to the previous crop and is ignored)
            e1, e2 = G - dt.timedelta(days=ASSUMPTION_GREENUP_LAG[1]), G - dt.timedelta(days=ASSUMPTION_GREENUP_LAG[0])
            S = e1 + (e2 - e1) / 2
            s_method, s_w = (f'Crop-pixel green-up {G:%d-%b} (NDVI 10 % of cycle) minus planting-to-green-up lag {ASSUMPTION_GREENUP_LAG[0]}-'
                             f'{ASSUMPTION_GREENUP_LAG[1]} d (ASSUMPTION – not a measured sowing date)'), 0.5
        elif model in CROP_MODELS:
            wins = [(nm, w) for nm, w, _ in sow_windows(model, r.state, lgd, TODAY)
                    if w and w[0] <= TODAY and (TODAY - w[0]).days <= CROP_MODELS[model]['harvest'][1]]
            if wins:
                nm, (e1, e2) = max(wins, key=lambda x: x[1][0])
                S = e1 + (e2 - e1) / 2
                s_method, s_w = f'Calendar window only ({nm}); no crop-specific observation – NOT a measured date', 0.2
        unc_days = (e2 - e1).days if (e1 and e2) else None
        sow_conf = round(100 * s_w * (1 - min(1, (unc_days or 0) / 90))) if S else ''
        # rice: transplanting must precede optical green-up by 0-45 d, else the radar date is not trusted for staging
        greenup = ph['cx'].get('c10') if (ph and ph.get('cx')) else None
        rice_conflict = bool(model == 'RICE' and S and greenup and not (-14 <= (greenup - S).days <= 45))
        if rice_conflict:
            unc_notes.append(f'S1 transplanting ~{S:%d-%b} vs S2 green-up {greenup:%d-%b}: {(greenup - S).days:+d} d (expected -14 to +45) – '
                             'dates inconsistent, stage not assigned')

        # ---- presence label (current-season evidence only; an old map never gives "High-confidence")
        if model == 'RICE':
            s1n = fnum(mp.get('rice_s1_obs')) or 0
            dwf = (fnum(mp.get('rice_dw_frac')) or 0) / (fnum(mp.get('rice_frac')) or 1)
            s2_ok = bool(ph and ph['cx'] and ph['phase'] in ('rising', 'peak', 'declining', 'post') and not rice_conflict)
            c['presence'] = ('High-confidence satellite identification' if (s2_ok and s1n >= 6 and dwf >= 0.5)
                             else 'Probable crop identification')
            if not s2_ok:
                unc_notes.append('paddy flooding + radar canopy growth detected, but optical crop development not confirmed')
            if dwf < 0.5:
                unc_notes.append(f'Dynamic World labels only {dwf:.0%} of these pixels as crops / flooded vegetation (last 60 d)')
        if field:
            c['presence'] = 'Confirmed – field verified'
        w_id = {'Confirmed – field verified': 1.0, 'High-confidence satellite identification': 0.85, 'Probable crop identification':
                (0.5 if 'Historical' in c['src'] else 0.55 if 'Satellite' in c['src'] else 0.3), 'Crop group identified, exact crop unknown': 0.35,
                'Active cropland, crop unknown': 0.1, 'No active crop detected': 0.5, 'Insufficient data': 0.0}[c['presence']]
        id_conf = round(100 * w_id * ((0.5 + 0.5 * dq) if 'Satellite' in c['src'] or 'Historical' in c['src'] else 1))

        # ---- crop-specific stage (each crop uses ONLY its own stage table; tehsil-wide curves are never used as a crop stage)
        season_lbl, cal_cands, est_stage, final, f_stage, stage_note = f'{SEASON} {SEASON_START.year}-{(SEASON_START.year + 1) % 100:02d}', [], NA, UNC, 0.0, ''
        harvest_lo = harvest_hi = mat_lo = mat_hi = None; gdd = gdd_last = None; prev_s = next_s = ''; next_date = None; code = 'NOT-DETERMINED'
        rice_rc = []; prefix = ''; stage_win = None; dat_lo = dat_hi = None
        if model in ('RICE', 'RICE_RECENT', 'RICE_HARVESTED') and S:
            season_name, rc = rice_context(S, r.state, lgd)
            rice_rc = rc if season_name else []
            if season_name:
                season_lbl += f' · calendar: {season_name}'
            else:
                qc.append('QC6: transplanting date outside all rice calendar windows (TNAU / GGCMI) – duration class unknown, wide range used')
            F_lo, F_hi = rice_field_duration(rc)
            dat_lo, dat_hi = (TODAY - e2).days, (TODAY - e1).days
            t_lo, t_hi = rice_stage_table(F_lo), rice_stage_table(F_hi)
            table = [(n, (a, b), bk, al) for (n, a, bk, al), (_, b, _, _) in zip(t_lo, t_hi)]
            cal_cands = stages_at(table, dat_lo, dat_hi)
            harvest_lo, harvest_hi = e1 + dt.timedelta(days=F_lo), e2 + dt.timedelta(days=F_hi)
            mat_lo, mat_hi = harvest_lo, harvest_hi              # rice: harvest-ready at physiological maturity (H + 30 d)
            gdd, gdd_last = gdd_since(wx, S, RICE_MODEL['tbase'])
            prefix = 'RICE'
        elif model in CROP_MODELS and S:
            m = CROP_MODELS[model]
            table = m['stages']
            dat_lo, dat_hi = (TODAY - e2).days, (TODAY - e1).days
            cal_cands = stages_at(table, dat_lo, dat_hi)
            harvest_lo, harvest_hi = e1 + dt.timedelta(days=m['harvest'][0]), e2 + dt.timedelta(days=m['harvest'][1])
            mi = next((k for k, t in enumerate(table) if 'maturity' in t[0].lower()), None)
            if mi is not None:
                mat_lo, mat_hi = e1 + dt.timedelta(days=table[mi][1][0]), e2 + dt.timedelta(days=table[mi][1][1])
            gdd, gdd_last = gdd_since(wx, S, m['tbase'])
            prefix = model.upper().replace(' ', '')[:6]
        if cal_cands:
            est_stage = (' / '.join(cn[0] for cn in cal_cands) + f' (calendar only, {max(0, dat_lo)}-{dat_hi} days after '
                         + ('transplanting' if prefix == 'RICE' else 'sowing') + (', sowing window still open' if dat_lo < 0 else '') + ')')
        # gates – evidence that is too weak or inconsistent never produces a stage
        if table and S and S > TODAY:
            stage_note = 'planting date in the future – rejected'
        elif model in CROP_MODELS and S and s_w < 0.5:
            stage_note = ('sowing date from the crop calendar only (no crop-specific green-up observed yet) – a stage from calendar '
                          'dates alone would be a guess')
        elif model == 'RICE_HARVESTED':
            final, f_stage = 'Harvested (this season)', 0.8
            stage_note = (f'paddy cycle detected by Sentinel-1 (flood → canopy) {dat_lo}-{dat_hi} d ago and Sentinel-2 NDVI now < '
                          f'{RICE_HARVEST_NDVI} → harvested; harvest is NOT inferred from an NDVI fall alone')
        elif table and dat_hi is not None and dat_hi > (F_hi if prefix == 'RICE' else CROP_MODELS.get(model, {}).get('harvest', (0, 400))[1]) + 45:
            stage_note = 'days since planting exceed the crop duration by > 45 d – planting date implausible for a standing crop'
        elif rice_conflict:
            stage_note = 'radar transplanting date and optical green-up disagree'
        elif model in CROP_MODELS and S and 0.5 <= s_w < 1 and sow_windows(model, r.state, lgd, S) and not any(
                w and w[0] - dt.timedelta(days=30) <= S <= w[1] + dt.timedelta(days=30) for _, w, _ in sow_windows(model, r.state, lgd, S)):
            stage_note = f'estimated sowing {S:%d-%b} is outside every {model} calendar window (TNAU/GGCMI, ±30 d) – crop identity / date doubtful'
            qc.append('QC6: planting date outside crop calendar windows (±30 d) – stage withheld')
        elif model == 'RICE_RECENT' and cal_cands:
            early = [cn for cn in cal_cands if cn[0] in ('Puddling and transplanting', 'Establishment (recovery)', 'Tillering')]
            if early and len(early) <= 2:
                final = early[0][0] if len(early) == 1 else f'{early[0][0]} → {early[1][0]} (transition)'
                f_stage, stage_note = 0.6, 'standing water still seen by Sentinel-1 and no canopy rise yet – consistent with early stages only'
            else:
                stage_note = 'recent flooding but calendar stage not early – rain-flooded field? not resolved'
        elif table and (ph or vh):
            final, f_stage, stage_note = decide_stage(cal_cands, ph, vh, RICE_VH_ALLOWED if prefix == 'RICE' else None)
        elif table and not crop_trend:
            stage_note = 'no crop-specific satellite pixels; the tehsil cropland curve is NOT used as a crop-specific stage'
        elif model == 'RICE' and not S:
            stage_note = 'paddy present but transplanting dates are spread over several cohorts – one tehsil stage would be misleading'
        elif model in CROP_MODELS and not S:
            stage_note = 'no sowing date: crop-specific green-up not yet observed and no calendar window for this tehsil'
        elif not table:
            stage_note = ('crop not resolved to a species with a sourced stage model' if c['group'] != 'Unidentified'
                          else 'crop unknown') + ' – stage not estimated'
        if c.get('trend') == 'OTH' and final != UNC:
            f_stage = min(f_stage, 0.6)
            unc_notes.append('stage read from the curve of all remaining annual cropland, where this crop dominates the modelled mix – '
                             'conditional on the crop identity')
        if model in ('Maize', 'Wheat') and final != UNC and 'Historical' in c['src']:
            f_stage = min(f_stage, 0.6)
            unc_notes.append(f'{model.lower()} identity is from a 2021 map + current activity (not verified this season) – stage is conditional on the crop')
        if field and field.get('stage') and str(field['stage']) != 'nan':
            final, f_stage, stage_note = str(field['stage']), 1.0, f"field-observed on {field.get('observation_date', '')}"
        names_t = [t[0] for t in table] if table else []
        cur_names = [x for x in final.replace(' (transition)', '').split(' → ')] if final != UNC else []
        if table and cur_names and all(x in names_t for x in cur_names):
            i0, i1 = names_t.index(cur_names[0]), names_t.index(cur_names[-1])
            code = f'{prefix}-S{i0 + 1:02d}' + (f'/S{i1 + 1:02d}' if i1 != i0 else '')
            prev_s = table[i0 - 1][0] if i0 > 0 else '—'
            lo_ = lambda k: (table[k][1][0] if isinstance(table[k][1], tuple) else table[k][1])
            hi_ = lambda k: (table[k][1][1] if isinstance(table[k][1], tuple) else table[k][1])
            st_a = e1 + dt.timedelta(days=lo_(i0))
            st_b = (e2 + dt.timedelta(days=hi_(i1 + 1) - 1)) if i1 + 1 < len(table) else harvest_hi
            stage_win = (st_a, st_b)
            if i1 + 1 < len(table):
                next_s = table[i1 + 1][0]
                next_date = (e1 + dt.timedelta(days=lo_(i1 + 1)), e2 + dt.timedelta(days=hi_(i1 + 1)))
            else:
                next_s = '— (crop cycle complete)'
        ev_q = max((ph['q'] if ph else 0), (vh['q'] if vh else 0), {'RICE_RECENT': 0.6, 'RICE_HARVESTED': 0.75}.get(model, 0))
        if model == 'RICE_HARVESTED' and final != UNC:
            code, prev_s, next_s = 'RICE-HARVESTED', 'Physiological maturity – harvest-ready', '— (next crop / fallow)'
        date_q = 1 - min(0.5, (unc_days or 0) / 60)              # wider planting-date range → lower stage confidence
        stage_conf = round(min(90, 100 * f_stage * ev_q * date_q * (min(1, share / 0.5) if ph else 1))) if final != UNC else 0
        if field and field.get('stage') and str(field['stage']) != 'nan':
            stage_conf = 100
        if final != UNC and est_stage != NA:
            est_stage += (f' | current stage window {stage_win[0]:%d-%b} – {stage_win[1]:%d-%b-%Y}' if stage_win else '')

        # ---- harvest (never inferred from an NDVI fall alone)
        if harvest_lo:
            days_left = (harvest_lo - TODAY).days
            if ph and ph['phase'] in ('declining', 'post') and TODAY >= harvest_lo - dt.timedelta(days=10):
                readiness = 'Harvest-ready / harvesting (calendar duration + measured senescence agree)'
            elif ph and ph['phase'] == 'post' and TODAY < harvest_lo - dt.timedelta(days=10):
                readiness = 'Early decline – possible early harvest or crop failure; verify in field'
            elif days_left <= 15:
                readiness = 'Approaching harvest window'
            else:
                readiness = 'Not ready'
        else:
            readiness, days_left = NA, None
        harv_conf = round((sow_conf or 0) * (0.8 if final != UNC else 0.4)) if harvest_lo else ''
        if harvest_lo:
            h_txt = ((f'Maturity {wtxt(mat_lo, mat_hi)} | ' if mat_lo and prefix != 'RICE' else '')
                     + f'Harvest {wtxt(harvest_lo, harvest_hi)} ({readiness})'
                     + (' – rice harvest-ready at physiological maturity' if prefix == 'RICE' else '')
                     + f' | harvest-date confidence {harv_conf} (provisional score)')
        else:
            h_txt = NA
        if model == 'RICE_HARVESTED':
            h_txt = 'Already harvested (S2 NDVI < %.2f in the last 30 d after a full paddy cycle)' % RICE_HARVEST_NDVI
            harv_conf = ''

        # ---- health / stress (indicative only – no disease or nutrient diagnosis)
        if ph and 'POSSIBLE STRESS' in stage_note:
            health = 'Possible stress – vegetation declining before expected maturity; requires field inspection'
        elif ndvi_yoy is None:
            health = 'Not assessed (no clear same-month observation last year)'
        else:
            health = ('Vegetation similar to last year' if ndvi_yoy >= -5 else 'Below last year – watch' if ndvi_yoy >= -15
                      else 'Vegetation decline vs last year – possible stress, requires field inspection') + f' (tehsil cropland NDVI {ndvi_yoy:+.0f}% YoY)'
        if d_n is not None and d_n <= -25:
            health += '; rainfall well below normal – possible water stress (verify)'
        if d_n is not None and d_n >= 50 and (g('NDWI_CUR') or -1) > 0:
            health += '; rainfall far above normal with surface water signal – possible excess-water stress (verify)'
        health_conf = round(100 * 0.7 * dq) if ndvi_yoy is not None else 0
        wx_conf = 80 if rain is not None else 0

        sat_parts = [f"S2 NDVI: {PHASES[ph['phase']]}" if ph and ph['phase'] else None,
                     f"S2 EVI: {PHASES[ph_evi['phase']]}" if ph_evi and ph_evi['phase'] else None,
                     f"S1 VH: {PHASES[vh['phase']]}" if vh and vh['phase'] else None]
        sat_stage = ((' | '.join(x for x in sat_parts if x) + ' – crop-specific pixels (curve phase, not a crop stage)') if any(sat_parts) else
                     ('Insufficient recent clear observations on crop pixels' if (ph or vh) else
                      ('Sentinel-1 flooding (puddling) observed, no canopy yet' if model == 'RICE_RECENT' else
                       f"No crop-specific pixels (tehsil cropland: {PHASES.get(ph_all['phase'], 'n/a')} – context only)")))
        tb = RICE_MODEL['tbase'] if prefix == 'RICE' else CROP_MODELS.get(model, {}).get('tbase')
        reason = ' | '.join(x for x in [
            (f'Days after {"transplanting" if prefix == "RICE" else "sowing"}: {max(0, dat_lo)}-{dat_hi}') if dat_lo is not None else None,
            (f'Stage window (expected): {stage_win[0]:%d-%b} to {stage_win[1]:%d-%b-%Y}') if stage_win else None,
            f'Previous: {prev_s}' if prev_s else None,
            (f'Next: {next_s}' + (f' expected {wtxt(*next_date)}' if next_date else '')) if next_s else None,
            f"Optical: {ph['reason']}" if ph else None,
            f"EVI: {ph_evi['reason']}" if ph_evi else None,
            f"Radar: {vh['reason']}" if vh else None,
            cohort_txt or None,
            (f'GDD since planting {gdd} °C·d to {gdd_last} (Tb {tb} °C; supporting evidence only – no validated GDD stage thresholds '
             'for local varieties)') if gdd is not None else None,
            f'Decision: {stage_note}' if stage_note else None, '; '.join(unc_notes) or None] if x)

        # ---- QC checks – hard failures REJECT the stage (publish gate), soft ones are flagged
        reject = []
        if 'Sugarcane' in c['crop'] and any(k in final for k in ('Grain', 'Milk', 'Dough', 'Heading', 'Silking', 'Tassel')):
            reject.append('QC1: cereal stage on sugarcane')
        if not crop_trend and final != UNC and not field and model not in ('RICE_RECENT', 'RICE_HARVESTED'):
            reject.append('QC2: stage without crop-specific pixels')
        if 'Historical' in c['src'] and c['presence'] in ('High-confidence satellite identification', 'Confirmed – field verified') and not field:
            qc.append('QC3: historical map cannot confirm current presence'); c['presence'] = 'Probable crop identification'
        if over > 1:
            qc.append(f'QC5: identified crop area exceeds active cropland by {over:,.0f} ha')
        if final != UNC and table and stage_win and cur_names:
            bi = STAGE_BUCKETS.index(table[names_t.index(cur_names[0])][2])
            if bi <= STAGE_BUCKETS.index('Vegetative') and days_left is not None and days_left < 15 and model != 'RICE_HARVESTED':
                reject.append('QC7: harvest due within 15 d but stage is vegetative')
        if ph and ph['n'] < 6:
            qc.append(f"QC8: only {ph['n']}/{TREND_STEPS} clear crop-pixel composites – reliability reduced")
        if share < 0.3:
            qc.append(f'QC9: clear-sky cropland share {share:.0%} in last 30 d (cloud/shadow masked {1 - share:.0%})')
        if 'POSSIBLE STRESS' in stage_note:
            qc.append('QC10: decline flagged as possible stress, not harvest')
        if rain is not None and rain_n is None:
            qc.append('QC11: long-term normal unavailable – only year-on-year rainfall comparison')
        if final != UNC and not field and table and model != 'RICE_HARVESTED' and not all(x in names_t for x in cur_names):
            reject.append('QC13: stage not in this crop\'s own stage sequence')
        if fnum(mp.get('active_frac')) is None:
            qc.append('QC14: active-cropland fraction missing – areas not computed (not zero)')
        s2_last = fnum(cur.get('s2_last_t')); s1_last = fnum(cur.get('s1_last_t'))
        s2_last_d = (TODAY - dt.timedelta(days=30) + dt.timedelta(days=int(s2_last))) if s2_last is not None else None
        s1_last_d = (TODAY - dt.timedelta(days=30) + dt.timedelta(days=int(s1_last))) if s1_last is not None else None
        if s2_last_d is None or (TODAY - s2_last_d).days > 20:
            qc.append('QC15: no clear Sentinel-2 scene in the last 20 days (mean over cropland) – optical evidence stale')
        if c['src'].startswith('Historical') and not field:
            qc.append('QC16: crop identity from a 2021 map + current greenness – unverified this season')
        if reject and not field:
            qc += [x + ' – stage rejected' for x in reject]
            stage_note = (stage_note + '; ' if stage_note else '') + 'rejected by ' + ', '.join(x.split(':')[0] for x in reject)
            final, code, stage_conf, prev_s, next_s, next_date, stage_win = UNC, 'NOT-DETERMINED', 0, '', '', None, None
            reason += ' | Publish gate: ' + '; '.join(reject) + ' → Stage not determined'
            est_stage = est_stage.split(' | current stage window')[0]
        # ---- v3.5 Option A: never leave the stage empty – publish the crop-calendar stage, clearly labelled, with a low confidence
        est_kind = None
        if CALENDAR_ESTIMATE and final == UNC and not field:
            S_est, s_est_txt = S, ''
            if model in ('RICE', 'RICE_RECENT') and not table:      # paddy without one transplanting date: use the mean flood date
                ft_mean = fnum(mp.get('rice_flood_t' if model == 'RICE' else 'rice_recent_t'))
                if ft_mean is not None:
                    S_est = S1_START + dt.timedelta(days=int(ft_mean))
                    s_est_txt = f'mean Sentinel-1 flooding (puddling) date of the paddy pixels ~{S_est:%d-%b-%Y}'
                    _sn, rc_ = rice_context(S_est, r.state, lgd)
                    F_lo, F_hi = rice_field_duration(rc_)
                    t_lo, t_hi = rice_stage_table(F_lo), rice_stage_table(F_hi)
                    table = [(n, (a_, b_), bk, al) for (n, a_, bk, al), (_, b_, _, _) in zip(t_lo, t_hi)]
                    prefix = 'RICE'
            dur_hi = ((F_hi if prefix == 'RICE' else CROP_MODELS.get(model, {}).get('harvest', (0, 0))[1]) if table else None)
            final, code, est_kind, stage_conf, next_s, next_date, est_why = calendar_estimate(
                c, model, table, S_est, s_w if S else 0.3, dur_hi, prefix, ph, ph_all, r, lgd, s_est_txt)
            names_t = [t[0] for t in table] if table else names_t
            reason += f' | Option A – {est_why}'
            if est_kind == 'calendar':
                qc.append('QC17: stage is a CALENDAR ESTIMATE (not confirmed by satellite evidence) – low confidence')
            elif est_kind == 'curve':
                qc.append('QC17: stage is a generic CROPLAND-CURVE ESTIMATE (no crop-specific stage model) – low confidence')
        dq_status = 'Good' if (share >= 0.5 and ph and ph['n'] >= 8) else ('Fair' if share >= 0.2 else 'Poor')
        qc.insert(0, f'Data quality: {dq_status}')

        area_conf = {'Satellite (current season)': 75, 'Historical map (2021) + current activity': 45, 'Satellite (12-month greenness)': 55,
                     'Modelled (IFPRI SPAM 2020)': 25, 'Modelled (CROPGRIDS 2020)': 25,
                     'Govt district statistics (apportioned)': 40}.get(c['src'], 30)
        confs = [x for x in (id_conf, stage_conf, sow_conf, area_conf) if isinstance(x, (int, float))]
        overall = round(np.mean(confs)) if confs else ''
        evid = (f'CropID {id_conf} | Stage {stage_conf} | Sowing {sow_conf or 0} | Harvest {harv_conf or 0} | Area {area_conf} | '
                f'Health {health_conf} | Weather {wx_conf} — {PROVISIONAL}. Overall = mean(CropID, Stage, Sowing, Area).')
        refs = ['LGD', 'WORLDCOVER', 'S2', 'S1', 'ERA5'] + (['S1_RICE', 'TNAU_RICE_SEASONS', 'TNAU_RICE_NURSERY', 'RICE_PHASES', 'RICE_MILK_DOUGH']
               if model == 'RICE' else [CROP_MODELS[model]['ref'], 'TN_SEASONS_OTHER'] if model in CROP_MODELS else []) + \
               (['WORLDCEREAL'] if 'Historical' in c['src'] else []) + ([MIX_REF] if c['src'] == MIX_SRC or c.get('perennial') else []) + \
               (['GDD_BASE'] if gdd is not None else []) + (['IMD_HEAVY'] if heavy is not None else [])
        source = ' || '.join(REFS[k] for k in dict.fromkeys(refs))
        insight = (f"{c['crop']}: {final if final != UNC else 'stage uncertain'}"
                   + (f"; planted ~{S:%d-%b} ({s_method.split(' (')[0].split(';')[0]})" if S else '')
                   + (f"; harvest {wtxt(harvest_lo, harvest_hi)}" if harvest_lo else '')
                   + f"; {health.split(';')[0]}"
                   + ('. Field verification recommended: ' + ('confirm crop identity' if c['presence'] not in
                      ('High-confidence satellite identification', 'Confirmed – field verified') else 'confirm stage')
                      if not field else ''))
        geo_val = '; '.join(x for x in [
            'LGD codes present' if (r.slgd and r.dlgd and lgd) else 'Missing LGD code',
            'duplicate tehsil name in district (disambiguated by LGD code)' if DUP_NAME.get(lgd) else None,
            f'area {geo_ha:,.0f} ha (EPSG:6933 equal-area, full-resolution LGD polygon)'] if x)
        s2s, s1s = g('s2_scenes'), g('s1_scenes')
        sow_ev = (f'{s_method}; uncertainty ±{(unc_days or 0) // 2} d' if S else 'Not estimable – no crop-specific signal or calendar window')

        T1_ROWS.append([r.state, r.slgd, r.district, r.dlgd, r.tehsil, lgd, BOUNDARY_UNIT,
            c['crop'], season_lbl, c['presence'],
            f"Group: {c['group']} | {c['method']} | Observation period: {TREND_ENDS[0] - dt.timedelta(days=14)} to {TODAY} | "
            f"Alternatives: {c['alts']} | Ground verification: {'Yes' if field else 'No'} | Presence score {id_conf}/100 is a provisional "
            f"rubric score, NOT a probability", id_conf,
            round(c['area'], 1), round(cl_ha, 1), (round(c['darea'], 1) if c['darea'] else ''), f"{c['ev']} [{c['src']}] || {area_text}",
            ds(S), ds(e1), ds(e2), sow_conf, sow_ev,
            sat_stage, est_stage, final, code, (EST_SOURCE[est_kind] if est_kind else 'Field observation' if field and f_stage == 1.0 else
                ('Sentinel-1 transplanting date + TNAU/IRRI rice stage durations + S2 NDVI/EVI and S1 VH curves of paddy pixels'
                 if prefix == 'RICE' and model == 'RICE' else
                 'Sentinel-1 recent flooding + TNAU rice calendar' if model == 'RICE_RECENT' else
                 'Crop-pixel S2 NDVI/EVI curves + published crop-specific stage durations')
                if (table and final != UNC) else ('Calendar only – NOT confirmed, not used as the stage' if table else NA)),
            ds(s2_last_d) if s2_last_d else (ds(ph['obs']) if ph and ph['obs'] else ''), stage_conf, reason, h_txt,
            health, wsig or NA, (f"Crop pixels: {ph['reason']}" if ph else f"Tehsil cropland (context only): {ph_all['reason']}"), insight,
            r4(g('NDVI_CUR')), r4(g('EVI_CUR')), r4(g('NDRE_CUR')), r4(g('NDMI_CUR')), r4(g('NDWI_CUR')),
            r4(g('s1_VV')), r4(g('s1_VH')), r4(g('VV_VH')),
            ' | '.join('' if v is None else f'{v:.2f}' for v in (crop_trend if crop_trend else trend_all)) +
            (' [crop pixels]' if crop_trend else ' [tehsil cropland]'),
            pr_(rain), rain_period, pr_(t30),
            round(share, 3), (f"{(s2s or 0):.0f} / {(s1s or 0):.0f} in last 30 d | last clear S2 ≈ {ds(s2_last_d) or 'none'} | "
                              f"last S1 ≈ {ds(s1_last_d) or 'none'} | latest optical composite {TREND_ENDS[-1] - dt.timedelta(days=14)} to "
                              f"{TREND_ENDS[-1]}"), 'Field verified' if field else 'Not field-verified',
            geo_val, evid, '; '.join(qc), overall, MODEL_VERSION, source, ds(TODAY), RUN_ID])

        # ---- Tab 8: ideal windows from TNAU calendar + published stage durations (fallback: tehsil satellite history)
        ideal_src, ideal_conf, w_sow, dur_txt, ideal_h, nursery_mid = NA, '', None, '', None, 0
        bucket_cols = []
        if prefix == 'RICE' and S:
            seasons = [s for s in RICE_SEASONS_TN if (f'TNAU {s[0]}', s[2]) in set(rice_rc)]
            ggw = [w for w in gg_windows(lgd, 'RICE', S) if any(k == f"gg_{w['dur']}" for _, k in rice_rc)]
            if ggw and not seasons:
                w_tr = (min(w['win'][0] for w in ggw), max(w['win'][1] for w in ggw))
                w_sow = w_tr
                dmin, dmax = min(w['dur'] for w in ggw), max(w['dur'] for w in ggw)
                dur_txt = f'{dmin}-{dmax} (GGCMI multi-year average, planting → maturity)'
                ideal_src, ideal_conf = 'GGCMI Phase 3 rice calendar (0.5°) + published phase durations', 45
                ideal_table, base_w = table, w_tr
                ideal_h = (w_tr[0] + dt.timedelta(days=dmin), w_tr[1] + dt.timedelta(days=dmax))
                nursery_mid = 0
            if seasons:
                sw = [month_window_dates(s[1], S - dt.timedelta(days=30)) for s in seasons]
                nur = (min(RICE_CLASSES[s[2]]['nursery'][0] for s in seasons), max(RICE_CLASSES[s[2]]['nursery'][1] for s in seasons))
                w_sow = (min(w[0] for w in sw), max(w[1] for w in sw))
                w_tr = (w_sow[0] + dt.timedelta(days=nur[0]), w_sow[1] + dt.timedelta(days=nur[1]))
                F_lo, F_hi = rice_field_duration([(s[0], s[2]) for s in seasons])
                tot = (min(RICE_CLASSES[s[2]]['total'][0] for s in seasons), max(RICE_CLASSES[s[2]]['total'][1] for s in seasons))
                dur_txt = f'{tot[0]}-{tot[1]} (TNAU, from nursery sowing)'
                ideal_src, ideal_conf = 'TNAU rice season calendar + published phase durations', 60
                ideal_table, base_w = table, w_tr
                ideal_h = (w_tr[0] + dt.timedelta(days=F_lo), w_tr[1] + dt.timedelta(days=F_hi))
                nursery_mid = (nur[0] + nur[1]) // 2
        elif model in CROP_MODELS and S:
            wl = [(nm, w) for nm, w, _ in sow_windows(model, r.state, lgd, S) if w and w[0] - dt.timedelta(days=45) <= S <= w[1] + dt.timedelta(days=45)]
            if wl:
                nm_w, w_sow = min(wl, key=lambda x: abs((x[1][0] - S).days))
                ideal_src, ideal_conf = f'{nm_w} sowing calendar + published stage durations', (55 if nm_w.startswith('TNAU') else 45)
                ideal_table, base_w = table, w_sow
                dur_txt = f"{CROP_MODELS[model]['harvest'][0]}-{CROP_MODELS[model]['harvest'][1]}"
                ideal_h = (w_sow[0] + dt.timedelta(days=CROP_MODELS[model]['harvest'][0]),
                           w_sow[1] + dt.timedelta(days=CROP_MODELS[model]['harvest'][1]))
        final_bucket = None
        if table and final != UNC and cur_names and cur_names[-1] in names_t:
            final_bucket = STAGE_BUCKETS.index(table[names_t.index(cur_names[-1])][2])
        for bi, bucket in enumerate(STAGE_BUCKETS):
            if ideal_src == NA or not table:
                bucket_cols += [NA if table is None else '', '', '', '']
                continue
            members = [(i, t) for i, t in enumerate(ideal_table) if t[2] == bucket]
            if not members:
                bucket_cols += ['Not applicable for this crop', '', '', '']
                continue
            i0, t0 = members[0]; i1, _ = members[-1]
            s_lo = t0[1][0] if isinstance(t0[1], tuple) else t0[1]
            nxt = ideal_table[i1 + 1] if i1 + 1 < len(ideal_table) else None
            e_hi = ((nxt[1][1] if isinstance(nxt[1], tuple) else nxt[1]) - 1) if nxt else None
            i_s = base_w[0] + dt.timedelta(days=s_lo)
            i_e = (base_w[1] + dt.timedelta(days=e_hi)) if e_hi is not None else None
            act = dev = ''
            if final_bucket is not None and bi <= final_bucket and S:
                a_d = S + dt.timedelta(days=s_lo)
                i_mid = i_s + (base_w[1] - base_w[0]) / 2          # deviation vs the MIDDLE of the ideal window (v3.0 used its first day)
                act, dev = ds(a_d), (a_d - i_mid).days
            bucket_cols += [ds(i_s), ds(i_e), act, dev]
        sow_dev = timing = ''
        if w_sow and S:
            mid = w_sow[0] + (w_sow[1] - w_sow[0]) / 2
            ref_S = S - dt.timedelta(days=nursery_mid) if prefix == 'RICE' else S   # rice: compare nursery sowing with TNAU window
            sow_dev = (ref_S - mid).days
            timing = 'Early' if ref_S < w_sow[0] else ('Late' if ref_S > w_sow[1] else 'On Time')
        nb = next((t[2] for t in (table or []) if t[0] == next_s), None)
        T8_ROWS.append([r.state, r.slgd, r.district, r.dlgd, r.tehsil, lgd, c['crop'], season_lbl, final,
            ideal_src, ideal_conf, wtxt(*w_sow) if w_sow else NA, wtxt(*ideal_h) if ideal_h else NA, dur_txt or NA,
            (ds(S) + (' (transplanting' if prefix == 'RICE' else ' (sowing') + (', estimated)' if s_w < 1 else ', field record)')) if S else '', sow_dev, timing or NA,
            ds(harvest_lo + (harvest_hi - harvest_lo) / 2) if harvest_lo else '', (max(0, (harvest_lo + (harvest_hi - harvest_lo) / 2 - TODAY).days) if harvest_lo else ''),
            next_s or '—', (ds(next_date[0]) if next_date else ''), MACHINERY_BY_BUCKET.get(nb, 'None') if nb else 'None']
            + bucket_cols + ['; '.join(qc), RUN_ID])

    # ---- QC12: different crops in one tehsil must not share an identical stage + planting date without independent evidence
    mine = [x for x in T1_ROWS if x[5] == lgd]
    # ---- v3.5 Dominant crop for Tabs 2-7 = the NAMED crop with the largest area this season (all its rows summed, e.g. standing +
    #      newly transplanted + harvested paddy); its stage = the stage of that crop's largest row (confirmed or Option A estimate).
    #      Never a placeholder: unnamed cropland is not a candidate; with no named crop the tehsil's crop mix names the crop.
    UNNAMED = ('Active cropland – crop unknown', '—', 'Perennial / long-duration crops', 'Fallow – no active crop')
    _unk = sum(x[12] or 0 for x in mine if x[7] == 'Active cropland – crop unknown')
    _agg = {}
    for x in mine:
        if x[7] not in UNNAMED:
            _agg[x[7]] = _agg.get(x[7], 0) + (x[12] or 0)
    _act = max(area_bal['active'], 1)
    if _agg and max(_agg.values()) >= 1:
        _crop, _a = max(_agg.items(), key=lambda kv: kv[1])
        _row = max((x for x in mine if x[7] == _crop), key=lambda x: x[12] or 0)
        DOM[lgd] = (_crop, _row[8], _row[23])
        DOM_NOTE[lgd] = (f'Dominant crop = largest named crop this season: {_crop} {_a:,.0f} ha = {min(1, _a / _act):.0%} of active '
                         f'cropland ({_row[9]}; {_row[25].split(":")[0]}); unattributed active cropland {_unk:,.0f} ha')
    elif area_bal['active'] < 1 and share >= 0.2:
        DOM[lgd] = ('Fallow – no active crop', mine[0][8] if mine else f'{SEASON} {SEASON_START.year}', 'Fallow – no active crop')
        DOM_NOTE[lgd] = 'No cropland pixel was green this season (Sentinel-2 max NDVI < 0.35)'
    else:
        _mix = {k: v for k, v in mix_for(r).items() if k in SPAM_SEASON['Both'] or k in SPAM_SEASON.get(SEASON, set())
                or k in SPAM_SEASON['Annual']}
        if _mix:
            _code = max(_mix, key=_mix.get)
            _crop, _model = SPAM_NAMES.get(_code, _code), SPAM_TO_MODEL.get(_code)
            _st = calendar_estimate(dict(presence='Probable crop identification', perennial=_code in SPAM_SEASON['Annual']),
                                    _model, None, None, 0.2, None, '', None, ph_all, r, lgd)
            if _model in CROP_MODELS:
                _w = [(nm, w) for nm, w, _ in sow_windows(_model, r.state, lgd, TODAY)
                      if w and w[0] <= TODAY and (TODAY - w[0]).days <= CROP_MODELS[_model]['harvest'][1]]
                if _w:
                    _nm, (_a1, _a2) = max(_w, key=lambda x: x[1][0])
                    _st = calendar_estimate(dict(presence='Probable crop identification'), _model, CROP_MODELS[_model]['stages'],
                                            _a1 + (_a2 - _a1) / 2, 0.2, CROP_MODELS[_model]['harvest'][1], _model.upper()[:6],
                                            None, ph_all, r, lgd, f'{_nm} sowing window mid-point')
            DOM[lgd] = (_crop, mine[0][8] if mine else f'{SEASON} {SEASON_START.year}', _st[0])
            DOM_NOTE[lgd] = (f'No crop separable by satellite this season ({"cloud" if share < 0.2 else "unattributed cropland"}); '
                             f'dominant crop = largest {SEASON}/all-season crop of the {MIX_NAME} mix ({mix_how(r)}); stage: {_st[6][:160]}')
        else:
            raise RuntimeError(f'Tehsil {lgd} ({r.tehsil}): no named crop and no crop-mix data – refusing to write a placeholder')
    seen = {}
    for x in mine:
        if x[23] != UNC and 'estimate' not in x[23] and x[23] not in ('Fallow – no active crop', 'Standing perennial crop (no seasonal stage)'):
            seen.setdefault((x[23], x[16]), []).append(x)
    for (stg, sd), xs in seen.items():
        if len({x[7] for x in xs}) > 1:
            for x in xs:
                x[51] += '; QC12: identical stage and planting date as another crop in this tehsil – check independence'

T1_BY_KIND = Counter('Stage not determined' if x[23] == UNC else 'calendar / curve estimate (Option A)' if 'estimate' in x[23] else
                     'transition (2 consecutive stages)' if '(transition)' in x[23] else 'confirmed single stage' for x in T1_ROWS)
print(f'✅ Tab 1 rows: {len(T1_ROWS)} | Tab 8 rows: {len(T8_ROWS)} | ' + ' | '.join(f'{k}: {v}' for k, v in T1_BY_KIND.items()))


# ===== CELL 11: Tabs 2–7 – index MoM tabs (one row per tehsil, 24 months across) =====
IDX_TABS = {'NDVI': 'NDVI_MoM', 'EVI': 'EVI_MoM', 'NDWI': 'NDWI_MoM', 'NDRE': 'NDRE_MoM', 'SAVI': 'SAVI_MoM', 'BSI': 'BSI_MoM'}
IDX_HEADER = ['State', 'State LGD Code', 'District', 'District LGD Code', 'Tehsil', 'Tehsil LGD Code', 'Dominant Crop', 'Crop Season',
    'Final Crop Stage (current)', 'Latest Month', 'Latest Value', 'Latest MoM %', 'YoY % (latest month vs same month last year)',
    '24M Average', '24M Min', '24M Max', 'Trend Direction (Improving / Stable / Declining, 3-month rolling)', 'Valid Months (out of 24)']
VAL_COL = {}
for i, lbl in enumerate(MONTH_LABELS):
    VAL_COL[i] = len(IDX_HEADER)
    IDX_HEADER.append(f'{lbl} Value')
    if i > 0:
        IDX_HEADER.append(f'{lbl} MoM %')
IDX_HEADER += ['QC Flags', 'Run ID']

def trend_dir(vals, invert):
    a = [v for v in vals[-6:-3] if v is not None]
    b = [v for v in vals[-3:] if v is not None]
    p = pct(np.mean(b), np.mean(a)) if a and b else None
    if p is None:
        return NA
    p = -p if invert else p
    return 'Improving' if p > 5 else ('Declining' if p < -5 else 'Stable')

IDX_ROWS, IDX_COLOURS = {}, {}
for idx in IDX6:
    rows, colours = [], []
    for ri, r in enumerate(TEH.itertuples()):
        vals, srcs = SERIES[idx][r.lgd]
        dom = DOM[r.lgd]
        okv = [v for v, s_ in zip(vals, srcs) if v is not None]
        filled = [MONTH_LABELS[i] for i, s_ in enumerate(srcs) if s_ == FILL]
        missing = [MONTH_LABELS[i] for i, v in enumerate(vals) if v is None]
        qc = [f'{MONTH_LABELS[-1]} partial (to {TODAY:%d-%b})', 'tehsil cropland mean (not crop-specific)']
        if DOM_NOTE.get(r.lgd):
            qc.append(DOM_NOTE[r.lgd])
        if CL_HA.get(r.lgd, 0) < 1:
            qc.append('No cropland (WorldCover)')
        if filled:
            qc.append(f'Gap-filled from measured neighbours / district median (grey italic): {", ".join(filled)}')
        if missing:
            qc.append(f'Not available: {", ".join(missing)}')
        if idx == 'BSI':
            qc.append('BSI: lower = more crop cover')
        yoy = pct(vals[-1], vals[-13]) if (srcs[-1] == SAT and srcs[-13] == SAT) else None
        row = [r.state, r.slgd, r.district, r.dlgd, r.tehsil, r.lgd, dom[0], dom[1], dom[2],
               MONTH_LABELS[-1], r4(vals[-1]), pct(vals[-1], vals[-2]), yoy,
               r4(float(np.mean(okv))) if okv else None, r4(min(okv)) if okv else None, r4(max(okv)) if okv else None,
               trend_dir(vals, invert=(idx == 'BSI')), srcs.count(SAT)]
        for i, v in enumerate(vals):
            row.append(r4(v))
            if i > 0:
                row.append(pct(v, vals[i - 1]))
            if srcs[i] == FILL:
                colours.append((ri + 1, VAL_COL[i], FILL))
        rows.append(row + ['; '.join(qc), RUN_ID])
    IDX_ROWS[idx], IDX_COLOURS[idx] = rows, colours
# ---- v3.5 Phase 3 fill audit: Dominant Crop / Final Crop Stage must be real values in every row (run stops otherwise)
BANNED = ('mixed / mostly unidentified', 'not identified', 'not applicable', 'insufficient data', 'crop unknown')
_bad = [(rw[5], rw[6], rw[8]) for rw in IDX_ROWS['NDVI'] if not str(rw[6]).strip() or not str(rw[8]).strip()
        or any(b in f'{rw[6]} {rw[8]}'.lower() for b in BANNED) or (CALENDAR_ESTIMATE and rw[8] == UNC)]
_bad += [(rw[5], rw[7], rw[23]) for rw in T1_ROWS if not str(rw[23]).strip() or (CALENDAR_ESTIMATE and rw[23] == UNC)]
if _bad:
    raise RuntimeError(f'Fill audit failed for {len(_bad)} rows, e.g. {_bad[:5]}')
_dk = Counter('confirmed' if ('estimate' not in d[2] and d[2] not in ('Fallow – no active crop',)) else
              'fallow' if d[2] == 'Fallow – no active crop' else 'estimate' for d in DOM.values())
print(f'✅ Fill audit passed – Dominant Crop and Final Crop Stage filled for all {len(DOM)} tehsils | dominant-crop stage: '
      + ', '.join(f'{k} {v}' for k, v in _dk.items()))
print('   Most common dominant crops:', Counter(d[0] for d in DOM.values()).most_common(10))
_cells = 57 * (len(T1_ROWS) + 1) + 52 * (len(T8_ROWS) + 1) + 6 * len(IDX_HEADER) * (len(TEH) + 1)
print(f'✅ Index tabs ready ({len(IDX_HEADER)} cols) | workbook ≈ {_cells / 1e6:.2f} M cells (Google Sheets limit 10 M)')


# ===== CELL 11b: Validation report (Part 15) – only with real ground truth =====
VALIDATION_TXT = 'NOT VALIDATED – no field observations supplied. All confidence scores are provisional rubric scores, not accuracy.'
if FIELD:
    recs = []
    for lgd, obs in FIELD.items():
        rows_l = [x for x in T1_ROWS if x[5] == lgd]
        for o in obs:
            oc = str(o.get('crop', '')).strip().lower()
            pred = next((x for x in rows_l if oc and oc in str(x[7]).lower()), None)
            top = max(rows_l, key=lambda x: x[12] or 0) if rows_l else None
            recs.append(dict(tehsil_lgd=lgd, observed_crop=o.get('crop'), predicted_top_crop=top[7] if top else '—',
                             crop_listed=bool(pred), observed_stage=o.get('stage'), predicted_stage=pred[23] if pred else '—',
                             stage_uncertain=(pred[23] == UNC) if pred else True))
    V = pd.DataFrame(recs)
    crop_cm = pd.crosstab(V.observed_crop, V.predicted_top_crop, dropna=False)
    sv = V[~V.stage_uncertain & V.observed_stage.notna()]
    stage_cm = pd.crosstab(sv.observed_stage, sv.predicted_stage, dropna=False) if len(sv) else pd.DataFrame()
    acc_crop = (V.observed_crop.str.lower() == V.predicted_top_crop.str.lower()).mean() if len(V) else float('nan')
    acc_stage = (sv.observed_stage.str.lower() == sv.predicted_stage.str.lower()).mean() if len(sv) else float('nan')
    VALIDATION_TXT = (f'Ground truth records: {len(V)} | top-crop accuracy {acc_crop:.1%} | crop listed in tehsil {V.crop_listed.mean():.1%} | '
                      f'stage accuracy where a stage was given {acc_stage:.1%} (n={len(sv)}) | stage reported uncertain for '
                      f'{V.stage_uncertain.mean():.1%}. Field records were also used as inputs → for an independent test, '
                      'hold out part of the field data.')
    _vp = f'{WORK_DIR}/validation_report.xlsx'
    os.makedirs(os.path.dirname(_vp), exist_ok=True)
    with pd.ExcelWriter(_vp) as xw:
        V.to_excel(xw, sheet_name='records', index=False)
        crop_cm.to_excel(xw, sheet_name='crop_confusion')
        if len(stage_cm):
            stage_cm.to_excel(xw, sheet_name='stage_confusion')
    print('💾 Validation report saved:', _vp)
print('🧪 Validation:', VALIDATION_TXT)

# ---- Independent-reference checks (official figures) + v3.0 → v3.1 before/after + publish-gate summary
REF_ROWS = []
for ref in REFERENCE_AREAS:
    teh = TEH[TEH.state.map(norm_state) == norm_state(ref['state'])]
    if ref.get('district'):
        teh = teh[teh.district.str.lower().str.contains(ref['district'].lower())]
    if not len(teh):
        continue
    keep = set(teh.lgd)
    est = sum(x[12] or 0 for x in T1_ROWS if x[7] == 'Rice (paddy)' and x[5] in keep)   # standing + recent + harvested paddy
    old = sum((fnum(SAT_MAPS.get(l, {}).get('rice_frac_v30')) or 0) * GEO_AREA_HA.get(l, 0) for l in teh.lgd)
    ratio = est / ref['ha'] if ref['ha'] else None
    verdict = ('within reference' if ref['kind'] == 'upper_bound' and est <= ref['ha'] else
               'EXCEEDS upper bound' if ref['kind'] == 'upper_bound' else
               'consistent (±30 %)' if ratio and 0.7 <= ratio <= 1.3 else 'INCONSISTENT (>30 % off)')
    REF_ROWS.append({'Scope': ref['scope'], 'Reference': ref['what'], 'Reference ha': ref['ha'], 'As of': ref['date'],
                     'Source': ref['source'], 'v3.0 rule ha (same run)': round(old), 'Current-rule estimate ha': round(est),
                     'Estimate / reference': round(ratio, 2) if ratio else '', 'Verdict': verdict,
                     'Note': 'Partial coverage if the run is filtered to some states/districts' if STATE_FILTER else '',
                     **({f'Calibration {cs} ha': round(sum((fnum(SAT_MAPS.get(l, {}).get(f'rice_cal_{k}')) or 0) * GEO_AREA_HA.get(l, 0)
                                                               for l in teh.lgd)) for k, cs in enumerate(RICE_CAL_SETS)} if RICE_CALIBRATE else {})})
STAGE_KIND = Counter((x[7], 'single stage' if (x[23] != UNC and '(transition)' not in x[23]) else
                      'transition' if x[23] != UNC else 'not determined') for x in T1_ROWS)
GATE = Counter(q.split(':')[0] for x in T1_ROWS for q in str(x[51]).split('; ') if 'stage rejected' in q)
VAL_SUMMARY = pd.DataFrame([{'Crop': k[0], 'Stage result': k[1], 'Rows': v} for k, v in sorted(STAGE_KIND.items())])
print('📏 Reference checks:')
for rr in REF_ROWS:
    print(f"   {rr['Scope']}: old v3.0 rule {rr['v3.0 rule ha (same run)']:,} ha → current rule {rr['Current-rule estimate ha']:,} ha vs {rr['Reference']} "
          f"{rr['Reference ha']:,} ha → {rr['Verdict']}")
if RICE_CALIBRATE:
    print('🎚️ Rice threshold calibration (VH flood, VV flood, drop, rise) → paddy ha this season (standing + harvested + recently flooded):')
    for rr in REF_ROWS:
        print(f"   {rr['Scope']} (reference {rr['Reference ha']:,} ha, {rr['Reference']}):")
        for k, cs in enumerate(RICE_CAL_SETS):
            v = rr[f'Calibration {cs} ha']
            print(f"      {cs}: {v:,} ha  = {v / rr['Reference ha']:.0%} of reference")
print('🚦 Publish gate rejections:', dict(GATE) or 'none')

DATA_SOURCES = pd.DataFrame([
    ('Sentinel-2 L2A harmonized', 'ESA / Copernicus', 'S2_SR_HARMONIZED', '10-20 m', '~5 days', f'to {TODAY}', 'Earth Engine',
     'Clouds; SCL mask imperfect', 'Crop development curves (stage evidence)'),
    ('Sentinel-1 GRD IW', 'ESA / Copernicus', 'S1_GRD', '10 m', '~6-12 days', f'{S1_START} to {TODAY}', 'Earth Engine',
     'Speckle; terrain; rain-flooded fields can mimic puddling', 'Rice flooding/transplanting, rice VH growth curve'),
    ('ESA WorldCover', 'ESA', 'v200 (2021)', '10 m', 'static', '2021', 'Earth Engine', 'Static 2021 map', 'Cropland mask'),
    ('Dynamic World V1', 'Google / WRI', 'GOOGLE/DYNAMICWORLD/V1', '10 m', 'per Sentinel-2 scene', f'{TODAY - dt.timedelta(days=60)} to {TODAY}',
     'Earth Engine', 'Generic classes (crops / flooded vegetation), not crop types; cloud-limited', 'Current-season corroboration of rice pixels'),
    ('ESA WorldCereal', 'ESA', '2021 v100', '10 m', 'seasonal', '2021', 'Earth Engine', 'Historical 2021 map; wheat/barley not separable',
     'Historical maize / winter-cereal location (with current activity only)'),
    ('IFPRI SPAM', 'IFPRI', '2020 V2r2', '~10 km', 'static', 'c. 2019-21', 'Harvard Dataverse (manual download)',
     'Modelled allocation, coarse', 'Probable crop mix only'),
    ('ERA5-Land daily', 'ECMWF / Copernicus', 'DAILY_AGGR', '11.1 km', 'daily (~5-day latency)', f'to {_era_last}', 'Earth Engine',
     'Reanalysis, not station data', 'Rainfall, air temperature, normals, GDD'),
    ('CHIRPS daily', 'UCSB-CHG', '2.0', '~5.5 km', 'daily (weeks latency)', f'to {_ch_last}', 'Earth Engine', 'Latency',
     'Rainfall when current'),
    ('MODIS LST', 'NASA', 'MOD11A2 v061', '1 km', '8-day', 'last 32 days', 'Earth Engine', 'Land-surface, not air temperature',
     'Context only'),
    ('LGD sub-districts', 'Govt of India LGD via bharatlas.com', 'LGD_Subdistricts.parquet', 'polygon', 'static', 'as published',
     'HTTP download', 'Boundary vintage per publisher', 'Tehsil units + LGD codes'),
    ('TNAU / ICAR crop calendars & durations', 'TNAU, ICAR', 'web publications (see REFS)', 'state / zone', 'static', 'as published',
     'Literature', 'Variety-specific durations vary', 'Stage timing evidence'),
], columns=['Dataset', 'Provider', 'Version', 'Spatial resolution', 'Temporal resolution', 'Observation period', 'Access method',
            'Known limitations', 'Use in CropIntel'])


# ===== CELL 12: Write all 8 tabs to Google Sheets =====
sh = gc.open_by_key(SHEET_ID)
FMT = {FILL: {'textFormat': {'italic': True, 'foregroundColor': {'red': 0.5, 'green': 0.5, 'blue': 0.5}}}}

def clean(v):
    if v is None:
        return ''
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return '' if (math.isnan(v) or math.isinf(v)) else v
    return v

def get_ws(title, nrows, ncols):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(nrows, 2), cols=ncols)
    ws.clear()
    sh.batch_update({'requests': [{'repeatCell': {'range': {'sheetId': ws.id}, 'cell': {'userEnteredFormat': {}},
                                                  'fields': 'userEnteredFormat'}}]})
    ws.resize(rows=max(nrows, 2), cols=ncols)
    return ws

def write_tab(title, header, rows, colours=None):
    ws = get_ws(title, len(rows) + 1, len(header))
    data = [header] + [[clean(v) for v in row] for row in rows]
    for i in range(0, len(data), 3000):
        ws.update(range_name=f'A{i + 1}', values=data[i:i + 3000], value_input_option='USER_ENTERED')
        time.sleep(1.5)
    ws.format('1:1', {'textFormat': {'bold': True}, 'wrapStrategy': 'WRAP',
                      'backgroundColor': {'red': 0.85, 'green': 0.92, 'blue': 0.83}})
    ws.freeze(rows=1, cols=6)
    try:
        ws.set_basic_filter()          # filter row: e.g. show only 'Overall Confidence Score' ≥ 80
    except Exception:
        pass
    if colours:
        reqs = [{'repeatCell': {'range': {'sheetId': ws.id, 'startRowIndex': rr, 'endRowIndex': rr + 1,
                                          'startColumnIndex': cc, 'endColumnIndex': cc + 1},
                                'cell': {'userEnteredFormat': FMT[k]}, 'fields': 'userEnteredFormat.textFormat'}}
                for rr, cc, k in colours]
        for i in range(0, len(reqs), 1000):
            sh.batch_update({'requests': reqs[i:i + 1000]})
            time.sleep(1.2)
    print(f'   ✍️ {title}: {len(rows)} rows × {len(header)} cols')

TABS = [('Crop_Intelligence_Current', T1_HEADER, T1_ROWS, None)] + \
       [(IDX_TABS[i], IDX_HEADER, IDX_ROWS[i], IDX_COLOURS[i]) for i in IDX6] + \
       [('Ideal_Crop_Window', T8_HEADER, T8_ROWS, None)]

# always keep a copy in Drive (also the fallback if the Sheet refuses access)
XLSX = f'{WORK_DIR}/Crop_Intelligence_{RUN_ID}.xlsx'
with pd.ExcelWriter(XLSX) as xw:
    for title, header, rows, _ in TABS:
        pd.DataFrame([[clean(v) for v in r] for r in rows], columns=header).to_excel(xw, sheet_name=title[:31], index=False)
    pd.DataFrame(AREA_ROWS).to_excel(xw, sheet_name='Area_Balance', index=False)
    DATA_SOURCES.to_excel(xw, sheet_name='Data_Sources', index=False)
    pd.DataFrame({'Reference key': list(REFS), 'Reference': list(REFS.values())}).to_excel(xw, sheet_name='References', index=False)
    pd.DataFrame({'Validation': [VALIDATION_TXT, 'Confidence scores are PROVISIONAL rubric scores – not accuracy.']}).to_excel(
        xw, sheet_name='Validation', index=False)
    pd.DataFrame(REF_ROWS).to_excel(xw, sheet_name='Validation', index=False, startrow=4)
    VAL_SUMMARY.to_excel(xw, sheet_name='Validation', index=False, startrow=8 + len(REF_ROWS))
    pd.DataFrame([{'QC gate': k, 'Stages rejected': v} for k, v in GATE.items()]).to_excel(
        xw, sheet_name='Validation', index=False, startrow=12 + len(REF_ROWS) + len(VAL_SUMMARY))
print('💾 Excel copy saved:', XLSX, '(uploaded as a workflow artifact)')

try:
    for title, header, rows, colours in TABS:
        write_tab(title, header, rows, colours)
    print(f'✅ Done – {RUN_ID}  https://docs.google.com/spreadsheets/d/{SHEET_ID}')
except gspread.exceptions.APIError as e:
    print('⛔ Google Sheets API error – the Excel copy (workflow artifact) still has all results:', str(e)[:300])
    raise
