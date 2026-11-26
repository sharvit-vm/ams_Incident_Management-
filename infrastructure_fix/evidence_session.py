"""Per-investigation, verified-target evidence collection and caching."""
from threading import RLock


class EvidenceSession:
    def __init__(self, target, collector, platform: str):
        if platform not in {"aws", "onprem"}:
            raise ValueError("Unsupported infrastructure platform")
        self._target = target
        self._collector = collector
        self._platform = platform
        self._records = {}
        self._lock = RLock()
        self._onprem_collected = False

    def read(self, *kinds: str) -> dict:
        """Collect requested AWS evidence once; on-prem uses one fixed diagnostic call."""
        with self._lock:
            if self._platform == "onprem" and not self._onprem_collected:
                for record in self._collector.collect(self._target):
                    self._records[record.kind] = record
                self._onprem_collected = True
            result = {}
            for kind in kinds:
                if self._platform == "aws" and kind not in self._records:
                    self._records[kind] = self._collector.collect_kind(self._target, kind)
                record = self._records.get(kind)
                result[kind] = record.model_dump(mode="json") if record else {
                    "kind": kind, "status": "unavailable", "reason": "Evidence was not collected."
                }
            return result

    def records(self):
        with self._lock:
            return list(self._records.values())
