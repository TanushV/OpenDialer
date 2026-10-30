"""Offline US number geography. Area codes indicate numbering origin, not current location.

NPA snapshot: https://reports.nanpa.com/public/npa_report.csv, file date 2026-10-30.
Metro preferences below are product defaults, not population or pickup-rate rankings.
No network lookup or location inference is used for compliance or calling hours.
"""

from collections import Counter
from hashlib import sha256

# state | name | preferred metros | preferred area codes | in-service area codes
_STATE_DATA = """
AL|Alabama|Birmingham|205|205 251 256 334 483 659 938
AK|Alaska|Anchorage|907|907
AZ|Arizona|Phoenix|602 480|480 520 602 623 928
AR|Arkansas|Little Rock|501|327 479 501 870
CA|California|Los Angeles / San Francisco / San Diego|213 415 619|209 213 279 310 323 341 350 357 369 408 415 424 442 510 530 559 562 619 626 628 650 657 661 669 707 714 738 747 760 805 818 820 831 837 840 858 909 916 925 949 951
CO|Colorado|Denver|303 720|303 719 720 748 970 983
CT|Connecticut|Hartford / New Haven|860 203|203 475 860 959
DE|Delaware|Wilmington|302|302
DC|District of Columbia|Washington|202|202 771
FL|Florida|Miami / Orlando / Tampa|305 407 813|239 305 321 324 352 386 407 448 561 645 656 689 727 728 754 772 786 813 850 863 904 941 954
GA|Georgia|Atlanta|404 470|229 404 470 478 678 706 762 770 912 943
HI|Hawaii|Honolulu|808|808
ID|Idaho|Boise|208|208 986
IL|Illinois|Chicago|312 773|217 224 309 312 331 447 464 618 630 708 730 773 779 815 847 861 872
IN|Indiana|Indianapolis|317|219 260 317 463 574 765 812 930
IA|Iowa|Des Moines|515|319 515 563 641 712
KS|Kansas|Wichita / Kansas City|316 913|316 620 785 913
KY|Kentucky|Louisville|502|270 364 502 606 859
LA|Louisiana|New Orleans / Baton Rouge|504 225|225 318 337 457 504 985
ME|Maine|Portland|207|207
MD|Maryland|Baltimore|410 443|227 240 301 410 443 667
MA|Massachusetts|Boston|617 857|339 351 413 508 617 774 781 857 978
MI|Michigan|Detroit|313|231 248 269 313 517 586 616 679 734 810 906 947 989
MN|Minnesota|Minneapolis / Saint Paul|612 651|218 320 507 612 651 763 924 952
MS|Mississippi|Jackson|601|228 471 601 662 769
MO|Missouri|Kansas City / Saint Louis|816 314|235 314 417 557 573 636 660 816 975
MT|Montana|Billings|406|406
NE|Nebraska|Omaha|402|308 402 531
NV|Nevada|Las Vegas|702 725|702 725 775
NH|New Hampshire|Manchester|603|603
NJ|New Jersey|Newark / Jersey City|973 201|201 551 609 640 732 848 856 862 908 973
NM|New Mexico|Albuquerque|505|505 575
NY|New York|New York City / Buffalo|212 646 716|212 315 329 332 347 363 465 516 518 585 607 624 631 646 680 716 718 838 845 914 917 929 934
NC|North Carolina|Charlotte / Raleigh|704 919|252 336 472 704 743 828 910 919 980 984
ND|North Dakota|Fargo|701|701
OH|Ohio|Columbus / Cleveland / Cincinnati|614 216 513|216 220 234 283 326 330 380 419 436 440 513 567 614 740 937
OK|Oklahoma|Oklahoma City / Tulsa|405 918|405 539 572 580 918
OR|Oregon|Portland|503 971|458 503 541 971
PA|Pennsylvania|Philadelphia / Pittsburgh|215 412|215 223 267 272 412 445 484 570 582 610 717 724 814 835 878
RI|Rhode Island|Providence|401|401
SC|South Carolina|Charleston / Columbia|843 803|803 821 839 843 854 864
SD|South Dakota|Sioux Falls|605|605
TN|Tennessee|Nashville / Memphis|615 901|423 615 629 729 731 865 901 931
TX|Texas|Dallas / Houston / Austin|214 713 512|210 214 254 281 325 346 361 409 430 432 469 512 621 682 713 726 737 806 817 830 832 903 915 936 940 945 956 972 979
UT|Utah|Salt Lake City|801 385|385 435 801
VT|Vermont|Burlington|802|802
VA|Virginia|Richmond / Northern Virginia|804 703|276 434 540 571 686 703 757 804 826 948
WA|Washington|Seattle|206 425|206 253 360 425 509 564
WV|West Virginia|Charleston|304|304 681
WI|Wisconsin|Milwaukee / Madison|414 608|262 274 353 414 534 608 715 920
WY|Wyoming|Cheyenne|307|307
"""


STATES = {}
AREA_STATES = {}
for _line in _STATE_DATA.strip().splitlines():
    _code, _name, _metro, _preferred, _areas = _line.split("|")
    STATES[_code] = {
        "name": _name,
        "metros": _metro,
        "preferred": _preferred.split(),
        "areas": _areas.split(),
    }
    AREA_STATES.update(dict.fromkeys(_areas.split(), _code))

STATE_NAMES = {info["name"].upper(): code for code, info in STATES.items()}


def normalize_state(value):
    text = str(value or "").strip().upper()
    if text.startswith("US-"):
        text = text[3:]
    return text if text in STATES else STATE_NAMES.get(text)


def area_code(record):
    region = str(record.get("region") or "")
    phone = str(record.get("phone") or "")
    return region if len(region) == 3 and region.isdigit() else phone[2:5] if phone.startswith("+1") else ""


def coverage_region(record):
    explicit = normalize_state(record.get("state")) or normalize_state(record.get("jurisdiction"))
    if explicit:
        return explicit
    region = str(record.get("region") or "")
    if region == "general":
        return region
    return normalize_state(region) or AREA_STATES.get(area_code(record)) or region or "general"


def suggested_codes(state, leads=()):
    """Prefer familiar metros represented in the list, then other in-state defaults."""
    if state not in STATES:
        return []
    counts = Counter(
        area_code(row)
        for row in leads
        if coverage_region(row) == state and AREA_STATES.get(area_code(row)) == state
    )
    preferred = STATES[state]["preferred"]
    ordered = sorted(preferred, key=lambda code: -counts[code])
    return list(
        dict.fromkeys(
            [*ordered, *sorted(counts, key=lambda code: (-counts[code], code)), *STATES[state]["areas"]]
        )
    )


def planning_key(campaign_id, capacity=None, list_name=None):
    key = f"planning:{campaign_id}:{capacity or 'configured'}"
    return key if list_name is None else key + ":list-" + sha256(list_name.encode()).hexdigest()[:20]
