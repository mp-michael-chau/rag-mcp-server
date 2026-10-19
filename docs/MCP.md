# MCP Setup Details

The MCP server config and the quick setup live in the main [README](../README.md). This document adds the Section-ID search reference.

## Section-ID Search

The `search_shared_knowledge` tool recognizes section/clause references directly in `query`, or explicitly via the optional `ref` parameter:

| Query surface | Canonical ID | Behavior |
| --- | --- | --- |
| `PS 1.2.3`, `Clause 1.2.3(d)`, `1.4.8` | `1.2.3`, `1.2.3`(+sub `d`), `1.4.8` | ≥3-segment clause: returns that exact clause parent first |
| `Section 12`, `Section 15` | `12`, `15` | vector-ranks clauses within that section |
| `Schedule No. 2`, `Schedule 2`, `Sch.4`, `SC No. 2`, `SOR Schedule Nr. 2` | `schedule-2`, `schedule-4` | vector-ranks clauses within that schedule |
| `AB1-C2`, `XY9-Z8`, `1234-56` | verbatim lowercase | returns that exact item's block first |

Exact hits are tagged `[Exact ref match: <id>]` and followed by normal semantic results; a `Referenced by:` block lists sources mentioning the matched ID. Bare dotted/alphanumeric tokens are only trusted when the ID exists in the index (so standard numbers like `IEC 61010-1` never match). Letter sub-items (`1.2.3(d)`) resolve to the containing clause.
