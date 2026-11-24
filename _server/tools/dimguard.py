# -*- coding: utf-8 -*-
"""GXGuard 的 Python 侧实现（必须与 vendor/guard.js 算法完全一致）。

约定：
  key  = PBKDF2-HMAC-SHA256(pass, salt, iters, 64)
  kEnc = key[0:32]   kMac = key[32:64]
  ks_i = HMAC(kEnc, iv || u32be(i))
  ct   = pt XOR ks
  tag  = HMAC(kMac, iv || ct)[0:16]
  kid  = HMAC(kMac, b"kid")[0:8]
"""
import hashlib
import hmac
import struct
import base64


def pbkdf2(pass_bytes: bytes, salt: bytes, iters: int, dk_len: int = 64) -> bytes:
    return hashlib.pbkdf2_hmac('sha256', pass_bytes, salt, iters, dk_len)


def derive(pass_str: str, salt: bytes, iters: int):
    key = pbkdf2(pass_str.encode('utf-8'), salt, iters, 64)
    k_enc, k_mac = key[:32], key[32:]
    kid = base64.b64encode(hmac.new(k_mac, b'kid', hashlib.sha256).digest()[:8]).decode()
    return k_enc, k_mac, kid


def _keystream_xor(k_enc: bytes, iv: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    idx = 0
    done = 0
    while done < len(data):
        block = hmac.new(k_enc, iv + struct.pack('>I', idx), hashlib.sha256).digest()
        take = min(32, len(data) - done)
        for i in range(take):
            out[done + i] = data[done + i] ^ block[i]
        done += take
        idx += 1
    return bytes(out)


def encrypt(pass_str: str, salt: bytes, iv: bytes, plain: str, iters: int) -> dict:
    k_enc, k_mac, kid = derive(pass_str, salt, iters)
    ct = _keystream_xor(k_enc, iv, plain.encode('utf-8'))
    tag = hmac.new(k_mac, iv + ct, hashlib.sha256).digest()[:16]
    return {
        'kid': kid,
        'ct': base64.b64encode(ct).decode(),
        'tag': base64.b64encode(tag).decode(),
    }


def decrypt(pass_str: str, salt: bytes, iv: bytes, ct_b64: str, tag_b64: str, iters: int):
    k_enc, k_mac, _ = derive(pass_str, salt, iters)
    ct = base64.b64decode(ct_b64)
    want = base64.b64decode(tag_b64)
    got = hmac.new(k_mac, iv + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(got, want):
        return None
    return _keystream_xor(k_enc, iv, ct).decode('utf-8')


def lookup_kid(pass_str: str, salt: bytes, iters: int) -> str:
    return derive(pass_str, salt, iters)[2]


if __name__ == '__main__':
    import os
    s = os.urandom(16)
    v = os.urandom(12)
    r = encrypt('张三1234', s, v, '{"hello":"世界"}', 1000)
    print('kid', r['kid'])
    print('roundtrip ok:', decrypt('张三1234', s, v, r['ct'], r['tag'], 1000) == '{"hello":"世界"}')
    print('wrong pass  :', decrypt('李四5678', s, v, r['ct'], r['tag'], 1000))
    # 输出测试向量给 Node 跨语言校验
    import json
    print('VECTOR ' + json.dumps({
        'salt': base64.b64encode(s).decode(), 'iv': base64.b64encode(v).decode(),
        'iters': 1000, 'pass': '张三1234', 'kid': r['kid'], 'ct': r['ct'], 'tag': r['tag'],
        'plain': '{"hello":"世界"}'}, ensure_ascii=False))
