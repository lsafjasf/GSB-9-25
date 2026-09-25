"""报表模块（重构后）：只调用统一编解码接口。"""
from codec import decode as _decode, encode as _encode


def serialize_report_row(record) -> bytes:
    return _encode(record)


def parse_report_row(data: bytes):
    return _decode(data)
