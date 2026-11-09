# HubSpot and Treg live check, November 9, 2026

Run against a scratch copy of the owner's installed workspace (relay files left out, so the running app kept its
connection), with the owner's HubSpot Service Key and Treg token. Only one test contact was touched: "OpenDialer Test",
+12038211101, owned by the owner (HubSpot contact 261425233976), created for this check.

| Check | Result |
|---|---|
| HubSpot key check (Settings row) | Connected; the 4 app fields already existed from the owner's first check |
| Treg token check | Connected |
| HubSpot owners | 1 user; "Calls for" set to it |
| HubSpot lists | The account has none (4,174 contacts, 0 lists). The key cannot create lists (no `crm.lists.write`), so the list read was patched to the one test contact; everything else was real |
| Sync | 1 lead added from the contact |
| Waterfall | Trestle check: `conflict` (the number's owner name differs from "OpenDialer Test"), $0.03 of a $0.50 limit |
| `phone_status` write-back | `conflict` on the HubSpot contact |
| Review | Lists the lead under "may belong to someone else" with its fix |
| Call outcomes | All 7 app outcomes map to the account's 7 dispositions |
| Call record | Created with disposition, direction, duration and to-number; association to the contact |
| Repeat log | HubSpot refuses a duplicate `opendialer_call_id` with HTTP 400; the app now finds the first record instead (fixed this day, test added) |
| Summary update | Written to `hs_call_body` |
| Opt-out write | `opendialer_opt_out` and its reason written, then cleared |

Not covered: RealValidito (no key saved), the real list read (no list exists), and a carrier call.
