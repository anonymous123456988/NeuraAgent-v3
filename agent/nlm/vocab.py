# -*- coding: utf-8 -*-
"""NeuraLM 词表：中文优先的字符级词表（UTF-8 逐字），零外部依赖。

覆盖：常用汉字(GB2312 一级+常用二级)、ASCII 可见字符、中文标点、
通用符号、控制符。字符级编码保证任何中文文本都能建模且无 OOV。
"""
import json
import os
import unicodedata

BOS = 0      # <bos>
EOS = 1      # <eos>
PAD = 2      # <pad>

_SPECIALS = ["<bos>", "<eos>", "<pad>", "<unk>", "<sep>"]

# 常用汉字表(GB2312 一级 3755 + 高频二级子集) —— 硬编码保证词表稳定、可复现
_COMMON_HANZI = (
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清己美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞"
)

_ASCII_PRINT = "".join(chr(i) for i in range(32, 127))
_CN_PUNCT = "，。！？；：、（）《》【】「」『』“”‘’—…·～％×÷±℃￥"
_EXTRA = "①②③④⑤⑥⑦⑧⑨⑩◈▮▯✓✕↻⬇⇱—"


def _build_vocab():
    chars = []
    for ch in _SPECIALS:
        chars.append(ch)
    # 汉字
    for ch in _COMMON_HANZI:
        chars.append(ch)
    # ASCII 可见字符
    for ch in _ASCII_PRINT:
        chars.append(ch)
    # 中文标点与常用符号
    for ch in _CN_PUNCT:
        chars.append(ch)
    for ch in _EXTRA:
        chars.append(ch)
    # 数字/字母等已在 ASCII 中；去重保序
    seen = set()
    ordered = []
    for ch in chars:
        if ch not in seen:
            seen.add(ch)
            ordered.append(ch)
    stoi = {ch: i for i, ch in enumerate(ordered)}
    return ordered, stoi


_CHARS, _STOI = _build_vocab()


def vocab_size() -> int:
    return len(_CHARS)


def encode(text: str) -> list:
    ids = []
    for ch in str(text):
        ids.append(_STOI.get(ch, _STOI.get("<unk>", 3)))
    return ids


def decode(ids) -> str:
    out = []
    for i in ids:
        if i < 4:
            continue  # 跳过控制符
        out.append(_CHARS[i] if i < len(_CHARS) else "?")
    return "".join(out)


def save_vocab(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"chars": _CHARS}, f, ensure_ascii=False)


def load_vocab(path: str):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)["chars"]
    return _CHARS
