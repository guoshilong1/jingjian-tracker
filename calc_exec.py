#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
镜检行动 · 执行力扣分定时脚本
由 GitHub Actions 在 每周日 23:00（type=submit）与 每周三 23:00（type=track）调用。

逻辑：
  submit：本周（周一至今）未提交过镜检的门店，各扣 1 分。
  track ：上周（上周一至上周日）被检查门店的问题，若全部仍为「待整改」，
         即视为未复盘追踪，该门店扣 1 分。

注意：门店列表需与前端 STORE_LIST 保持一致（变更门店时同步此文件）。
"""
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://ltjmbmgofjqeezoskqrc.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_KrCOzlUPTcrb8S4-ajTjww_EhKibtaC')
TABLE_MIRROR = 'mirror_checks'
TABLE_EXEC = 'execution_deductions'

# 与前端 STORE_LIST 保持一致（如门店变动请同步）
STORE_LIST = [
    '长治4店（壶关五八八广场）',
    '太原16店（万象城）',
    '太原15店（钟楼街）',
    '太原17店（昌盛万达广场）',
    '太原7店（康达时光里）',
    '三门峡3店（灵宝市天基茂业店）',
    '长治5店（金威超市世纪城店）',
    '太原14店（铜锣湾国际购物中心）',
    '太原3店（北美新天地购物中心）',
    '太原19店（泰享里）',
]


def _headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': 'Bearer ' + SUPABASE_KEY,
        'Content-Type': 'application/json',
        'User-Agent': 'jingjian-exec-cron',
    }


def supabase_get(path, params=''):
    url = f'{SUPABASE_URL}/rest/v1/{path}?{params}'
    req = urllib.request.Request(url, headers=_headers())
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode('utf-8')), None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}: {e.read().decode("utf-8","ignore")[:300]}'
    except Exception as e:
        return None, str(e)


def supabase_insert(path, rows):
    url = f'{SUPABASE_URL}/rest/v1/{path}'
    body = json.dumps(rows).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=_headers(), method='POST')
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.read().decode('utf-8'), None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}: {e.read().decode("utf-8","ignore")[:300]}'
    except Exception as e:
        return None, str(e)


def monday_of(d):
    """返回 d 所在自然周的周一 00:00（本地时间）。"""
    x = d.replace(hour=0, minute=0, second=0, microsecond=0)
    day = (x.weekday())  # 0=周一
    x = x - timedelta(days=day)
    return x


def compute_unreported_this_week(records):
    ws = monday_of(datetime.now())
    reported = set()
    for r in records:
        try:
            t = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
        except Exception:
            continue
        # 转本地（简单按 naive 比较，created_at 已是北京时间写入）
        t_local = t.replace(tzinfo=None)
        if t_local >= ws and r.get('submitter_store') in STORE_LIST:
            reported.add(r['submitter_store'])
    return [s for s in STORE_LIST if s not in reported]


def compute_untracked_last_week(records):
    lw = monday_of(datetime.now()) - timedelta(days=7)
    lwe = lw + timedelta(days=7)
    has_any = {}
    has_tracked = {}
    for r in records:
        try:
            t = datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')).replace(tzinfo=None)
        except Exception:
            continue
        if t < lw or t >= lwe:
            continue
        for it in (r.get('items') or []):
            s = r.get('target_store')
            if not s:
                continue
            has_any[s] = True
            if it.get('status') and it['status'] != 'pending':
                has_tracked[s] = True
    return [s for s in STORE_LIST if has_any.get(s) and not has_tracked.get(s)]


def run(deduct_type):
    # 读取所有镜检记录
    data, err = supabase_get(TABLE_MIRROR, 'select=id,submitter_store,target_store,created_at,items&order=created_at.desc')
    if err:
        print('读取镜检记录失败：', err)
        sys.exit(1)
    records = data or []

    if deduct_type == 'submit':
        stores = compute_unreported_this_week(records)
        week_start = monday_of(datetime.now()).strftime('%Y-%m-%d')
        reason = '本周未提交镜检（自动扣分）'
    else:
        stores = compute_untracked_last_week(records)
        week_start = (monday_of(datetime.now()) - timedelta(days=7)).strftime('%Y-%m-%d')
        reason = '上周问题未复盘追踪（自动扣分）'

    if not stores:
        print(f'[{deduct_type}] 无需扣分：所有门店均已达标。')
        return

    # 幂等：查询该周期已记录的门店
    filt = f'select=store&deduct_type=eq.{deduct_type}&week_start=eq.{week_start}'
    done_data, err = supabase_get(TABLE_EXEC, filt)
    done = set((d.get('store') for d in (done_data or [])))
    to_insert = [s for s in stores if s not in done]
    if not to_insert:
        print(f'[{deduct_type}] 该周期扣分已执行过，无需重复。')
        return

    rows = [{
        'store': s,
        'deduct_type': deduct_type,
        'week_start': week_start,
        'deduct_date': datetime.now().isoformat(),
        'reason': reason,
        'value': -1,
    } for s in to_insert]
    _, err = supabase_insert(TABLE_EXEC, rows)
    if err:
        print(f'[{deduct_type}] 写入扣分失败：', err)
        sys.exit(1)
    print(f'[{deduct_type}] 已对 {len(to_insert)} 家门店扣分：{to_insert}')


if __name__ == '__main__':
    dt = sys.argv[1] if len(sys.argv) > 1 else 'submit'
    if dt not in ('submit', 'track'):
        print('参数错误：请传 submit 或 track')
        sys.exit(1)
    run(dt)
