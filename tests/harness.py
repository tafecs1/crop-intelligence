"""Synthetic, Earth-Engine-free harness: runs cells 2-12 (Excel part) of crop_intel.py on 10 made-up tehsils."""
import re, sys, os, math, json, random, datetime as dt, collections, types
import pandas as pd, numpy as np
def build(path, work='/tmp/cropintel_test'):
    src = open(path).read()
    parts = re.split(r'^(?=# ===== CELL )', src, flags=re.M)
    code = {m.group(1): c for c in parts for m in [re.match(r'# ===== CELL (\w+):', c)] if m}
    ns = {}
    exec("import json, os, re, time, math, hashlib, datetime as dt\nfrom collections import Counter\nimport numpy as np, pandas as pd\n", ns)
    exec(code['2'], ns); ns['RUN_DATE']='2026-11-19'; ns['GOVT_APY_CSV']='/tmp/none.csv'
    os.makedirs(f'{work}/crop_intel', exist_ok=True)
    pd.DataFrame([dict(tehsil_lgd=5003, crop='Rice', stage='Heading / flowering', observation_date='2026-11-15', planting_date='')]).to_csv(f'{work}/crop_intel/fo.csv', index=False)
    ns['FIELD_OBS_CSV']=f'{work}/crop_intel/fo.csv'
    exec(code['4'], ns); exec(code['5'], ns); exec(code['5b'], ns)
    import requests; ns['requests']=requests
    ns['IDX6']=['NDVI','EVI','NDWI','NDRE','SAVI','BSI']; ns['FILL']='Gap-filled (Estimated)'
    # 7b definitions only
    c7b = code['7b']; exec(c7b[:c7b.index('if USE_SPAM:')], ns)
    c7b2 = code['7b2']; exec(c7b2[:c7b2.index('if USE_CROPGRIDS and not SPAM_TEH:')], ns)
    TEH = pd.DataFrame([dict(state='Tamil Nadu' if i<8 else 'Punjab', state_lgd=33 if i<8 else 3, district=f'D{i%3}' if i<8 else 'Ludhiana', district_lgd=600+i%3 if i<8 else 40, tehsil=f'T{i}', tehsil_lgd=5000+i) for i in range(10)])
    N=ns['norm_code']; TEH['lgd'],TEH['slgd'],TEH['dlgd']=TEH.tehsil_lgd.map(N),TEH.state_lgd.map(N),TEH.district_lgd.map(N)
    TEH['dkey']=TEH.state.map(ns['norm_state'])+'|'+TEH.district.map(ns['norm_name']); TEH['gkey']=TEH.dlgd
    TODAY=ns['TODAY']; TE=ns['TREND_ENDS']; S1S=ns['S1_START']; BINS=ns['RICE_BINS']
    print('RICE_BINS:', [(str(S1S+dt.timedelta(days=x)), str(S1S+dt.timedelta(days=y-1))) for x,y in BINS])
    def curve(kind):
        if kind=='rice':   return [0.35,0.34,0.32,0.30,0.28,0.27,0.26,0.27,0.30,0.38,0.48,0.56]   # like real delta data: rise from late Oct
        if kind=='mixed':  return [0.33,0.32,0.27,0.24,0.23,0.21,0.22,0.26,0.22,0.24,0.45,0.58]
        if kind=='maize':  return [0.2,0.2,0.19,0.18,0.18,0.2,0.24,0.3,0.38,0.45,None,0.55]
        if kind=='flat':   return [0.6]*12
        if kind=='cloudy': return [None]*9+[0.4,0.42,None]
        if kind=='stress': return [0.2,0.18,0.15,0.2,0.3,0.45,0.55,0.6,0.5,0.4,0.3,0.25]
        if kind in ('recent','wheatmap'): return [0.3]*12
        if kind=='pbwheat': return [0.3,0.5,0.7,0.8,0.75,0.6,0.4,0.25,0.22,0.21,0.22,0.24]
        if kind=='pbriceharv': return [0.3,0.5,0.7,0.8,0.75,0.6,0.4,0.25,0.22,0.21,0.22,0.24]
    VH = {'rice':[-14,-14.5,-15,-16,-18,-21,-22,-22,-21,-19,-17,-15.5], 'stress':[-15,-15,-21,-22,-20,-18,-16,-15,-15.5,-16,-17,-18],
          'mixed':[-16,-17,-18,-19,-20,-21,-21,-21,-20,-19,-17,-15.5]}
    SAT_MONTHLY,SAT_TREND,SAT_CUR,SAT_MAPS,SAT_WX={},{},{},{},{}
    kinds=['rice','mixed','maize','flat','cloudy','stress','recent','wheatmap','pbwheat','pbriceharv']
    for i,lgd in enumerate(TEH.lgd):
        SAT_MONTHLY[lgd]={**{f'{x}_{ym}':0.3+0.2*math.sin(k/2) for k,ym in enumerate(ns['YM']) for x in ns['IDX6']}, **{f'valid_{ym}':0.6 for ym in ns['YM']}}
        k=kinds[i]; c=curve(k); isr = k in ('rice','stress','mixed')
        SAT_TREND[lgd]={**{f'NDVI_T{j:02d}':c[j] for j in range(12)},
                        **{f'NDVIRICE_T{j:02d}':(c[j] if isr else None) for j in range(12)},
                        **{f'EVIRICE_T{j:02d}':((c[j]*0.7) if isr and c[j] is not None else None) for j in range(12)},
                        **{f'VHRICE_T{j:02d}':(VH[k][j] if isr else None) for j in range(12)},
                        **{f'NDVIMZ_T{j:02d}':(c[j] if k=='maize' else None) for j in range(12)}, **{f'NDVIOTH_T{j:02d}':([0.2,0.2,0.19,0.18,0.18,0.2,0.24,0.3,0.38,0.45,0.5,0.55][j] if i in (0,1) else None) for j in range(12)}, **{f'NDVIWC_T{j:02d}':(c[j] if k in ('pbwheat','wheatmap') else None) for j in range(12)}, **{f'EVIWC_T{j:02d}':((c[j]*0.7) if k in ('pbwheat','wheatmap') else None) for j in range(12)},
                        **{f'EVIMZ_T{j:02d}':((c[j]*0.7) if k=='maize' and c[j] is not None else None) for j in range(12)}}
        SAT_CUR[lgd]=dict(area_ha=20000,crop_share=0.6,valid_CUR=0.15 if k=='cloudy' else 0.55,NDVI_CUR=0.5,EVI_CUR=.3,NDRE_CUR=.2,NDMI_CUR=.1,NDWI_CUR=-.3,BSI_CUR=-0.1,
                          s2_scenes=5,s1_scenes=4,s1_VV=-10,s1_VH=-16,VV_VH=6, s2_day_max=(25 if k=='cloudy' else 57), s2_day_median=(20 if k=='cloudy' else 55), s1_day_max=58)
        bi = lambda d: next(j for j,(a,b) in enumerate(BINS) if a <= (d-S1S).days < b)
        coh = {'rice':{bi(dt.date(2026,10,8)):0.6, bi(dt.date(2026,10,20)):0.3, bi(dt.date(2026,9,25)):0.1}, 'stress':{bi(dt.date(2026,9,20)):1.0},
               'mixed':{bi(dt.date(2026,6,10)):0.15, bi(dt.date(2026,7,10)):0.15, bi(dt.date(2026,8,20)):0.25, bi(dt.date(2026,10,10)):0.2, bi(dt.date(2026,10,25)):0.25}}.get(k,{})
        mp=dict(rice_frac=0.15 if isr else 0, rice_recent_frac=0.05 if k=='recent' else 0, rice_frac_v30=0.5,
            rice_recent_t=((dt.date(2026,11,2)-S1S).days if k=='recent' else None), rice_s1_obs=9, rice_dw_frac=0.13 if isr else 0, mz_dw_frac=0,
            wc_frac=0.1 if k=='wheatmap' else 0.3 if k=='pbwheat' else 0, wc_active_frac=0.08 if k=='wheatmap' else 0.25 if k=='pbwheat' else 0, wc_conf=None,
            rice_harv_frac=0.4 if k=='pbriceharv' else 0, rice_harv_t=((dt.date(2026,6,20)-S1S).days if k=='pbriceharv' else None),
            mz_frac=0.05 if k=='maize' else 0, mz_active_frac=0.04 if k=='maize' else 0,
            per_frac=0.05 if k=='flat' else 0, rice_pend_frac=0.02 if k=='mixed' else 0, active_frac=0.4, noncrop_veg_frac=0.2, water_frac=0.03, dw_crop_frac=0.4, dw_obs=6)
        for j in range(len(BINS)): mp[f'rice_coh_{j:02d}'] = 0.15*coh.get(j,0)
        SAT_MAPS[lgd]=mp
        wx={'era_rain':350,'era_rain_ly':300,'era_rain_norm':420,'heavy_rain_days':1,'t30':26.5,'t30_norm':26.0}
        for d in range(240):
            day=dt.date(2026,11,10)-dt.timedelta(days=d); wx[f'{day:%Y%m%d}_tmin']=22.0; wx[f'{day:%Y%m%d}_tmax']=31.0
        SAT_WX[lgd]=wx
    ns.update(TEH=TEH, SAT_MONTHLY=SAT_MONTHLY, SAT_TREND=SAT_TREND, SAT_CUR=SAT_CUR, SAT_MAPS=SAT_MAPS, SAT_WX=SAT_WX,
              GEO_AREA_HA={l:20000.0 for l in TEH.lgd}, _era_last=dt.date(2026,11,10), _ch_last=None, RAIN_LABEL='x', CHIRPS_LAST=None, RAIN_PERIOD='x')
    ns['SPAM_TEH'].update({l:{'grou':2900,'opul':800,'cott':300,'sugc':300,'cnut':900,'rice':2000,'chic':50} for l in list(TEH.lgd)[:2]})
    ns['SPAM_TEH'][TEH.lgd[8]]={'whea':5000,'rice':4000,'rape':300}
    ns.update(MIX_SRC='Modelled (CROPGRIDS 2020)', MIX_NAME='CROPGRIDS 2020', MIX_REF='CROPGRIDS')
    ns['SPAM_INFO']='IFPRI SPAM 2020 test'
    ns['WORK_DIR']=work; ns['DRIVE_OK']=True
    exec(code['7c'], ns)
    ns['GG'].update({l:{'wwh':[('ir',305,120)],'ri1':[('ir',170,290)]} for l in TEH.lgd[8:]})
    for cname in ('8','9','10','11','11b'): exec(code[cname], ns)

    c12 = code['12']; x = c12[c12.index('def clean(v):'):c12.index('def get_ws')] + c12[c12.index('TABS = ['):c12.index("print('💾 Excel")]
    exec(x, ns)
    return ns
