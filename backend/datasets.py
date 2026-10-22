"""Shared MTB recognition, independent of exact original workbook filenames.

Recognition selects conservative policy review only. It never validates a schema,
authorizes field mappings or creates account/loan joins.
"""

import re
from pathlib import PurePath
from typing import Any

MTB_FILES = {"MTB_Query_1.xlsx", "MTB_Query_2.xlsx", "MTB_Query_4.xlsx"}
QUERY_NAME = re.compile(r"^mtb[ _-]+query[ _-]+(1|2|4)(?=$|[ _().-])", re.IGNORECASE)
SIGNATURES = {
    1: {"Behavioral_Underwriting_Program__c", "Estimated_Annual_Revenue__c", "SCOC__CharacteristicKey__c"},
    2: {"SCOC__IndicatorKey__c", "SCOC__IndicatorValue__c", "LLC_BI__Loan__c"},
    4: {"nFUSE__Credit_Report_Freeze__c", "nFUSE__OFAC_Alert_Indicator__c", "nFUSE__Number_Of_Bankruptcies__c"},
}


def mtb_query_ids(name: str, metadata: dict[str, Any]) -> set[int]:
    """Accept supported XLSX naming variants, worksheet labels or distinctive headers."""
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    if PurePath(basename).suffix.casefold() != ".xlsx":
        return set()
    match = QUERY_NAME.match(PurePath(basename).stem)
    if match:
        return {int(match.group(1))}
    queries: set[int] = set()
    for sheet in metadata.get("sheets", []):
        if sheet.get("is_reference"):
            continue
        match = QUERY_NAME.match(str(sheet.get("name", "")))
        if match:
            queries.add(int(match.group(1)))
            continue
        headers = set(sheet.get("headers", []))
        # Query 1 includes bureau columns too. Prefer the more specific master signature.
        for query in (1, 2, 4):
            if SIGNATURES[query] <= headers:
                queries.add(query)
                break
    return queries