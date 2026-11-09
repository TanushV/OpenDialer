"""HubSpot through one Service Key (D17, section 9B): lists, owners, contacts, dispositions and native call records.

One account and one set of paths. A 429 waits for `Retry-After` (at most three tries) and a 5xx retries once; anything
else is a `HubSpotError` the caller turns into a retry job or a Needs attention entry. Calling never waits on this.
The exact Service Key scopes and the do-not-call property are settled with the owner's account in R9B.7.
"""

import asyncio

import httpx

BASE_URL = "https://api.hubapi.com"
CALL_TO_CONTACT = 194  # HubSpot-defined association type: call to contact.
BATCH = 100
SCOPES = (
    "crm.lists.read",
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
    "crm.objects.owners.read",
    "crm.schemas.contacts.write",
)
# Properties the app creates once at setup (R9B.10). Our own opt-out flag is the one a sync reads and writes.
OWN_PROPERTIES = {
    "contacts": [
        {
            "name": "phone_status",
            "label": "Phone status (OpenDialer)",
            "type": "enumeration",
            "fieldType": "select",
            "groupName": "contactinformation",
            "options": [
                {"label": label, "value": value, "displayOrder": i}
                for i, (value, label) in enumerate(
                    [
                        ("confirmed", "Confirmed"),
                        ("unconfirmed", "Unconfirmed"),
                        ("conflict", "Conflict"),
                        ("wrong_number", "Wrong number"),
                        ("dead", "Dead"),
                    ]
                )
            ],
        },
        {
            "name": "opendialer_opt_out",
            "label": "Do not call (OpenDialer)",
            "type": "enumeration",
            "fieldType": "booleancheckbox",
            "groupName": "contactinformation",
            "options": [
                {"label": "Yes", "value": "true", "displayOrder": 0},
                {"label": "No", "value": "false", "displayOrder": 1},
            ],
        },
        {
            "name": "opendialer_opt_out_reason",
            "label": "Do-not-call reason (OpenDialer)",
            "type": "string",
            "fieldType": "text",
            "groupName": "contactinformation",
        },
    ],
    "calls": [
        {
            "name": "opendialer_call_id",
            "label": "OpenDialer call ID",
            "type": "string",
            "fieldType": "text",
            "groupName": "callinformation",
            "hasUniqueValue": True,
        }
    ],
}


class HubSpotError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class HubSpot:
    def __init__(self, key, *, http=None, base_url=BASE_URL, sleep=asyncio.sleep, timeout=30):
        self.key, self.http, self.base_url, self.sleep, self.timeout = key, http, base_url, sleep, timeout

    async def request(self, method, path, **kwargs):
        async def go(http):
            for attempt in range(3):
                try:
                    response = await http.request(
                        method,
                        self.base_url + path,
                        headers={"Authorization": "Bearer " + self.key},
                        timeout=self.timeout,
                        **kwargs,
                    )
                except httpx.HTTPError as exc:
                    if attempt == 2:
                        raise HubSpotError("HubSpot unreachable: " + type(exc).__name__) from exc
                    await self.sleep(1 + attempt)
                    continue
                if response.status_code == 429 and attempt < 2:
                    await self.sleep(min(10.0, float(response.headers.get("Retry-After") or 1 + attempt)))
                    continue
                if response.status_code >= 500 and attempt == 0:
                    await self.sleep(1)
                    continue
                if response.status_code == 404:
                    return None
                if response.status_code >= 400:
                    raise HubSpotError(f"HubSpot HTTP {response.status_code}", response.status_code)
                return response.json() if response.content else {}
            raise HubSpotError("HubSpot kept answering 429 or 5xx")

        if self.http:
            return await go(self.http)
        async with httpx.AsyncClient() as http:
            return await go(http)

    async def _pages(self, path, key="results", params=None):
        after, out = None, []
        while True:
            page = await self.request(
                "GET", path, params={**(params or {}), **({"after": after} if after else {})}
            )
            out += (page or {}).get(key, [])
            after = ((page or {}).get("paging") or {}).get("next", {}).get("after")
            if not after:
                return out

    async def lists(self):
        found = await self.request("POST", "/crm/v3/lists/search", json={"query": "", "count": 500})
        return [
            {"id": str(item["listId"]), "name": item.get("name", "")}
            for item in (found or {}).get("lists", [])
            if str(item.get("objectTypeId", "0-1")) == "0-1"
        ]

    async def owners(self):
        return [
            {
                "id": str(o["id"]),
                "name": " ".join(filter(None, [o.get("firstName"), o.get("lastName")])) or o.get("email", ""),
                "email": o.get("email", ""),
            }
            for o in await self._pages("/crm/v3/owners", params={"limit": 100})
            if not o.get("archived")
        ]

    async def members(self, list_id):
        rows = await self._pages(f"/crm/v3/lists/{list_id}/memberships", params={"limit": 250})
        return list(dict.fromkeys(str(row["recordId"]) for row in rows))

    async def contacts(self, ids, properties):
        out = []
        for start in range(0, len(ids), BATCH):
            body = {
                "properties": sorted(set(properties)),
                "inputs": [{"id": i} for i in ids[start : start + BATCH]],
            }
            found = await self.request("POST", "/crm/v3/objects/contacts/batch/read", json=body)
            out += (found or {}).get("results", [])
        return out

    async def dispositions(self):
        found = await self.request("GET", "/calling/v1/dispositions")
        return [{"id": d["id"], "label": d["label"]} for d in found or []]

    async def find_call(self, opendialer_call_id):
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "opendialer_call_id", "operator": "EQ", "value": opendialer_call_id}
                    ]
                }
            ],
            "limit": 1,
        }
        found = await self.request("POST", "/crm/v3/objects/calls/search", json=body)
        results = (found or {}).get("results", [])
        return results[0]["id"] if results else None

    async def create_call(self, properties, contact_id):
        body = {
            "properties": properties,
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": CALL_TO_CONTACT}
                    ],
                }
            ],
        }
        created = await self.request("POST", "/crm/v3/objects/calls", json=body)
        return created["id"]

    async def update_call(self, call_id, properties):
        await self.request("PATCH", f"/crm/v3/objects/calls/{call_id}", json={"properties": properties})

    async def update_contact(self, contact_id, properties):
        await self.request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties})

    async def create_contact(self, properties):
        created = await self.request("POST", "/crm/v3/objects/contacts", json={"properties": properties})
        return created["id"]

    async def ensure_properties(self):
        """Create the app's own properties once; existing ones are left as they are."""
        made = []
        for kind, specs in OWN_PROPERTIES.items():
            for spec in specs:
                if await self.request("GET", f"/crm/v3/properties/{kind}/{spec['name']}") is None:
                    await self.request("POST", f"/crm/v3/properties/{kind}", json=spec)
                    made.append(f"{kind}.{spec['name']}")
        return made

    async def check(self):
        """Settings check on save: an authenticated read the sync needs."""
        try:
            await self.request("GET", "/crm/v3/owners", params={"limit": 1})
        except HubSpotError as exc:
            if exc.status in (401, 403):
                return False, "HubSpot refused the key or a scope is missing (it needs " + ", ".join(
                    SCOPES
                ) + ")"
            return False, str(exc)
        return True, "Connected"
