"""GLEIF LEI MCP server: search legal entities, fetch LEI records, ownership (parents/children), validate LEIs.

Data: Global Legal Entity Identifier Foundation (GLEIF) public API, LEI data under CC0 1.0.
Not affiliated with GLEIF. Ported from the gleif-lei-lookup Apify actor (no Apify dependency).
"""
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(__file__))
from gleif import Gleif, english_name  # noqa: E402
from records import LEI_RE, PERSONAL_CATEGORIES, build_record, search_name  # noqa: E402

mcp = FastMCP("gleif-lei", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
log = logging.getLogger("gleif-lei-mcp")
SOURCE = "GLEIF LEI data (https://www.gleif.org), CC0 1.0. Not affiliated with or endorsed by GLEIF."
MAX_SEARCH = 25
MAX_CHILDREN = 100
_g: Gleif | None = None


def _gleif() -> Gleif:
    """One shared, rate-limited client (GLEIF allows 60 requests/minute)."""
    global _g
    if _g is None:
        _g = Gleif(log)
    return _g


def _norm_lei(lei: str) -> str:
    return "".join((lei or "").split()).upper()


def lei_checksum_ok(lei: str) -> bool:
    """ISO 17442 / ISO 7064 MOD 97-10: letters -> 10..35, the whole number mod 97 must equal 1."""
    if not LEI_RE.match(lei):
        return False
    return int("".join(str(int(c, 36)) for c in lei)) % 97 == 1


def _lei_error(lei: str) -> str | None:
    if not LEI_RE.match(lei):
        return f"'{lei}' is not a valid LEI format (20 chars: 18 alphanumeric + 2 check digits)."
    if not lei_checksum_ok(lei):
        return f"'{lei}' fails the ISO 17442 checksum (typo?)."
    return None


def _personal_stub(rec: dict) -> dict:
    at = rec.get("attributes") or {}
    return {"lei": rec.get("id"), "category": "SOLE_PROPRIETOR", "status": (at.get("entity") or {}).get("status"),
            "registrationStatus": (at.get("registration") or {}).get("status"),
            "note": "Sole-proprietor LEI (a natural person): details are not returned for privacy."}


def _summary(rec: dict, score: float | None = None) -> dict:
    at = rec.get("attributes") or {}
    e = at.get("entity") or {}
    la = e.get("legalAddress") or {}
    out = {"lei": rec.get("id"), "legalName": (e.get("legalName") or {}).get("name"), "legalNameEn": english_name(e),
           "country": la.get("country"), "city": la.get("city"), "status": e.get("status"),
           "registrationStatus": (at.get("registration") or {}).get("status"), "category": e.get("category"),
           "registrationAuthorityEntityId": e.get("registeredAs")}
    if score is not None:
        out["matchScore"] = score
    return out


@mcp.tool()
async def search_companies(name: str, country: str | None = None, limit: int = 10, min_score: float = 0) -> dict:
    """Find legal entities in the Global LEI Index by company name (fuzzy, any language/script).

    name: company name, legal-form words (Ltd, GmbH, Corporation, Public Company Limited...) are ignored for scoring,
          e.g. "Siam Commercial Bank", "Toyota Motor", "Deutsche Bank".
    country: optional ISO 3166 alpha-2 code of the legal address, e.g. "TH", "JP", "US", "DE".
    limit: 1..25 (default 10). min_score: drop candidates below this 0..100 similarity (100 = exact core name).
    Returns {source, query, count, results:[{lei, legalName (original script), legalNameEn, country, city, status
    (ACTIVE/INACTIVE), registrationStatus (ISSUED/LAPSED/RETIRED...), category (GENERAL/FUND/BRANCH...),
    registrationAuthorityEntityId (company number in the local register), matchScore}]}, best match first.
    Sole-proprietor LEIs (natural persons) are excluded. Use get_lei for the full record. Matching is statistical:
    confirm with register number/address before compliance decisions. Takes ~2-5 s (GLEIF rate limit 60 req/min).
    """
    name = (name or "").strip()
    if not name:
        return {"error": "name is empty", "source": SOURCE}
    country = (country or "").strip().upper() or None
    limit = max(1, min(int(limit), MAX_SEARCH))
    g = _gleif()
    ranked = await search_name(g, name[:200], country, limit + 5)
    results = [_summary(r, s) for s, r in ranked
               if (r["attributes"]["entity"].get("category") not in PERSONAL_CATEGORIES) and s >= min_score][:limit]
    return {"source": SOURCE, "query": name, "country": country, "count": len(results), "results": results}


@mcp.tool()
async def get_lei(lei: str, include_ownership: bool = True) -> dict:
    """Full GLEIF reference record for one LEI (20-character Legal Entity Identifier, e.g. "5493006W3QUS5LMH6R84").

    Returns {source, record:{lei, legalName, legalNameEn, otherNames, status, registrationStatus, category,
    legalFormCode (ISO 20275), legalForm, jurisdiction, country/city/region/postalCode/addressLine (legal address),
    hqCountry/hqCity/hqAddressLine, registrationAuthorityId, registrationAuthorityEntityId (local company number),
    entityCreationDate, initialRegistrationDate, lastUpdate, nextRenewal, corroborationLevel, managingLou,
    bic (SWIFT codes), successorLei, url (search.gleif.org page), and with include_ownership=true also
    directParent / ultimateParent {lei, name, country, exceptionReason}}}.
    exceptionReason explains a missing parent (e.g. NATURAL_PERSONS, NON_CONSOLIDATING, NO_KNOWN_PERSON).
    Returns {error} for malformed / checksum-failing / unknown LEIs. Sole-proprietor records are reduced to status only.
    """
    lei = _norm_lei(lei)
    if err := _lei_error(lei):
        return {"error": err, "source": SOURCE}
    g = _gleif()
    d = await g.get(f"lei-records/{lei}")
    rec = (d or {}).get("data")
    if not rec:
        return {"error": f"LEI {lei} not found in GLEIF (or the API is temporarily unavailable).", "source": SOURCE}
    if (rec.get("attributes") or {}).get("entity", {}).get("category") in PERSONAL_CATEGORIES:
        return {"source": SOURCE, "record": _personal_stub(rec)}
    return {"source": SOURCE, "record": await build_record(g, rec, include_ownership)}


async def _children(g: Gleif, lei: str, kind: str, limit: int) -> dict:
    d = await g.get(f"lei-records/{lei}/{kind}", {"page[size]": limit, "page[number]": 1})
    data = (d or {}).get("data") or []
    total = (((d or {}).get("meta") or {}).get("pagination") or {}).get("total", len(data))
    items = [_personal_stub(r) if (r.get("attributes") or {}).get("entity", {}).get("category") in PERSONAL_CATEGORIES
             else _summary(r) for r in data]
    return {"total": total, "returned": len(items), "items": items}


@mcp.tool()
async def company_ownership(lei: str, max_children: int = 25) -> dict:
    """Corporate group structure for an LEI from GLEIF Level 2 ("who owns whom", accounting consolidation) data.

    Returns {source, lei, name, directParent, ultimateParent, directChildren, ultimateChildren}.
    Parents are {lei, name, country, exceptionReason}; when no parent is reported, lei is null and exceptionReason
    gives the official reason (NATURAL_PERSONS, NON_CONSOLIDATING, NO_KNOWN_PERSON, NON_PUBLIC, ...).
    Children are {total, returned, items:[{lei, legalName, legalNameEn, country, city, status, registrationStatus,
    category}]} - only entities with their own LEI that reported this parent. max_children: 1..100 per list
    (default 25); "total" tells how many exist. Ownership percentages are not part of GLEIF Level 2 data.
    """
    lei = _norm_lei(lei)
    if err := _lei_error(lei):
        return {"error": err, "source": SOURCE}
    g = _gleif()
    d = await g.get(f"lei-records/{lei}")
    rec = (d or {}).get("data")
    if not rec:
        return {"error": f"LEI {lei} not found in GLEIF.", "source": SOURCE}
    if (rec.get("attributes") or {}).get("entity", {}).get("category") in PERSONAL_CATEGORIES:
        return {"source": SOURCE, **_personal_stub(rec)}
    full = await build_record(g, rec, True)
    n = max(1, min(int(max_children), MAX_CHILDREN))
    return {"source": SOURCE, "lei": lei, "name": full["legalNameEn"], "country": full["country"],
            "directParent": full["directParent"], "ultimateParent": full["ultimateParent"],
            "directChildren": await _children(g, lei, "direct-children", n),
            "ultimateChildren": await _children(g, lei, "ultimate-children", n)}


@mcp.tool()
async def validate_lei(lei: str) -> dict:
    """Validate an LEI: format (20 chars), ISO 17442 MOD 97-10 checksum, and - if the checksum passes - whether it
    exists in GLEIF and its current status.

    Returns {lei, formatValid, checksumValid, exists, registrationStatus (ISSUED = current; LAPSED = not renewed;
    RETIRED/MERGED/ANNULLED/DUPLICATE/...), entityStatus (ACTIVE/INACTIVE), legalName, nextRenewal, isCurrent
    (exists and ISSUED and ACTIVE), source}. One GLEIF request at most.
    """
    lei = _norm_lei(lei)
    fmt = bool(LEI_RE.match(lei))
    chk = fmt and lei_checksum_ok(lei)
    out = {"lei": lei, "formatValid": fmt, "checksumValid": chk, "exists": None, "source": SOURCE}
    if not chk:
        out["exists"] = False
        out["isCurrent"] = False
        return out
    d = await _gleif().get(f"lei-records/{lei}")
    rec = (d or {}).get("data")
    out["exists"] = bool(rec)
    if rec:
        at = rec.get("attributes") or {}
        e, reg = at.get("entity") or {}, at.get("registration") or {}
        out.update({"registrationStatus": reg.get("status"), "entityStatus": e.get("status"),
                    "legalName": None if e.get("category") in PERSONAL_CATEGORIES else english_name(e),
                    "nextRenewal": reg.get("nextRenewalDate")})
        out["isCurrent"] = reg.get("status") == "ISSUED" and e.get("status") == "ACTIVE"
    else:
        out["isCurrent"] = False
    return out


@mcp.tool()
async def list_entities(country: str, entity_status: str = "ACTIVE", registration_status: str = "ISSUED",
                        category: str | None = None, limit: int = 25, page: int = 1) -> dict:
    """List legal entities from the Global LEI Index by filters (no name).

    country: ISO alpha-2 of the legal address, e.g. "TH" (required). entity_status: ACTIVE | INACTIVE | ANY.
    registration_status: ISSUED | LAPSED | RETIRED | MERGED | ANNULLED | PENDING_TRANSFER | ... | ANY.
    category: optional GENERAL | FUND | BRANCH | RESIDENT_GOVERNMENT_ENTITY | INTERNATIONAL_ORGANIZATION.
    limit: 1..100 per page (default 25), page: 1-based page number.
    Returns {source, total (all matches), page, lastPage, count, results:[{lei, legalName, legalNameEn, country,
    city, status, registrationStatus, category, registrationAuthorityEntityId}]}. Sole proprietors are excluded.
    """
    country = (country or "").strip().upper()
    if len(country) != 2:
        return {"error": "country must be an ISO 3166 alpha-2 code, e.g. TH", "source": SOURCE}
    params = {"filter[entity.legalAddress.country]": country, "page[size]": max(1, min(int(limit), 100)),
              "page[number]": max(1, int(page))}
    for v, f in ((entity_status, "filter[entity.status]"), (registration_status, "filter[registration.status]"),
                 (category, "filter[entity.category]")):
        v = (v or "").strip().upper()
        if v and v != "ANY":
            params[f] = v
    d = await _gleif().get("lei-records", params)
    if d is None:
        return {"error": "GLEIF API returned no data (bad filter value or API unavailable).", "source": SOURCE}
    pg = ((d.get("meta") or {}).get("pagination") or {})
    res = [_summary(r) for r in d.get("data") or []
           if (r.get("attributes") or {}).get("entity", {}).get("category") not in PERSONAL_CATEGORIES]
    return {"source": SOURCE, "total": pg.get("total"), "page": pg.get("currentPage"), "lastPage": pg.get("lastPage"),
            "count": len(res), "results": res}


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
