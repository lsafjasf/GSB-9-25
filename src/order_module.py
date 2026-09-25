"""订单模块：重构后只调用统一编解码接口，不再自带序列化实现。"""
from src.codec import decode_record, encode_record


def serialize_order(record):
    return encode_record(record)


def parse_order(data):
    return decode_record(data)
