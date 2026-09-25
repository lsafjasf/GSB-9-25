"""旧版字符实体编解码工具（保留五类现网缺陷，仅用于复现，请勿使用）。

缺陷清单：
1. encode 无条件转义 `&`，已编码内容被二次转义；
2. decode 对不完整实体（如 `&lt`）静默原样输出，下游解析失败；
3. decode 对数字实体不校验范围，越界/代理区码位产生非法字符或裸 ValueError；
4. decode 不识别 `&apos;`，encode/decode 往返不能还原原文；
5. 实体名大小写敏感，`&AMP;` 与 `&amp;` 被区别对待。
"""

import re

NAMED = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    # 缺陷 4：遗漏 "apos"，encode 产出的 &apos; 无法被 decode 还原
}

_ENTITY_RE = re.compile(r"&([A-Za-z]+|#[0-9]+|#x[0-9A-Fa-f]+);")


def encode(text):
    # 缺陷 1：无条件转义 &，" &lt; " 变成 "&amp;lt;"
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def decode(text):
    def repl(match):
        name = match.group(1)
        # 缺陷 5：按原样查表，大小写不同即 KeyError
        if name.startswith("#x"):
            # 缺陷 3：不校验码位范围，&#xD800; 产生非法代理区字符
            return chr(int(name[2:], 16))
        if name.startswith("#"):
            return chr(int(name[1:], 10))
        return NAMED[name]

    # 缺陷 2：正则不匹配的残缺实体（如 "&lt"）被静默原样保留
    return _ENTITY_RE.sub(repl, text)
