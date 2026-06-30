"""
extract_kz_config.py — CSVs (or ZIP) → single JSON

KEY FIX: The CSV has 13 platform-level keys and 32 account-level keys.
Account-level keys (bank-infos, ewallet-infos, security-settings, etc.)
are per-user. We merge dicts across accounts, take first for simple values.

Usage:
    python extract_kz_config.py --zip 20260511_kz-group-config.zip --out kz_config_20260511.json
    python extract_kz_config.py --av AV.csv --fp FP.csv --fm FM.csv --out kz_config.json
"""
import argparse, json, math, os, re, zipfile
from datetime import datetime, timezone
import pandas as pd

def _safe_json(raw):
    if pd.isna(raw) or not str(raw).strip(): return None
    try: return json.loads(str(raw))
    except: return None

def _parse_dt(val):
    if pd.isna(val): return None
    s = str(val).strip()
    m = re.match(r"\w+ (\w+ \d+ \d+ \d+:\d+:\d+) GMT", s)
    if m:
        try: return datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S").isoformat()
        except: pass
    return s

def _read_csv(source_map, hint):
    for name, df in source_map.items():
        if hint.lower() in name.lower(): return df
    raise KeyError(f"No CSV matching '{hint}' in: {list(source_map)}")

def load_csvs(av_path=None, fp_path=None, fm_path=None, zip_path=None):
    if zip_path:
        src = {}
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.endswith('.csv'):
                    with zf.open(name) as f:
                        src[os.path.basename(name)] = pd.read_csv(f, low_memory=False)
        return _read_csv(src,'account variable'), _read_csv(src,'funding provider'), _read_csv(src,'funding method')
    return pd.read_csv(av_path, low_memory=False), pd.read_csv(fp_path, low_memory=False), pd.read_csv(fm_path, low_memory=False)

# Drop entirely (large HTML blobs, no dashboard value)
AV_DROP_KEYS = {'referral-instructions', 'cash-back-tnc'}

# Keys where account-level dicts should be MERGED across accounts
AV_MERGE_KEYS = {'bank-infos', 'ewallet-infos', 'payment-methods'}

def _slim_content(key, content):
    """Reduce heavy nested content to dashboard-useful fields only."""
    if not isinstance(content, dict):
        return content

    if key == 'analytics-settings':
        cg = content.get('categoryGroups', {})
        def _slim_cond(c):
            ops = c.get('operators', [])
            return {
                'key': c.get('key'),
                'operators': [{'op': o.get('operator'), 'val': o.get('expectedValue')} for o in ops],
                'helperText': c.get('helperText'),
            }
        return {
            'categoryGroups': {
                cgk: {'conditions': [_slim_cond(c) for c in cgv.get('conditions',[])]}
                for cgk, cgv in cg.items() if isinstance(cgv, dict)
            }
        }
    if key == 'bank-infos':
        return {
            k: {'code':v.get('code'), 'name':v.get('name'), 'visibility':v.get('visibility')}
            for k, v in content.items() if isinstance(v, dict)
        }
    if key == 'ewallet-infos':
        return {
            k: {'name':v.get('name'), 'code':v.get('code'), 'visibility':v.get('visibility')}
            for k, v in content.items() if isinstance(v, dict)
        }
    if key == 'payment-methods':
        return {'count': len(content)}
    if key == 'terms-and-conditions':
        return {lang: True for lang in content.keys()}
    if key == 'seon-settings':
        eids = content.get('excludedGroupIds', [])
        return {'excludedGroupCount': len(eids) if isinstance(eids, list) else 0}
    return content


def extract_account_variables(df_av):
    """
    Extract AV content handling both platform and account-level keys.
    - Platform keys (13): 1 row per group, extract directly
    - Account keys (32): multiple rows per group, merge dicts / take first
    Returns (av_dict, av_key_counts).
    """
    # Count ALL unique keys per group for accurate avCount
    all_key_counts = {}
    for _, row in df_av.iterrows():
        g = row['group']
        all_key_counts.setdefault(g, set()).add(row['key'])

    # Separate platform vs account level
    is_platform = df_av['accountId'] == 'platform' if 'accountId' in df_av.columns else pd.Series([True]*len(df_av))

    result = {}

    # 1. Platform-level keys (1 row per group+key)
    for _, row in df_av[is_platform].iterrows():
        group, key = row['group'], row['key']
        if key in AV_DROP_KEYS: continue
        content = _safe_json(row.get('content'))
        content = _slim_content(key, content)
        result.setdefault(group, {})[key] = content

    # 2. Account-level keys (multiple rows per group+key)
    acct_df = df_av[~is_platform]
    for (group, key), chunk in acct_df.groupby(['group', 'key']):
        if key in AV_DROP_KEYS: continue
        if key in result.get(group, {}): continue  # platform version exists, skip

        if key in AV_MERGE_KEYS:
            # Merge dicts across accounts (bank-infos, ewallet-infos)
            # Prefer entries with non-null visibility over null ones
            merged = {}
            for _, row in chunk.iterrows():
                content = _safe_json(row.get('content'))
                if isinstance(content, dict):
                    for k, v in content.items():
                        if k not in merged:
                            merged[k] = v
                        elif isinstance(v, dict) and v.get('visibility') is not None and \
                             isinstance(merged[k], dict) and merged[k].get('visibility') is None:
                            merged[k] = v  # upgrade: null visibility → real visibility
            result.setdefault(group, {})[key] = _slim_content(key, merged)
        else:
            # Take first non-null value
            for _, row in chunk.iterrows():
                content = _safe_json(row.get('content'))
                if content is not None:
                    result.setdefault(group, {})[key] = _slim_content(key, content)
                    break

    key_counts = {g: len(keys) for g, keys in all_key_counts.items()}
    return result, key_counts


def extract_funding_providers(df_fp):
    result = {}
    for _, row in df_fp.iterrows():
        group = row['group']
        cfg = _safe_json(row.get('config')) or {}
        dbl = cfg.get('depositBlacklist', {}); wbl = cfg.get('withdrawBlacklist', {})
        dbl_val = dbl.get('value', []) if isinstance(dbl, dict) else []
        wbl_val = wbl.get('value', []) if isinstance(wbl, dict) else []
        oe = cfg.get('overrideError', {}); oe_val = oe.get('value') if isinstance(oe, dict) else oe
        provider = {
            'id':row['id'],'key':row['key'],'name':row['name'],'status':row['status'],
            'configured':bool(row.get('configured')) if pd.notna(row.get('configured')) else None,
            'balance':float(row['balance']) if pd.notna(row.get('balance')) else None,
            'lastSyncedAt':_parse_dt(row.get('lastSyncedAt')),
            'sortPriority':int(row['sortPriority']) if pd.notna(row.get('sortPriority')) else None,
            'show3rdBalance':bool(row.get('show3rdBalance')) if pd.notna(row.get('show3rdBalance')) else None,
            'createdAt':_parse_dt(row.get('createdAt')),'updatedAt':_parse_dt(row.get('updatedAt')),
            'overrideError':oe_val,'depositBlacklist':dbl_val,'withdrawBlacklist':wbl_val,
        }
        result.setdefault(group, []).append(provider)
    for group in result:
        result[group].sort(key=lambda p: (p['sortPriority'] is None, p['sortPriority'] or 0))
    return result


def extract_funding_methods(df_fm, providers_by_group):
    pname = {}
    for group, ps in providers_by_group.items():
        for p in ps: pname[p['id']] = p['name']
    result = {}
    for _, row in df_fm.iterrows():
        group = row['group']; pid = row.get('fundingProviderId')
        rt = row.get('returnType')
        if isinstance(rt, float) and (math.isnan(rt) or math.isinf(rt)): rt = None
        method = {
            'id':row['id'],'fundingProviderId':pid,'providerName':pname.get(str(pid)),
            'type':row['type'],'key':row['key'],'method':row['method'],'displayName':row['name'],
            'min':float(row['min']) if pd.notna(row.get('min')) else None,
            'max':float(row['max']) if pd.notna(row.get('max')) else None,
            'currency':row.get('currency') if pd.notna(row.get('currency')) else None,
            'sort':int(row['sort']) if pd.notna(row.get('sort')) else None,
            'status':row['status'],'disabled':bool(row.get('disabled')),
            'isDefault':bool(row.get('isDefault')),'isPassive':bool(row.get('isPassive')),
            'proofRequired':bool(row.get('proofRequired')),'skipAmountInput':bool(row.get('skipAmountInput')),
            'fundingProfileRequired':bool(row.get('fundingProfileRequired')),
            'reuseRepeatDeposit':bool(row.get('reuseRepeatDeposit')),
            'reuseRepeatDepositTimeOut':int(row['reuseRepeatDepositTimeOut']) if pd.notna(row.get('reuseRepeatDepositTimeOut')) else None,
            'creditReqAmount':float(row['creditReqAmount']) if pd.notna(row.get('creditReqAmount')) else None,
            'returnType':rt if pd.notna(rt) else None,
            'createdAt':_parse_dt(row.get('createdAt')),'updatedAt':_parse_dt(row.get('updatedAt')),
        }
        result.setdefault(group, {}).setdefault(str(pid), []).append(method)
    for group in result:
        for pid in result[group]:
            result[group][pid].sort(key=lambda m: (m['sort'] is None, m['sort'] or 0))
    return result


def build_summary(providers, methods, av, av_key_counts):
    summary = {}
    for group in set(list(providers) + list(methods) + list(av)):
        ps = providers.get(group, [])
        all_m = [m for ms in methods.get(group, {}).values() for m in ms]
        act_p = [p for p in ps if p['status'] == 'active']
        act_d = [m for m in all_m if m['type'] == 'deposit' and m['status'] == 'active']
        act_w = [m for m in all_m if m['type'] == 'withdraw' and m['status'] == 'active']
        wc = (av.get(group, {}).get('withdraw-method') or {})
        summary[group] = {
            'providers': {'total':len(ps),'active':len(act_p),'inactive':len(ps)-len(act_p),
                'totalBalance':round(sum(p['balance'] or 0 for p in act_p),2)},
            'depositMethods': {'active':len(act_d),'inactive':len([m for m in all_m if m['type']=='deposit' and m['status']!='active'])},
            'withdrawMethods': {'active':len(act_w),'inactive':len([m for m in all_m if m['type']=='withdraw' and m['status']!='active'])},
            'withdrawConfig': {
                'minAmount':(wc.get('withdrawLimits') or {}).get('min'),
                'maxAmount':(wc.get('withdrawLimits') or {}).get('max'),
                'auditErrorWithdraws':wc.get('auditErrorWithdraws')},
            'accountVariableCount': av_key_counts.get(group, 0),
        }
    return summary


def sanitize(obj):
    if isinstance(obj, float): return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict): return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    return obj


def extract(av_path=None, fp_path=None, fm_path=None, zip_path=None, out_path='kz_config.json'):
    print('Loading CSVs...')
    df_av, df_fp, df_fm = load_csvs(av_path, fp_path, fm_path, zip_path)
    print(f'  Account Variables : {len(df_av):,} rows')
    print(f'  Funding Providers : {len(df_fp):,} rows')
    print(f'  Funding Methods   : {len(df_fm):,} rows')

    if 'accountId' in df_av.columns:
        plat = (df_av['accountId']=='platform').sum()
        acct = len(df_av) - plat
        print(f'  AV breakdown: {plat} platform + {acct} account-level rows')

    print('Extracting...')
    av, av_key_counts = extract_account_variables(df_av)
    providers = extract_funding_providers(df_fp)
    methods = extract_funding_methods(df_fm, providers)
    summary = build_summary(providers, methods, av, av_key_counts)

    date_m = re.search(r'(\d{8})', out_path)
    snap = date_m.group(1) if date_m else datetime.today().strftime('%Y%m%d')

    output = sanitize({
        'meta': {'snapshotDate':snap,'extractedAt':datetime.now(timezone.utc).isoformat(),
                 'groups':sorted(set(list(providers)+list(av)))},
        'summary': summary,
        'accountVariables': av,
        'fundingProviders': providers,
        'fundingMethods': methods,
    })

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',',':'), default=str, allow_nan=False)

    size_mb = os.path.getsize(out_path) / 1e6
    print(f'\n✓ {out_path} ({size_mb:.1f} MB)')
    # Validation
    for g in sorted(output['meta']['groups'])[:3]:
        gd = av.get(g,{})
        bi = gd.get('bank-infos',{})
        ei = gd.get('ewallet-infos',{})
        anl = gd.get('analytics-settings',{})
        tnc = gd.get('terms-and-conditions',{})
        sec = gd.get('security-settings',{})
        pw = gd.get('paranoid-withdraw-handling')
        bi_n = len(bi) if isinstance(bi,dict) else 0
        ei_n = len(ei) if isinstance(ei,dict) else 0
        anl_n = len(anl.get('categoryGroups',{})) if isinstance(anl,dict) else 0
        print(f'  {g}: keys={len(gd)}, banks={bi_n}, ewallets={ei_n}, analytics={anl_n}, tnc={list(tnc.keys()) if isinstance(tnc,dict) else tnc}, sec={sec}, paranoid={pw}')
    print(f'  ... {len(output["meta"]["groups"])} groups total')
    return output


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--av'); ap.add_argument('--fp'); ap.add_argument('--fm')
    ap.add_argument('--zip'); ap.add_argument('--out', default='kz_config.json')
    args = ap.parse_args()
    if not args.zip and not (args.av and args.fp and args.fm):
        ap.error('Provide --zip OR all three --av / --fp / --fm')
    extract(av_path=args.av, fp_path=args.fp, fm_path=args.fm, zip_path=args.zip, out_path=args.out)