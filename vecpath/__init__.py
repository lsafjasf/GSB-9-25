"""矢量路径解析与几何计算库 (仅标准库)。"""

from .parser import Segment, parse
from .geometry import (
    point_at,
    segment_bbox,
    path_bbox,
    segment_length,
    path_length,
)

__all__ = [
    "Segment",
    "parse",
    "point_at",
    "segment_bbox",
    "path_bbox",
    "segment_length",
    "path_length",
]
