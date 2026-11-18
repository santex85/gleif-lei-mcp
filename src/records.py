"""Record building and name search, ported from the gleif-lei-lookup actor (no Apify dependency)."""
import re

from gleif import Gleif, english_name, match_score

LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
PERSONAL_CATEGORIES = {"SOLE_PROPRIETOR"}


def _addr(a: dict | None) -> dict:
    a = a or {}
    lines = [x for x in (a.get("addressLines") or []) if x]
    return {
        "addressLine": ", ".join(lines) or None,
        "city": a.get("city"),
        "region": a.get("region"),
        "postalCode": a.get("postalCode"),
        "country": a.get("country"),
    }


async def build_record(g: Gleif, rec: dict, include_ownership: bool, query: str | None = None) -> dict:
    at = rec.get("attributes") or {}
    e = at.get("entity") or {}
    reg = at.get("registration") or {}
    lei = at.get("lei") or rec.get("id")
    legal = _addr(e.get("legalAddress"))
    hq = _addr(e.get("headquartersAddress"))
    lf = e.get("legalForm") or {}
    out = {
        "lei": lei,
        "legalName": (e.get("legalName") or {}).get("name"),
        "legalNameEn": english_name(e),
        "otherNames": [n.get("name") for n in (e.get("otherNames") or []) + (e.get("transliteratedOtherNames") or []) if n.get("name")],
        "status": e.get("status"),
        "registrationStatus": reg.get("status"),
        "category": e.get("category"),
        "legalFormCode": lf.get("id"),
        "legalForm": lf.get("other") or await g.legal_form_name(lf.get("id")),
        "jurisdiction": e.get("jurisdiction"),
        "country": legal["country"],
        "city": legal["city"],
        "region": legal["region"],
        "postalCode": legal["postalCode"],
        "addressLine": legal["addressLine"],
        "hqCountry": hq["country"],
        "hqCity": hq["city"],
        "hqAddressLine": hq["addressLine"],
        "registrationAuthorityId": (e.get("registeredAt") or {}).get("id"),
        "registrationAuthorityEntityId": e.get("registeredAs"),
        "entityCreationDate": e.get("creationDate"),
        "initialRegistrationDate": reg.get("initialRegistrationDate"),
        "lastUpdate": reg.get("lastUpdateDate"),
        "nextRenewal": reg.get("nextRenewalDate"),
        "corroborationLevel": reg.get("corroborationLevel"),
        "managingLou": reg.get("managingLou"),
        "bic": at.get("bic") or [],
        "successorLei": (e.get("successorEntity") or {}).get("lei"),
        "url": f"https://search.gleif.org/#/record/{lei}",
    }
    if query is not None:
        out["matchedQuery"] = query
        out["matchScore"] = match_score(query, e)
    if include_ownership:
        for key, path in (("directParent", "direct-parent"), ("ultimateParent", "ultimate-parent")):
            p = await g.get(f"lei-records/{lei}/{path}")
            if p and p.get("data"):
                pe = p["data"]["attributes"]["entity"]
                out[key] = {
                    "lei": p["data"]["id"],
                    "name": english_name(pe),
                    "country": (pe.get("legalAddress") or {}).get("country"),
                    "exceptionReason": None,
                }
            else:
                ex = await g.get(f"lei-records/{lei}/{path}-reporting-exception")
                reason = ((ex or {}).get("data") or {}).get("attributes", {}).get("reason") if ex else None
                out[key] = {"lei": None, "name": None, "country": None, "exceptionReason": reason}
    return out


async def fetch_by_leis(g: Gleif, leis: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for i in range(0, len(leis), 100):
        chunk = leis[i:i + 100]
        d = await g.get("lei-records", {"filter[lei]": ",".join(chunk), "page[size]": 100})
        for r in (d or {}).get("data", []):
            found[r["id"]] = r
    return found


async def search_name(g: Gleif, name: str, country: str | None, limit: int) -> list[tuple[float, dict]]:
    """Candidates from GLEIF fuzzy completions + name filter, ranked by our similarity score."""
    cands: dict[str, dict] = {}
    params = {"filter[entity.names]": name, "page[size]": min(max(limit * 3, 10), 100)}
    if country:
        params["filter[entity.legalAddress.country]"] = country
    d = await g.get("lei-records", params)
    for r in (d or {}).get("data", []):
        cands[r["id"]] = r

    fz = await g.get("fuzzycompletions", {"field": "fulltext", "q": name})
    fuzzy_leis = []
    for x in (fz or {}).get("data", []):
        rel = ((x.get("relationships") or {}).get("lei-records") or {}).get("data") or {}
        if rel.get("id") and rel["id"] not in cands and rel["id"] not in fuzzy_leis:
            fuzzy_leis.append(rel["id"])
    if fuzzy_leis:
        cands.update(await fetch_by_leis(g, fuzzy_leis))

    if not cands:  # last resort: full-text search over all fields
        params = {"filter[fulltext]": name, "page[size]": min(max(limit * 2, 10), 100)}
        if country:
            params["filter[entity.legalAddress.country]"] = country
        d = await g.get("lei-records", params)
        for r in (d or {}).get("data", []):
            cands[r["id"]] = r

    ranked = []
    for r in cands.values():
        e = r["attributes"]["entity"]
        if country and (e.get("legalAddress") or {}).get("country") != country:
            continue
        ranked.append((match_score(name, e), r))
    ranked.sort(key=lambda t: -t[0])
    return ranked[:limit]
