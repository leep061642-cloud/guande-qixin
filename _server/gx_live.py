# -*- coding: utf-8 -*-
"""实时取数：直接从问卷网报表接口拉当前答卷，供站点上的 /api/lookup 使用。

设计要点
  · 只依赖标准库：部署容器里只有 python3，没有第三方包。
  · 算分复用 build_radar_html.py（同一份 profile + records_to_students），
    保证「实时出图」和「烘焙页面」的数字完全一致。
  · 全量结果缓存 60 秒，单个学号另做 10 秒去抖：一次查询不会把四个班都拖一遍。
  · 接口失败时不返回「空数据」，而是抛错 —— 空数据和接口挂了必须区分开，
    否则一次网络抖动就会让所有学生都查不到自己。
"""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
# 本地跑时算分模块就在同目录；部署到站点时被放在 _server/tools/ 下
for _cand in (_HERE, os.path.join(_HERE, 'tools')):
    if os.path.isfile(os.path.join(_cand, 'build_radar_html.py')):
        sys.path.insert(0, _cand)
        break
import build_radar_html as B  # noqa: E402
SITE = 'https://www.wenjuan.com'
WEB_SITE = 'ai_skills'

CACHE_TTL = 60          # 全量缓存（秒）
STALE_MAX = 600         # 接口挂了还能拿多久的旧缓存
SID_DEBOUNCE = 6        # 同一学号最短重取间隔（客户端的重试在 5 秒后，这里要比它小）

_lock = threading.Lock()
_cfg = None
_state = {
    'at': 0.0,          # 上次成功刷新时间
    'students': None,   # [{fc, sid, cls, complete_rows, skipped}]
    'by_sid': {},
    'last_err': '',
    'last_ok_at': 0.0,
    'refreshes': 0,
}
_sid_last = {}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config(config_dir):
    """配置目录里放 classes.json / profile_brief.json / dims_brief.json。"""
    global _cfg
    if _cfg is not None:
        return _cfg
    p = os.path.join(config_dir, 'classes.json')
    cfg = json.load(open(p, encoding='utf-8'))
    cfg['_dir'] = config_dir
    _cfg = cfg
    B.apply_profile(os.path.join(config_dir, cfg.get('profile', 'profile_brief.json')))
    return _cfg


def read_token(config_dir):
    """令牌优先取环境变量，其次读 _server/creds.json（该目录不对外提供）。"""
    t = (os.environ.get('WJ_TOKEN') or '').strip()
    if t:
        return t
    dirs = [config_dir, os.path.dirname(os.path.abspath(config_dir))]
    for d in dirs:
        for name in ('creds.json', 'token.json'):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    dd = json.load(open(p, encoding='utf-8'))
                    tok = dd.get('access_token') or dd.get('token') or ''
                    if isinstance(tok, dict):
                        tok = tok.get('access_token') or ''
                    if tok:
                        return str(tok).strip()
                except (OSError, ValueError):
                    continue
    return ''


# --------------------------------------------------------------------------- #
# 问卷网接口
# --------------------------------------------------------------------------- #
class UpstreamError(Exception):
    pass


def _http(url, token=None, payload=None, timeout=15):
    data = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace') or '{}')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise UpstreamError('token_expired')
        raise UpstreamError('http_%d' % e.code)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise UpstreamError('network: %s' % e)


def _signed_query():
    """签名由问卷网的服务端代签，本地不持有 appkey/secret。"""
    d = _http(SITE + '/app_api/create/signature', payload={'web_site': WEB_SITE, 'params': {}})
    d = d.get('data') if isinstance(d.get('data'), dict) else d
    if not (d.get('appkey') and d.get('signature') and d.get('timestamp')):
        raise UpstreamError('sign_failed')
    return urllib.parse.urlencode({
        'appkey': d['appkey'],
        'web_site': d.get('web_site') or WEB_SITE,
        'timestamp': d['timestamp'],
        'signature': d['signature'],
    })


def _qnum(cid):
    c = str(cid or '')
    if c.startswith('Q') and c[1:].isdigit():
        return str(int(c[1:]))
    return None


def _detail_to_record(pid, seq, rid, token, q):
    det = _http('%s/report/api/pc_rspd_detail/%s/%s/?%s&jwt=1'
                % (SITE, pid, rid, q), token=token)
    d = det.get('data') or {}
    values = {}
    for qq in (d.get('question_list') or []):
        n = _qnum(qq.get('cid'))
        if not n:
            continue
        opts = qq.get('option_list') or []
        parts = []
        for a in (qq.get('original_answer') or []):
            if not isinstance(a, dict):
                continue
            ans = a.get('answer')
            if isinstance(ans, dict):
                t = '' if ans.get('data') is None else str(ans.get('data')).strip()
                parts.append(t)
            elif ans not in (None, ''):
                parts.append(str(ans))
            elif a.get('option_id'):
                o = next((x for x in opts if x.get('id') == a['option_id']), None)
                if o:
                    parts.append(o.get('title') or '')
        v = ' | '.join([x for x in parts if x != ''])
        if v:
            values[n] = v
    bd = d.get('base_data') or {}
    return {
        'seq': str(seq or ''),
        'rid': rid,
        'submit': bd.get('finsh_time') or bd.get('finish_time') or '',
        'values': values,
    }


def fetch_pid(pid, token):
    """拉一个项目的全部答卷，结构对齐 tools/fetch_full_answers.js。

    列表分页：真正的每页条数参数是 **page_size**（page_count 只是响应里回显的字段）。
    默认 10 条，page_size 最大可用 100（≥120 会被拒，返回空）。
    所以一页取 100，再按 total_answer 继续翻页 —— 一个班 30 人就是 1 次列表请求，
    几百份也只需要几次，不会再把后面几页的学生漏掉。

    详情并发 12 路：一份详情约 0.3–0.4s，12 路时 120 份约 3–4s；
    串行或 6 路时学生第一次查询会等到前端超时。
    """
    # 本地联调用：GX_LIVE_FAKE=<api_full.json> 时不打问卷网，直接读文件（线上不会设这个变量）
    fake = os.environ.get('GX_LIVE_FAKE')
    if fake and os.path.isfile(fake):
        d = json.load(open(fake, encoding='utf-8'))
        return d.get('records') if isinstance(d, dict) else d
    q = _signed_query()
    page_size = 100
    items = []
    total = None
    page = 1
    while True:
        lst = _http('%s/report/api/answers/detail_list/%s/?%s&jwt=1&page=%d&page_size=%d'
                    % (SITE, pid, q, page, page_size), token=token)
        data = (lst.get('data') or {})
        answers = data.get('answers') or data.get('anwsers')
        if answers is None:
            # 拿不到列表字段时不能当成「0 份」，否则一次异常会把线上数据清空
            msg = str(lst.get('err_msg') or lst)[:120]
            raise UpstreamError('token_expired' if 'token' in msg.lower() else 'list_shape: %s' % msg)
        try:
            if total is None and data.get('total_answer') is not None:
                total = int(data.get('total_answer') or 0)
        except (TypeError, ValueError):
            total = None
        page_items = [(it.get('seq'), it.get('rid') or '') for it in answers if it.get('rid')]
        items.extend(page_items)
        if not page_items or len(page_items) < page_size:
            break
        if total is not None and len(items) >= total:
            break
        page += 1
        if page > 100:                       # 安全上限：10000 份，正常用不到
            break
    if not items:
        return []

    def _one(item):
        seq, rid = item
        try:
            return _detail_to_record(pid, seq, rid, token, q)
        except UpstreamError:
            try:                                  # 单份失败重试一次，别让一名学生凭空消失
                return _detail_to_record(pid, seq, rid, token, q)
            except UpstreamError:
                return None

    with ThreadPoolExecutor(max_workers=12) as ex:
        rows = list(ex.map(_one, items))
    return [r for r in rows if r]


def refresh(token=None):
    """全量刷新（四个班）。失败抛 UpstreamError，不动旧缓存。

    **四个班并行拉**：串行时「4 个班 × 30 份」要 20 秒以上，学生第一次查询会等到前端超时；
    并行后大致等于「最慢那个班」的耗时（约 4 秒）。
    解析仍按班顺序做：build_radar_html 里有一批模块级配置，不在多线程里调用它更稳。
    """
    cfg = _cfg
    token = token or read_token(cfg['_dir'])
    if not token:
        raise UpstreamError('no_token')
    classes = cfg.get('classes') or []

    def _pull(c):
        try:
            return c, fetch_pid(c['pid'], token), None
        except UpstreamError as e:
            return c, None, e

    if len(classes) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(classes))) as ex:
            pulled = list(ex.map(_pull, classes))
    else:
        pulled = [_pull(c) for c in classes]

    records = []
    errs = []
    with tempfile.TemporaryDirectory() as td:
        for c, rows, err in pulled:
            if err is not None:
                # 单个班失败不影响其它班，但整体标记为「部分失败」
                errs.append('%s: %s' % (c['label'], err))
                continue
            if not rows:
                continue
            p = os.path.join(td, '%s_live.json' % c['pid'])
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'pid': c['pid'], 'total': len(rows), 'records': rows}, f, ensure_ascii=False)
            rs = B.load_records([], B.SKIP_NAME_LIKE, B.SKIP_SID_PREFIX, [p])
            for r in rs:
                r['cls'] = c['label']
            records += rs
    _state['last_err'] = '; '.join(errs)
    if not records and _state['last_err']:
        raise UpstreamError(_state['last_err'])
    students = B.records_to_students(records)
    by_sid = {}
    for s in students:
        by_sid[_norm_sid(s['sid'])] = s
    _state['students'] = students
    _state['by_sid'] = by_sid
    _state['at'] = time.time()
    _state['last_ok_at'] = _state['at']
    _state['refreshes'] += 1
    return students


def _ensure(token=None, force=False, sid=None):
    now = time.time()
    fresh = _state['students'] is not None and (now - _state['at']) < CACHE_TTL
    if fresh and not force:
        return
    if force and sid:
        last = _sid_last.get(_norm_sid(sid), 0)
        if now - last < SID_DEBOUNCE:
            return
        _sid_last[_norm_sid(sid)] = now
        # 刚刷过（4 秒内）就别再刷：一个班几十个人同时查、或者有人一直打错学号时，
        # 每次未命中都全量刷新会把上游打爆。前端 7 秒后还有一次重试兜住"刚交完卷"的情况。
        if _state['students'] is not None and (now - _state['at']) < 4:
            return
    with _lock:
        now = time.time()
        if not force and _state['students'] is not None and (now - _state['at']) < CACHE_TTL:
            return
        try:
            refresh(token)
        except UpstreamError as e:
            _state['last_err'] = str(e)
            # 有旧缓存就继续用（最多 10 分钟），完全没数据才向上抛
            if _state['students'] is None or (now - _state['last_ok_at']) > STALE_MAX:
                raise


def _norm_sid(x):
    """学号归一：只留数字与字母、统一大写。

    2026-11-10 起学号允许「数字 + 字母」共 10 位（如 V530006255），
    所以不能再当纯数字处理；大小写由这里抹平，学生大小写随便打。
    """
    return re.sub(r'[^0-9A-Za-z]', '', str(x or '')).upper()


def _find(sid):
    """在当前缓存里找这个学号。

    依次尝试：完全命中 → 唯一后缀命中（旧短学号）→ 数字部分命中
    （老答卷只存了 9 位数字、新答卷是 V530006255 这种情况，两边要认成同一个人）。
    """
    key = _norm_sid(sid)
    if not key:
        return None
    st = _state['by_sid'].get(key)
    if st is None:
        cands = [k for k in _state['by_sid'] if k.endswith(key)]
        if len(cands) == 1:
            st = _state['by_sid'][cands[0]]
    if st is None:
        digits = re.sub(r'\D', '', key)
        if digits:
            cands = [k for k in _state['by_sid']
                     if re.sub(r'\D', '', k).endswith(digits)]
            if len(cands) == 1:
                st = _state['by_sid'][cands[0]]
    return st


def _pack(st):
    return {
        'n': st.get('fc') or '',
        'sid': st.get('sid') or '',
        'c': st.get('cls') or '',
        'rounds': st.get('complete_rows') or [],
        'skipped': st.get('skipped') or 0,
    }


_bg = {'running': False}


def _bg_refresh(token=None):
    """后台补一次过期缓存，不阻塞当前请求（stale-while-revalidate）。

    有缓存命中时走这条路：页面立刻拿到数据，顺便把 60 秒过期的缓存补上，
    下一次查询就又是新鲜的。
    """
    if _bg['running']:
        return
    _bg['running'] = True

    def run():
        try:
            with _lock:
                if _state['students'] is None or (time.time() - _state['at']) >= CACHE_TTL:
                    refresh(token)
        except Exception as e:                       # noqa: BLE001
            _state['last_err'] = str(e)
        finally:
            _bg['running'] = False

    threading.Thread(target=run, daemon=True).start()


def lookup(sid, token=None, force=False):
    """按学号查一名学生；返回 {n, sid, c, rounds, skipped} 或 None（查无此人）。

    这里有一条很重要的分支：**缓存里查不到人时，先同步拉一次最新答卷再回答**。
    以前是「缓存还在 60 秒有效期内就直接返回空」，于是刚交完卷、或者上一个请求
    刷出来的缓存刚好不包含这条记录时，第一次点「查看」必然查不到、第二次才命中 ——
    用户看到的就是「第一次报错，再点一次就进去了」。
    命中时则直接返回，只在后台把过期缓存补上，正常查询始终是毫秒级。
    """
    st = _find(sid)

    if st is not None and not force:
        if _state['students'] is None or (time.time() - _state['at']) >= CACHE_TTL:
            _bg_refresh(token)
        return _pack(st)

    if st is None and not force:
        # 查不到：可能是新答卷还没进缓存，同步刷一次再找（同一学号 6 秒内不重复刷）
        _ensure(token, force=True, sid=sid)
        st = _find(sid)
        return _pack(st) if st else None

    _ensure(token, force=force, sid=sid)
    st = _find(sid)
    return _pack(st) if st else None


def status():
    return {
        'students': len(_state['students'] or []),
        'classes': [c['label'] for c in (_cfg or {}).get('classes') or []],
        'cachedAt': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_state['at'])) if _state['at'] else None,
        'ageSec': int(time.time() - _state['at']) if _state['at'] else None,
        'refreshes': _state['refreshes'],
        'lastError': _state['last_err'],
    }


def teacher_stat(token=None):
    """教师端实时聚合：每个班 + 全部班级的七维均值、学生数、轮次数。

    口径与构建时（build_radar_html.py 的 _avg）保持一致：
      · 只用完整答卷（complete_rows）
      · avg  = 所有「轮次」的维度均值（不是先按人合并）
      · students = 去重学号数；rounds = 轮次条数
    """
    _ensure(token)
    students = _state['students'] or []
    dims = list(getattr(B, 'DIM_ORDER', None) or [])

    rows = []                     # (班级, 学号, [7 维])
    for s in students:
        cls = s.get('cls') or '未分班'
        sid = s.get('sid') or ''
        for r in (s.get('complete_rows') or []):
            d = r.get('dims')
            if isinstance(d, list) and d and (not dims or len(d) == len(dims)):
                rows.append((cls, sid, d))

    def _avg(rs):
        if not rs:
            return None, 0, 0
        n = len(rs)
        k = len(rs[0][2])
        av = [round(sum(x[2][i] for x in rs) / n, 3) for i in range(k)]
        return av, len({x[1] for x in rs if x[1]}), n

    out = []
    for c in (_cfg or {}).get('classes') or []:
        label = c.get('label') or ''
        av, ns, nr = _avg([x for x in rows if x[0] == label])
        out.append({'name': label, 'avg': av, 'students': ns, 'rounds': nr})

    av, ns, nr = _avg(rows)
    overall = {'name': '全部班级', 'avg': av, 'students': ns, 'rounds': nr}
    return {
        'updatedAt': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_state['at'])) if _state['at'] else '',
        'classes': [overall] + out,
        'overall': overall,
        'dims': dims,
    }
