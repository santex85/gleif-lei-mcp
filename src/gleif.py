"""Thin, rate-limited client for the public GLEIF API (https://api.gleif.org/api/v1), ported from the gleif-lei-lookup actor.

GLEIF publishes LEI data under CC0 and rate-limits the API to 60 requests per minute per user.
We keep at least MIN_INTERVAL seconds between requests and back off on HTTP 429/5xx.
"""
import asyncio
import os
import difflib
import re
import time
import unicodedata

import httpx

API = "https://api.gleif.org/api/v1"
MIN_INTERVAL = 1.1  # ~55 requests/minute, under GLEIF's 60/min limit
UA = os.environ.get("GLEIF_USER_AGENT") or "gleif-lei-mcp/0.1 (MCP server for the public GLEIF LEI API; python-httpx)"


class Gleif:
    def __init__(self, log):
        self.log = log
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.requests = 0
        self.client = httpx.AsyncClient(
            headers={"Accept": "application/vnd.api+json", "User-Agent": UA}, timeout=httpx.Timeout(20, connect=10)
        )
        self._legal_forms: dict[str, str | None] = {}

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, params: dict | None = None) -> dict | None:
        """GET a JSON:API resource. Returns None on 404 (e.g. no parent reported)."""
        url = path if path.startswith("http") else f"{API}/{path}"
        for attempt in range(3):
            async with self._lock:
                wait = self._last + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                self.requests += 1
            try:
                r = await self.client.get(url, params=params)
            except httpx.HTTPError as e:
                self.log.warning(f"{url} -> {e}")
                await asyncio.sleep(2 + attempt * 3)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                retry = r.headers.get("Retry-After")
                delay = min(int(retry) if retry and retry.isdigit() else 5 + attempt * 5, 15)
                self.log.warning(f"{url} -> HTTP {r.status_code}, waiting {delay}s")
                await asyncio.sleep(delay)
                continue
            self.log.warning(f"{url} -> HTTP {r.status_code}: {r.text[:200]}")
            return None
        return None

    async def legal_form_name(self, code: str | None) -> str | None:
        """Resolve an ISO 20275 ELF code to a readable name (cached)."""
        if not code or code in ("8888", "9999"):
            return None
        if code not in self._legal_forms:
            d = await self.get(f"entity-legal-forms/{code}")
            name = None
            if d:
                names = (d.get("data", {}).get("attributes", {}) or {}).get("names") or []
                en = [n for n in names if n.get("languageCode") == "en"]
                pick = (en or names or [{}])[0]
                name = pick.get("localName") or pick.get("transliteratedName")
            self._legal_forms[code] = name
        return self._legal_forms[code]


# ---------- name helpers ----------

def _is_latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return bool(letters) and all(ord(c) < 0x250 for c in letters)


def all_names(entity: dict) -> list[str]:
    names = [(entity.get("legalName") or {}).get("name")]
    names += [n.get("name") for n in entity.get("otherNames") or []]
    names += [n.get("name") for n in entity.get("transliteratedOtherNames") or []]
    return [n for n in names if n]


def english_name(entity: dict) -> str | None:
    """Best Latin-script name: legal name if Latin, else an English/transliterated alternative."""
    legal = (entity.get("legalName") or {}).get("name")
    if legal and _is_latin(legal):
        return legal
    for group in ("transliteratedOtherNames", "otherNames"):
        for n in entity.get(group) or []:
            if n.get("name") and _is_latin(n["name"]):
                return n["name"]
    return legal


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_LEGAL_WORDS = {
    "the", "public", "company", "co", "limited", "ltd", "inc", "incorporated", "corp", "corporation",
    "plc", "llc", "llp", "lp", "gmbh", "ag", "sa", "s", "a", "nv", "bv", "spa", "srl", "sas", "kk",
    "pte", "pt", "tbk", "pcl", "bhd", "sdn", "holdings", "holding", "group", "and", "&", "of",
}


def _core(s: str) -> str:
    """Normalised name without legal-form words ("The X Public Company Limited" -> "x")."""
    return " ".join(w for w in _norm(s).split() if w not in _LEGAL_WORDS)


def match_score(query: str, entity: dict) -> float:
    """0..100 similarity between the query and the closest entity name.
    Exact match on the core name (legal-form words removed) scores 100; names that merely
    start with the query score 85-95; otherwise a plain sequence-similarity ratio."""
    q, qc = _norm(query), _core(query)
    best = 0.0
    for n in all_names(entity):
        nn, nc = _norm(n), _core(n)
        if qc and qc == nc:
            return 100.0
        ratio = difflib.SequenceMatcher(None, qc or q, nc or nn).ratio()
        if qc and nc.startswith(qc):
            ratio = max(ratio, 0.85 + 0.10 * len(qc) / max(len(nc), 1))
        best = max(best, ratio)
    return round(best * 100, 1)
