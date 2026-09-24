"""
field_crypto — 字段级加密库（仅依赖 Python 标准库）

设计要点
--------
* 标准库没有 AES，因此用 HMAC-SHA256 构造 CTR 风格的密钥流做加密，
  并对每个分块做 encrypt-then-MAC（HMAC-SHA256）认证。
* 密文自封头：MAGIC | 密钥版本 | 随机 IV | AAD 标签 | 明文长度 | 分块参数，
  之后是若干 ``密文分块 + 分块标签``。分块 MAC 使得篡改可以定位到具体分块/字节偏移。
* 关联信息（AAD，如记录主键）被纳入认证：AAD 不匹配时解密失败，
  从而发现跨记录复制密文。
* 密钥按版本管理：旧版本密钥保留用于解密，新写入总是使用 active 版本；
  重加密以单条记录为原子单位，可中断、可重入。

密文格式（全部大端）::

    MAGIC            4 字节  b"FLE1"
    KEY_VERSION      4 字节  密钥版本号
    IV              16 字节  随机初始向量
    AAD_TAG         32 字节  HMAC(mac_key, "aad-tag" || aad)
    PLAINTEXT_LEN    8 字节  明文总长度
    CHUNK_SIZE       4 字节  每分块明文字节数
    NUM_CHUNKS       4 字节  分块数量（>=1，空明文也是 1 个空分块）
    重复 NUM_CHUNKS 次:
        CHUNK_CT     <= CHUNK_SIZE 字节
        CHUNK_TAG    32 字节  HMAC(mac_key, "chunk" || HEADER || aad || idx || CHUNK_CT)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Callable, Iterable, List, Optional, Tuple

MAGIC = b"FLE1"
IV_LEN = 16
TAG_LEN = 32
CHUNK_SIZE = 64 * 1024  # 每个分块的明文字节数
HEADER_STRUCT = struct.Struct(">4sI16s32sQII")
HEADER_LEN = HEADER_STRUCT.size  # 72
_STREAM_BLOCK = hashlib.sha256().digest_size  # 32 字节密钥流/块
_BLOCKS_PER_CHUNK = CHUNK_SIZE // _STREAM_BLOCK

__all__ = [
    "FieldEncryptionError",
    "KeyMissingError",
    "UnknownKeyVersionError",
    "InvalidFormatError",
    "TruncatedCiphertextError",
    "LengthMismatchError",
    "IntegrityError",
    "AssociatedDataMismatchError",
    "KeyStore",
    "generate_key",
    "encrypt",
    "decrypt",
    "peek_key_version",
    "needs_rotation",
    "rotate",
    "reencrypt_batch",
]


# ---------------------------------------------------------------- 错误类型

class FieldEncryptionError(Exception):
    """本库所有错误的基类。"""


class KeyMissingError(FieldEncryptionError):
    """密钥库为空、或没有 active 密钥，无法加密。"""


class UnknownKeyVersionError(FieldEncryptionError):
    """密文引用的密钥版本在密钥库中不存在。"""

    def __init__(self, version: int):
        self.version = version
        super().__init__(f"unknown key version: {version}")


class InvalidFormatError(FieldEncryptionError):
    """MAGIC 不符，不是本库产生的密文。"""


class TruncatedCiphertextError(FieldEncryptionError):
    """密文被截断：实际长度小于头部声明的长度。"""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"ciphertext truncated: header expects {expected} bytes, got {actual}"
        )


class LengthMismatchError(FieldEncryptionError):
    """长度与头部不符：头部字段自相矛盾，或实际长度多于声明长度。"""


class IntegrityError(FieldEncryptionError):
    """分块完整性校验失败（密文被篡改），携带篡改位置。"""

    def __init__(self, chunk_index: int, byte_offset: int):
        self.chunk_index = chunk_index
        self.byte_offset = byte_offset
        super().__init__(
            f"integrity check failed at chunk {chunk_index} "
            f"(ciphertext byte offset {byte_offset})"
        )


class AssociatedDataMismatchError(FieldEncryptionError):
    """关联信息（AAD）不匹配：密文可能被从别的记录复制过来。"""


# ---------------------------------------------------------------- 密钥管理

def generate_key() -> bytes:
    """生成一个 256 位随机密钥。"""
    return os.urandom(32)


class KeyStore:
    """版本化密钥库。

    * ``add(version, key)`` 登记一个密钥版本；第一个加入的版本自动成为 active。
    * ``set_active(version)`` 切换写密钥（轮换）；旧版本保留用于解密。
    * 密钥永不在库内被删除——删除前必须保证所有引用它的密文都已重加密。
    """

    def __init__(self) -> None:
        self._keys: dict[int, bytes] = {}
        self.active_version: Optional[int] = None

    def add(self, version: int, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise ValueError("key must be at least 16 bytes")
        self._keys[int(version)] = bytes(key)
        if self.active_version is None:
            self.active_version = int(version)

    def set_active(self, version: int) -> None:
        if version not in self._keys:
            raise KeyMissingError(f"cannot activate missing key version {version}")
        self.active_version = version

    def get(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise UnknownKeyVersionError(version) from None

    def active(self) -> Tuple[int, bytes]:
        if self.active_version is None or not self._keys:
            raise KeyMissingError("no active key in keystore")
        return self.active_version, self._keys[self.active_version]

    @property
    def versions(self) -> List[int]:
        return sorted(self._keys)


# ---------------------------------------------------------------- 内部工具

def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _derive_keys(master: bytes, version: int) -> Tuple[bytes, bytes]:
    """从主密钥派生加密密钥与 MAC 密钥（按版本隔离）。"""
    v = struct.pack(">I", version)
    enc_key = _hmac(master, b"field-crypto/enc" + v)
    mac_key = _hmac(master, b"field-crypto/mac" + v)
    return enc_key, mac_key


def _keystream(enc_key: bytes, iv: bytes, first_block: int, nbytes: int) -> bytes:
    """生成 nbytes 密钥流，起始计数器为 first_block。"""
    out = bytearray()
    counter = first_block
    while len(out) < nbytes:
        out += _hmac(enc_key, iv + struct.pack(">Q", counter))
        counter += 1
    return bytes(out[:nbytes])


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _chunk_tag(mac_key: bytes, header: bytes, aad: bytes, index: int, ct: bytes) -> bytes:
    h = hmac.new(mac_key, digestmod=hashlib.sha256)
    h.update(b"chunk")
    h.update(header)
    h.update(aad)
    h.update(struct.pack(">Q", index))
    h.update(ct)
    return h.digest()


def _expected_length(plaintext_len: int, chunk_size: int, num_chunks: int) -> int:
    """根据头部字段计算整个 token 应有的字节数。"""
    if num_chunks < 1:
        raise LengthMismatchError("num_chunks must be >= 1")
    if plaintext_len > num_chunks * chunk_size:
        raise LengthMismatchError(
            f"plaintext_len {plaintext_len} exceeds capacity of {num_chunks} chunks"
        )
    last = plaintext_len - (num_chunks - 1) * chunk_size
    if not (0 < last <= chunk_size) and not (plaintext_len == 0 and num_chunks == 1):
        raise LengthMismatchError(
            f"num_chunks {num_chunks} inconsistent with plaintext_len {plaintext_len}"
        )
    return HEADER_LEN + (num_chunks - 1) * (chunk_size + TAG_LEN) + last + TAG_LEN


# ---------------------------------------------------------------- 加解密

def encrypt(store: KeyStore, plaintext: bytes, aad: bytes = b"") -> bytes:
    """用 active 版本密钥加密，返回自描述密文 token。"""
    version, master = store.active()
    enc_key, mac_key = _derive_keys(master, version)
    iv = os.urandom(IV_LEN)

    num_chunks = max(1, (len(plaintext) + CHUNK_SIZE - 1) // CHUNK_SIZE)
    aad_tag = _hmac(mac_key, b"aad-tag" + aad)
    header = HEADER_STRUCT.pack(
        MAGIC, version, iv, aad_tag, len(plaintext), CHUNK_SIZE, num_chunks
    )

    out = bytearray(header)
    for i in range(num_chunks):
        pt_chunk = plaintext[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        stream = _keystream(enc_key, iv, i * _BLOCKS_PER_CHUNK, len(pt_chunk))
        ct_chunk = _xor(pt_chunk, stream)
        out += ct_chunk
        out += _chunk_tag(mac_key, header, aad, i, ct_chunk)
    return bytes(out)


def _parse_header(token: bytes):
    if len(token) < HEADER_LEN:
        raise TruncatedCiphertextError(HEADER_LEN, len(token))
    magic, version, iv, aad_tag, pt_len, chunk_size, num_chunks = HEADER_STRUCT.unpack(
        token[:HEADER_LEN]
    )
    if magic != MAGIC:
        raise InvalidFormatError("bad magic: not a field_crypto token")
    if chunk_size != CHUNK_SIZE:
        raise LengthMismatchError(
            f"unsupported chunk_size {chunk_size}, expected {CHUNK_SIZE}"
        )
    return version, iv, aad_tag, pt_len, num_chunks


def peek_key_version(token: bytes) -> int:
    """不解密读取密文的密钥版本（用于判断是否需要轮换）。"""
    version, _, _, _, _ = _parse_header(token)
    return version


def decrypt(store: KeyStore, token: bytes, aad: bytes = b"") -> bytes:
    """解密并校验完整性；AAD 不匹配或密文被篡改时抛出对应错误。"""
    version, iv, aad_tag, pt_len, num_chunks = _parse_header(token)
    expected = _expected_length(pt_len, CHUNK_SIZE, num_chunks)
    if len(token) < expected:
        raise TruncatedCiphertextError(expected, len(token))
    if len(token) > expected:
        raise LengthMismatchError(
            f"token has {len(token) - expected} trailing bytes beyond header-declared length"
        )

    master = store.get(version)  # 未知版本 -> UnknownKeyVersionError
    enc_key, mac_key = _derive_keys(master, version)
    header = token[:HEADER_LEN]

    if not hmac.compare_digest(aad_tag, _hmac(mac_key, b"aad-tag" + aad)):
        raise AssociatedDataMismatchError(
            "associated data mismatch: token was not encrypted for this record"
        )

    out = bytearray()
    pos = HEADER_LEN
    for i in range(num_chunks):
        clen = min(CHUNK_SIZE, pt_len - i * CHUNK_SIZE)
        ct_chunk = token[pos:pos + clen]
        tag = token[pos + clen:pos + clen + TAG_LEN]
        if not hmac.compare_digest(tag, _chunk_tag(mac_key, header, aad, i, ct_chunk)):
            raise IntegrityError(chunk_index=i, byte_offset=pos)
        stream = _keystream(enc_key, iv, i * _BLOCKS_PER_CHUNK, clen)
        out += _xor(ct_chunk, stream)
        pos += clen + TAG_LEN

    if len(out) != pt_len:  # 防御性检查，正常流程不会触发
        raise LengthMismatchError("decrypted length does not match header")
    return bytes(out)


# ---------------------------------------------------------------- 轮换 / 重加密

def needs_rotation(store: KeyStore, token: bytes) -> bool:
    """密文是否仍用旧版本密钥加密。"""
    return peek_key_version(token) != store.active_version


def rotate(store: KeyStore, token: bytes, aad: bytes = b"") -> bytes:
    """把单条密文重加密到 active 版本；已是当前版本则原样返回。

    该操作是纯函数：要么返回完整的新密文，要么抛异常且原密文不受影响，
    因此调用方以“替换字段值”为原子动作即可保证中断后数据始终可解。
    """
    if not needs_rotation(store, token):
        return token
    return encrypt(store, decrypt(store, token, aad), aad)


def reencrypt_batch(
    store: KeyStore,
    records: Iterable[dict],
    field: str,
    aad_of: Callable[[dict], bytes],
    should_stop: Optional[Callable[[int], bool]] = None,
) -> Tuple[int, int]:
    """批量重加密，可中断、可重入。

    逐条处理：每条记录要么仍是旧密文，要么已换成新密文，不存在中间态；
    中断后再次调用会跳过已轮换的记录（幂等）。

    :param records:      可变的 dict 序列，``record[field]`` 为密文 bytes
    :param aad_of:       由记录生成 AAD（如主键）的函数
    :param should_stop:  每处理一条后回调，返回 True 则模拟中断/提前停止
    :return:             (已轮换条数, 跳过条数)
    """
    rotated = skipped = 0
    for rec in records:
        token = rec[field]
        if not needs_rotation(store, token):
            skipped += 1
            continue
        rec[field] = rotate(store, token, aad_of(rec))
        rotated += 1
        if should_stop is not None and should_stop(rotated):
            break
    return rotated, skipped
