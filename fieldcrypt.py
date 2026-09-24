"""fieldcrypt — 字段级加密库（仅 Python 标准库）。

设计要点
========
* 加密算法: ChaCha20 (RFC 8439) 流加密，纯 Python 实现。
* 完整性:   Encrypt-then-MAC，HMAC-SHA256 截断为 16 字节标签。
            头部单独一个标签，密文按 4 KiB 分块、每块一个标签，
            因此篡改可以被定位到具体的块 / 字节偏移。
* 关联数据: AAD（如记录主键）参与 MAC 计算，不随密文存储；
            AAD 不匹配时解密失败，可发现跨记录复制密文。
* 密钥轮换: 密文头部携带 2 字节密钥版本；解密按版本取钥，
            加密永远使用当前活跃版本。重加密按记录原子替换，
            可中断、可重入。

密文格式（大端）::

    magic    2B   b"FC"
    fmt_ver  1B   格式版本，当前为 1
    key_ver  2B   密钥版本
    nonce    12B  随机初始向量
    ct_len   8B   密文体长度（不含头部与标签）
    body     N B  ChaCha20 密文
    tag_h    16B  HMAC(km, "hdr" || aad_hash || header)
    tag_i    16B  HMAC(km, "chk" || aad_hash || header || idx || chunk_i)
                  每个 4 KiB 密文块一个标签

空明文没有密文块，仅含 tag_h。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Callable, Dict, MutableSequence, Optional, Sequence

__all__ = [
    "FieldCryptError", "KeyMissingError", "UnknownKeyVersionError",
    "FormatError", "TruncatedCiphertextError", "LengthMismatchError",
    "AuthenticationError", "IntegrityError", "AadMismatchError",
    "KeyStore", "encrypt", "decrypt", "encrypt_many", "decrypt_many",
    "peek_key_version", "ciphertext_info", "needs_rotation", "rotate",
    "reencrypt_batch", "CHUNK_SIZE", "TAG_SIZE", "HEADER_SIZE",
]

# ---------------------------------------------------------------- 错误类型


class FieldCryptError(Exception):
    """所有 fieldcrypt 错误的基类。"""


class KeyMissingError(FieldCryptError):
    """密钥缺失：密钥库为空、没有活跃密钥，或库中根本没有任何密钥。"""


class UnknownKeyVersionError(FieldCryptError):
    """密钥版本未知：密文头部声明的版本在密钥库中不存在。"""

    def __init__(self, version: int):
        self.version = version
        super().__init__(f"未知密钥版本: {version}")


class FormatError(FieldCryptError):
    """密文格式非法（魔数 / 格式版本不匹配）。"""


class TruncatedCiphertextError(FieldCryptError):
    """密文被截断：实际长度小于头部声明的长度。"""

    def __init__(self, section: str, expected: int, actual: int):
        self.section = section
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"密文被截断 ({section}): 期望 {expected} 字节, 实际 {actual} 字节"
        )


class LengthMismatchError(FieldCryptError):
    """长度与头部不符：实际长度大于头部声明的长度（尾部多出字节）。"""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"长度与头部不符: 头部声明总长 {expected} 字节, 实际 {actual} 字节"
        )


class AuthenticationError(FieldCryptError):
    """完整性 / 真实性校验失败的基类。"""

    def __init__(self, message: str, location: Optional[str] = None,
                 offset: Optional[int] = None):
        self.location = location
        self.offset = offset
        super().__init__(message)


class IntegrityError(AuthenticationError):
    """密文被篡改：包含篡改位置（区域与字节偏移）。"""


class AadMismatchError(AuthenticationError):
    """提供了 AAD 且校验失败：关联信息不匹配（或密文被篡改）。

    密码学上无法区分「AAD 不匹配」与「密文被篡改」，只要调用方
    传入了 AAD 且认证失败就抛出本异常。
    """


# ---------------------------------------------------------------- 常量

MAGIC = b"FC"
FMT_VERSION = 1
CHUNK_SIZE = 4096
TAG_SIZE = 16
KEY_SIZE = 32

_HEADER = struct.Struct(">2sBH12sQ")  # magic, fmt_ver, key_ver, nonce, ct_len
HEADER_SIZE = _HEADER.size  # 25
_U32 = struct.Struct(">I")


# ---------------------------------------------------------------- ChaCha20 (RFC 8439)

def _rotl32(v: int, c: int) -> int:
    return ((v << c) & 0xFFFFFFFF) | (v >> (32 - c))


def _quarter_round(s: list, a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & 0xFFFFFFFF
    s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & 0xFFFFFFFF
    s[b] = _rotl32(s[b] ^ s[c], 7)


_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)


def _chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    state = (list(_CONSTANTS) + list(struct.unpack("<8I", key))
             + [counter] + list(struct.unpack("<3I", nonce)))
    work = state.copy()
    for _ in range(10):
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
    return struct.pack("<16I", *((work[i] + state[i]) & 0xFFFFFFFF
                                 for i in range(16)))


def _chacha20_xor(key: bytes, nonce: bytes, data: bytes,
                  initial_counter: int = 1) -> bytes:
    out = bytearray(len(data))
    counter = initial_counter
    offset = 0
    n = len(data)
    while offset + 64 <= n:
        block = _chacha20_block(key, counter & 0xFFFFFFFF, nonce)
        mixed = int.from_bytes(data[offset:offset + 64], "little") ^ \
            int.from_bytes(block, "little")
        out[offset:offset + 64] = mixed.to_bytes(64, "little")
        counter += 1
        offset += 64
    if offset < n:
        block = _chacha20_block(key, counter & 0xFFFFFFFF, nonce)
        tail = data[offset:]
        out[offset:] = bytes(a ^ b for a, b in zip(tail, block))
    return bytes(out)


# ---------------------------------------------------------------- 密钥派生 / MAC

def _derive_keys(master: bytes) -> tuple:
    """从 32 字节主密钥派生加密钥与 MAC 钥。"""
    ke = hmac.new(master, b"fieldcrypt/enc", hashlib.sha256).digest()
    km = hmac.new(master, b"fieldcrypt/mac", hashlib.sha256).digest()
    return ke, km


def _tag(km: bytes, *parts: bytes) -> bytes:
    h = hmac.new(km, digestmod=hashlib.sha256)
    for p in parts:
        h.update(p)
    return h.digest()[:TAG_SIZE]


# ---------------------------------------------------------------- 密钥库


class KeyStore:
    """版本化密钥库。

    * ``add(version, key)`` 注册一个版本，默认设为活跃版本。
    * 解密按密文头部版本取钥；加密永远使用活跃版本。
    * 旧版本密钥保留即可解密旧数据；确认全部重加密后可删除旧版本。
    """

    def __init__(self) -> None:
        self._keys: Dict[int, bytes] = {}
        self._active: Optional[int] = None

    def add(self, version: int, key: bytes, make_active: bool = True) -> None:
        if not isinstance(version, int) or not (0 <= version <= 0xFFFF):
            raise ValueError("version 必须是 0..65535 的整数")
        if len(key) != KEY_SIZE:
            raise ValueError(f"密钥长度必须为 {KEY_SIZE} 字节")
        self._keys[version] = bytes(key)
        if make_active or self._active is None:
            self._active = version

    def remove(self, version: int) -> None:
        if version == self._active:
            raise ValueError("不能删除活跃密钥版本")
        self._keys.pop(version, None)

    @property
    def active_version(self) -> Optional[int]:
        return self._active

    def set_active(self, version: int) -> None:
        if version not in self._keys:
            raise UnknownKeyVersionError(version)
        self._active = version

    def versions(self) -> Sequence[int]:
        return sorted(self._keys)

    def active_key(self) -> bytes:
        if self._active is None or self._active not in self._keys:
            raise KeyMissingError("密钥库中没有活跃密钥，无法加密")
        return self._keys[self._active]

    def key_for(self, version: int) -> bytes:
        if not self._keys:
            raise KeyMissingError("密钥库为空，没有任何密钥")
        try:
            return self._keys[version]
        except KeyError:
            raise UnknownKeyVersionError(version) from None


# ---------------------------------------------------------------- 加解密


def _chunk_count(ct_len: int) -> int:
    return (ct_len + CHUNK_SIZE - 1) // CHUNK_SIZE


def _expected_total(ct_len: int) -> int:
    return HEADER_SIZE + ct_len + TAG_SIZE * (1 + _chunk_count(ct_len))


def encrypt(plaintext: bytes, store: KeyStore, aad: bytes = b"") -> bytes:
    """加密单个字段。``aad`` 为关联信息（如记录主键），解密时必须一致。"""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    if isinstance(aad, str):
        aad = aad.encode("utf-8")
    key = store.active_key()
    ke, km = _derive_keys(key)
    nonce = os.urandom(12)
    header = _HEADER.pack(MAGIC, FMT_VERSION, store.active_version, nonce,
                          len(plaintext))
    ct = _chacha20_xor(ke, nonce, plaintext)
    aad_hash = hashlib.sha256(aad).digest()
    tags = [_tag(km, b"hdr", aad_hash, header)]
    for i in range(_chunk_count(len(ct))):
        chunk = ct[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        tags.append(_tag(km, b"chk", aad_hash, header, _U32.pack(i), chunk))
    return header + ct + b"".join(tags)


def _parse_header(data: bytes):
    if len(data) < HEADER_SIZE:
        raise TruncatedCiphertextError("header", HEADER_SIZE, len(data))
    magic, fmt_ver, key_ver, nonce, ct_len = _HEADER.unpack(
        data[:HEADER_SIZE])
    if magic != MAGIC:
        raise FormatError(f"魔数不匹配: {magic!r}")
    if fmt_ver != FMT_VERSION:
        raise FormatError(f"不支持的格式版本: {fmt_ver}")
    return key_ver, nonce, ct_len


def decrypt(ciphertext: bytes, store: KeyStore, aad: bytes = b"") -> bytes:
    """解密并校验完整性。AAD 不匹配或密文被篡改都会抛出异常。"""
    if isinstance(aad, str):
        aad = aad.encode("utf-8")
    key_ver, nonce, ct_len = _parse_header(ciphertext)
    expected = _expected_total(ct_len)
    actual = len(ciphertext)
    if actual < expected:
        raise TruncatedCiphertextError("body", expected, actual)
    if actual > expected:
        raise LengthMismatchError(expected, actual)

    key = store.key_for(key_ver)
    ke, km = _derive_keys(key)
    header = ciphertext[:HEADER_SIZE]
    ct = ciphertext[HEADER_SIZE:HEADER_SIZE + ct_len]
    tags = ciphertext[HEADER_SIZE + ct_len:]
    aad_hash = hashlib.sha256(aad).digest()
    aad_given = len(aad) > 0

    def _fail(location: str, offset: int):
        msg = f"完整性校验失败: {location} (字节偏移 {offset})"
        if aad_given:
            raise AadMismatchError(
                msg + "；AAD 不匹配或密文被篡改", location, offset)
        raise IntegrityError(msg, location, offset)

    if not hmac.compare_digest(
            tags[:TAG_SIZE], _tag(km, b"hdr", aad_hash, header)):
        _fail("header/tag", 0)
    for i in range(_chunk_count(ct_len)):
        chunk = ct[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        want = _tag(km, b"chk", aad_hash, header, _U32.pack(i), chunk)
        got = tags[(i + 1) * TAG_SIZE:(i + 2) * TAG_SIZE]
        if not hmac.compare_digest(got, want):
            _fail(f"chunk {i}", HEADER_SIZE + i * CHUNK_SIZE)
    return _chacha20_xor(ke, nonce, ct)


def encrypt_many(items: Sequence[bytes], store: KeyStore,
                 aad_for: Optional[Callable[[int], bytes]] = None) -> list:
    """批量加密。``aad_for(i)`` 返回第 i 条的关联信息。"""
    return [encrypt(p, store, aad=aad_for(i) if aad_for else b"")
            for i, p in enumerate(items)]


def decrypt_many(items: Sequence[bytes], store: KeyStore,
                 aad_for: Optional[Callable[[int], bytes]] = None) -> list:
    """批量解密。任一记录失败即抛出对应异常。"""
    return [decrypt(c, store, aad=aad_for(i) if aad_for else b"")
            for i, c in enumerate(items)]


# ---------------------------------------------------------------- 轮换 / 重加密


def peek_key_version(ciphertext: bytes) -> int:
    """只解析头部，返回密文的密钥版本（不需要密钥）。"""
    key_ver, _, _ = _parse_header(ciphertext)
    return key_ver


def ciphertext_info(ciphertext: bytes) -> dict:
    """返回密文的结构信息（版本、明文长度、块数、总长度）。"""
    key_ver, _, ct_len = _parse_header(ciphertext)
    return {
        "key_version": key_ver,
        "plaintext_len": ct_len,
        "chunks": _chunk_count(ct_len),
        "total_len": _expected_total(ct_len),
    }


def needs_rotation(ciphertext: bytes, store: KeyStore) -> bool:
    """密文是否不是用当前活跃密钥加密的。"""
    return peek_key_version(ciphertext) != store.active_version


def rotate(ciphertext: bytes, store: KeyStore, aad: bytes = b"") -> bytes:
    """把单条密文重加密为活跃版本；已是活跃版本则原样返回（幂等）。"""
    if not needs_rotation(ciphertext, store):
        return ciphertext
    return encrypt(decrypt(ciphertext, store, aad=aad), store, aad=aad)


def reencrypt_batch(records: MutableSequence, store: KeyStore,
                    aad_for: Optional[Callable[[int], bytes]] = None,
                    on_record: Optional[Callable[[int], None]] = None,
                    start: int = 0) -> int:
    """就地批量重加密，可中断、可重入。

    每条记录独立处理：先算好新密文，再一次赋值替换 ``records[i]``。
    任意时刻中断，序列中每个元素要么是完整的旧版本密文，要么是
    完整的新版本密文，不存在中间态；重新调用（可带 ``start`` 断点，
    或不带从头扫描）即可继续，已是活跃版本的记录会被跳过。

    返回本次实际重加密的记录数。
    """
    rotated = 0
    for i in range(start, len(records)):
        new = rotate(records[i], store,
                     aad=aad_for(i) if aad_for else b"")
        if new is not records[i]:
            records[i] = new
            rotated += 1
        if on_record is not None:
            on_record(i)
    return rotated
