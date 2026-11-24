# -*- coding: utf-8 -*-
"""观德七心站点服务：静态文件 + 实时查询接口。

    python3 _server/live_server.py 3000

相对「python3 -m http.server」只多两件事：
  · GET /api/lookup?sid=&fc=  用问卷网当前答卷实时出图（缓存 60 秒）
  · GET /api/health           自检（缓存年龄、最后一次错误）
并且补上了 Range 支持（手机端视频拖动需要），以及禁止访问 _server/ 本身。

任何情况下静态页面都能正常访问：接口挂了只是查询时会回落到页面里烘焙的数据。
"""
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # 站点根（web/）
CONFIG_DIR = os.path.join(HERE, 'config')

sys.path.insert(0, HERE)
import gx_live  # noqa: E402

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('video/mp4', '.mp4')
mimetypes.add_type('image/svg+xml', '.svg')

RL_WINDOW = 60
RL_MAX = 30                                   # 每个 IP 每分钟最多 30 次查询
_rl = {}
_rl_lock = threading.Lock()

# 设备自检回传（/diag/ 页面 POST 上来），只留最近 30 条，纯内存，重启即清
DIAG = []
_DIAG_LOCK = threading.Lock()

# 教师页 / 学生页内嵌教师面板：实时注入缓存  {绝对路径: (html, at)}
_T_CACHE = {}
_T_LOCK = threading.Lock()
_T_TTL = 60


def _find_obj_end(s, start):
    """从 s[start]=='{' 起做括号计数（跳过字符串与转义），返回匹配的 '}' 下标；找不到返回 -1。"""
    depth = 0
    in_str = False
    esc = False
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def inject_teacher(html):
    """把页面里 window.GX_T 的 classes / updatedAt 换成实时聚合。

    页面 JS 完全不用改：它本来就在 mount() 时读 window.GX_T，
    这里只是让它读到最新的班级数据（学生数、轮次、七维均值）。
    取不到实时数据时按原样返回，页面仍显示烘焙数据，不会白屏。

    注意：
      · 必须定位 `window.GX_T =`（带赋值号）——页面注释里也出现过
        「数据来自 window.GX_T：{ dims, ... }」，只找变量名会锚到注释里的花括号上。
      · 只替换 classes / updatedAt，auth、tiers 等其它字段原样保留。
    """
    m = re.search(r'window\.GX_T\s*=\s*\{', html)
    if not m:
        return html
    j = html.index('{', m.start())
    k = _find_obj_end(html, j)
    if k < 0:
        return html
    try:
        d = json.loads(html[j:k + 1])
    except ValueError:
        return html
    if not isinstance(d, dict):
        return html
    try:
        stat = gx_live.teacher_stat()
    except Exception:                            # noqa: BLE001 上游挂了就用烘焙数据
        return html
    d['classes'] = stat.get('classes') or d.get('classes')
    if stat.get('updatedAt'):
        d['updatedAt'] = stat['updatedAt']
    new = json.dumps(d, ensure_ascii=False)
    return html[:j] + new + html[k + 1:]


def teacher_page(path=None):
    """返回注入过实时数据的页面 HTML（60 秒缓存）。

    path 缺省为独立的 /teacher/ 页；学生落地页 brief/index.html 里**也内嵌了同一套教师面板**
    （学生封面右上角「教师入口」弹出的就是它），所以两个文件都要注入。
    """
    if path is None:
        path = os.path.join(ROOT, 'teacher', 'index.html')
    now = time.time()
    with _T_LOCK:
        hit = _T_CACHE.get(path)
        if hit and (now - hit[1]) < _T_TTL:
            return hit[0]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            html = f.read()
    except OSError:
        return None
    out = inject_teacher(html)
    with _T_LOCK:
        _T_CACHE[path] = (out, now)
    return out


def rate_ok(ip):
    now = time.time()
    with _rl_lock:
        hits = [t for t in _rl.get(ip, []) if now - t < RL_WINDOW]
        if len(hits) >= RL_MAX:
            _rl[ip] = hits
            return False
        hits.append(now)
        _rl[ip] = hits
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = 'GuandeRadar/1.0'
    protocol_version = 'HTTP/1.1'

    # ---------------- 工具 ----------------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _denied(self, code=403):
        body = b'forbidden'
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html_body(self, code, text):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        # 这份 HTML 内嵌了实时班级数据，必须每次都取最新，不能用缓存副本
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    # ---------------- 路由 ----------------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path.startswith('/api/'):
            return self._api(path, parse_qs(u.query))
        if path.startswith('/_server'):
            return self._denied()
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith('/_server'):
            return self._denied()
        if u.path != '/api/diag':
            return self._json(404, {'ok': False, 'reason': 'not_found_api'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(min(n, 32768)) if n > 0 else b''
            obj = json.loads(raw.decode('utf-8', 'replace') or '{}')
            if not isinstance(obj, dict):
                raise ValueError('not dict')
        except Exception:                            # noqa: BLE001
            return self._json(400, {'ok': False, 'reason': 'bad_body'})
        obj['_ip'] = self.client_address[0]
        obj['_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        with _DIAG_LOCK:
            DIAG.append(obj)
            del DIAG[:-30]
        sys.stderr.write('[diag] %s\n' % json.dumps(obj, ensure_ascii=False)[:6000])
        return self._json(200, {'ok': True, 'n': len(DIAG)})

    # ---------------- 接口 ----------------
    def _api(self, path, qs):
        if path == '/api/health':
            return self._json(200, dict(ok=True, **gx_live.status()))
        if path == '/api/diag':
            # 带参数 = 上报（GET 探针）。手机端用一个 Image 请求即可，
            # 不依赖 POST、不依赖 XHR，网关和 WebView 都拦不住。
            if qs.get('type'):
                rec = {k: v[0][:500] for k, v in qs.items()}
                rec['_ip'] = self.client_address[0]
                rec['_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                with _DIAG_LOCK:
                    DIAG.append(rec)
                    del DIAG[:-40]
                sys.stderr.write('[diag-get] %s\n' % json.dumps(rec, ensure_ascii=False)[:2000])
                return self._json(200, {'ok': True, 'n': len(DIAG)})
            with _DIAG_LOCK:
                return self._json(200, {'ok': True, 'n': len(DIAG), 'items': list(DIAG)})
        if path == '/api/teacher':
            # 教师端实时聚合（调试用；页面本身走 _inject_teacher 服务端注入）
            try:
                return self._json(200, dict(ok=True, **gx_live.teacher_stat()))
            except gx_live.UpstreamError as e:
                return self._json(503, {'ok': False, 'reason': 'upstream', 'detail': str(e)[:80]})
            except Exception as e:                   # noqa: BLE001
                return self._json(503, {'ok': False, 'reason': 'upstream', 'detail': type(e).__name__})
        if path != '/api/lookup':
            return self._json(404, {'ok': False, 'reason': 'not_found_api'})

        ip = self.client_address[0]
        if not rate_ok(ip):
            return self._json(429, {'ok': False, 'reason': 'too_many'})

        # 学号可以是「数字 + 字母」10 位（如 V530006255），所以这里只剔掉空格和标点，
        # 字母要留着（以前用 \D 会把字母删掉，带字母的学号永远查不到）。
        sid = re.sub(r'[^0-9A-Za-z]', '', (qs.get('sid') or [''])[0]).upper()
        fc = (qs.get('fc') or [''])[0].strip()
        force = (qs.get('force') or ['0'])[0] in ('1', 'true')
        if not sid:
            return self._json(400, {'ok': False, 'reason': 'bad_param'})
        try:
            rec = gx_live.lookup(sid, force=force)
        except gx_live.UpstreamError as e:
            return self._json(503, {'ok': False, 'reason': 'upstream', 'detail': str(e)[:80]})
        except Exception as e:                       # noqa: BLE001 兜底，绝不让接口 500 掉页面
            return self._json(503, {'ok': False, 'reason': 'upstream', 'detail': type(e).__name__})
        if not rec:
            return self._json(200, {'ok': True, 'found': False})
        return self._json(200, {'ok': True, 'found': True, 'rec': rec,
                                'updatedAt': gx_live.status().get('cachedAt')})

    # ---------------- 静态文件（含 Range） ----------------
    def _static(self, path):
        rel = path.lstrip('/')
        if not rel:
            rel = 'index.html'
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.normpath(ROOT)):
            return self._denied()
        if os.path.isdir(full):
            full = os.path.join(full, 'index.html')
            if not os.path.isfile(full):
                full = os.path.join(os.path.dirname(full), 'index.html')
        if not os.path.isfile(full):
            return self._notfound()

        # 教师页 + 学生落地页（内嵌同一套教师面板）：注入实时班级数据
        # 带 Range 的请求走原文件，避免破坏分段
        rel_norm = rel.replace('\\', '/').strip('/')
        if rel_norm in ('teacher', 'teacher/', 'teacher/index.html',
                        'brief', 'brief/', 'brief/index.html') and not self.headers.get('Range'):
            page = teacher_page(os.path.normpath(full))
            if page is not None:
                return self._html_body(200, page)

        try:
            size = os.path.getsize(full)
            f = open(full, 'rb')
        except OSError:
            return self._notfound()

        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
            ctype += '; charset=utf-8'

        rng = self.headers.get('Range') or ''
        start, end = 0, size - 1
        partial = False
        m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
        if m and size:
            a, b = m.group(1), m.group(2)
            if a == '' and b != '':
                start, end = max(0, size - int(b)), size - 1
            else:
                start = int(a or 0)
                end = int(b) if b else size - 1
                end = min(end, size - 1)
            if start > end or start >= size:
                f.close()
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            partial = True

        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        if partial:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Content-Length', str(end - start + 1))
        # 页面本体（含学生数据）不缓存，保证老师删答卷后学生刷新就能看到最新
        if full.endswith('.html'):
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        elif full.endswith(('.js', '.css')):
            # 脚本/样式也走协商缓存：改了立刻生效。
            # 微信 WebView 会把 HTML 缓存很久，页面上引的脚本是唯一能「事后补救」的通道。
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        else:
            self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()

        if self.command == 'HEAD':
            f.close()
            return
        try:
            f.seek(start)
            remain = end - start + 1
            while remain > 0:
                chunk = f.read(min(65536, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def _notfound(self):
        body = '<!doctype html><meta charset="utf-8"><title>404</title><p>404 Not Found'
        b = body.encode('utf-8')
        self.send_response(404)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        if '/api/' in (args[0] if args else ''):
            sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def warmup():
    """启动后台预热：第一个学生的查询就不用等接口跑完四个班。"""
    try:
        gx_live.refresh()
        sys.stderr.write('预热完成：%s\n' % json.dumps(gx_live.status(), ensure_ascii=False))
    except Exception as e:                       # noqa: BLE001
        sys.stderr.write('预热失败（不影响页面，查询时会重试）：%s\n' % e)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    if os.path.isdir(CONFIG_DIR):
        try:
            gx_live.load_config(CONFIG_DIR)
        except Exception as e:                       # noqa: BLE001
            sys.stderr.write('配置载入失败（接口将不可用）：%s\n' % e)
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    srv.daemon_threads = True
    sys.stderr.write('观德七心站点已启动：0.0.0.0:%d  根目录 %s\n' % (port, ROOT))
    threading.Thread(target=warmup, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
