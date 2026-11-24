"""Independent audit of a CropIntel workbook. Usage: python3 audit.py <workbook.xlsx> <crop_intel.py> <out.xlsx>"""
import sys, re, datetime as dt, io, contextlib
import pandas as pd, numpy as np
sys.path.insert(0, __import__('os').path.join(__import__('os').path.dirname(__import__('os').path.abspath(__file__)), '..', 'tests'))
from harness import build
WB, SRC, OUT = sys.argv[1:4]
with contextlib.redirect_stdout(io.StringIO()):
    ns = build(SRC)                                   # stage tables / calendars exactly as implemented
CM, rst = ns['CROP_MODELS'], ns['rice_stage_table']
T1 = pd.read_excel(WB, 0, dtype=str, keep_default_na=False)
T8 = pd.read_excel(WB, 'Ideal_Crop_Window', dtype=str, keep_default_na=False)
IDX = {s: pd.read_excel(WB, s) for s in ['NDVI_MoM', 'EVI_MoM', 'NDWI_MoM', 'NDRE_MoM', 'SAVI_MoM', 'BSI_MoM']}
TODAY = dt.date.fromisoformat(T1['Processing Date'].iloc[0][:10])
CROP2MODEL = {'Rice (paddy)': 'RICE', 'Rice (paddy) – newly transplanted': 'RICE', 'Rice (paddy) – harvested this season': 'RICE'}
for m in CM:
    CROP2MODEL[m] = m
for c in T1.Crop.unique():
    for m in CM:
        if c.startswith(m) or (m == 'Rapeseed & Mustard' and 'Mustard' in c) or (m == 'Wheat' and c.startswith('Wheat')):
            CROP2MODEL[c] = m
PERENNIAL = {'Sugarcane', 'Coconut', 'Banana', 'Mango', 'Tea', 'Coffee', 'Rubber', 'Arecanut', 'Cashew', 'Citrus', 'Grapes', 'Papaya',
             'Pineapple', 'Oil palm', 'Black pepper', 'Ginger', 'Spices (turmeric, coriander, cumin …)', 'Plantain', 'Other fruits',
             'Perennial / long-duration crops', 'Perennial / plantation crops (type not separable)'}
RICE_TABLES = {F: rst(F) for F in range(85, 161, 5)}
RICE_NAMES = {t[0] for tab in RICE_TABLES.values() for t in tab}
GENERIC = ('cropland curve estimate', 'season calendar estimate', 'steady green canopy')
SPECIAL_OK = {'Harvested (this season)', 'Fallow – no active crop', 'Standing perennial crop (no seasonal stage)'}
UNRES = ('Stage unresolved', 'Stage not determined')

def d(v):
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except Exception:
        return None

def parts(stage):
    s = re.sub(r' \((transition|calendar estimate|model-inferred)\)$', '', stage)
    return [p.strip() for p in s.split(' → ')]

def stage_at(table, dap):
    cur = 0
    for k, t in enumerate(table):
        st = t[1]; lo = (st[0] + st[1]) / 2 if isinstance(st, tuple) else st
        if dap >= lo:
            cur = k
    return table[cur][0]

def expected(model, S):
    if not S:
        return set()
    dap = (TODAY - S).days
    if model == 'RICE':
        return {stage_at(tab, dap) for tab in RICE_TABLES.values()} | ({'Harvested (this season)'} if dap > 85 else set())
    if model in CM:
        return {stage_at(CM[model]['stages'], dap)}
    return set()

EX = []
def ex(chk, r, err, cause, fix, exp=''):
    EX.append({'Check': chk, 'Record ID': f"{r['Tehsil LGD Code']}|{r['Crop']}|{r.name + 2}", 'District': r['District'], 'Tehsil': r['Tehsil'],
               'Crop': r['Crop'], 'Current stage': r.get('Final Crop Stage', r.get('Final Crop Stage (current)', '')),
               'Expected stage': exp, 'Existing evidence': (r.get('Stage Source', '') or r.get('QC Flags', ''))[:220],
               'Error identified': err, 'Root cause': cause, 'Recommended correction': fix})

for i, r in T1.iterrows():
    st, crop, model = r['Final Crop Stage'], r['Crop'], CROP2MODEL.get(r['Crop'])
    S = d(r['Sowing Date Estimated'])
    calendar_S = 'Calendar window' in r['Sowing Date Evidence'] or 'calendar' in r['Sowing Date Evidence'].lower() and 'Sentinel' not in r['Sowing Date Evidence'] and 'green-up' not in r['Sowing Date Evidence']
    unresolved = st.startswith(UNRES)
    # 2 generic
    if any(g in st for g in GENERIC):
        ex('02 Generic vegetation-curve stage', r, 'Generic curve/season label used as the crop stage', 'v3.5 Option A calendar_estimate() mapped the '
           'tehsil cropland NDVI phase to a generic stage', 'v4.0: resolve_unconfirmed() → explicit "Stage unresolved – …" (fixed in code)')
    # 1 / 3 crop-specific stage validity
    if not unresolved and st not in SPECIAL_OK and not any(g in st for g in GENERIC):
        names = RICE_NAMES if model == 'RICE' else ({t[0] for t in CM[model]['stages']} if model in CM else set())
        bad = [p for p in parts(st) if p not in names and p not in ('Land Prep', 'Harvested / post-harvest', 'Harvested (this season)')]
        if not names:
            ex('03 Incorrect crop-stage combination', r, f'Stage given for a crop with no stage model ({crop})', 'no sourced stage table',
               'Report "Stage unresolved – no sourced stage model"')
        elif bad:
            ex('03 Incorrect crop-stage combination', r, f'Stage {bad} is not in the {crop} stage table', 'stage from another crop / generic',
               'Use the crop\'s own table only')
        if model == 'RICE' and not crop.startswith('Rice') or (model != 'RICE' and any(p in RICE_NAMES - {t[0] for m in CM.values() for t in m['stages']} for p in parts(st))):
            ex('03 Incorrect crop-stage combination', r, 'Rice stage on a non-rice crop', '', 'Use the crop\'s own table')
        # 1 expected stage from observed sowing
        if S and not calendar_S and names:
            exp = expected(model, S)
            if exp and not set(parts(st)) & exp and 'Harvested' not in st:
                ex('01 Stage vs crop phenology model', r, f'{(TODAY - S).days} d after planting; reported stage outside the duration model',
                   'stage window and curve disagreement / transition logic', 'Field-verify; report expected range', ', '.join(sorted(exp)))
    # 4 historical / calendar only
    if '(calendar estimate)' in st or 'Calendar estimate' in r['Stage Source']:
        ex('04 Stage from calendar without current-season evidence', r, 'Calendar-estimated stage published as Final Crop Stage',
           'v3.5 Option A', 'Unresolved status (v4.0)')
    if 'Modelled' in r['Crop Identification Method'] + r['Crop Area Evidence'] and r['Crop Presence Status'] == 'Probable crop identification' \
            and 'Satellite' not in r['Crop Area Evidence'][:60]:
        ex('04 Crop from historical prior only', r, 'Crop presence + area taken from CROPGRIDS/SPAM (c. 2020) – no current-season evidence',
           'crops_v3 modelled rows scaled to unexplained active area', 'List as Alternative Crop Candidate only (v4.0)')
    # 5 acquisition dates
    if r['Stage Observation Date'].startswith('1970') or '1970-01-01' in r['S2 Scenes / S1 Scenes']:
        ex('05 Invalid satellite acquisition date', r, 'Observation date 1970-01-01 (epoch)', '_mask_s2 dropped system:time_start',
           'copyProperties + obs_image max/median (v4.0)')
    # 6 stale / cloud
    try:
        vs = float(r['Valid S2 Pixel Share'] or 'nan')
    except ValueError:
        vs = np.nan
    if 'QC15' in r['QC Flags'] and not unresolved and model not in ('RICE',) and st not in SPECIAL_OK:
        ex('06 Freshness not verifiable', r, 'Stage published while QC15 flags stale optics; QC15 itself is driven by the 1970 date, so real freshness is unknown',
           'epoch date bug + no freshness gate', 'Actual acquisition dates + STALE_DAYS gate (v4.0)')
    if not np.isnan(vs) and vs < 0.2 and not unresolved and st not in SPECIAL_OK:
        ex('06 Cloud-contaminated observation', r, f'Valid S2 pixel share {vs:.2f} < 0.2 but stage reported', 'cloud', 'Unresolved – insufficient observations')
    # 7 sowing dates
    if S:
        if S > TODAY:
            ex('07 Sowing date', r, 'Sowing date in the future', '', 'Remove')
        if calendar_S:
            ex('07 Sowing date', r, 'Calendar window midpoint presented as the estimated sowing date', 'S from sow_windows() shown via ds(S)',
               'Blank sowing date; show window as recommended (v4.0)')
        if model in CM and model in ns['TN_WINDOWS']:
            ok = False
            for nm, (m1, m2) in ns['TN_WINDOWS'][model]:
                for yr in (S.year - 1, S.year):
                    a = dt.date(yr, m1, 1); b = dt.date(yr + (m2 < m1), m2, 28)
                    ok |= a - dt.timedelta(days=30) <= S <= b + dt.timedelta(days=30)
            if not ok:
                ex('07 Sowing date', r, f'Sowing {S} outside every TNAU {model} window ±30 d', 'green-up date from a different crop / cohort',
                   'Treat as conflicting evidence → unresolved')
        if model == 'RICE' and not (dt.date(TODAY.year, 5, 1) <= S <= TODAY):
            ex('07 Sowing date', r, f'Transplanting {S} outside the current season search period', '', 'Check cohort selection')
    # 9 perennial
    if crop in PERENNIAL and 'named' in r['Crop Identification Method'] and 'largest perennial crop' in r['Crop Identification Method']:
        ex('09 Perennial classification', r, f'All perennially-green cropland named "{crop}" from the prior', 'v3.5 perennial naming rule',
           'Perennial group, crop type not separable (v4.0)')
    if crop in PERENNIAL and st not in SPECIAL_OK and not unresolved and crop != 'Sugarcane':
        ex('09 Perennial classification', r, 'Seasonal stage on a perennial crop', '', 'Perennial stage or unresolved')
    if crop in PERENNIAL and re.search(r'grain|milk|dough|heading', st, re.I):
        ex('09 Perennial classification', r, 'Grain-filling stage on perennial crop', '', 'Remove')
    # 10 unidentified
    if crop.startswith('Active cropland') and not unresolved:
        ex('10 Unidentified cropland with stage', r, 'Unidentified cropland received a stage', 'Option A curve estimate', U := 'Stage unresolved – crop identity not established')

# 8 harvest (Tab 8) & 14 ideal windows & 15 machinery
NEXT_BUCKET = {}
for m, dct in CM.items():
    for t in dct['stages']:
        NEXT_BUCKET[(m, t[0])] = t[2]
for tab in RICE_TABLES.values():
    for t in tab:
        NEXT_BUCKET[('RICE', t[0])] = t[2]
MB = ns['MACHINERY_BY_BUCKET']
DISTRICT_T8 = 'Tehsil LGD Code' not in T8.columns
if DISTRICT_T8:                                           # v4.1 district calendar
    for i, r in T8.iterrows():
        r = r.copy(); r['Tehsil'] = '(district)'; r['Tehsil LGD Code'] = r['District LGD Code']; r['Final Crop Stage'] = r['Observed Current Stage (district)']
        sw, hw, du = r['Ideal Sowing Window (current / next cycle)'], r['Ideal Harvest Window'], r['Ideal Crop Duration Days']
        if ' to ' in sw and ' to ' in hw and '-' in du:
            s0 = dt.datetime.strptime(sw.split(' to ')[0], '%d-%b-%Y').date(); h0 = dt.datetime.strptime(hw.split(' to ')[0], '%d-%b-%Y').date()
            if (h0 - s0).days != int(du.split('-')[0]):
                ex('14 Ideal window', r, f'harvest start − sowing start {(h0 - s0).days} d ≠ minimum duration {du}', '', 'Recompute')
        if not r['Calendar Source'] or r['Source Confidence (High / Medium / Low)'] not in ('High', 'Medium', 'Low'):
            ex('14 Ideal window', r, 'Calendar row without source / confidence', '', 'Add source')
        if str(r['Tehsils with Observed Planting']) in ('', '0') and (r['Observed Planting Date (district median)'] or r['Expected Harvest (from observed planting)']):
            ex('07 Sowing date', r, 'Observed planting / harvest shown without observed tehsils', '', 'Blank')
        m = r['Machinery Need – Next Stage (Tillage / Seeding / Spraying / Harvesting / None)']
        if m not in ('', 'None') and not r['Machinery Recommendation (crop & operation)']:
            ex('15 Machinery', r, f'Machinery "{m}" without a crop-specific recommendation', '', 'machinery engine')
for i, r in (T8.iloc[0:0] if DISTRICT_T8 else T8).iterrows():
    model = CROP2MODEL.get(r['Crop']); st = r['Final Crop Stage (current)']
    A, H = d(r['Actual Sowing Date (this season)']), d(r['Expected Harvest Date (adjusted)'])
    if A and H:
        days = (H - A).days
        lo, hi = ((80, 165) if model == 'RICE' else CM[model]['harvest'] if model in CM else (None, None))
        if lo and not (lo - 10 <= days <= hi + 15):
            ex('08 Harvest date vs duration', r, f'Harvest − sowing = {days} d outside the {r["Crop"]} duration {lo}-{hi} d', '', 'Recompute from duration table',
               f'{A + dt.timedelta(days=lo)} – {A + dt.timedelta(days=hi)}')
        if H < TODAY and not st.startswith(('Harvest', 'Physiological', 'Stage unresolved')):
            ex('08 Harvest date vs observed stage', r, f'Expected harvest {H} already past while stage is "{st}"', '', 'Re-derive from observed stage')
    if A and 'calendar' in r['Actual Sowing Date (this season)'].lower():
        ex('14 Ideal window', r, 'Calendar date shown as actual sowing', '', 'Blank (v4.0)')
    sw, hw = r['Ideal Sowing Window'], r['Ideal Harvest Window']
    if ' to ' in sw and ' to ' in hw:
        s0 = dt.datetime.strptime(sw.split(' to ')[0], '%d-%b-%Y').date(); h0 = dt.datetime.strptime(hw.split(' to ')[0], '%d-%b-%Y').date()
        dur = (h0 - s0).days
        if model in CM and not (CM[model]['harvest'][0] - 15 <= dur <= CM[model]['harvest'][1] + 15):
            ex('14 Ideal window', r, f'Ideal harvest start − ideal sowing start = {dur} d vs {r["Crop"]} duration {CM[model]["harvest"]}', '', 'Recompute')
    elif model in CM and sw in ('Not available', ''):
        ex('14 Ideal window', r, 'No ideal window for a crop with a calendar', 'no TNAU/GGCMI window for tehsil', 'Add calendar source')
    nxt = r['Next Stage']; mach = r['Machinery Need – Next Stage (Tillage / Seeding / Spraying / Harvesting / None)']
    nb = NEXT_BUCKET.get((model, nxt))
    if nb:
        if MB.get(nb, 'None') != (mach or 'None'):
            ex('15 Machinery', r, f'Machinery "{mach or "blank"}" but next stage "{nxt}" ({nb}) needs "{MB.get(nb)}"', 'bucket lookup', 'machinery_for() (v4.0)')
    elif mach not in ('', 'None'):
        ex('15 Machinery', r, f'Machinery "{mach}" without a valid next agricultural stage ("{nxt}")', '', 'None')
    if mach in ('', ) and nxt not in ('', '—'):
        ex('15 Machinery', r, 'Machinery blank while a next stage exists', '', 'machinery_for()')

# 11 area
for lgd, g in T1.groupby('Tehsil LGD Code'):
    r = g.iloc[0]
    m = re.search(r'cropland ([\d,]+) \| active this season ([\d,]+) \| identified crops ([\d,]+) \| unidentified active ([\d,]+)', r['Crop Area Evidence'])
    a = pd.to_numeric(g['Estimated Crop Area Ha'], errors='coerce').fillna(0)
    if m:
        cl, act, idn, unk = (float(x.replace(',', '')) for x in m.groups())
        if a.sum() > act * 1.01 + 1:
            ex('11 Area / overlap', r, f'Sum of crop rows {a.sum():,.0f} ha > active cropland {act:,.0f} ha (double counting)', 'overlapping masks / Both-season totals', 'Mutually exclusive masks')
        if act > cl * 1.01 + 1:
            ex('11 Area / overlap', r, f'Active {act:,.0f} ha > cropland {cl:,.0f} ha', '', 'Clip')
        if abs(idn + unk - act) > max(5, 0.01 * act):
            ex('11 Area / overlap', r, f'identified {idn:,.0f} + unidentified {unk:,.0f} ≠ active {act:,.0f}', '', 'Reconcile')
    if g.duplicated(['Crop']).any():
        for _, rr in g[g.duplicated(['Crop'], keep=False)].iterrows():
            ex('13 Duplicate record', rr, 'Same crop twice in one tehsil', 'rice cohort rows share the crop name' if rr['Crop'].startswith('Rice') else '',
               'Distinct crop/cohort label or aggregate')
mod = T1[T1['Crop Area Evidence'].str.contains('Modelled') & ~T1['Crop Area Evidence'].str.startswith(tuple('0123456789')) ]
# 12 MoM recompute
mom_rows = []
for s, df in IDX.items():
    vals = [c for c in df.columns if c.endswith(' Value') and c != 'Latest Value']
    for i, r in df.iterrows():
        for k in range(1, len(vals)):
            pc = vals[k].replace(' Value', ' MoM %')
            if pc not in df.columns:
                continue
            a, b, rep = r[vals[k]], r[vals[k - 1]], r[pc]
            if pd.isna(rep):
                continue
            if pd.isna(a) or pd.isna(b):
                mom_rows.append((s, r['Tehsil'], vals[k], 'MoM % reported with a missing value')); continue
            if b <= 0.05:
                mom_rows.append((s, r['Tehsil'], vals[k], f'% change on baseline {b:.3f} (negative / near zero) – misleading'))
                continue
            calc = (a - b) / b * 100
            if abs(calc - rep) > 0.2 + 0.01 / b:          # tolerance for 4-dp rounding of the stored values
                mom_rows.append((s, r['Tehsil'], vals[k], f'reported {rep} vs recomputed {calc:.1f}'))
        lv = r['Latest Value']; last = r[vals[-1]]
        if not (pd.isna(lv) and pd.isna(last)) and abs((lv or 0) - (last or 0)) > 1e-6:
            mom_rows.append((s, r['Tehsil'], 'Latest Value', 'Latest Value ≠ last monthly value'))
MOM = pd.DataFrame(mom_rows, columns=['Sheet', 'Tehsil', 'Column', 'Problem'])
# 13 keys / consistency
keys = {'T1': set(T1['Tehsil LGD Code']), 'T8': set(T8['Tehsil LGD Code']) if 'Tehsil LGD Code' in T8.columns else set(T1['Tehsil LGD Code'])} | {s: set(df['Tehsil LGD Code'].astype(str)) for s, df in IDX.items()}
key_issue = {k: len(v ^ keys['T1']) for k, v in keys.items()}
row_mis = 0 if 'Tehsil LGD Code' not in T8.columns else int(((T1['Crop'] != T8['Crop']) | (T1['Final Crop Stage'] != T8['Final Crop Stage (current)'])).sum())
missing = {c: int((T1[c].str.strip() == '').sum()) for c in ['Crop', 'Crop Season', 'Final Crop Stage', 'Stage Source', 'Sowing Date Estimated',
                                                            'Stage Observation Date', 'Estimated Crop Area Ha']}
EXD = pd.DataFrame(EX)
# summaries
T1['Stage class'] = np.select([T1['Final Crop Stage'].str.contains('|'.join(GENERIC)), T1['Final Crop Stage'].str.contains('calendar estimate'),
                               T1['Final Crop Stage'].str.startswith(UNRES), T1['Final Crop Stage'].isin(SPECIAL_OK)],
                              ['Generic curve/season estimate', 'Calendar estimate', 'Unresolved', 'Fallow / perennial status'], 'Crop-specific stage')
bad_ids = set(EXD['Record ID']) if len(EXD) else set()
T1['Record ID'] = T1['Tehsil LGD Code'] + '|' + T1['Crop'] + '|' + (T1.index + 2).astype(str)
T1['Has exception'] = T1['Record ID'].isin(bad_ids)
crop_sum = T1.groupby('Crop').agg(Rows=('Crop', 'size'), Area_ha=('Estimated Crop Area Ha', lambda s: round(pd.to_numeric(s, errors='coerce').sum())),
                                  Crop_specific=('Stage class', lambda s: (s == 'Crop-specific stage').sum()),
                                  Generic=('Stage class', lambda s: (s == 'Generic curve/season estimate').sum()),
                                  Calendar=('Stage class', lambda s: (s == 'Calendar estimate').sum()),
                                  Status=('Stage class', lambda s: (s == 'Fallow / perennial status').sum()),
                                  Rows_with_exception=('Has exception', 'sum')).sort_values('Area_ha', ascending=False).reset_index()
crop_sum['Rows passing validation'] = crop_sum.Rows - crop_sum.Rows_with_exception
dist_sum = T1.groupby('District').agg(Rows=('Crop', 'size'), Crop_specific=('Stage class', lambda s: (s == 'Crop-specific stage').sum()),
                                      Generic=('Stage class', lambda s: (s == 'Generic curve/season estimate').sum()),
                                      Calendar=('Stage class', lambda s: (s == 'Calendar estimate').sum()),
                                      Rows_with_exception=('Has exception', 'sum')).reset_index()
dist_sum['% rows passing'] = (100 * (1 - dist_sum.Rows_with_exception / dist_sum.Rows)).round(1)
chk = EXD.groupby('Check').agg(Exceptions=('Record ID', 'size'), Records=('Record ID', 'nunique')).reset_index() if len(EXD) else pd.DataFrame()
summary = pd.DataFrame([
    ('Workbook', WB.split('/')[-1]), ('Run ID', T1['Run ID'].iloc[0]), ('Model version', ', '.join(T1['Model Version'].unique())),
    ('Tab 1 rows', len(T1)), ('Tehsils', T1['Tehsil LGD Code'].nunique()),
    ('Rows with ≥ 1 exception', int(T1['Has exception'].sum())), ('Rows passing all checks', int((~T1['Has exception']).sum())),
    ('MoM % problems (Tabs 2-7)', len(MOM)), ('Tab1/Tab8 row mismatches', row_mis),
    ('Geographic key mismatches vs Tab 1', str(key_issue)), ('Missing fields (Tab 1)', str(missing)),
] + [(f'Check {r.Check}', f'{r.Exceptions} exceptions / {r.Records} records') for r in chk.itertuples()], columns=['Item', 'Value'])
with pd.ExcelWriter(OUT) as xw:
    summary.to_excel(xw, 'Summary', index=False)
    EXD.to_excel(xw, 'Exceptions', index=False)
    crop_sum.to_excel(xw, 'Crop_Stage_Validation', index=False)
    dist_sum.to_excel(xw, 'District_Stage_Validation', index=False)
    MOM.to_excel(xw, 'MoM_Recalculation', index=False)
print(summary.to_string(index=False)); print(crop_sum.head(15).to_string(index=False))
