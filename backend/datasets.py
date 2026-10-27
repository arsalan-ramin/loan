"""Shared MTB recognition, independent of exact original workbook filenames.

Recognition selects conservative policy review only. It never validates a schema,
authorizes field mappings or creates account/loan joins.
"""

import re
from pathlib import PurePath
from typing import Any

MTB_FILES = {"MTB_Query_1.xlsx", "MTB_Query_2.xlsx", "MTB_Query_4.xlsx"}
QUERY_NAME = re.compile(r"^(?:mtb[ _-]+)?query[ _-]+(1|2|3|4)(?=$|[ _().-])", re.IGNORECASE)
SIGNATURES = {
    1: {"Behavioral_Underwriting_Program__c", "Estimated_Annual_Revenue__c", "SCOC__CharacteristicKey__c"},
    2: {"SCOC__IndicatorKey__c", "SCOC__IndicatorValue__c", "LLC_BI__Loan__c"},
    4: {"nFUSE__Credit_Report_Freeze__c", "nFUSE__OFAC_Alert_Indicator__c", "nFUSE__Number_Of_Bankruptcies__c"},
}
CANONICAL_ENTITY_SHEETS = {
    "account", "loan", "entityinvolvement", "creditreport", "loanextension",
    "lcrresponse", "lcrentity", "datasourceanalysis",
    "indicator", "characteristic", "regulation",
    "llc_bi_loan__c", "llc_bi_legal_entities__c", "nfuse_credit_report__c",
    "loan_extension__c", "loan_extension_c", "scoc_lcresponse__c", "scoc__lcresponse__c", "scoc_lcrentity__c", "scoc__lcrentity__c",
    "scoc_lcrdatasourceanalysis__c", "scoc_lccharacteristic__c", "scoc_lcindicator__c",
}

# Exact semantic aliases after case/punctuation normalization. These fields are
# foreign keys only; never treat generic record IDs or account display names as
# relationship values.
ACCOUNT_REFERENCE_HEADERS = {
    "account", "accountid", "accountidc", "accountkey", "accountfk",
    "accountforeignkey", "accountlookup", "accountreference", "accountref",
    "accountc", "accountrelationshipid", "accountrelationshipc", "accountidrid",
    "accountidr", "accountlookupid", "llcbiaccount", "llcbiaccountc",
    "llcbiaccountr", "llcbiaccountrid", "llcbiaccountid", "nfuseaccount",
    "nfuseaccountc", "nfuseaccountr", "nfuseaccountrid", "nfuseaccountid",
    "accountcolumn", "accountidcolumn", "borroweraccount", "borroweraccountid", "borroweraccountidc", "borroweraccountref",
    "llcbiborroweraccount", "llcbiborroweraccountc", "clientaccountid",
}
LOAN_REFERENCE_HEADERS = {
    "loanid", "loanidc", "loanrequestid", "llcbiloan", "llcbiloanc",
}
BORROWER_ROLE_HEADERS = {
    "role", "borrowerrole", "borrowertype", "borrowertypec", "llcbiborrowertype",
    "llcbiborrowertypec",
}

# SOQL exports are flattened, but their root Id still belongs to one known
# object per query. Never apply a generic Id to every entity in a row.
SOQL_ROOT_SIGNATURES = {
    1: ("EntityInvolvement", {
        "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r.LLC_BI__Loan__c",
        "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r.LLC_BI__Account__c",
        "SCOC__LCRDataSourceAnalysis__r.SCOC__LCREntity__r.SCOC__Entity_Involvement__r.LLC_BI__Borrower_Type__c",
        "SCOC__CharacteristicKey__c", "SCOC__CharacteristicValue__c",
    }),
    2: ("Indicator", {"SCOC__IndicatorKey__c", "SCOC__IndicatorValue__c"}),
    4: ("CreditReport", {"nFUSE__Account__c", "nFUSE__Credit_Score__c"}),
}
SOQL_TABLES_BY_ROLE = {
    1: {"Loan", "Account", "EntityInvolvement", "CreditReport", "Characteristic"},
    2: {"Loan", "Account", "LCRESPONSE", "LCREntity",
        "DataSourceAnalysis", "Indicator"},
    4: {"CreditReport"},
}
SOQL_REQUIRED_INPUT_TARGETS = {
    "Loan.as_of_date": "Loan as-of date",
    "Loan.response_id": "Scalar SBSS response record ID (the supplied score is independently linked by Loan ID)",
    "LCRESPONSE.score_date": "SBSS response score date (freshness cannot be checked)",
    "Indicator.analysis_id": "Indicator analysis foreign key",
    "Characteristic.analysis_id": "Characteristic analysis foreign key",
    "CreditReport.report_date": "Credit report date",
    "CreditReport.report_role": "Credit report borrower/guarantor role",
    "Account.ebitda": "Explicit EBITDA (EBIT is not interchangeable)",
    "CreditReport.max_delinquency_public_records": "Maximum-delinquency public-record code",
    "CreditReport.bankruptcy_discharge_date": "Bankruptcy discharge date",
}

# Exact normalized suffixes accepted only when they occur in a fully named
# SOQL column. These are source aliases, not fuzzy or substring mappings.
SOQL_FIELD_SUFFIXES = {
    ("Loan", "requested_amount"): ("llcbiloanrllcbiamountc",),
    ("Loan", "product"): ("llcbiloanrllcbiproductc", "llcbiloanrllcbiproducttypec"),
    ("Loan", "sbss"): ("scocsbss7scorec",),
    ("EntityInvolvement", "loan_id"): ("llcbiloanc",),
    ("EntityInvolvement", "account_id"): ("llcbiaccountc",),
    ("EntityInvolvement", "role"): ("llcbiborrowertypec",),
    # Approval/creation timestamps are retained as raw source evidence; they
    # are not substituted for an explicit policy as-of or account-opened date.
    ("Account", "name"): ("llcbiaccountrname",),
    ("Account", "dob"): ("llcbiaccountrbirthdatec",),
    ("Account", "ownership_years"): ("llcbiaccountrnumberofyearsascurrentownerc",),
    ("Account", "naics"): ("llcbiaccountrnaicscodeformulac",),
    ("Account", "estimated_annual_revenue"): ("llcbiaccountrestimatedannualrevenuec",),
    ("Account", "business_net_income"): ("llcbiaccountrbusinessnetincomec",),
    ("Account", "cash_and_equiv"): ("llcbiaccountrsbss7cashandequivc",),
    ("Account", "net_worth"): ("llcbiaccountrsbss7networthc",),
    ("Account", "total_assets"): ("llcbiaccountrsbss7totalassetsc",),
    ("Account", "total_liabilities"): ("llcbiaccountrsbss7totalliabilitiesc",),
    ("CreditReport", "id"): ("nfusecreditreportrid",),
    ("CreditReport", "report_date"): ("nfusecreditreportdatec", "nfusereportdatec"),
    ("CreditReport", "account_id"): ("nfusecreditreportrnfuseaccountc", "nfuseaccountc"),
    ("CreditReport", "status"): ("nfusecreditreportrnfusestatusc", "nfusestatusc"),
    ("CreditReport", "fico"): ("nfusecreditreportrnfusecreditscorec", "nfusecreditscorec"),
    ("CreditReport", "report_category"): ("nfusecreditreportrnfusereportcategoryc", "nfusereportcategoryc"),
    ("CreditReport", "report_role"): ("llcbiborrowertypec",),
    ("CreditReport", "judgment_count"): ("nfusecreditreportrnfusenumberofjudgementsc", "nfusenumberofjudgementsc"),
    ("CreditReport", "bankruptcy_count"): ("nfusecreditreportrnfusenumberofbankruptciesc", "nfusenumberofbankruptciesc"),
    ("CreditReport", "ofac_alert"): ("nfusecreditreportrnfuseofacalertindicatorc", "nfuseofacalertindicatorc"),
    ("CreditReport", "credit_freeze"): ("nfusecreditreportrnfusecreditreportfreezec", "nfusecreditreportfreezec"),
    ("CreditReport", "past_due"): ("nfusecreditreportrnfusetotalpastduec", "nfusetotalpastduec"),
    ("CreditReport", "available_credit"): ("nfusecreditreportrnfuseavailablecreditc", "nfuseavailablecreditc"),
    ("CreditReport", "bankruptcy_amount"): ("nfusecreditreportrnfusebankruptcyamountc", "nfusebankruptcyamountc"),
    ("Indicator", "key"): ("scocindicatorkeyc",),
    ("Indicator", "value"): ("scocindicatorvaluec",),
    ("Characteristic", "key"): ("scoccharacteristickeyc",),
    ("Characteristic", "value"): ("scoccharacteristicvaluec",),
}


def normalize_header(value: str) -> str:
    """Normalize exact header spelling variants, not arbitrary semantic names."""
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def account_reference_headers(headers) -> list[str]:
    output = []
    for header in headers:
        if not isinstance(header, str):
            continue
        normalized = normalize_header(header)
        explicit_alias = normalized in ACCOUNT_REFERENCE_HEADERS
        namespaced_lookup = ("account" in normalized and normalized.endswith((
            "accountid", "accountidc", "accountr", "accountrid", "accountc",
            "accountlookup", "accountreference", "accountref", "accountkey",
            "accountfk", "accountforeignkey",
        )))
        if explicit_alias or namespaced_lookup:
            output.append(header)
    return output


def loan_reference_headers(headers) -> list[str]:
    """Return explicit Loan IDs/FKs, including namespaced SOQL relationship paths."""
    return [header for header in headers if isinstance(header, str) and (
        normalize_header(header) in LOAN_REFERENCE_HEADERS
        or normalize_header(header).endswith(("llcbiloanc", "loanrequestid", "loanidc", "loanid"))
    )]


def borrower_role_headers(headers) -> list[str]:
    """Return explicit borrower/guarantor role fields, not names or descriptions."""
    return [header for header in headers if isinstance(header, str) and (
        normalize_header(header) in BORROWER_ROLE_HEADERS
        or normalize_header(header).endswith(("llcbiborrowertypec", "borrowerrole", "borrowertype"))
    )]


def soql_root_table(query_role: int | None, headers) -> str | None:
    """Determine the root object's Id target from exact query-role signatures."""
    if query_role not in SOQL_ROOT_SIGNATURES:
        return None
    target, signature = SOQL_ROOT_SIGNATURES[query_role]
    available = set(headers)
    if not signature <= available:
        return None
    if query_role == 2 and not loan_reference_headers(headers):
        return None
    return target


def soql_tables_for_role(query_role: int | None) -> set[str]:
    """SOQL-export entities with explicit columns in this query's contract."""
    return set(SOQL_TABLES_BY_ROLE.get(query_role, set()))


def soql_field_headers(table: str, field: str, headers, *, root_table: str | None = None) -> list[str]:
    """Return source headers for one canonical SOQL field, using an allowlist."""
    output: list[str] = []
    if field == "id" and root_table == table:
        output.extend(header for header in headers if isinstance(header, str) and header == "Id")
    suffixes = SOQL_FIELD_SUFFIXES.get((table, field), ())
    for header in headers:
        if not isinstance(header, str):
            continue
        normalized = normalize_header(header)
        if any(normalized.endswith(suffix) for suffix in suffixes):
            output.append(header)
    if table == "Account" and field == "id":
        output.extend(account_reference_headers(headers))
    if table == "Loan" and field == "id":
        output.extend(loan_reference_headers(headers))
    if field in {"account_id", "borrower_account_id"}:
        output.extend(account_reference_headers(headers))
    if table in {"EntityInvolvement", "Loan", "CreditReport"} and field in {"role", "report_role"}:
        output.extend(borrower_role_headers(headers))
    return list(dict.fromkeys(output))


def soql_mapping_coverage(query_role: int | None, headers, manifest: dict,
                          amount_names: dict[str, str] | None = None, *, sheet_name: str | None = None) -> tuple[set[str], set[str]]:
    """Return mapped header names and canonical ``Table.field`` targets.

    Only exact manifest aliases and the SOQL suffix allowlist count as mapped.
    A header may be mapped to source evidence without implying that it supplies
    every value required by a decision rule.
    """
    root_table = soql_root_table(query_role, headers)
    canonical_table = manifest.get("canonical_table_aliases", {}).get(sheet_name, sheet_name)
    canonical = canonical_table in manifest["tables"]
    if root_table is None and not canonical:
        return set(), set()
    flattened = manifest["tables"] if canonical else manifest.get("flattened", {})
    mapped: set[str] = set()
    targets: set[str] = set()
    for table in ([canonical_table] if canonical else soql_tables_for_role(query_role)):
        mapping = flattened.get(table)
        if not isinstance(mapping, dict):
            continue
        id_candidates = list(mapping.get("id", []))
        id_candidates.extend(soql_field_headers(table, "id", headers, root_table=root_table))
        if table == "Account":
            id_candidates.extend(account_reference_headers(headers))
        if table == "Loan":
            id_candidates.extend(loan_reference_headers(headers))
        id_hits = set(headers) & set(id_candidates)
        if id_hits:
            mapped.update(id_hits)
            targets.add(f"{table}.id")
        raw_fields = mapping.get("fields", {})
        field_map = ({key: [key] for key in raw_fields} if isinstance(raw_fields, list)
                     else {key: list(aliases) for key, aliases in raw_fields.items()})
        if canonical:
            for key in manifest.get("expanded_canonical_fields", {}).get(table, []):
                field_map.setdefault(key, [key])
        for key, aliases in manifest.get("verified_usd_source_fields", {}).get(table, {}).items():
            field_map.setdefault(key, [key]).extend(aliases)
        for field, aliases in field_map.items():
            candidates = list(aliases)
            if not canonical:
                candidates.extend(soql_field_headers(table, field, headers, root_table=root_table))
            if field in {"account_id", "borrower_account_id"}:
                candidates.extend(account_reference_headers(headers))
            if amount_names and field in amount_names:
                candidates.append(amount_names[field])
            hits = set(headers) & set(candidates)
            if hits:
                mapped.update(hits)
                targets.add(f"{table}.{field}")
    role_hits = set(headers) & set(borrower_role_headers(headers))
    if role_hits:
        mapped.update(role_hits)
        targets.add("EntityInvolvement.role")
    return mapped, targets


def soql_unavailable_inputs(mapped_targets: set[str]) -> list[str]:
    """Canonical screenshot inputs with no recognized source field in the SOQL set."""
    return [label for target, label in SOQL_REQUIRED_INPUT_TARGETS.items()
            if target not in mapped_targets]


def mtb_query_ids(name: str, metadata: dict[str, Any]) -> set[int]:
    """Accept supported XLSX naming variants, worksheet labels or distinctive headers."""
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    if PurePath(basename).suffix.casefold() != ".xlsx":
        return set()
    match = QUERY_NAME.match(PurePath(basename).stem)
    if match:
        return {int(match.group(1))}
    queries: set[int] = set()
    canonical_sheets = {str(sheet.get("name", "")).casefold().strip()
                        for sheet in metadata.get("sheets", []) if not sheet.get("is_reference")}
    if canonical_sheets & CANONICAL_ENTITY_SHEETS:
        queries.add(1)
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
            # The copied Excel headers use fully qualified Salesforce paths;
            # they need not contain the older short, standalone field names.
            # Recognition alone does not authorize joins or infer root IDs.
            if SIGNATURES[query] <= headers or soql_root_table(query, headers) is not None:
                queries.add(query)
                break
    return queries