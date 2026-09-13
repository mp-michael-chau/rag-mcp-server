"""Unit tests for ref_ids.py — pure functions, no Qdrant/Ollama needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ref_ids


def ids(tokens):
    return [t["id"] for t in tokens]


# --- normalize_query_refs: prefixed forms (certain) ---


def test_ps_dotted():
    toks = ref_ids.normalize_query_refs("What does PS 10.3.6 require?")
    assert ids(toks) == ["10.3.6"]
    assert toks[0]["kind"] == "clause" and not toks[0]["bare"]


def test_clause_letter_sub():
    toks = ref_ids.normalize_query_refs("Clause 10.3.11(d) retention frequency")
    assert ids(toks) == ["10.3.11"]
    assert toks[0]["sub"] == "d"


def test_roman_sub_with_space():
    toks = ref_ids.normalize_query_refs("clause 10.3.4 (b) to (c)")
    assert ids(toks) == ["10.3.4"]
    assert toks[0]["sub"] == "b"


def test_section_top_level():
    toks = ref_ids.normalize_query_refs("Show me Section 23 requirements")
    assert ids(toks) == ["23"]
    assert toks[0]["kind"] == "section"


def test_schedule_variants():
    assert ids(ref_ids.normalize_query_refs("Schedule No. 3 occupancy sensing")) == ["schedule-3"]
    assert ids(ref_ids.normalize_query_refs("Schedule 3")) == ["schedule-3"]
    assert ids(ref_ids.normalize_query_refs("Sch.1 guidelines")) == ["schedule-1"]
    assert ids(ref_ids.normalize_query_refs("SC No. 1")) == ["schedule-1"]
    assert ids(ref_ids.normalize_query_refs("SOR Schedule Nr. 3 Item N")) == ["schedule-3"]
    assert ids(ref_ids.normalize_query_refs("(Optional) Schedule No. 4")) == ["schedule-4"]


def test_schedule_letter_sub_ignored_in_id():
    toks = ref_ids.normalize_query_refs("Schedule No. 1(B)(a) RBAC")
    assert ids(toks) == ["schedule-1"]


def test_sections_plural_prefix():
    assert ids(ref_ids.normalize_query_refs("Sections 4.7-4.8 of the FSD")) == ["4.7", "4.8"]


def test_prefixed_comma_continuation():
    toks = ref_ids.normalize_query_refs("PS 10.3.1(d), 10.3.12, 10.3.20 and Schedule No. 1")
    assert ids(toks) == ["10.3.1", "10.3.12", "10.3.20", "schedule-1"]
    assert toks[0]["sub"] == "d"


def test_prefixed_none_when_word_without_number():
    # "Schedule of Rates" has no number after the prefix word -> no ref
    assert ref_ids.normalize_query_refs("Variations with item not listed in Schedule of Rates") == []


# --- normalize_query_refs: bare forms (candidates flagged bare) ---


def test_bare_dotted_candidate():
    toks = ref_ids.normalize_query_refs("requirements of 10.4.8 for PV")
    assert ids(toks) == ["10.4.8"]
    assert toks[0]["bare"]


def test_bare_alnum_candidates():
    for q, expected in [("TC2-T3", "tc2-t3"), ("TQ-T8-1", "tq-t8-1"), ("2400-01", "2400-01")]:
        toks = ref_ids.normalize_query_refs(f"reply to {q} please")
        assert ids(toks) == [expected]
        assert toks[0]["bare"] and toks[0]["kind"] == "alnum"


def test_bare_dotted_dedupe_against_prefixed():
    # "PS 10.3.6" yields one certain token, not a certain + a bare duplicate
    toks = ref_ids.normalize_query_refs("PS 10.3.6")
    assert len(toks) == 1 and not toks[0]["bare"]


def test_iec_standard_not_extracted():
    assert ref_ids.normalize_query_refs("cables to IEC 60502-1:2004") == []


def test_prose_decimal_and_ranges_not_alnum():
    assert ref_ids.extract_mentioned_refs("Minimum conductor size shall be 1.5 mm²") == []
    assert ref_ids.extract_mentioned_refs("16-30 deg C range; see PDF p.18-19") == []


def test_no_refs_in_plain_query():
    assert ref_ids.normalize_query_refs(
        "embedded type webbased programmable controller data logging function web server I/O"
    ) == []


# --- promote_clause_headers ---


def test_promote_inserts_header_above_and_keeps_original():
    src = "## 10.3 Energy Monitoring System ('EMS')\n\n- 10.3.6 The Energy Monitoring System controller shall be an embedded type webbased programmable controller.\n"
    out = ref_ids.promote_clause_headers(src)
    lines = out.split("\n")
    assert lines[2] == "### 10.3.6"
    assert lines[3] == "- 10.3.6 The Energy Monitoring System controller shall be an embedded type webbased programmable controller."


def test_promote_idempotent():
    src = "- 10.3.6 The controller shall be embedded type.\n"
    once = ref_ids.promote_clause_headers(src)
    twice = ref_ids.promote_clause_headers(once)
    assert once == twice


def test_promote_numbered_artifact_not_promoted():
    src = "2. (a) Carbon Dioxide Sensor;\n3. (a) All wiring and power distribution cables.\n"
    assert ref_ids.promote_clause_headers(src) == src


def test_promote_letter_items_not_promoted():
    src = "- (c) Energy consumption shall be real-time recorded;\n"
    assert ref_ids.promote_clause_headers(src) == src


def test_promote_prose_decimal_not_promoted():
    src = "Minimum conductor size shall be 1.5 mm² and cables to IEC 60502-1:2004.\n"
    assert ref_ids.promote_clause_headers(src) == src


def test_promote_preliminaries_dotted_rows():
    out = ref_ids.promote_clause_headers("- 1.01 Employer\n")
    assert out == "### 1.01\n- 1.01 Employer\n"


def test_promote_demotes_letter_subsection_headers():
    src = "## Schedule No. 1 Specification for CDHK EMS Dashboard Functional Requirements\n\n## (A) General\n\n- (a) The Energy Monitoring System (EMS) of Dashboard...\n"
    out = ref_ids.promote_clause_headers(src)
    assert "### (A) General" in out
    assert "## (A) General" not in out.split("\n")


def test_promote_keeps_word_parenthesis_headers():
    # "(Optional)" is a word, not a letter subsection — stays H2
    src = "## (Optional) Schedule No. 3 Specification for Occupancy Sensing System\n"
    assert ref_ids.promote_clause_headers(src) == src


# --- extract_header_ref_ids (breadcrumb path) ---


def test_breadcrumb_clause_path():
    bc = "Particular Specifications > 10 Equipment and Installation Requirements > 10.3 Energy Monitoring System ('EMS') > 10.3.6"
    assert ref_ids.extract_header_ref_ids(bc) == ["10", "10.3", "10.3.6"]


def test_breadcrumb_sibling_subsection_ancestors():
    # H2 subsections are siblings of their parent section in this corpus:
    # "23.1 General" must still yield "23"; a promoted clause nests under 10.3
    assert ref_ids.extract_header_ref_ids("23.1 General") == ["23.1", "23"]
    assert ref_ids.extract_header_ref_ids("10.3 Energy Monitoring System ('EMS') > 10.3.6") == ["10.3", "10.3.6", "10"]
    assert ref_ids.extract_header_ref_ids("25.1 General") == ["25.1", "25"]


def test_breadcrumb_top_level_section():
    assert ref_ids.extract_header_ref_ids("23 (Optional)  Integration  of existing data systems") == ["23"]


def test_breadcrumb_optional_schedule():
    assert ref_ids.extract_header_ref_ids("(Optional) Schedule No. 3 Specification for Occupancy Sensing System") == ["schedule-3"]
    assert ref_ids.extract_header_ref_ids("Schedule No. 1 Specification for CDHK EMS Dashboard Functional Requirements") == ["schedule-1"]


def test_breadcrumb_schedule_of_rates_is_not_a_schedule_ref():
    assert ref_ids.extract_header_ref_ids("21 Variations with item not listed in Schedule of Rates") == ["21"]


def test_breadcrumb_alnum_headers():
    assert ref_ids.extract_header_ref_ids("TC2-T3") == ["tc2-t3"]
    assert ref_ids.extract_header_ref_ids("TQ-T8-1 - MVAC Control Reference, Temperature Offset and Control Data") == ["tq-t8-1"]
    assert ref_ids.extract_header_ref_ids("Question TQ-T2-13:") == ["tq-t2-13"]


def test_breadcrumb_sheet_h1_and_row():
    bc = "2400 - EMS Functional Development Specification_R0_Comments.xlsx — Sheet: Sheet 1 - clpe-comments-respons > 2400-01"
    assert ref_ids.extract_header_ref_ids(bc) == ["2400", "2400-01"]


def test_breadcrumb_plain_headers_have_no_ids():
    assert ref_ids.extract_header_ref_ids("School Principal") == []
    assert ref_ids.extract_header_ref_ids("(v) what user accounts and access rights will be provided") == []
    assert ref_ids.extract_header_ref_ids("Please confirm:") == []


# --- extract_mentioned_refs (parent body text) ---


def test_mentioned_prefixed_clause():
    assert ref_ids.extract_mentioned_refs("the systems of equipment specified in clause 10.3.3.") == ["10.3.3"]


def test_mentioned_ps_and_section():
    assert ref_ids.extract_mentioned_refs("comply with PS 22.5 and Section 25") == ["22.5", "25"]


def test_mentioned_schedule_with_subs():
    assert ref_ids.extract_mentioned_refs("aligned with Schedule No. 1(B)(a)") == ["schedule-1"]


def test_mentioned_alnum_ids():
    assert ref_ids.extract_mentioned_refs("Basis: PTQ8 TQ-T8-1(ii),(vi) and TQ-T8-2(iii)") == ["tq-t8-1", "tq-t8-2"]
    assert ref_ids.extract_mentioned_refs("see 2400-01 for details") == ["2400-01"]


def test_mentioned_comma_and_dash_continuations():
    refs = ref_ids.extract_mentioned_refs("Use the issued references, including PS 10.3.1(d), 10.3.12, 10.3.20, 22.2(c), 22.5, 10.3.26 and Schedule No. 1")
    assert refs == ["10.3.1", "10.3.12", "10.3.20", "22.2", "22.5", "10.3.26", "schedule-1"]
    assert ref_ids.extract_mentioned_refs("per PS 15.1-15.2") == ["15.1", "15.2"]


def test_mentioned_bare_dotted_without_prefix_ignored():
    # the kept original clause line "- 10.3.6 The ..." must not self-reference
    assert ref_ids.extract_mentioned_refs("- 10.3.6 The Energy Monitoring System controller shall be an embedded type.") == []


def test_mentioned_dedup():
    refs = ref_ids.extract_mentioned_refs("clause 10.3.3 and clause 10.3.3 again")
    assert refs == ["10.3.3"]
