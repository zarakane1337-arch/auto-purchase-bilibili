"""本地 JSON 持久化：Cookie、账号信息、常用配置。"""

from __future__ import annotations

import copy
import json
import os
import threading


class Store:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}

    def save(self) -> None:
        with self._lock:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = copy.deepcopy(value)
            self.save()

    def update(self, patch: dict) -> None:
        with self._lock:
            self._data.update(copy.deepcopy(patch or {}))
            self.save()

    def drop(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self.save()
