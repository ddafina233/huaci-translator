# -*- coding: utf-8 -*-
"""生词本存储：JSON 持久化、按天分组、搜索与 CSV 导出。"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except Exception:
        return _now_local()


def normalize_key(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


class VocabStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.load()

    # ------------------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            d = json.loads(raw)
            entries = d.get("entries") if isinstance(d, dict) else None
            if isinstance(entries, list):
                self._entries = [e for e in entries if isinstance(e, dict) and e.get("text")]
            self._sort()
        except Exception:
            log.exception("读取生词本失败，已备份损坏文件")
            try:
                backup = self.path.with_name(
                    self.path.stem + ".损坏-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".json"
                )
                os.replace(self.path, backup)
            except Exception:
                pass
            self._entries = []

    def _sort(self) -> None:
        self._entries.sort(
            key=lambda e: _parse_dt(str(e.get("created_at") or "")).timestamp(),
            reverse=True,
        )

    def save(self) -> None:
        with self._lock:
            data = {"version": 1, "entries": self._entries}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(self.path)
            except Exception:
                log.exception("保存生词本失败")

    # ------------------------------------------------------------------
    def add(
        self,
        text: str,
        translation: str,
        pos: str = "",
        src_lang: str = "en",
        engine: str = "",
        created_at: Optional[datetime] = None,
    ) -> tuple[dict[str, Any], bool]:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        translation = str(translation or "").strip()
        with self._lock:
            key = normalize_key(text)
            for e in self._entries:
                if normalize_key(str(e.get("text") or "")) == key:
                    return e, False
            dt = created_at or _now_local()
            entry = {
                "id": uuid.uuid4().hex[:12],
                "text": text,
                "translation": translation,
                "pos": str(pos or "").strip(),
                "src_lang": "zh" if src_lang == "zh" else "en",
                "engine": str(engine or "").strip(),
                "created_at": dt.isoformat(timespec="seconds"),
            }
            self._entries.insert(0, entry)
            self._sort()
            self.save()
            return entry, True

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.get("id") != entry_id]
            if len(self._entries) != before:
                self.save()
                return True
            return False

    def has(self, text: str) -> bool:
        key = normalize_key(text)
        with self._lock:
            return any(normalize_key(str(e.get("text") or "")) == key for e in self._entries)

    def find(self, text: str) -> Optional[dict[str, Any]]:
        key = normalize_key(text)
        with self._lock:
            for e in self._entries:
                if normalize_key(str(e.get("text") or "")) == key:
                    return dict(e)
        return None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._entries]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def today_count(self) -> int:
        today = _now_local().date()
        with self._lock:
            return sum(
                1 for e in self._entries
                if _parse_dt(str(e.get("created_at") or "")).date() == today
            )

    # ------------------------------------------------------------------
    def grouped(self, search: str = "") -> list[dict[str, Any]]:
        """按自然日分组，日期倒序。返回 [{date, label, weekday, entries}]。"""
        q = normalize_key(search)
        with self._lock:
            entries = [dict(e) for e in self._entries]

        if q:
            filtered = []
            for e in entries:
                hay = " ".join([
                    str(e.get("text") or ""),
                    str(e.get("translation") or ""),
                    str(e.get("pos") or ""),
                ])
                if q in normalize_key(hay):
                    filtered.append(e)
            entries = filtered

        buckets: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for e in entries:
            buckets[_parse_dt(str(e.get("created_at") or "")).date()].append(e)

        today = _now_local().date()
        out: list[dict[str, Any]] = []
        for day in sorted(buckets.keys(), reverse=True):
            items = buckets[day]
            items.sort(
                key=lambda e: _parse_dt(str(e.get("created_at") or "")).timestamp(),
                reverse=True,
            )
            if day == today:
                label = "今天"
            elif day == today - timedelta(days=1):
                label = "昨天"
            elif day.year == today.year:
                label = f"{day.month}月{day.day}日"
            else:
                label = f"{day.year}年{day.month}月{day.day}日"
            out.append({
                "date": day,
                "label": label,
                "weekday": _WEEKDAYS[day.weekday()],
                "entries": items,
            })
        return out

    # ------------------------------------------------------------------
    def export_csv(self, path: Path) -> int:
        path = Path(path)
        with self._lock:
            entries = [dict(e) for e in self._entries]
        entries.sort(
            key=lambda e: _parse_dt(str(e.get("created_at") or "")).timestamp(),
            reverse=True,
        )
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["日期", "时间", "单词/短语", "词性", "翻译", "来源语言", "翻译引擎"])
            for e in entries:
                dt = _parse_dt(str(e.get("created_at") or ""))
                writer.writerow([
                    dt.strftime("%Y-%m-%d"),
                    dt.strftime("%H:%M:%S"),
                    e.get("text", ""),
                    e.get("pos", ""),
                    e.get("translation", ""),
                    "中文" if e.get("src_lang") == "zh" else "英文",
                    e.get("engine", ""),
                ])
        return len(entries)

    def time_text(self, entry: dict[str, Any]) -> str:
        return _parse_dt(str(entry.get("created_at") or "")).strftime("%H:%M")
