# -*- coding: utf-8 -*-
"""翻译核心：腾讯 TranSmart + 有道词典 + 降级通道 + 本地缓存。

特点：
- 零第三方依赖，使用持久 HTTPS 连接降低延迟；
- 单词走有道拿词性/释义/短语/例句，整句走腾讯翻译；
- 网络失败自动降级到百度词典 / MyMemory；
- 所有结果可序列化缓存，重复划词基本零等待。
"""
from __future__ import annotations

import gzip
import html
import http.client
import json
import logging
import re
import ssl
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_SSL_CONTEXT = ssl.create_default_context()


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Sense:
    pos: str
    meaning: str


@dataclass
class Result:
    text: str
    src_lang: str                 # en / zh
    translation: str
    senses: list[Sense] = field(default_factory=list)
    phrases: list[tuple[str, str]] = field(default_factory=list)
    forms: list[tuple[str, str]] = field(default_factory=list)
    examples: list[tuple[str, str]] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    phones: tuple[str, str] = ("", "")   # (英/拼音, 美)
    engine: str = ""
    latency_ms: int = 0
    from_cache: bool = False

    def pos_summary(self) -> str:
        out: list[str] = []
        for s in self.senses:
            p = (s.pos or "").strip()
            if p and p not in out:
                out.append(p)
        return " / ".join(out[:5])

    def plain_text(self) -> str:
        lines = [self.translation.strip()]
        for s in self.senses:
            if s.meaning:
                prefix = f"{s.pos} " if s.pos else ""
                lines.append(f"{prefix}{s.meaning}".strip())
        if self.phrases:
            lines.append("")
            lines.append("常用短语：")
            for a, b in self.phrases:
                lines.append(f"{a}  {b}".strip())
        if self.examples:
            lines.append("")
            for a, b in self.examples[:1]:
                lines.append(a)
                lines.append(b)
        return "\n".join(x for x in lines if x is not None).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "src_lang": self.src_lang,
            "translation": self.translation,
            "senses": [{"pos": s.pos, "meaning": s.meaning} for s in self.senses],
            "phrases": [list(x) for x in self.phrases],
            "forms": [list(x) for x in self.forms],
            "examples": [list(x) for x in self.examples],
            "terms": list(self.terms),
            "phones": list(self.phones),
            "engine": self.engine,
            "latency_ms": int(self.latency_ms),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Result":
        def pairs(key: str) -> list[tuple[str, str]]:
            out = []
            for item in d.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append((str(item[0]), str(item[1])))
            return out

        phones = d.get("phones") or ["", ""]
        if not isinstance(phones, (list, tuple)):
            phones = ["", ""]
        phones = tuple(str(x) for x in (list(phones) + ["", ""])[:2])
        return cls(
            text=str(d.get("text", "")),
            src_lang=str(d.get("src_lang", "en")),
            translation=str(d.get("translation", "")),
            senses=[
                Sense(str(x.get("pos", "")), str(x.get("meaning", "")))
                for x in (d.get("senses") or [])
                if isinstance(x, dict)
            ],
            phrases=pairs("phrases"),
            forms=pairs("forms"),
            examples=pairs("examples"),
            terms=[str(x) for x in (d.get("terms") or [])],
            phones=phones,  # type: ignore[arg-type]
            engine=str(d.get("engine", "")),
            latency_ms=int(d.get("latency_ms", 0) or 0),
        )


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_text(text: str) -> str:
    text = (text or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_lang(text: str) -> str:
    cjk = len(_CJK_RE.findall(text or ""))
    latin = sum(1 for c in (text or "") if ("a" <= c.lower() <= "z"))
    if cjk and cjk >= max(1, (cjk + latin) * 0.18):
        return "zh"
    return "en"


def _strip_wrapping(text: str) -> str:
    t = (text or "").strip()
    pairs = [
        ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
        ("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"),
        ("《", "》"), ("<", ">"),
    ]
    changed = True
    while changed and len(t) >= 2:
        changed = False
        for left, right in pairs:
            if t.startswith(left) and t.endswith(right):
                t = t[1:-1].strip()
                changed = True
        t = t.strip(" \t,，.。;；:：!！?？、")
    return t


def lookup_candidate(text: str) -> Optional[str]:
    """判断是否值得查词典；返回清理后的查询词。"""
    t = _strip_wrapping(normalize_text(text))
    if not t:
        return None
    if _CJK_RE.search(t):
        if len(t) <= 12 and re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9·]+", t):
            return t
        return None
    if any(ch in t for ch in ".?!;:。！？；："):
        return None
    tokens = t.split()
    if not (1 <= len(tokens) <= 4):
        return None
    if len(t) > 48:
        return None
    for tok in tokens:
        if not re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*", tok):
            return None
    return t


def _clean_meaning(s: str) -> str:
    def _label(m: re.Match) -> str:
        inner = (m.group(1) or "").strip()
        if inner.lower() in {"b", "i", "em", "span", "br", "strong", "sup", "sub"}:
            return ""
        return f"（{inner}）" if inner else ""

    s = re.sub(r"<([^<>]{1,18})>", _label, s or "")
    s = _HTML_TAG_RE.sub("", s)
    s = s.replace(";", "；")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("；;，, ")
    return s


def _i_text(node: Any) -> str:
    """有道 JSON 里的 i 字段可能是字符串、列表或字典。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, dict):
        if "#text" in node:
            return _i_text(node.get("#text"))
        if "i" in node:
            return _i_text(node.get("i"))
        return ""
    if isinstance(node, (list, tuple)):
        return "".join(_i_text(x) for x in node)
    return str(node)


_POS_MAP = {
    "n": "n.", "v": "v.", "vt": "vt.", "vi": "vi.", "adj": "adj.",
    "adv": "adv.", "prep": "prep.", "pron": "pron.", "conj": "conj.",
    "num": "num.", "art": "art.", "aux": "aux.", "int": "int.",
    "interj": "int.", "abbr": "abbr.", "phr": "phr.", "modal": "modal.",
    "det": "det.", "part": "part.", "pl": "pl.", "sing": "sing.",
}

_CN_POS = {
    "名词": "n.", "动词": "v.", "形容词": "adj.", "副词": "adv.",
    "代词": "pron.", "介词": "prep.", "连词": "conj.", "数词": "num.",
    "量词": "classifier", "助词": "part.", "叹词": "int.",
    "拟声词": "onom.", "成语": "idiom", "短语": "phr.",
}


def _parse_pos(raw: str) -> tuple[str, str]:
    """从 'n. 招呼...' / '【名】...' 里拆出词性和释义。"""
    s = (raw or "").strip()
    m = re.match(r"^([A-Za-z]{1,6}\.)\s*", s)
    if m:
        pos = m.group(1).lower()
        return _POS_MAP.get(pos.rstrip("."), pos), s[m.end():].strip()
    m = re.match(r"^【([^】]{1,4})】\s*", s)
    if m:
        zh = m.group(1)
        pos = _CN_POS.get(zh, zh)
        return pos, s[m.end():].strip()
    return "", s


def _dedupe_pairs(items: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a, b in items:
        a = re.sub(r"\s+", " ", a or "").strip()
        b = re.sub(r"\s+", " ", b or "").strip()
        key = (a + "\n" + b).casefold()
        if not a or key in seen:
            continue
        seen.add(key)
        out.append((a, b))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 持久 HTTPS 连接
# ---------------------------------------------------------------------------
class HttpSession:
    """每个主机一个长连接；调用方可能来自多个线程，用锁串行化。"""

    def __init__(self, host: str, timeout: float = 5.0):
        self.host = host
        self.timeout = timeout
        self._conn: Optional[http.client.HTTPSConnection] = None
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _connect(self) -> None:
        self.close()
        self._conn = http.client.HTTPSConnection(
            self.host, timeout=self.timeout, context=_SSL_CONTEXT
        )
        self._conn.connect()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> tuple[int, bytes]:
        hdrs = {
            "User-Agent": _UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            hdrs.update(headers)
        with self._lock:
            last_exc: Optional[BaseException] = None
            for attempt in range(2):
                try:
                    if self._conn is None:
                        self._connect()
                    assert self._conn is not None
                    if timeout is not None:
                        self._conn.timeout = timeout
                    self._conn.request(method, path, body=body, headers=hdrs)
                    resp = self._conn.getresponse()
                    data = resp.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        self.close()
                        raise ProviderError("响应过大")
                    status = int(resp.status)
                    enc = (resp.getheader("Content-Encoding") or "").lower()
                    if "gzip" in enc:
                        try:
                            data = gzip.decompress(data)
                        except Exception:
                            pass
                    elif "deflate" in enc:
                        try:
                            data = zlib.decompress(data)
                        except Exception:
                            try:
                                data = zlib.decompress(data, -zlib.MAX_WBITS)
                            except Exception:
                                pass
                    if resp.will_close:
                        self.close()
                    return status, data
                except ProviderError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    self.close()
                    if attempt == 0:
                        time.sleep(0.03)
                        continue
                    raise ProviderError(f"{self.host} 连接失败：{exc}") from exc
            raise ProviderError(f"{self.host} 连接失败：{last_exc}")


# ---------------------------------------------------------------------------
# 腾讯 TranSmart
# ---------------------------------------------------------------------------
class TencentTranslator:
    def __init__(self) -> None:
        self.http = HttpSession("transmart.qq.com", timeout=4.6)
        self.engine = "腾讯翻译"

    def translate(self, text: str, target: str) -> tuple[str, str]:
        payload = {
            "header": {
                "fn": "auto_translation",
                "client_key": "browser-chrome-120.0.0-Windows-10",
            },
            "type": "plain",
            "model_category": "normal",
            "source": {
                "lang": "auto",
                "text": text,
                "text_list": [text],
            },
            "target": {"lang": target},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, data = self.http.request(
            "POST", "/api/imt", body=body,
            headers={
                "Content-Type": "application/json",
                "Referer": "https://transmart.qq.com/zh-CN/index",
                "Origin": "https://transmart.qq.com",
            },
        )
        if status != 200:
            raise ProviderError(f"腾讯翻译 HTTP {status}")
        try:
            d = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise ProviderError("腾讯翻译返回格式错误") from exc
        header = d.get("header") or {}
        if str(header.get("ret_code")) != "succ":
            msg = d.get("message") or header.get("ret_code") or "未知错误"
            raise ProviderError(f"腾讯翻译：{msg}")
        arr = d.get("auto_translation") or []
        if isinstance(arr, list):
            out = "".join(str(x) for x in arr if x)
        else:
            out = str(arr)
        out = out.strip()
        if not out:
            raise ProviderError("腾讯翻译结果为空")
        return out, str(d.get("src_lang") or "")


# ---------------------------------------------------------------------------
# 有道词典
# ---------------------------------------------------------------------------
class YoudaoDictionary:
    def __init__(self) -> None:
        self.http = HttpSession("dict.youdao.com", timeout=4.8)
        self.engine = "有道词典"

    def lookup(self, query: str, lang: str) -> Optional[dict[str, Any]]:
        path = "/jsonapi?q=" + urllib.parse.quote(query, safe="")
        status, data = self.http.request(
            "GET", path,
            headers={"Referer": "https://dict.youdao.com/"},
            max_bytes=3 * 1024 * 1024,
        )
        if status != 200:
            raise ProviderError(f"有道词典 HTTP {status}")
        try:
            d = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise ProviderError("有道词典返回格式错误") from exc
        if lang == "zh":
            return self._parse_zh(d, query)
        return self._parse_en(d, query)

    # -- 英文 ----------------------------------------------------------
    def _parse_en(self, d: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
        ec = d.get("ec") or {}
        words = ec.get("word") or []
        if isinstance(words, dict):
            words = [words]
        if not words:
            return self._parse_web_only(d, query, "en")
        word = words[0] if isinstance(words[0], dict) else {}
        senses: list[Sense] = []
        for tr in (word.get("trs") or []):
            for item in (tr.get("tr") or []):
                l = item.get("l") or {}
                raw = _i_text(l.get("i"))
                if not raw:
                    continue
                lpos = str(l.get("pos") or "").strip()
                pos, meaning = _parse_pos(raw)
                if lpos:
                    pos = _POS_MAP.get(lpos.rstrip(".").lower(), lpos)
                meaning = _clean_meaning(meaning)
                if meaning:
                    senses.append(Sense(pos, meaning))
                if len(senses) >= 10:
                    break
            if len(senses) >= 10:
                break
        senses = _dedupe_senses(senses)

        phones = ("", "")
        simple = d.get("simple") or {}
        sw = simple.get("word") or []
        if isinstance(sw, dict):
            sw = [sw]
        if sw and isinstance(sw[0], dict):
            phones = (
                str(sw[0].get("ukphone") or sw[0].get("phone") or ""),
                str(sw[0].get("usphone") or ""),
            )

        forms: list[tuple[str, str]] = []
        for wf in (word.get("wfs") or []):
            f = wf.get("wf") or {}
            if f.get("name") and f.get("value"):
                forms.append((str(f["name"]), str(f["value"])))
        forms = _dedupe_pairs(forms, 8)

        phrases: list[tuple[str, str]] = []
        phrs = d.get("phrs") or {}
        for item in (phrs.get("phrs") or []):
            phr = item.get("phr") or {}
            head = _i_text((phr.get("headword") or {}).get("l"))
            trans: list[str] = []
            for t in (phr.get("trs") or []):
                tt = _i_text((t.get("tr") or {}).get("l"))
                if tt:
                    trans.append(_clean_meaning(tt))
            if head:
                phrases.append((head.strip(), "；".join(x for x in trans if x)))
        phrases = _dedupe_pairs(phrases, 8)

        examples = self._examples(d)
        summary = _make_summary(senses, phones)
        return {
            "senses": senses,
            "phrases": phrases,
            "forms": forms,
            "examples": examples,
            "terms": [],
            "phones": phones,
            "translation": summary,
            "engine": self.engine,
        }

    # -- 中文 ----------------------------------------------------------
    def _parse_zh(self, d: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
        ce = d.get("ce") or {}
        words = ce.get("word") or []
        if isinstance(words, dict):
            words = [words]
        terms: list[str] = []
        ce_senses: list[Sense] = []
        phones = ("", "")
        if words and isinstance(words[0], dict):
            w = words[0]
            phones = (str(w.get("phone") or ""), "")
            for tr in (w.get("trs") or []):
                for item in (tr.get("tr") or []):
                    l = item.get("l") or {}
                    term = _i_text(l.get("i")).strip()
                    if term:
                        for part in re.split(r"[;；]", term):
                            part = part.strip()
                            if part and part not in terms:
                                terms.append(part)
                    tr_text = str(l.get("#tran") or "").strip()
                    if tr_text:
                        ce_senses.append(Sense("", _clean_meaning(tr_text)))
        newhh = d.get("newhh") or {}
        data_list = newhh.get("dataList") or []
        if isinstance(data_list, dict):
            data_list = [data_list]
        zh_senses: list[Sense] = []
        for item in data_list:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("cat") or "").strip()
            for sense in (item.get("sense") or []):
                if not isinstance(sense, dict):
                    continue
                cat2 = str(sense.get("cat") or cat).strip()
                pos = _CN_POS.get(cat2, cat2)
                for definition in (sense.get("def") or []):
                    meaning = _clean_meaning(str(definition))
                    if meaning:
                        zh_senses.append(Sense(pos, meaning))
        if not zh_senses:
            zh_senses = ce_senses
        zh_senses = _dedupe_senses(zh_senses)
        examples = self._examples(d)
        if not terms and not zh_senses:
            return self._parse_web_only(d, query, "zh")
        terms = [t for t in terms if re.search(r"[A-Za-z]", t)]
        return {
            "senses": zh_senses[:8],
            "phrases": [],
            "forms": [],
            "examples": examples,
            "terms": terms[:8],
            "phones": phones,
            "translation": " / ".join(terms[:3]),
            "engine": self.engine,
        }

    def _examples(self, d: dict[str, Any]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        part = d.get("blng_sents_part") or {}
        pairs = part.get("sentence-pair") or []
        if isinstance(pairs, dict):
            pairs = [pairs]
        for p in pairs:
            if not isinstance(p, dict):
                continue
            src = _HTML_TAG_RE.sub("", str(p.get("sentence") or "")).strip()
            dst = _HTML_TAG_RE.sub("", str(p.get("sentence-translation") or "")).strip()
            src = re.sub(r"\s+", " ", src)
            dst = re.sub(r"\s+", " ", dst)
            if 3 <= len(src) <= 260 and dst:
                out.append((src, dst))
            if len(out) >= 2:
                break
        return out

    def _parse_web_only(self, d: dict[str, Any], query: str,
                        lang: str) -> Optional[dict[str, Any]]:
        wt = d.get("web_trans") or {}
        items = wt.get("web-translation") or []
        if not items:
            return None
        first = items[0] if isinstance(items[0], dict) else {}
        trans = first.get("trans") or []
        vals: list[str] = []
        for t in trans:
            if isinstance(t, dict) and t.get("value"):
                v = str(t["value"]).strip()
                if v and v not in vals:
                    vals.append(v)
        if not vals:
            return None
        term = " / ".join(vals[:3])
        return {
            "senses": [],
            "phrases": [],
            "forms": [],
            "examples": self._examples(d),
            "terms": vals[:5] if lang == "zh" else [],
            "phones": ("", ""),
            "translation": term,
            "engine": self.engine,
        }


def _dedupe_senses(senses: list[Sense]) -> list[Sense]:
    out: list[Sense] = []
    seen: set[str] = set()
    for s in senses:
        s.pos = (s.pos or "").strip()
        s.meaning = (s.meaning or "").strip()
        key = (s.pos + "\n" + s.meaning).casefold()
        if not s.meaning or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _make_summary(senses: list[Sense], phones: tuple[str, str]) -> str:
    bits: list[str] = []
    for s in senses[:3]:
        meaning = s.meaning
        # 只取前两个义项，避免主翻译过长
        parts = [x.strip() for x in re.split(r"[；;]", meaning) if x.strip()]
        if parts:
            bits.append("；".join(parts[:2]))
    text = "；".join(bits)
    if len(text) > 100:
        text = text[:99].rstrip("；，, ") + "…"
    return text


# ---------------------------------------------------------------------------
# 百度词典（轻量降级）
# ---------------------------------------------------------------------------
class BaiduDictionary:
    def __init__(self) -> None:
        self.http = HttpSession("fanyi.baidu.com", timeout=4.0)
        self.engine = "百度词典"

    def lookup(self, query: str) -> Optional[str]:
        body = urllib.parse.urlencode({"kw": query}).encode("utf-8")
        status, data = self.http.request(
            "POST", "/sug", body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://fanyi.baidu.com/",
            },
        )
        if status != 200:
            raise ProviderError(f"百度词典 HTTP {status}")
        try:
            d = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise ProviderError("百度词典返回格式错误") from exc
        if int(d.get("errno") or 0) != 0:
            raise ProviderError("百度词典返回错误")
        rows = d.get("data") or []
        if not rows:
            return None
        first = rows[0] if isinstance(rows[0], dict) else {}
        value = str(first.get("v") or "").strip()
        return value or None


# ---------------------------------------------------------------------------
# MyMemory（整句兜底）
# ---------------------------------------------------------------------------
class MyMemoryTranslator:
    def __init__(self) -> None:
        self.http = HttpSession("api.mymemory.translated.net", timeout=7.0)
        self.engine = "MyMemory"

    def translate(self, text: str, src_lang: str) -> str:
        pair = "zh-CN|en" if src_lang == "zh" else "en|zh-CN"
        q = urllib.parse.quote(text[:500], safe="")
        path = f"/get?q={q}&langpair={urllib.parse.quote(pair, safe='')}"
        status, data = self.http.request("GET", path)
        if status != 200:
            raise ProviderError(f"MyMemory HTTP {status}")
        try:
            d = json.loads(data.decode("utf-8", "replace"))
        except Exception as exc:
            raise ProviderError("MyMemory 返回格式错误") from exc
        rd = d.get("responseData") or {}
        out = html.unescape(str(rd.get("translatedText") or "")).strip()
        if not out or "PLEASE SELECT" in out.upper():
            raise ProviderError("MyMemory 结果为空")
        return out


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
class TranslationCache:
    def __init__(self, path: Optional[Path] = None, ttl_days: int = 21,
                 max_entries: int = 1600):
        self.path = Path(path) if path else None
        self.ttl = timedelta(days=ttl_days)
        self.max_entries = max_entries
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self.load()

    @staticmethod
    def key(text: str, src_lang: str) -> str:
        return f"{src_lang}|{text}"

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            entries = d.get("entries") if isinstance(d, dict) else {}
            if isinstance(entries, dict):
                self._data = {
                    str(k): v for k, v in entries.items() if isinstance(v, dict)
                }
            self._prune_locked()
        except Exception:
            log.exception("读取翻译缓存失败，将忽略旧缓存")
            self._data = {}

    def get(self, text: str, src_lang: str, force: bool = False) -> Optional[Result]:
        if force:
            return None
        key = self.key(text, src_lang)
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            try:
                ts = float(item.get("ts") or 0)
                if datetime.now().timestamp() - ts > self.ttl.total_seconds():
                    self._data.pop(key, None)
                    self._dirty = True
                    return None
                r = Result.from_dict(item.get("result") or {})
                r.from_cache = True
                r.latency_ms = 0
                return r
            except Exception:
                self._data.pop(key, None)
                return None

    def put(self, text: str, src_lang: str, result: Result) -> None:
        key = self.key(text, src_lang)
        with self._lock:
            self._data[key] = {
                "ts": datetime.now().timestamp(),
                "result": result.to_dict(),
            }
            self._prune_locked()
            self._dirty = True

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._dirty = True

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def save(self, force: bool = False) -> None:
        if not self.path:
            return
        with self._lock:
            if not (self._dirty or force):
                return
            snapshot = json.dumps(
                {"version": 1, "entries": self._data},
                ensure_ascii=False, separators=(",", ":"),
            )
            self._dirty = False
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(snapshot, encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("保存翻译缓存失败")

    def _prune_locked(self) -> None:
        now = datetime.now().timestamp()
        ttl = self.ttl.total_seconds()
        expired = [k for k, v in self._data.items()
                   if now - float(v.get("ts") or 0) > ttl]
        for k in expired:
            self._data.pop(k, None)
        if len(self._data) > self.max_entries:
            ordered = sorted(
                self._data.items(), key=lambda kv: float(kv[1].get("ts") or 0)
            )
            for k, _ in ordered[: len(self._data) - self.max_entries]:
                self._data.pop(k, None)


# ---------------------------------------------------------------------------
# 汇总翻译器
# ---------------------------------------------------------------------------
class Translator:
    def __init__(self, cache_path: Optional[Path] = None):
        self.tencent = TencentTranslator()
        self.youdao = YoudaoDictionary()
        self.baidu = BaiduDictionary()
        self.mymemory = MyMemoryTranslator()
        self.cache = TranslationCache(cache_path)

    def translate(self, text: str, src_override: Optional[str] = None,
                  force: bool = False) -> Result:
        text = normalize_text(text)
        if not text:
            raise ProviderError("没有可翻译的文字")
        if len(text) > 1500:
            text = text[:1500]
        src_lang = src_override or detect_lang(text)
        src_lang = "zh" if src_lang == "zh" else "en"
        target = "en" if src_lang == "zh" else "zh"
        if not force:
            cached = self.cache.get(text, src_lang)
            if cached:
                return cached

        started = time.perf_counter()
        candidate = lookup_candidate(text)
        got: dict[str, Any] = {}
        errors: list[str] = []

        def run_tencent() -> None:
            try:
                trans, detected = self.tencent.translate(text, target)
                got["tencent"] = (trans, detected)
            except Exception as exc:
                errors.append(str(exc))
                log.info("腾讯翻译失败：%s", exc)

        def run_youdao() -> None:
            if not candidate:
                return
            try:
                data = self.youdao.lookup(candidate, src_lang)
                if data:
                    got["youdao"] = data
            except Exception as exc:
                errors.append(str(exc))
                log.info("有道查询失败：%s", exc)

        threads = [
            threading.Thread(target=run_tencent, name="tencent", daemon=True),
        ]
        if candidate:
            threads.append(
                threading.Thread(target=run_youdao, name="youdao", daemon=True)
            )
        for t in threads:
            t.start()
        deadline = time.time() + 5.2
        for t in threads:
            t.join(max(0.0, deadline - time.time()))

        yd = got.get("youdao") or {}
        tc = got.get("tencent")
        translation = ""
        engines: list[str] = []
        if tc:
            translation = tc[0]
            engines.append(self.tencent.engine)
            detected = tc[1]
            if detected in ("en", "zh") and detected != src_lang:
                src_lang = detected
                target = "en" if src_lang == "zh" else "zh"
        if yd:
            if not translation:
                translation = str(yd.get("translation") or "")
            if yd.get("senses") or yd.get("phrases") or yd.get("examples"):
                engines.append(self.youdao.engine)

        if not translation:
            # 单词优先用百度，整句用 MyMemory
            if candidate:
                try:
                    v = self.baidu.lookup(candidate)
                    if v:
                        translation = v
                        engines.append(self.baidu.engine)
                except Exception as exc:
                    errors.append(str(exc))
                    log.info("百度词典失败：%s", exc)
            if not translation:
                try:
                    translation = self.mymemory.translate(text, src_lang)
                    engines.append(self.mymemory.engine)
                except Exception as exc:
                    errors.append(str(exc))
                    log.info("MyMemory 失败：%s", exc)

        if not translation:
            reason = errors[-1] if errors else "网络不可用"
            raise ProviderError(f"翻译失败：{reason}")

        senses: list[Sense] = list(yd.get("senses") or [])
        phrases = list(yd.get("phrases") or [])
        forms = list(yd.get("forms") or [])
        examples = list(yd.get("examples") or [])
        terms = list(yd.get("terms") or [])
        phones = tuple(yd.get("phones") or ("", ""))

        if not senses:
            # 从百度/腾讯的释义里至少给出可用信息，不强行拆词性
            pass
        if src_lang == "zh":
            # 中文查词时，把腾讯的英文翻译也作为表达之一
            if translation and re.search(r"[A-Za-z]", translation):
                for part in re.split(r"[/;；]", translation):
                    p = part.strip()
                    if p and p not in terms:
                        terms.insert(0, p)
            terms = _dedupe_strings(terms)[:6]

        latency = int((time.perf_counter() - started) * 1000)
        result = Result(
            text=text,
            src_lang=src_lang,
            translation=translation.strip(),
            senses=senses[:8],
            phrases=phrases[:8],
            forms=forms[:6],
            examples=examples[:2],
            terms=terms[:6],
            phones=phones,  # type: ignore[arg-type]
            engine=" + ".join(_dedupe_strings(engines)) or "在线翻译",
            latency_ms=latency,
        )
        try:
            self.cache.put(text, src_lang, result)
        except Exception:
            log.exception("写入翻译缓存失败")
        return result

    def save_cache(self, force: bool = False) -> None:
        self.cache.save(force=force)


def _dedupe_strings(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        s = str(x).strip()
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out
