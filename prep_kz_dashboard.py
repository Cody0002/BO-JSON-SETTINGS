"""
prep_kz_dashboard.py — outputs dashboard_data.json
Usage:
    python prep_kz_dashboard.py --file kz_config_20260511.json
    python prep_kz_dashboard.py --folder ./weekly_jsons/
"""
import argparse, glob, json, math, os, re, sys

COUNTRY_PREFIXES = ['ph','pk','bd','mx','id','in','co','pe','eg','br']
COUNTRY_NAMES = {'TH':'Thailand','PH':'Philippines','PK':'Pakistan','BD':'Bangladesh',
    'MX':'Mexico','ID':'Indonesia','IN':'India','CO':'Colombia','PE':'Peru','EG':'Egypt','BR':'Brazil'}

# After stripping country prefix, rename these short names
GROUP_RENAME = {'k1':'kzg1','k2':'kzg2'}

def parse_market(g):
    gl = g.lower()
    for px in sorted(COUNTRY_PREFIXES, key=len, reverse=True):
        if gl.startswith(px):
            gn = gl[len(px):]
            gn = GROUP_RENAME.get(gn, gn)
            return px.upper(), gn
    return 'TH', gl

def _d(v): return v if isinstance(v, dict) else {}
def _l(v): return v if isinstance(v, list) else []
def _cv(conds, name):
    for c in _l(conds):
        if c.get('name')==name and c.get('enabled'): return c.get('expectedValue')
    return None
def _ce(conds, name):
    return any(c.get('name')==name and c.get('enabled') for c in _l(conds))
def _b(v): return 1 if v else 0
def _dt(s):
    if not s or not isinstance(s, str): return None
    return s[:10] if len(s) >= 10 else s
def _sparse(d):
    return {k:v for k,v in d.items() if v is not None and v != '' and v != []}

def _count(v):
    if v is None: return 0
    if isinstance(v, dict):
        if 'count' in v: return v['count']
        return len(v)
    if isinstance(v, list): return len(v)
    return 0

def _extract_config_field(p, field, is_array=True):
    top = p.get(field)
    if top is not None and top != []:
        return top if not is_array else (top if isinstance(top, list) else [])
    cfg = _d(p.get('config'))
    nested = cfg.get(field, {})
    if isinstance(nested, dict) and 'value' in nested:
        return nested['value']
    return [] if is_array else None

def process_week(filepath):
    with open(filepath, encoding='utf-8') as f: raw = json.load(f)
    snap = raw.get('meta',{}).get('snapshotDate','')
    if snap and len(snap)==8: week = f'{snap[:4]}-{snap[4:6]}-{snap[6:]}'
    else:
        m = re.search(r'(\d{8})', os.path.basename(filepath))
        week = f'{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}' if m else 'unknown'

    groups = sorted(raw.get('meta',{}).get('groups',[]))
    av_raw = raw.get('accountVariables',{})
    fp_raw = raw.get('fundingProviders',{})
    fm_raw = raw.get('fundingMethods',{})
    sum_raw = raw.get('summary',{})

    cm = {}
    for g in groups:
        co, gn = parse_market(g)
        cm[g] = {'country':co,'gn':gn}

    # Summary
    summary = {}
    for g in groups:
        s = sum_raw.get(g,{})
        sp=s.get('providers',{}); sd=s.get('depositMethods',{}); sw=s.get('withdrawMethods',{}); wc=s.get('withdrawConfig',{})
        summary[g] = _sparse({
            'pA':sp.get('active',0),'pT':sp.get('total',0),'bal':round(sp.get('totalBalance',0),2),
            'dA':sd.get('active',0),'dI':sd.get('inactive',0),
            'wA':sw.get('active',0),'wI':sw.get('inactive',0),
            'wMin':wc.get('minAmount'),'wMax':wc.get('maxAmount'),
            'auditErr':wc.get('auditErrorWithdraws'),
            'avCount':s.get('accountVariableCount', len(av_raw.get(g,{}))),
        })

    # Providers
    providers = []
    for g in groups:
        for p in fp_raw.get(g,[]):
            dbl = _extract_config_field(p, 'depositBlacklist', is_array=True)
            wbl = _extract_config_field(p, 'withdrawBlacklist', is_array=True)
            oe  = _extract_config_field(p, 'overrideError', is_array=False)
            linked = fm_raw.get(g,{}).get(p['id'],[])
            providers.append(_sparse({
                'g':g,'n':p['name'],'k':p['key'],
                'st':p['status'],'cfg':_b(p.get('configured')),
                'b':round(p['balance'] or 0,2),'sp':p.get('sortPriority',0),
                'show3rd':_b(p.get('show3rdBalance')),
                'cAt':_dt(p.get('createdAt')),'uAt':_dt(p.get('updatedAt')),
                'oe':oe,'dbl':dbl,'wbl':wbl,
                'dm':sum(1 for m in linked if m['type']=='deposit' and m['status']=='active'),
                'wm':sum(1 for m in linked if m['type']=='withdraw' and m['status']=='active'),
            }))

    # Methods
    methods = []
    for g in groups:
        for pid, ms in fm_raw.get(g,{}).items():
            for m in ms:
                rt = m.get('returnType')
                if isinstance(rt, float) and (math.isnan(rt) or math.isinf(rt)): rt = None
                methods.append(_sparse({
                    'g':g,'p':m.get('providerName',''),'key':m.get('key',''),
                    'dn':m['displayName'],'t':m['type'],'method':m['method'],
                    'mn':m['min'],'mx':m['max'],'cur':m.get('currency'),
                    'st':m['status'],'dis':_b(m['disabled']),
                    'passive':_b(m.get('isPassive')),'default':_b(m.get('isDefault')),
                    'proofReq':_b(m.get('proofRequired')),'fpReq':_b(m.get('fundingProfileRequired')),
                    'skipAmt':_b(m.get('skipAmountInput')),
                    'reuseRD':_b(m.get('reuseRepeatDeposit')),'reuseTO':m.get('reuseRepeatDepositTimeOut'),
                    'creditReq':m.get('creditReqAmount'),
                    'retType':rt if not isinstance(rt,float) else None,
                    'cAt':_dt(m.get('createdAt')),'uAt':_dt(m.get('updatedAt')),
                }))

    # AV
    av_rows = []
    for g in groups:
        keys = av_raw.get(g,{})
        row = {'group':g}
        wm = _d(keys.get('withdraw-method')); lims = _d(wm.get('withdrawLimits')); conds = _l(wm.get('conditions'))
        row['wdMin']=lims.get('min'); row['wdMax']=lims.get('max')
        row['auditErrorWithdraws']=wm.get('auditErrorWithdraws')
        row['wdConds']=len(conds); row['wdCondsOn']=sum(1 for c in conds if c.get('enabled'))
        row['wdCondNames']=','.join(c.get('name','') for c in conds if c.get('enabled')) if conds else None
        row['seonFraudScore']=_ce(conds,'seonFraudScore'); row['seonVal']=_cv(conds,'seonFraudScore')
        row['blacklisted']=_ce(conds,'blacklisted'); row['threshold']=_cv(conds,'threshold')
        row['wdAvgDepRate']=_cv(conds,'withdrawToAverageDepositRate'); row['betDepRate24h']=_cv(conds,'betToDepositRate24h')
        row['autoRatio']=round(row['threshold']/row['wdMax']*100,1) if row['threshold'] and row['wdMax'] and row['wdMax']>0 else None
        ds = _d(keys.get('deposit-settings')); pdl = _d(ds.get('pendingDepositLimits')); dur = _d(pdl.get('duration'))
        row['pendingDepositDuration']=dur.get('value',0); row['pendingDepositUnit']=dur.get('unit','minute')
        mfp = _d(keys.get('member-funding-profile-settings'))
        row['banMemberEdit']=mfp.get('banMemberEdit')
        st = _d(mfp.get('supportedTypes')); row['fundingProfileTypes']=','.join(sorted(st.keys())) if st else None
        row['bankCount']=_count(keys.get('bank-infos')); row['ewalletCount']=_count(keys.get('ewallet-infos'))
        row['paymentMethodCount']=_count(keys.get('payment-methods'))
        cbs = _d(keys.get('cash-back-settings'))
        row['dailyCB']=_d(cbs.get('daily')).get('display'); row['weeklyCB']=_d(cbs.get('weekly')).get('display')
        row['monthlyCB']=_d(cbs.get('monthly')).get('display')
        icb = _d(keys.get('instant-cash-back-settings'))
        row['instantCBMinClaim']=icb.get('minimumClaim'); row['instantCBFreqHrs']=icb.get('claimFrequencyInHour')
        row['instantCBProportion']=icb.get('proportionOfTurnover')
        cm2 = _d(keys.get('cashback-mission-settings'))
        row['cbMissionEnabled']=cm2.get('enabled'); row['cbMissionCount']=len(_l(cm2.get('missions')))
        pbs = _d(keys.get('promotion-banner-settings')); row['bannerLayout']=pbs.get('layout'); banners=pbs.get('banners',{}); row['bannerCount']=len(banners) if isinstance(banners,(dict,list)) else 0
        pst2 = _d(keys.get('promotion-settings'))
        row['bottomPromptEnabled']=_d(pst2.get('bottomPrompt')).get('enabled'); row['scrollingTextEnabled']=_d(pst2.get('scrollingText')).get('enabled')
        dbd = _d(keys.get('daily-bonus-disbursement')); row['bonusResetTime']=dbd.get('resetTime')
        dbn = _d(dbd.get('newcomerBoost'))
        row['bonusNewcomerFrom']=dbn.get('from'); row['bonusNewcomerTo']=dbn.get('to'); row['bonusNewcomerCount']=dbn.get('count')
        dbp = _d(keys.get('daily-bonus-popup-settings'))
        row['dailyPopupEnabled']=dbp.get('enabled'); row['dailyPopupThreshold']=dbp.get('threshold'); row['dailyPopupThresholdEnabled']=dbp.get('thresholdEnabled')
        das = _d(keys.get('daily-accrual-settings')); row['accrualItems']=_count(das.get('items')); row['accrualNeedApproval']=das.get('needApproval')
        rs = _d(keys.get('referral-settings')); row['referralMinDep']=rs.get('minDepAmt'); row['referralMinCom']=rs.get('minComAmt'); row['referralVisibility']=rs.get('visibility')
        ric = _d(keys.get('referral-income-counter-settings')); row['referralCounterRate']=ric.get('rateOfChange'); row['referralCounterStart']=ric.get('startingAmount')
        rtb = _d(keys.get('referral-token-bonus')); row['referralTokenEnabled']=rtb.get('enabled')
        rtd = _d(keys.get('referral-token-disbursement')); row['referralTokenSwThreshold']=rtd.get('secretWalletThreshold')
        comp = _d(keys.get('compensation-settings')); nb = _d(comp.get('newcomerBoost'))
        row['compNewcomerFrom']=nb.get('from'); row['compNewcomerTo']=nb.get('to'); row['compNewcomerCount']=nb.get('count')
        tiers = _d(_d(comp.get('disbursementSetting')).get('tiers'))
        row['compBigProb']=_d(tiers.get('big')).get('probabilityBasisPoints')
        row['compMedProb']=_d(tiers.get('medium')).get('probabilityBasisPoints')
        row['compSmallProb']=_d(tiers.get('small')).get('probabilityBasisPoints')
        scs = _d(keys.get('secret-code-settings')); row['secretCodeGroups']=len(_l(_d(scs.get('friendParticipation')).get('eligibleGroups')))
        row['giftBoxEnabled']=_d(keys.get('deposit-gift-box-settings')).get('enabled')
        csp = keys.get('controlled-secret-promo'); row['controlledSecretPromo']=csp if isinstance(csp,bool) else None
        row['playSpinEnabled']=_d(keys.get('play-spin-settings')).get('enabled')
        row['skinnerBoxEnabled']=_d(keys.get('skinner-box-settings')).get('enabled')
        ms2 = _d(keys.get('mega-share-settings')); row['megaShareEnabled']=ms2.get('enabled'); row['megaShareDefaultMethod']=ms2.get('defaultSharingMethod')
        aa = _d(keys.get('auto-alliance-settings')); row['allianceEnabled']=aa.get('enabled'); row['allianceCooldownHrs']=aa.get('cooldownHours')
        row['allianceBadges']=len(_l(aa.get('badgeIds'))); row['allianceAccounts']=len(_l(aa.get('accountIds'))); row['allianceGroups']=len(_l(aa.get('groupIds')))
        row['secretWalletBonus']=_d(keys.get('secret-wallet-settings')).get('referralBonus')
        mst = _d(keys.get('member-settings')); row['authMode']=mst.get('authMode'); row['nameMode']=mst.get('nameMode')
        row['fundingMode']=mst.get('fundingMode'); row['googleAuth']=mst.get('googleAuth')
        row['depositMethodGrouping']=mst.get('depositMethodGrouping'); row['playerNameCollectionMode']=mst.get('playerNameCollectionMode')
        pic = mst.get('playerInfoCollections',[])
        if isinstance(pic,list):
            if pic and isinstance(pic[0],dict): row['playerInfo']=','.join(p.get('type','') for p in pic)
            elif pic and isinstance(pic[0],str): row['playerInfo']=','.join(pic)
        row['domainUrl']=_d(keys.get('account-info')).get('domainUrl')
        ns = _d(keys.get('naming-settings')); row['isLastNameValid']=ns.get('isLastNameValid') if ns else None
        row['blockDesktopAccess']=_d(keys.get('security-settings')).get('blockDesktopAccess')
        seon = _d(keys.get('seon-settings'))
        if 'excludedGroupCount' in seon:
            row['seonExcludedGroups'] = seon['excludedGroupCount']
        elif 'excludedGroupIds' in seon:
            row['seonExcludedGroups'] = len(_l(seon.get('excludedGroupIds')))
        else:
            row['seonExcludedGroups'] = 0
        row['analyticsCategories']=len(_d(_d(keys.get('analytics-settings')).get('categoryGroups')))
        row['paranoidWD']=keys.get('paranoid-withdraw-handling') if isinstance(keys.get('paranoid-withdraw-handling'),bool) else None
        row['autoSlideDuration']=_d(keys.get('banner-settings')).get('autoSlideDuration')
        gst = _d(keys.get('game-site-theme-config')); row['themeColor']=gst.get('color'); row['hasThemeConfig']=bool(gst)
        row['hasContactIcon']=bool(_d(keys.get('contact-group-icon')))
        cl = _d(keys.get('contact-link')); row['contactLink']=cl.get('link') if cl else None
        mcps = _d(keys.get('mini-cashback-prompt-settings'))
        row['miniCBEnabled']=mcps.get('enabled'); row['miniCBCooldown']=mcps.get('promptCooldownSeconds')
        row['miniCBWalletThreshold']=mcps.get('walletBalanceThreshold')
        row['miniCBCashbackThreshold']=mcps.get('claimableCashbackThreshold'); row['miniCBCountThreshold']=mcps.get('claimedCashbackCountThreshold')
        el = _d(keys.get('external-links')); elx = _d(el.get('externalLinks'))
        row['hasVipLink']=bool(elx.get('vip')); row['slotsHackVisibility']=elx.get('slotsHackVisibility')
        row['turnoverUtcOffset']=keys.get('turnover-utc-offset')
        row['smsProvider']=_d(keys.get('sms-provider')).get('provider')
        tc = _d(keys.get('terms-and-conditions')); row['tncLanguages']=','.join(sorted(tc.keys())) if tc else None
        # ── Nested: Bank codes (filter by visibility if any bank has True/False; show all if all null) ──
        bi_raw = keys.get('bank-infos')
        if isinstance(bi_raw, dict) and 'count' not in bi_raw:
            has_real_vis = any(v.get('visibility') is not None for v in bi_raw.values() if isinstance(v,dict))
            if has_real_vis:
                vb = [v.get('code') or k for k,v in bi_raw.items() if isinstance(v,dict) and v.get('visibility') is not False]
            else:
                vb = [v.get('code') or k for k,v in bi_raw.items() if isinstance(v,dict)]
            row['visibleBanks'] = ','.join(sorted(vb)) if vb else None
            row['bankTotal'] = len(bi_raw); row['bankVisible'] = len(vb)
        elif isinstance(bi_raw, dict) and 'count' in bi_raw:
            row['bankTotal'] = bi_raw['count']

        # ── Nested: E-wallet names (filter by visibility if any has True/False; show all if all null) ──
        ei_raw = keys.get('ewallet-infos')
        if isinstance(ei_raw, dict) and 'count' not in ei_raw:
            has_real_vis_ew = any(v.get('visibility') is not None for v in ei_raw.values() if isinstance(v,dict))
            if has_real_vis_ew:
                ew = [v.get('name') or k for k,v in ei_raw.items() if isinstance(v,dict) and v.get('visibility') is not False]
            else:
                ew = [v.get('name') or k for k,v in ei_raw.items() if isinstance(v,dict)]
                if not ew: ew = list(ei_raw.keys())
            row['ewalletNames'] = ','.join(sorted(ew)) if ew else None
        elif isinstance(ei_raw, dict) and 'count' in ei_raw:
            pass

        # ── Nested: Analytics settings breakdown ──
        anl_raw = keys.get('analytics-settings')
        if isinstance(anl_raw, dict) and 'categoryGroups' in anl_raw:
            cg = anl_raw.get('categoryGroups', {})
            total_rules = 0
            metric_counts = {}
            rules_detail = []
            def _fmt_range(ops):
                # Turn operators into a readable range, e.g. ">= -10% and < 10%"
                parts = []
                sym = {'gte':'\u2265','gt':'>','lte':'\u2264','lt':'<','eq':'=','ne':'\u2260'}
                for o in ops:
                    op = sym.get(o.get('op') or o.get('operator'), o.get('op') or o.get('operator') or '')
                    v = o.get('val') if o.get('val') is not None else o.get('expectedValue')
                    if isinstance(v, (int, float)):
                        # ratios are fractions → show as %
                        vs = f'{v*100:g}%' if -10 <= v <= 10 else f'{v:g}'
                    else:
                        vs = str(v)
                    parts.append(f'{op} {vs}')
                return ' and '.join(parts)
            for cgv in cg.values():
                conds = cgv.get('conditions', []) if isinstance(cgv, dict) else []
                total_rules += len(conds)
                for c in conds:
                    mk = c.get('key', '')
                    if mk: metric_counts[mk] = metric_counts.get(mk, 0) + 1
                    rules_detail.append({
                        'metric': mk,
                        'range': _fmt_range(c.get('operators', [])),
                        'note': (c.get('helperText') or '').strip(),
                    })
            # Dedupe rules_detail. The same condition repeats across category-group
            # tiers, sometimes with different thresholds. We key distinct RULE TYPES by
            # (metric, note); when tiers disagree on threshold we keep the first range
            # but flag that tiers vary, so the count matches the Analytics comparison view.
            def _nn(s): return (s or '').strip().rstrip('.').strip().lower()
            _seen = {}; distinct_detail = []
            for rdt in rules_detail:
                k = (rdt['metric'], _nn(rdt['note']))
                if k not in _seen:
                    _seen[k] = rdt; distinct_detail.append(rdt)
            _dmc = {}
            for rdt in distinct_detail:
                if rdt['metric']: _dmc[rdt['metric']] = _dmc.get(rdt['metric'],0)+1
            row['analyticsCatGroups'] = len(cg)
            row['analyticsRules'] = len(distinct_detail)       # distinct rule types
            row['analyticsRulesRaw'] = total_rules             # raw incl. tier repeats
            row['analyticsMetrics'] = len(_dmc)
            row['analyticsSummary'] = ' | '.join(f'{k}:{v}' for k,v in sorted(_dmc.items())) if _dmc else None
            row['analyticsRulesDetail'] = rules_detail if rules_detail else None
        elif isinstance(anl_raw, dict) and 'categoryGroups_count' in anl_raw:
            row['analyticsCatGroups'] = anl_raw['categoryGroups_count']

        # ── Nested: Referral commission levels ──
        rs2 = _d(keys.get('referral-settings'))
        comms = _d(rs2.get('commissions'))
        if comms:
            row['referralLevels'] = len(comms)
            row['referralCommLevels'] = ','.join(sorted(comms.keys()))

        # ── Nested: Banner names from promotion-banner-settings ──
        banner_raw = pbs.get('banners', {})
        if isinstance(banner_raw, dict) and banner_raw:
            row['bannerNames'] = ','.join(sorted(banner_raw.keys()))

        av_rows.append(_sparse(row))

    # Validation
    _s = {'dbl':0,'wbl':0,'oe':0}
    for p in providers:
        if p.get('dbl'): _s['dbl']+=1
        if p.get('wbl'): _s['wbl']+=1
        if p.get('oe'): _s['oe']+=1
    print(f'    Providers: {len(providers)} (dbl={_s["dbl"]}, wbl={_s["wbl"]}, oe={_s["oe"]})')
    print(f'    Methods: {len(methods)}, AV: {len(av_rows)}')

    countries = sorted(set(v['country'] for v in cm.values()))
    return {'week':week,'groups':groups,'countries':countries,'countryMap':cm,
            'summary':summary,'providers':providers,'methods':methods,'av':av_rows}

def sanitize(obj):
    if isinstance(obj, float): return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict): return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    return obj

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--folder'); ap.add_argument('--file'); ap.add_argument('--out',default='dashboard_data.json')
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.folder,'*.json'))) if args.folder else [args.file] if args.file else []
    if not files: ap.error('Provide --folder or --file')
    weeks = []
    for fp in files:
        print(f'Processing {os.path.basename(fp)}...')
        r = process_week(fp); weeks.append(r)
        print(f'  {r["week"]}: {len(r["providers"])} prov, {len(r["methods"])} methods')
    weeks.sort(key=lambda w:w['week'])
    weeks = sanitize(weeks)
    with open(args.out,'w',encoding='utf-8') as f:
        json.dump(weeks, f, separators=(',',':'), allow_nan=False)
    print(f'\nOK {args.out} ({os.path.getsize(args.out)/1024:.0f} KB, {len(weeks)} week(s))')

if __name__=='__main__': main()