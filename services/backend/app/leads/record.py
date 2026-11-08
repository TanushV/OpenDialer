"""Fields every lead carries about where it came from and whether its number was checked (R3.11).

CSV leads are taken as they are: no finder, Trestle or do-not-call check runs on them (D19, D23).
"""


def csv_fields():
    return {"source": "csv", "external_id": None, "signals": [], "number_status": "not_checked"}
