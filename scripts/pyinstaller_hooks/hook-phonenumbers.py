"""Authority sidecar: phone parsing metadata only.

app/compliance/phone.py calls parse, is_possible_number, is_valid_number and format_number. It never
geocodes or looks up carriers or time zones, so their 19 MB of tables stay out (the contrib hook
would collect every submodule).
"""

from PyInstaller.utils.hooks import collect_submodules

UNUSED = ("phonenumbers.geodata", "phonenumbers.carrierdata", "phonenumbers.tzdata")
UNUSED_MODULES = {"phonenumbers.geocoder", "phonenumbers.carrier", "phonenumbers.timezone"}

hiddenimports = collect_submodules(
    "phonenumbers", filter=lambda name: not name.startswith(UNUSED) and name not in UNUSED_MODULES
)
