# GLEIF LEI MCP server

MCP server for the **Global LEI Index**. It looks up legal entities (companies, funds, branches and government bodies) by name, fetches full LEI records, maps corporate ownership (parents and children) and validates LEI codes. No API key is needed.

It is ported from the `gleif-lei-lookup` Apify actor. Name matching and scoring, record flattening and parent lookups are the same, but there is no Apify dependency.

## Tools

| Tool | What it does |
|---|---|
| `search_companies` | Fuzzy name search in any language or script, with an optional country, ranked by a `matchScore` from 0 to 100. Legal-form words such as Ltd or GmbH are ignored. |
| `get_lei` | Full record for one LEI: legal and HQ address, legal form, register ID, status, renewal date, BICs, and the direct and ultimate parent. |
| `company_ownership` | Direct and ultimate parent (or the official reason none is reported), plus direct and ultimate children with totals. |
| `validate_lei` | Format and ISO 17442 MOD 97-10 checksum check, then whether the LEI exists, its status and whether it is current. |
| `list_entities` | Paged list of entities by country, entity status, registration status and category. |

Example calls:

```jsonc
// search_companies
{"name": "Siam Commercial Bank", "limit": 3}
{"name": "Toyota Motor", "country": "JP"}
// get_lei
{"lei": "54930068OELHUBHH1U03", "include_ownership": true}
// company_ownership
{"lei": "5493006W3QUS5LMH6R84", "max_children": 10}
// validate_lei
{"lei": "5493006W3QUS5LMH6R84"}
// list_entities
{"country": "TH", "entity_status": "ACTIVE", "registration_status": "ISSUED", "category": "FUND", "limit": 25, "page": 1}
```

Sample `search_companies` result:

```json
{"query": "Siam Commercial Bank", "count": 2, "results": [
  {"lei": "54930068OELHUBHH1U03", "legalName": "บริษัท ธนาคารไทยพาณิชย์ จำกัด (มหาชน)",
   "legalNameEn": "THE SIAM COMMERCIAL BANK PUBLIC COMPANY LIMITED", "country": "TH", "city": "BANGKOK",
   "status": "ACTIVE", "registrationStatus": "ISSUED", "category": "GENERAL",
   "registrationAuthorityEntityId": "0107536000102", "matchScore": 100.0}, "..."]}
```

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8094 .venv/bin/python src/server.py                 # streamable-http on 0.0.0.0:8094/mcp
MCP_TRANSPORT=stdio .venv/bin/python src/server.py       # stdio
```

Environment variables: `HOST` and `PORT` (default `0.0.0.0:8080`), `MCP_TRANSPORT` (default `streamable-http`) and `GLEIF_USER_AGENT`.

## Limits

- GLEIF allows 60 requests per minute. The client keeps at least 1.1 s between requests across the whole process and backs off on 429 and 5xx responses: at most 3 tries, with waits capped at 15 s and a 20 s HTTP timeout.
- `search_companies` returns at most 25 results. It takes 2 to 5 requests, so about 2 to 5 s.
- `get_lei` with ownership takes 3 to 6 requests, about 4 to 7 s.
- `company_ownership` takes about 6 to 9 requests, about 7 to 10 s. It returns at most 100 children per list, and `total` gives the full count.
- `list_entities` returns at most 100 results per page.
- Sole-proprietor LEIs describe natural persons. They are left out of searches and lists, and `get_lei` reduces them to their status.

## Data source and licence

- Data comes live from the public [GLEIF API](https://api.gleif.org/api/v1), which is built on the GLEIF Golden Copy. LEI data is published free of charge under **CC0 1.0** (see GLEIF's LEI Data Terms of Use). Source: Global Legal Entity Identifier Foundation, https://www.gleif.org.
- This server is **not affiliated with or endorsed by GLEIF** or any LEI issuer (LOU). Match scores and the flattened format are this project's own work.
- Ownership data is GLEIF Level 2 accounting consolidation ("who owns whom"). It contains no ownership percentages and does not cover natural-person owners. When an entity reports no parent, the `exceptionReason` field says why, for example `NATURAL_PERSONS`, `NON_CONSOLIDATING` or `NO_KNOWN_PERSON`.
- Fuzzy matching is statistical. Before making compliance decisions, confirm a match against the register number and address.
