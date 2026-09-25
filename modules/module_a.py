"""订单模块（重构后）：只调用统一编解码接口。"""
from codec import decode as _decode, encode as _encode


def serialize_order(record) -> bytes:
    return _encode(record)


def parse_order(data: bytes):
    return _decode(data)
