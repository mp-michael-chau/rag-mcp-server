"""ref_ids.py — canonical reference-ID model for section-ID search.

Pure functions, stdlib only. Responsibilities:

1. normalize_query_refs(text): extract reference tokens ("PS 10.3.6",
   "Clause 10.3.11(d)", "Section 23", "Schedule 3", "Sch.1", "SC No. 1",
   "TC2-T3", bare "10.4.8") into canonical lowercase IDs. Prefixed forms are
   certain; bare dotted/alphanumeric forms are flagged "bare" so the caller
   can existence-check them against the index before trusting them.
2. promote_clause_headers(content): insert a "### <id>" line immediately above
   dotted clause list lines ("- 10.3.6 The ...") so each clause becomes its own
   parent at ingest time. In-memory only — source files are never modified.
3. extract_header_ref_ids(breadcrumb) / extract_mentioned_refs(text): derive
   the payload fields parent_ref_ids (own IDs on the breadcrumb path) and
   mentioned_refs (reference-shaped tokens in the parent body text).

Canonical forms (stored lowercase in Qdrant payload arrays):
- dotted numeric (>=2 segments): verbatim ("10.3.6"); a letter sub-item is
  stripped and reported separately ("10.3.11(d)" -> id "10.3.11", sub "d")
- top-level section: bare integer ("23")
- named schedule: "schedule-3", "schedule-1"
- alphanumeric row/item IDs: verbatim lowercase ("tc2-t3", "tq-t8-1", "2400-01")
"""

import re

# --- prefix words (longest first so alternation prefers the longest match) ---

_PREFIX_WORDS = (
    "sor schedule nr",  # SOR Schedule Nr. 3
    "schedule no",      # Schedule No. 3
    "schedule nr",      # Schedule Nr. 3
    "sc no",            # SC No. 1
    "schedule",         # Schedule 3
    "section",          # Section 23
    "sch",              # Sch.1 / Sch 1 / Sch. No. 1
    "clause",           # Clause 10.3.11(d)
    "ps",               # PS 10.3.6
)

_SCHEDULE_PREFIXES = {"sor schedule nr", "schedule no", "schedule nr", "sc no", "schedule", "sch"}

_PREFIXED_RE = re.compile(
    r"\b(?P<prefix>" + "|".join(_PREFIX_WORDS) + r")s?\.?[ \t]*(?:(?:no|nr)\.?[ \t]*)?"
    r"(?P<num>\d+(?:\.\d+)*)"
    r"(?:[ \t]*(?P<sub>\([0-9]{1,3}\)|\([A-Za-z]{1,4}\)))?",
    re.IGNORECASE,
)

# Bare dotted numeric (>=2 segments); lookarounds keep "3.6" inside "10.3.6"
# and decimals inside larger tokens from matching.
_BARE_DOTTED_RE = re.compile(r"(?<![\w.])(\d+(?:\.[0-9]+)+)(?![\w.])")

# Alphanumeric row/item IDs: letter-starting with >=1 dash and >=1 digit
# (TQ-T8-1, TC2-T3), or 3-4 leading digits (2400-01; excludes 16-30, 18-19).
_ALNUM_RE = re.compile(
    r"(?<![\w-])([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+|\d{3,4}(?:-[A-Za-z0-9]+)+)(?![\w-])"
)

# Comma/semicolon/dash-continued dotted numbers after a prefixed ref
# ("PS 10.3.1(d), 10.3.12, 10.3.20", "PS 15.1-15.2"); a trailing sub-item
# like "22.2(c)," is consumed so the chain keeps going.
_CONTINUATION_RE = re.compile(
    r"^[ \t]*(?:,|;|-)[ \t]*(\d+(?:\.[0-9]+)+)(?:[ \t]*\([0-9]{1,3}\)|[ \t]*\([A-Za-z]{1,4}\))?"
)

# Dotted clause list line: "- 10.3.6 The ..." (>=2 dotted segments; the body
# must start with a non-space char so bare "- 10.3.6" lines are not promoted)
_CLAUSE_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)-[ \t]+(?P<id>\d+(?:\.[0-9]+)+)[ \t]+\S.*$")

# Letter/number subsection headers ("## (A) General", "## (h) Monthly ...")
# demoted to H3 at ingest time so they nest under their parent section H2
# (e.g. "Schedule No. 1 ... > (B) Performance Specifications"); as H2 siblings
# they would orphan the parent's ref id. "(Optional)" (a word) is unaffected.
_LETTER_HEADER_RE = re.compile(r"^## (?P<title>\([0-9]{1,3}\)[ \t]+\S.*|\([A-Za-z]{1,4}\)[ \t]+\S.*)$")

_HEADER_OPTIONAL_RE = re.compile(r"^\(optional\)[ \t]+", re.IGNORECASE)
_HEADER_LEADING_DOTTED_RE = re.compile(r"^(\d+(?:\.[0-9]+)+)(?=$|\s)")
_HEADER_LEADING_INT_RE = re.compile(r"^(\d+)(?=$|\s)")


def _norm_prefix(prefix):
    """Lowercase, drop a plural s and any dot/space noise -> comparable prefix word."""
    p = prefix.lower().strip().rstrip(".").strip()
    return p[:-1] if p.endswith("s") else p


def _sub_letter(sub_match):
    """'(d)'/'(B)'/'(ii)' -> 'd'/'b'/'ii', or None."""
    if not sub_match:
        return None
    return sub_match.strip("() \t").lower()


def _is_alnum_id(token):
    """Alnum dash-token filter: must contain at least one digit (kills
    'clpe-comments-respons'-style title fragments)."""
    return any(c.isdigit() for c in token)


def _token(kind, cid, bare, raw, sub=None):
    return {"id": cid, "kind": kind, "bare": bare, "raw": raw, "sub": sub}


def _prefixed_tokens(text):
    """Certain (prefixed) refs in appearance order, including comma/dash
    continuations of the dotted number list."""
    tokens = []
    for m in _PREFIXED_RE.finditer(text):
        prefix = _norm_prefix(m.group("prefix"))
        num = m.group("num")
        sub = _sub_letter(m.group("sub") or "")
        if prefix in _SCHEDULE_PREFIXES:
            tokens.append(_token("schedule", f"schedule-{num}", False, m.group(0).strip()))
        elif "." in num:
            tokens.append(_token("clause", num, False, m.group(0).strip(), sub))
        else:
            tokens.append(_token("section", num, False, m.group(0).strip()))
        # continuation list: ", 10.3.12", "- 15.2" right after the prefixed ref
        rest = text[m.end():]
        while True:
            cm = _CONTINUATION_RE.match(rest)
            if not cm:
                break
            tokens.append(_token("clause", cm.group(1), False, cm.group(0).strip()))
            rest = rest[cm.end():]
    return tokens


def _alnum_tokens(text):
    tokens = []
    for m in _ALNUM_RE.finditer(text):
        tok = m.group(1)
        if _is_alnum_id(tok):
            tokens.append(_token("alnum", tok.lower(), True, m.group(0)))
    return tokens


def normalize_query_refs(text):
    """Extract reference tokens from a query (or an explicit ref override).

    Returns a list of dicts in appearance order:
    {"id": canonical, "kind": clause|section|schedule|alnum, "bare": bool,
     "raw": matched text, "sub": letter sub-item or None}
    Prefixed forms are certain (bare=False); bare dotted/alphanumeric tokens
    are candidates (bare=True) that the caller should existence-check against
    parent_ref_ids before using (kills false positives like "IEC 60502-1").
    Duplicate ids are dropped, keeping the non-bare (certain) occurrence.
    """
    tokens = _prefixed_tokens(text)
    prefix_ids = {(t["id"], t["sub"]) for t in tokens}

    for m in _BARE_DOTTED_RE.finditer(text):
        tokens.append(_token("clause", m.group(1), True, m.group(0)))
    tokens.extend(_alnum_tokens(text))

    # dedupe by id, preferring the non-bare (certain) occurrence
    out = []
    seen = set()
    for t in sorted(tokens, key=lambda t: t["bare"]):
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        out.append(t)
    return out


def promote_clause_headers(content):
    """Insert a "### <id>" header line immediately above each dotted clause
    list line ("- 10.3.6 The ..."), keeping the original line unchanged as
    body text (MarkdownHeaderTextSplitter strips headers, so without the kept
    line a single-line clause would become an empty section).

    Requiring >=2 dotted segments excludes PDF-conversion artifacts like
    "2. (a) Carbon Dioxide Sensor;" (numbered, not dashed) and prose decimals
    ("1.5 mm²", "IEC 60502-1:2004" — never list lines starting with the id).
    Also demotes short letter/number subsection headers ("## (A) General") to
    H3 so they nest under their parent section (see _LETTER_HEADER_RE).
    Idempotent: a line whose predecessor is already the same promoted header
    is not promoted twice.
    """
    out = []
    for line in content.split("\n"):
        m = _CLAUSE_LINE_RE.match(line)
        if m:
            header = f"{m.group('indent')}### {m.group('id')}"
            if not out or out[-1].strip() != header.strip():
                out.append(header)
            out.append(line)
            continue
        lm = _LETTER_HEADER_RE.match(line)
        out.append(f"### {lm.group('title')}" if lm else line)
    return "\n".join(out)


def _header_ids(header):
    """Canonical ref IDs for one breadcrumb segment (one header line text)."""
    ids = []
    h = _HEADER_OPTIONAL_RE.sub("", header)  # "(Optional) Schedule No. 3 ..." -> "Schedule No. 3 ..."

    m = _HEADER_LEADING_DOTTED_RE.match(h)
    if m:
        ids.append(m.group(1))
    else:
        m = _HEADER_LEADING_INT_RE.match(h)
        if m:
            ids.append(m.group(1))

    # schedules name themselves mid-header ("Schedule No. 3 Specification ...");
    # only schedule-prefixed forms count as identity — ps/clause/section words
    # inside a header are mentions, not the section's own ID
    for t in _prefixed_tokens(h):
        if t["kind"] == "schedule" and t["id"] not in ids:
            ids.append(t["id"])

    for t in _alnum_tokens(h):
        if t["id"] not in ids:
            ids.append(t["id"])
    return ids


def extract_header_ref_ids(breadcrumb):
    """Own IDs of a parent: the clause/section/schedule ID of every header on
    the breadcrumb path ("H1 > H2 > H3"), plus the ancestor prefixes of each
    dotted ID ("23.1" also yields "23") — H2 subsections are siblings of their
    parent section in this corpus, so the ancestor is often not literally on
    the path. Children of clause 10.3.6 get ["10", "10.3", "10.3.6"]; children
    under "(Optional) Schedule No. 3 ..." get ["schedule-3"]."""
    ids = []
    if not breadcrumb:
        return ids
    for header in breadcrumb.split(" > "):
        for cid in _header_ids(header.strip()):
            if cid not in ids:
                ids.append(cid)
    # ancestor prefixes of dotted IDs ("22.5" -> "22"), appended after own IDs
    for cid in list(ids):
        if "." not in cid:
            continue
        parts = cid.split(".")
        for i in range(1, len(parts)):
            ancestor = ".".join(parts[:i])
            if ancestor not in ids:
                ids.append(ancestor)
    return ids


def extract_mentioned_refs(text):
    """Reference-shaped canonical IDs mentioned in parent body text: prefixed
    refs ("clause 10.3.3", "PS 22.5", "Section 25"), their comma/dash
    continuations ("PS 10.3.1(d), 10.3.12"), schedules ("Schedule No. 1(B)(a)"
    -> "schedule-1") and alphanumeric IDs ("2400-01", "TQ-T8-1"). Bare dotted
    numbers without a prefix are deliberately NOT extracted (over-extraction
    is tolerable here, but bare decimals are pure noise)."""
    ids = []
    for t in _prefixed_tokens(text) + _alnum_tokens(text):
        if t["id"] not in ids:
            ids.append(t["id"])
    return ids
