# -*- coding: utf-8 -*-
"""构建「观德七心 · 我的综合雷达图」单页 H5。

用法示例：
  python tools/build_radar_html.py --xlsx downloads/xxx.xlsx --out outputs/观德七心_我的综合雷达图.html
  python tools/build_radar_html.py --xlsx downloads/xxx.xlsx --demo --out outputs/观德七心_我的综合雷达图_演示版.html

规则（已与用户确认）：
  · 维度分 = 该维度 3 道绑定题得分之和 ÷ 3；未答按 0 分计（与问卷网报告口径一致）
  · 3 道注意检验题（导出表 Q10/Q16/Q19）不计分，直接忽略
  · 只计入「完整作答」的轮次：21 道计分题全部有作答
  · 身份 = Q1 字段中解析出的「姓名首字 + 学号后四位」
"""
import argparse
import base64
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_export import parse_xlsx           # noqa: E402
import dimguard                               # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 维度 → 导出表题号（来源：项目后台「维度设置」的 binding_list，已与平台报告逐项校验）
DIM_MAP = {
    '心端': [11, 3, 20],
    '心敬': [21, 12, 4],
    '心平': [13, 5, 22],
    '心专': [23, 6, 14],
    '心通': [24, 15, 7],
    '心纯': [25, 17, 8],
    '心宏': [18, 26, 9],
}
DIM_ORDER = ['心端', '心敬', '心平', '心专', '心通', '心纯', '心宏']
CHECK_Q = {10, 16, 19}                         # 3 道注意检验题（不计分）
SCORED_Q = [n for n in range(3, 27) if n not in CHECK_Q]   # 21 道计分题

PROFILE = None          # --profile 载入后覆盖上面的默认值（多项目共用同一套构建脚本）
PIN = {}                # --pin：答题序号 → 原始「学号+姓名」，用于补平台侧已丢失的身份字段
REVERSE_Q = set()       # 反向计分题号（profile.reverseQ）；导出里的分是原始分，这里统一折算
# 问卷改版（插入注意力检测题）后，同一项目里会并存两种题号：
#   老答卷 = 旧结构（题号冻结），新答卷 = 新结构（检测题占一个题号，其后全部 +1）。
# qVersions 描述「新结构相对最老结构」的差异，读取时把新答卷还原成最老题号，检测题单独剥离。
Q_VERSIONS = []         # [{checkQ, shiftAbove, probeQ, since}]
CHECK_EXPECT = ''       # 检测题的期望作答前缀（profile.checkExpect）
MAX_SCORE = 7.0
SID_MODE = 4             # 学号输入模式（默认=标准卷）：4 = 后四位+姓名首字；简版/完整版由 profile.sidMode 覆盖为 6
SKIP_NAME_LIKE = ()      # profile.skipNameLike：姓名含这些词即为测试答卷，不进入页面
SKIP_SID_PREFIX = ()     # profile.skipSidPrefix：学号以这些前缀开头（如 0000000000）也不进入页面

# 英文对照（维度名、档位名、解读、建议模板、各维行为指标）
_EN_PATH = os.path.join(ROOT, 'tools', 'tiers_en.json')
EN = json.load(open(_EN_PATH, encoding='utf-8')) if os.path.isfile(_EN_PATH) else {}


def build_tiers_en(tiers):
    """按中文档位生成对应的英文文案（同一套 5 档，仅文案不同）。"""
    if not EN:
        return {}
    out = {}
    for d, lst in tiers.items():
        arr = []
        for t in lst:
            m = re.search(r'([ⅠⅡⅢⅣⅤ])', t['label'] or '')
            k = m.group(1) if m else 'Ⅲ'
            obs = EN.get('obs', {}).get(d, '')
            dim_en = EN.get('dims', {}).get(d, d)
            tip = EN.get('tips', {}).get(k, '').replace('{obs}', obs).replace('{dimEn}', dim_en)
            arr.append({
                'label': EN.get('labels', {}).get(k, ''),
                'read': EN.get('reads', {}).get(k, ''),
                'tip': tip,
            })
        out[d] = arr
    return out


# --------------------------------------------------------------------------- #
# 文案档位
# --------------------------------------------------------------------------- #
def load_tiers(dims_json_path):
    raw = json.load(open(dims_json_path, encoding='utf-8'))
    metrics = (raw.get('data') or raw).get('metrics') or []
    tiers, ref = {}, None
    for m in metrics:
        name = m.get('name')
        if name not in DIM_MAP:
            continue
        lst = []
        for r in (m.get('result') or []):
            cond = r.get('score_condition') or []
            if len(cond) != 4:
                continue
            lo_op, lo, hi_op, hi = cond[0], float(cond[1]), cond[2], float(cond[3])
            ps = re.findall(r'<p>(.*?)</p>', r.get('desc') or '', re.S)
            if len(ps) < 3:
                continue
            lst.append({
                'min': lo, 'max': hi, 'mi': lo_op == '>=', 'xi': hi_op == '<=',
                'label': re.sub(r'<[^>]+>', '', ps[0]).strip(),
                'read': re.sub(r'<[^>]+>', '', ps[1]).strip(),
                'tip': re.sub(r'<[^>]+>', '', ps[2]).strip(),
            })
        if lst:
            tiers[name] = lst
        r = m.get('reference_score')
        if isinstance(r, (int, float)) and r >= 0 and ref is None:
            ref = float(r)
    missing = [d for d in DIM_ORDER if d not in tiers]
    if missing:
        raise SystemExit('维度文案缺失：%s' % missing)
    for d in tiers:
        tiers[d].sort(key=lambda t: t['min'])
    return tiers, (ref if ref is not None else 4.0)


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def _score(cell):
    """从『5 较符合 得分：5』里取分数；量表题为纯数值单元格；空白返回 None。"""
    if cell is None or cell == '':
        return None
    if isinstance(cell, (int, float)):
        return int(cell)
    m = re.search(r'得分：(-?\d+)', str(cell))
    if m:
        return int(m.group(1))
    # 下拉单选（disp_type=dropdown）：单元格是整段选项文案，如「5 完全符合 / Completely agree」
    m = re.match(r'^\s*(\d+)\s+\S', str(cell))
    if m:
        return int(m.group(1))
    m = re.match(r'^\s*(\d+)\s*$', str(cell))
    if m:
        return int(m.group(1))
    return None


def _answered(cell):
    """单元格是否有作答（检验题没有『得分：』后缀，仍算作答）。"""
    return bool(cell and str(cell).strip())


def _clean_id_raw(raw):
    """清洗第 1 题的原始答案。

    第 1 题改成「学号 / 姓名」两格填空后，平台有**两种**导出格式：

    1) 老格式（多行「标签:值」）：
         姓名 Your name:2025010101张三
    2) 新格式（单行内联，带题干键与 open 标记，2026-10-27 实测）：
         学号 Student ID:2333333333|姓名 Your nameQ1|A1|open:绝对小子
       其中「Q1|A1|open:」是平台内部的题目/选项序列化标记，必须剥掉，
       否则「绝对小子」提取不出来，学号还会被解析成题干键里的「1」。

    统一洗成「学号 | 姓名」两段，交给 parse_identity 只处理值本身。
    """
    s = str(raw or '').strip()
    if not s:
        return ''
    s = s.replace('\r', '\n')
    # 「|open:」之后才是真正填写的内容 → 当作分隔符
    s = re.sub(r'\s*\|\s*open\s*[:：]', '\n', s, flags=re.I)
    toks = re.split(r'[\n|\t]', s)
    vals = []
    for tok in toks:
        t = tok.strip()
        # 「标签:值」→ 只留值
        t = re.sub(r'^\s*(?:学号|姓名)[^:：]{0,24}[:：]\s*', '', t)
        t = re.sub(r'^\s*open\s*[:：]\s*', '', t, flags=re.I)
        # 「姓名 Your nameQ1」这类只剩标签+题干键的片段 → 去掉标签后为空
        t = re.sub(r'^\s*(?:学号|姓名)[A-Za-z0-9\s]*', '', t).strip()
        if re.match(r'^[QA]\d+$', t):      # 纯题干键 / 选项键
            continue
        if t:
            vals.append(t)
    if len(vals) <= 1:
        return vals[0] if vals else ''
    return ' | '.join(vals)


def parse_identity(raw):
    """从 Q1 原始文本解析「学号 + 姓名」。

    现行格式（第 1 题改成两个填空格后）：学号 «分隔符» 姓名。
      - 报表接口（pc_rspd_detail）用「 | 」拼多格：2025010101 | 张三
      - xlsx 导出用制表符：2025010101\\t张三
    历史格式（单格连写）：「123456」「张三1234」等纯学号或姓名+学号。
    学号取完整 10 位；历史短学号（4/6 位）保留原样，由后续合并逻辑按尾号归并。
    姓名 = 分隔符后一段（完整姓名），或连写格式中的中文部分。

    2026-11-10：学号允许「数字 + 字母」共 10 位（如 v530006255），
    所以这里不能再只抓数字——以前 `re.search(r'\\d+')` 会把字母丢掉，
    带字母的学号被截成 9 位数字，和网站输入的 10 位永远对不上。
    现在取「含数字的最长一段字母数字组合」，统一转大写（学生大小写随便打）。
    """
    if not raw:
        return None, ''
    s = str(raw).strip()

    def _sid_token(text):
        """从一格文本里挑学号：含数字的最长一段字母数字组合，最多 10 位。"""
        runs = [r for r in re.findall(r'[0-9A-Za-z]+', text) if re.search(r'\d', r)]
        if not runs:
            return None
        best = max(runs, key=len)
        if len(best) > 10:
            best = best[-10:]
        return best.upper()

    if '\t' in s or ' | ' in s:
        parts = re.split(r'\t|\s*\|\s*', s)
        a = parts[0].strip()
        b = parts[1].strip() if len(parts) > 1 else ''
        # 2026-10-27 起第 1 题改成「姓名在上、学号在下」，两格的先后顺序不再固定，
        # 所以按内容判断：含 4 位以上连续数字的一段是学号，另一段是姓名。
        # 这样新旧答卷（学号在前 / 姓名在前）都能正确解析。
        def _has_num(x):
            return bool(re.search(r'\d{4,}', x))

        if b and _has_num(b) and not _has_num(a):
            sid_part, name_part = b, a
        else:
            sid_part, name_part = a, b
        return _sid_token(sid_part), name_part
    # 无分隔符：历史单格格式。整格按一段处理，取「含数字的最长字母数字段」，
    # 避免把题干键（Q1/A1）当成学号（2026-10-27 踩过）。
    name = ''
    nm = re.search(r'([\u4e00-\u9fff]+)', s)
    if nm:
        name = nm.group(1)
    return _sid_token(s), name


def _skipped(rec, skip_name_like, skip_sid_prefix):
    """过滤测试答卷（假学号 / 名字里带「测试」），避免混进学生数据。"""
    name = rec.get('fc') or ''
    for kw in skip_name_like or ():
        if kw and kw in name:
            return True
    sid = rec.get('sid') or ''
    for pre in skip_sid_prefix or ():
        if pre and sid.startswith(pre):
            return True
    return False


def load_records(xlsx_paths, skip_name_like=None, skip_sid_prefix=None, api_paths=None):
    """读一份或多份导出并合并。

    问卷网改题会归档旧答卷，归档批次不再出现在新导出里；因此这里把多份导出并起来，
    以「答题序号」去重（同一份答卷在每份导出里序号相同），后导出的覆盖先导出的，
    这样学生补填的姓名等字段也能更新到最新值。

    换项目时会有「两个 pid 的导出同时在用」（旧的存历史、新的收新答卷），
    两个项目的答题序号都从 1 开始，所以去重键带上 pid 前缀，避免互相覆盖。

    api_paths：tools/fetch_full_answers.js 拉下来的答卷 JSON。项目改题后平台导出会
    永久失败，而报表接口能拿到全部答卷（含归档批次），所以用它补导出里没有的序号。
    已有导出记录优先（导出是当时的口径，字段最全），JSON 只补空缺。
    """
    if isinstance(xlsx_paths, str):
        xlsx_paths = [xlsx_paths]
    out = []
    seen = {}
    for path in xlsx_paths:
        src = os.path.basename(path).split('_')[0]      # 导出文件名以 pid 开头
        for rec in _load_one(path):
            if _skipped(rec, skip_name_like, skip_sid_prefix):
                continue
            key = (src, rec['seq'] or (rec['raw_id'] + '|' + rec['submit'] + '|' + rec['fill_date']))
            if key in seen:
                # 改题后重新导出时，被归档的老答卷在新结构里读不出答案（整行空白）。
                # 所以同一份答卷不是「后导出的一定更新」，而是「答得更多的更新」；
                # 作答数相同才以后导出的为准（用于补姓名等字段）。
                old = out[seen[key]]
                if rec['_ans'] >= old['_ans']:
                    out[seen[key]] = rec
            else:
                seen[key] = len(out)
                out.append(rec)

    if isinstance(api_paths, str):
        api_paths = [api_paths]
    for path in api_paths or []:
        pid, recs = _load_api(path)
        for rec in recs:
            if _skipped(rec, skip_name_like, skip_sid_prefix):
                continue
            key = (pid, rec['seq'])
            if key in seen:
                # 接口只补「导出里没有」或「导出里是空白」的答卷：
                # 改题前的归档答卷在新结构下接口也读不准，绝不能覆盖导出里已有的完整记录。
                if rec['_ans'] > out[seen[key]]['_ans']:
                    out[seen[key]] = rec
                continue
            seen[key] = len(out)
            out.append(rec)
    for rec in out:
        rec.pop('_ans', None)
    if PIN:
        # 兜底：某些答卷平台侧的身份答案已经丢了（改题型把选项换掉），
        # 用改题前导出的「答题序号 → 学号+姓名」补回来，别让记录从图里消失。
        # 注意：补完要再过一次过滤，否则测试答卷会因为「原本无身份」而漏网。
        kept = []
        for rec in out:
            if not rec.get('sid'):
                raw = PIN.get(str(rec.get('seq')))
                if raw:
                    sid, name = parse_identity(raw)
                    if sid:
                        rec['sid'] = sid
                        rec['fc'] = name or rec.get('fc') or ''
                        rec['raw_id'] = raw
            if _skipped(rec, skip_name_like, skip_sid_prefix):
                continue
            kept.append(rec)
        out = kept
    return out


def _norm_values(vals):
    """把「插入检测题之后」的答卷题号还原成最老结构，并剥离检测题作答。

    vals: {题号(str): 答案文本}
    返回 (vals_old, check_ans)；check_ans 为 None 表示这份答卷里没有检测题（老答卷）。

    判定依据是 probeQ：只有新结构才存在的那个题号（= 旧最大题号 + 1）。
    新结构答卷它一定有答案（必答），老答卷则是空 —— 比按提交时间判定更可靠。
    """
    check_ans = None
    for ver in Q_VERSIONS:
        probe = ver.get('probeQ')
        if probe is None:
            continue
        pv = vals.get(str(probe))
        if pv is None or not str(pv).strip():
            continue
        cq = int(ver['checkQ'])
        above = int(ver.get('shiftAbove', cq))
        v = dict(vals)
        check_ans = v.pop(str(cq), None)
        moved = {}
        for k, val in v.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                moved[k] = val
                continue
            moved[str(n - 1 if n > above else n)] = val
        vals = moved
    return vals, check_ans


def _assemble(seq, raw_q1, fill_date, submit, cell_of, check_ans=None):
    """把「一题的原始单元格」-> 一条完整记录的公共逻辑（导出/接口两个数据源共用）。"""
    scores = {n: (_score(cell_of(n)) if cell_of(n) != '' else None) for n in SCORED_Q}
    # 反向题折算：平台导出的一律是原始作答分（reverse_score 开关对导出无效），
    # 这里按 6 - 原始分（5 分制）/ 8 - 原始分（7 分制）统一反向。
    for n in scores:
        if n in REVERSE_Q and scores[n] is not None:
            scores[n] = int(MAX_SCORE) + 1 - scores[n]
    answered = {n: _answered(cell_of(n)) for n in SCORED_Q}
    complete = all(answered[n] for n in SCORED_Q)
    dims = {}
    for d in DIM_ORDER:
        vals = [scores[n] if scores[n] is not None else 0 for n in DIM_MAP[d]]
        dims[d] = sum(vals) / float(len(DIM_MAP[d])) if DIM_MAP[d] else 0.0
    sid, name = parse_identity(raw_q1)
    submit = (submit or '').strip()
    date = submit[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', submit) else (fill_date or '').strip()
    # 检测题结果：None=这份答卷没有检测题（老结构），True/False=答对/答错
    if check_ans is None or not str(check_ans).strip():
        check_ok = None
    else:
        check_ok = str(check_ans).strip().startswith(CHECK_EXPECT) if CHECK_EXPECT else None
    return {
        'seq': str(seq or '').strip(),
        'raw_id': raw_q1 or '',
        'fill_date': fill_date or '',
        'submit': submit,
        'date': date,
        'fc': name, 'sid': sid,
        'complete': complete,
        'check_ok': check_ok,
        'check_ans': (str(check_ans).strip() if check_ans else ''),
        'dims': dims,
        'total': sum(v for v in scores.values() if v is not None),
        '_ans': sum(1 for n in SCORED_Q if answered[n]),   # 已作答题数，合并时用它挑更完整的一份
    }


def _load_api(json_path):
    """读 tools/fetch_full_answers.js 的输出。values 的键是题号（等同导出表 Q<n>）。"""
    d = json.load(open(json_path, encoding='utf-8'))
    pid = d.get('pid') or os.path.basename(json_path).split('_')[0]
    out = []
    for r in d.get('records') or []:
        vals, check_ans = _norm_values(dict(r.get('values') or {}))
        out.append(_assemble(
            r.get('seq'), vals.get('1', ''), vals.get('2', ''), r.get('submit', ''),
            lambda n: vals.get(str(n), ''),
            check_ans=check_ans,
        ))
    return pid, out


def _load_one(xlsx_path):
    rows = parse_xlsx(xlsx_path)
    hdr = dict(rows[0])
    need = ['答题序号', '得分', 'Q1_', 'Q2_填写日期', '提交时间']
    col = {}
    for want in need:
        for k, v in hdr.items():
            if v.startswith(want) or want in v:
                col[want] = k
                break
    qcol = {}
    # 新结构比旧结构多一题（检测题），列范围要留出这一列，否则新答卷的最后一题读不到
    for n in range(1, (max(SCORED_Q) if SCORED_Q else 26) + 2):
        for k, v in hdr.items():
            if v.startswith('Q%d_' % n):
                qcol[n] = k
                break

    out = []
    for r in rows[1:]:
        if not (r.get(col['答题序号'], '') or '').strip():
            continue                              # 跳过「学员答案」说明行
        get = lambda c: (r.get(c, '') or '')
        cells = {n: get(c) for n, c in qcol.items()}
        cells, check_ans = _norm_values(cells)
        out.append(_assemble(
            get(col['答题序号']), _clean_id_raw(get(col['Q1_'])), get(col['Q2_填写日期']), get(col['提交时间']),
            lambda n: cells.get(n, ''),
            check_ans=check_ans,
        ))
    return out


# --------------------------------------------------------------------------- #
# 演示数据（验收用；真实发布前不要带 --demo）
# --------------------------------------------------------------------------- #
def demo_students():
    def rnd(seed):
        x = seed
        def nxt():
            nonlocal x
            x = (x * 1103515245 + 12345) % 2147483648
            return x / 2147483648.0
        return nxt

    def series(seed, rounds, base, gain):
        nxt = rnd(seed)
        out = []
        for k in range(rounds):
            v = []
            for j in range(len(DIM_ORDER)):
                t = base[j] + gain[j] * (k / max(1, rounds - 1))
                v.append(round(max(0.0, min(MAX_SCORE, t + (nxt() - 0.5) * 1.0)), 2))
            out.append(v)
        return out

    specs = [
        # (首字, 学号, 轮次, 起始基线, 每维总增幅, 起始日期, 间隔天)
        ('张', '1234', 6, [2.0, 3.3, 3.5, 3.0, 3.0, 2.3, 3.0], [1.8, 1.2, 1.5, 2.0, 1.6, 1.6, 1.3], '2026-09-08', 21),
        ('李', '5678', 3, [3.5, 4.0, 4.3, 4.0, 3.7, 3.0, 4.3], [0.5, -0.3, 0.3, 0.4, 0.6, -0.4, 0.2], '2026-09-15', 28),
        ('王', '9012', 2, [4.0, 4.7, 4.3, 4.7, 4.3, 3.7, 4.7], [0.7, 0.3, 0.7, 0.3, 0.7, 0.3, 0.3], '2026-09-22', 35),
        ('陈', '3456', 1, [3.0, 4.0, 3.3, 3.7, 3.3, 4.0, 3.7], [0, 0, 0, 0, 0, 0, 0], '2026-10-06', 0),
    ]
    out = []
    for si, (fc, sid, rounds, base, gain, start, gap) in enumerate(specs):
        # 演示基线按满分缩放，5 分制项目也能落在合理档位
        k = MAX_SCORE / 7.0
        base = [x * k for x in base]
        gain = [x * k for x in gain]
        d0 = dt.date.fromisoformat(start)
        rows = []
        for k, v in enumerate(series(1000 + si * 37, rounds, base, gain)):
            rows.append({'date': (d0 + dt.timedelta(days=gap * k)).isoformat(), 'dims': v})
        out.append({'fc': fc, 'sid': sid, 'complete_rows': rows, 'skipped': 1 if sid == '5678' else 0})
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def records_to_students(records):
    """把答卷记录合并成「每名学生一条」的结构（含历次轮次）。

    构建脚本与实时查询接口共用这一份逻辑：改一处两边同时生效，
    避免线上实时出图与页面烘焙数据的口径跑偏。
    """
    by_student = {}
    for r in records:
        if not r['sid'] or not r['complete']:
            continue
        # 学号可能有字母（v530006255），大小写由这里统一，避免同一个人被拆成两条
        r['sid'] = re.sub(r'[^0-9A-Za-z]', '', str(r['sid'])).upper()
        by_student.setdefault(r['sid'], []).append(r)

    # 学号格式换过代：同一名学生可能既有「后四位/后 6 位」的旧记录、又有「10 位」的新记录。
    # 若某个短学号唯一地是某个 10 位学号的后缀，就并到那一条上，保证历次记录连贯。
    longs = [k for k in by_student if len(k) >= 10]
    merged = {}
    for sid, rows in by_student.items():
        tgt = sid
        if len(sid) < 10:
            cands = [L for L in longs if L.endswith(sid)]
            if len(cands) == 1:
                tgt = cands[0]
        merged.setdefault(tgt, []).extend(rows)
    by_student = merged

    students = []
    for sid, rows in by_student.items():
        rows.sort(key=lambda x: x['submit'] or x['date'])
        fc = next((x['fc'] for x in rows if x['fc']), '')
        students.append({
            'fc': fc, 'sid': sid, 'cls': (rows[-1].get('cls') or ''),
            'complete_rows': [{'date': x['date'], 'dims': [round(x['dims'][d], 4) for d in DIM_ORDER]} for x in rows],
            'skipped': 0,
        })
    # 未答完的轮次计数（仅用于页面上告知，不参与画图）
    for r in records:
        if r['complete'] or not r['sid']:
            continue
        for st in students:
            if st['sid'] == r['sid'] or st['sid'].endswith(r['sid']):
                st['skipped'] += 1
                break

    students.sort(key=lambda s: (s['sid'], s['fc']))
    return students


def build(args):
    tiers, ref = load_tiers(args.dims)
    tiers_en = build_tiers_en(tiers)
    if PROFILE:
        ref = float(PROFILE.get('ref', ref))
    api_json = getattr(args, 'answers_json', None) or []
    records = load_records(args.xlsx or [], SKIP_NAME_LIKE, SKIP_SID_PREFIX, api_json) \
        if (args.xlsx or api_json) else []
    # 班级标签：主数据源打 --class-label，其余班级各按 --class-answers 追加。
    # 记录级别的 cls 只用于教师端的班级聚合；学生页面仍是跨班级同一套索引。
    pri_label = getattr(args, 'class_label', '') or ''
    if pri_label:
        for r in records:
            r['cls'] = pri_label
    for spec in (getattr(args, 'class_answers', None) or []):
        if '=' not in spec:
            print('  ⚠ 跳过无法解析的 --class-answers：%s' % spec)
            continue
        label, path = spec.split('=', 1)
        if not os.path.isfile(path):
            print('  ⚠ 班级「%s」没有答卷文件，按 0 条处理' % label)
            continue
        rs = load_records([], SKIP_NAME_LIKE, SKIP_SID_PREFIX, [path])
        for r in rs:
            r['cls'] = label
        print('  班级「%s」：%d 条答卷' % (label, len(rs)))
        records += rs

    stats = {'total': len(records), 'complete': 0, 'incomplete': 0, 'unparsed': 0, 'legacy_ok': 0}
    for r in records:
        if r['sid']:
            stats['legacy_ok'] += 1
        else:
            stats['unparsed'] += 1
        if r['complete']:
            stats['complete'] += 1
        else:
            stats['incomplete'] += 1

    students = records_to_students(records)

    if args.demo:
        for d in demo_students():
            students = [s for s in students if (s['fc'], s['sid']) != (d['fc'], d['sid'])]
            students.append(d)
        students.sort(key=lambda s: (s['sid'], s['fc']))

    # ---- 注意力检测题：只在后台标记，不进入页面、不影响任何分数 ----
    chk = [r for r in records if r.get('check_ok') is not None]
    if chk:
        bad = [r for r in chk if not r['check_ok']]
        tag = (PROFILE.get('tag') if PROFILE else 'default') or 'default'
        print('  检测题：%d 份答卷包含检测题，按指示作答 %d，未按指示 %d' % (
            len(chk), len(chk) - len(bad), len(bad)))
        for r in bad:
            print('    ⚠ 未通过  %s %s  %s  选了「%s」' % (
                r['sid'], r['fc'], (r['submit'] or '')[:16], str(r.get('check_ans') or '')[:14]))
        try:
            rep = os.path.join(ROOT, 'backup', 'check_report_%s.json' % tag)
            json.dump({
                'tag': tag, 'expect': CHECK_EXPECT, 'total_with_check': len(chk),
                'failed': [{'sid': r['sid'], 'fc': r['fc'], 'submit': r['submit'],
                            'answer': r.get('check_ans') or ''} for r in bad],
            }, open(rep, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            print('  检测报告 → backup/check_report_%s.json' % tag)
        except OSError as e:
            print('  ⚠ 检测报告写入失败：', e)

    # ---- 数据指纹：学生 + 各自轮次（用于判断「有没有新答卷、需不需要重新发布」）----
    # 版式指纹（模板 / 维度文案 / profile）也要算进去：只改页面的改动同样需要重新发布，
    # 否则定时刷新只比数据、会漏推这类改版。
    def _tag(path):
        try:
            return hashlib.sha256(open(path, 'rb').read()).hexdigest()[:8]
        except (OSError, TypeError):
            return '0'

    layout = '-'.join([
        _tag(args.template),
        _tag(args.dims),
        _tag(getattr(args, 'profile', None)) if getattr(args, 'profile', None) else 'x',
    ])
    fp_src = json.dumps([
        layout,
        [{'n': s['fc'], 'sid': s['sid'], 'r': s['complete_rows'], 'k': s.get('skipped', 0)}
         for s in students],
    ], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(fp_src.encode('utf-8')).hexdigest()[:16]

    # ---- 加密 ----
    salt = resolve_salt(args)
    index = {}
    for s in students:
        # 密钥规则：sidMode=10 → 完整 10 位学号（姓名仅展示，不参与校验）；
        #           sidMode=4  → 姓名首字 + 学号后四位；sidMode=6 → 学号后 6 位
        if SID_MODE == 4:
            pass_str = s['fc'] + s['sid']
        else:
            pass_str = s['sid']
        iv = os.urandom(12)
        payload = json.dumps({
            'n': s['fc'], 'sid': s['sid'], 'c': s.get('cls', ''),
            'rounds': s['complete_rows'],
            'skipped': s.get('skipped', 0),
        }, ensure_ascii=False, separators=(',', ':'))
        enc = dimguard.encrypt(pass_str, salt, iv, payload, args.iters)
        index[enc['kid']] = {
            'iv': base64.b64encode(iv).decode(),
            'ct': enc['ct'], 'tag': enc['tag'],
        }

    data = {
        'dims': DIM_ORDER,
        'dimsEn': [EN.get('dims', {}).get(d, d) for d in DIM_ORDER],
        'tiersEn': tiers_en,
        'tiers': tiers,
        'ref': ref,
        'maxScore': MAX_SCORE,
        'sidMode': SID_MODE,
        'iters': args.iters,
        'passSalt': base64.b64encode(salt).decode(),
        'updatedAt': args.updated_at or dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'refreshMin': getattr(args, 'refresh_min', 15) or 0,
        'count': len(students),
        'index': index,
    }
    classes = sorted({s.get('cls', '') for s in students if s.get('cls')})
    if classes:
        data['classes'] = classes
        data['studentClass'] = {s['sid']: s.get('cls', '') for s in students}

    # ---- 教师端聚合：按班级 / 全量的七维平均分（用答题记录算，不用学生合并后的数据）----
    agg = None
    if getattr(args, 'agg_out', None):
        def _avg(rows):
            rows = [r for r in rows if r.get('complete') and r.get('sid')]
            if not rows:
                return None, 0, 0
            n = len(rows)
            return ([round(sum(r['dims'][d] for r in rows) / n, 3) for d in DIM_ORDER],
                    len({r['sid'] for r in rows}), n)

        buckets = {}
        for r in records:
            if not r.get('complete') or not r.get('sid'):
                continue
            buckets.setdefault(r.get('cls') or '未分班', []).append(r)

        agg = {
            'dims': DIM_ORDER,
            'dimsEn': data['dimsEn'],
            'tiers': tiers,
            'tiersEn': tiers_en,
            'ref': ref,
            'maxScore': MAX_SCORE,
            'updatedAt': data['updatedAt'],
            'refreshMin': data['refreshMin'],
            'classes': [],
        }
        known = []
        if pri_label:
            known.append(pri_label)
        for spec in (getattr(args, 'class_answers', None) or []):
            if '=' in spec:
                known.append(spec.split('=', 1)[0])
        # 按 refresh.py 里配置的班级顺序输出（周二→周三→周四→周六），
        # 不能用 sorted：汉字的 Unicode 顺序会把「周三」排到「周二」前面。
        order = list(dict.fromkeys(list(known) + list(buckets.keys())))
        for name in order:
            av, ns, nr = _avg(buckets.get(name) or [])
            agg['classes'].append({'name': name, 'avg': av, 'students': ns, 'rounds': nr})
        av, ns, nr = _avg(records)
        agg['overall'] = {'name': '全部班级', 'avg': av, 'students': ns, 'rounds': nr}
        agg['count'] = len(students)
        agg['rounds'] = sum(len(s['complete_rows']) for s in students)
        os.makedirs(os.path.dirname(os.path.abspath(args.agg_out)), exist_ok=True)
        with open(args.agg_out, 'w', encoding='utf-8') as f:
            f.write(json.dumps(agg, ensure_ascii=False, indent=1))
        print('  班级聚合 → %s（%d 个班级）' % (os.path.basename(args.agg_out), len(agg['classes'])))

    tpl = open(args.template, encoding='utf-8').read()
    guard = open(args.guard, encoding='utf-8').read()
    echarts = open(args.echarts, encoding='utf-8').read()
    html = tpl.replace('<!--GX_GUARD-->', '<script>\n' + guard + '\n</script>')
    html = html.replace('<!--GX_ECHARTS-->', '<script>\n' + echarts + '\n</script>')
    html = html.replace('/*GX_DATA*/', 'window.GX_DATA = ' + json.dumps(data, ensure_ascii=False) + ';')

    # ---- 教师面板：数据 + 共享逻辑（和学生封面上那个「教师入口」配合）----
    # 没有 agg（未算班级聚合）时注入空对象，入口仍可点开，只是提示数据未就绪。
    tdata, tpwd = None, None
    if agg:
        import build_teacher as _bt
        tdata, tpwd = _bt.compose(agg, args.teacher_auth)
        if tpwd:
            print('  ⚠ 首次生成教师账号：用户名 %s / 密码 %s（请改掉）' % (tdata['auth']['user'], tpwd))
    html = html.replace('/*GX_T*/',
                        'window.GX_T = ' +
                        (json.dumps(tdata, ensure_ascii=False) if tdata else '{}') + ';')
    html = _bt.inject_core(html, args.teacher_core) if agg else html.replace('<!--GX_T_CORE-->', '')

    # ---- 流体交互层：弹簧面板 / 拖拽关闭 / 按下即反馈 ----
    # 缺文件时退化成空脚本，页面仍走 CSS 那套动画，不会白屏。
    try:
        fluid = open(args.fluid, encoding='utf-8').read()
    except OSError:
        fluid = ''
    html = html.replace('<!--GX_FLUID-->',
                        ('<script>\n' + fluid + '\n</script>') if fluid else '')
    # 首屏素材的相对路径：根目录 assets/，子目录页 ../assets/
    html = html.replace('__ASSET_BASE__', args.asset_base or 'assets/')
    # 构建标记：诊断模式（?diag=1）里显示，用来确认手机上跑的是不是最新一版
    html = html.replace('__BUILD__', time.strftime('%m-%d %H:%M'))

    # 自检：页面里不得出现任何学生的身份明文（首字+学号）或身份字段原文
    for s in students:
        # 只检查「姓名首字 + 学号」的组合；纯数字学号（4/6 位）易与压缩过的 ECharts 常量误撞，不检查
        probes = []
        if s['fc']:
            probes += [s['fc'] + s['sid'], s['sid'] + s['fc']]
        for probe in probes:
            if probe in html:
                raise SystemExit('❌ 页面中检出明文身份信息：%s' % probe)
    for r in records:
        raw = (r['raw_id'] or '').strip()
        # 仅检查含姓名首字等非数字成分的身份原文（纯数字学号易与 JS 常量误撞）
        if raw and re.search(r'[\u4e00-\u9fff]', raw) and len(raw) >= 4 and raw in html:
            raise SystemExit('❌ 页面中检出 Q1 原文：%s' % raw)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    open(args.out, 'w', encoding='utf-8').write(html)

    print('=== 取数 ===')
    print('  导出记录 %d 条：完整 %d / 未答完 %d；身份可解析 %d / 待按新格式重填 %d'
          % (stats['total'], stats['complete'], stats['incomplete'], stats['legacy_ok'], stats['unparsed']))
    print('=== 入库学生 %d 名 ===' % len(students))
    for s in students:
        ds = s['complete_rows']
        print('  %s** 尾号%s：%d 轮  %s' % (
            s['fc'], s['sid'], len(ds),
            ' '.join('%s[%s]' % (x['date'], '/'.join('%.2f' % v for v in x['dims'])) for x in ds[:2]) +
            (' …' if len(ds) > 2 else '')))
    print('=== 演示数据 ===', '已注入' if args.demo else '未注入')
    print('=== 输出 ===')
    print('  %s  (%.1f KB，含内联 ECharts + 加密记录)' % (args.out, len(html.encode('utf-8')) / 1024.0))
    print('=== 指纹 ===')
    print('  %s  学生 %d 名 / 完整轮次 %d' % (
        fingerprint, len(students), sum(len(s['complete_rows']) for s in students)))

    state_out = getattr(args, 'state_out', None)
    if state_out:
        os.makedirs(os.path.dirname(os.path.abspath(state_out)), exist_ok=True)
        open(state_out, 'w', encoding='utf-8').write(json.dumps({
            'fingerprint': fingerprint,
            'count': len(students),
            'rounds': sum(len(s['complete_rows']) for s in students),
            'updatedAt': data['updatedAt'],
            'demo': bool(args.demo),
            'out': os.path.abspath(args.out),
        }, ensure_ascii=False, indent=1))
        print('  已写入状态文件：', state_out)
    return students


def resolve_salt(args):
    """固定加密盐：--salt 优先；否则读 --salt-file，不存在则生成并落盘。

    固定盐能让同一名学生在新旧构建里的索引 key 保持一致，便于缓存与比对。
    """
    if getattr(args, 'salt', None):
        return base64.b64decode(args.salt)
    path = getattr(args, 'salt_file', None)
    if path and os.path.isfile(path):
        raw = open(path, encoding='utf-8').read().strip()
        if raw:
            return base64.b64decode(raw)
    salt = os.urandom(16)
    if path:
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        open(path, 'w', encoding='utf-8').write(base64.b64encode(salt).decode())
    return salt


def apply_profile(path):
    """载入 profile 并覆盖模块级配置（维度映射、题号、跳过规则等）。

    构建脚本与实时查询服务共用：两边必须用同一份口径算分，否则同一个学生
    在「烘焙页面」和「实时接口」里会得到不同的雷达图。
    """
    global PROFILE, MAX_SCORE, DIM_MAP, DIM_ORDER, CHECK_Q, SCORED_Q, REVERSE_Q, SID_MODE
    global SKIP_NAME_LIKE, SKIP_SID_PREFIX, Q_VERSIONS, CHECK_EXPECT
    prof = json.load(open(path, encoding='utf-8'))
    PROFILE = prof
    DIM_MAP = dict(prof['dimMap'])
    DIM_ORDER = list(prof['dimOrder'])
    CHECK_Q = set(prof.get('checkQ') or [])
    REVERSE_Q = set(int(n) for n in (prof.get('reverseQ') or []))
    SCORED_Q = sorted({n for v in DIM_MAP.values() for n in v})
    MAX_SCORE = float(prof.get('maxScore', 7))
    SID_MODE = int(prof.get('sidMode', 4))
    SKIP_NAME_LIKE = tuple(prof.get('skipNameLike') or ())
    SKIP_SID_PREFIX = tuple(prof.get('skipSidPrefix') or ())
    Q_VERSIONS = list(prof.get('qVersions') or [])
    CHECK_EXPECT = str(prof.get('checkExpect') or '')
    print('使用 profile：', os.path.basename(path), '| 维度', len(DIM_ORDER), '| 计分题', len(SCORED_Q),
          '| 参考线', prof.get('ref'), '| 满分', MAX_SCORE)
    if Q_VERSIONS:
        print('  改版归一化：', '；'.join(
            '检测题 Q%d（其后题号 -1，判据 Q%s）' % (int(v['checkQ']), v.get('probeQ')) for v in Q_VERSIONS))
    return prof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--xlsx', action='append', default=None,
                    help='问卷网导出 xlsx 路径，可重复传多份（会按答题序号合并）；缺省取 downloads/ 下最新一个')
    ap.add_argument('--answers-json', dest='answers_json', action='append', default=None,
                    help='tools/fetch_full_answers.js 输出的答卷 JSON，可重复传多份；只补导出里缺的序号')
    ap.add_argument('--dims', default=os.path.join(ROOT, 'backup', 'dim_live.json'))
    ap.add_argument('--template', default=os.path.join(ROOT, 'tools', 'radar_template_blackhole.html'))
    ap.add_argument('--guard', default=os.path.join(ROOT, 'vendor', 'guard.js'))
    ap.add_argument('--echarts', default=os.path.join(ROOT, 'vendor', 'echarts.min.js'))
    ap.add_argument('--out', required=True)
    ap.add_argument('--asset-base', dest='asset_base', default='assets/',
                    help='首屏视频/封面的相对路径前缀；brief、full 这类子目录页要传 ../assets/')
    ap.add_argument('--iters', type=int, default=50000)
    ap.add_argument('--salt', default=None, help='base64 的 16 字节 salt；固定它可让构建可复现')
    ap.add_argument('--salt-file', dest='salt_file', default=os.path.join(ROOT, 'backup', 'radar_salt.txt'),
                    help='固定 salt 的存放文件；--salt 未给时读它，不存在则生成并写入')
    ap.add_argument('--refresh-min', dest='refresh_min', type=int, default=15,
                    help='页面上告知的自动同步间隔（分钟），0 表示不显示')
    ap.add_argument('--state-out', dest='state_out', default=None,
                    help='构建后写出状态 JSON（含数据指纹），供刷新脚本判断是否需要重新发布')
    ap.add_argument('--updated-at', dest='updated_at', default=None)
    ap.add_argument('--demo', action='store_true', help='额外注入演示学生，用于验收交互与图表')
    ap.add_argument('--empty', action='store_true', help='按 0 条答卷构建（项目还没有任何提交）')
    ap.add_argument('--profile', default=None, help='雷达图构建配置（由 tools/make_profile.js 生成），用于多项目复用')
    ap.add_argument('--pin', default=None, help='答题序号→学号+姓名的兜底映射 JSON')
    ap.add_argument('--class-label', dest='class_label', default='',
                    help='给 --xlsx / --answers-json 这批数据打的班级名（如「周二班」）')
    ap.add_argument('--class-answers', dest='class_answers', action='append', default=None,
                    metavar='班级=JSON',
                    help='额外的班级答卷接口 JSON，格式 班级=路径，可重复传；每份即一个班级')
    ap.add_argument('--agg-out', dest='agg_out', default=None,
                    help='把「按班级 / 全量」的维度平均分写成 JSON，供教师端页面构建使用')
    ap.add_argument('--teacher-auth', dest='teacher_auth',
                    default=os.path.join(ROOT, 'backup', 'teacher_auth.json'),
                    help='教师账号配置（盐 + PBKDF2 哈希），随页面一起注入')
    ap.add_argument('--teacher-core', dest='teacher_core',
                    default=os.path.join(ROOT, 'tools', 'teacher_core.js'),
                    help='教师端共享逻辑，注入到页面的 <!--GX_T_CORE--> 处')
    ap.add_argument('--fluid', dest='fluid',
                    default=os.path.join(ROOT, 'tools', 'gx_fluid.js'),
                    help='流体交互层（弹簧面板 / 拖拽 / 按下反馈），注入到 <!--GX_FLUID--> 处')
    args = ap.parse_args()

    global PIN
    if args.pin and os.path.exists(args.pin):
        PIN = {str(k): v for k, v in json.load(open(args.pin, encoding='utf-8')).items()}
        print('身份兜底 pin：', os.path.basename(args.pin), len(PIN), '条')
    if args.profile:
        apply_profile(args.profile)

    if not args.xlsx and not args.answers_json and not getattr(args, 'empty', False):
        cands = sorted(glob.glob(os.path.join(ROOT, 'downloads', '*.xlsx')))
        args.xlsx = cands[-1] if cands else None
    if args.xlsx:
        print('使用导出文件（%d 份）：' % len(args.xlsx), '、'.join(os.path.basename(x) for x in args.xlsx))
    if args.answers_json:
        print('使用接口答卷（%d 份）：' % len(args.answers_json),
              '、'.join(os.path.basename(x) for x in args.answers_json))
    if not args.xlsx and not args.answers_json:
        print('⚠ 没有导出文件：按 0 条答卷构建（还没有人提交）')
    build(args)


if __name__ == '__main__':
    main()
