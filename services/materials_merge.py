"""
Merge AI-generated 補充資料 (materials_summary) sections into the public 公司簡介
(summary). Pure functions, no I/O — extracted out of routers/materials.py so the
router only has to hold HTTP glue.
"""
import re

# Public DD sections (from registry/web) live at the top level. Deck-only topics
# are grouped under one collapsible umbrella so they read as "公司概況 的延伸".
PUBLIC_SECTIONS = ["業務概況", "競業分析", "主要風險"]
UMBRELLA = "營運綜覽"
# Top-level reading order: the deck umbrella sits right under 業務概況.
TOP_ORDER = ["業務概況", UMBRELLA, "競業分析", "主要風險"]
# Order of deck topics inside the umbrella; unknown headings keep their order at end.
SUB_ORDER = ["產品與服務", "商業模式與市場", "團隊與股東", "財務與募資亮點", "投資亮點"]

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_SUBHEADING_RE = re.compile(r"^###\s+(.+?)\s*$")


def _parse_subsections(body: str) -> list[dict]:
    """Split umbrella body into `### heading` sub-sections."""
    subs: list[dict] = []
    cur: dict | None = None
    for line in (body or "").split("\n"):
        m = _SUBHEADING_RE.match(line.strip())
        if m:
            cur = {"heading": m.group(1).strip(), "body": []}
            subs.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for s in subs:
        s["body"] = "\n".join(s["body"]).strip("\n")
    return subs


def normalize_to_umbrella(sections: list[dict], valid_subs: set[str] | None = None) -> list[dict]:
    """Reshape a flat section list so public DD sections stay top-level and every
    other (deck) section is collected as a `### sub-section` under the UMBRELLA.
    Idempotent: an existing UMBRELLA is unpacked and rebuilt.

    If `valid_subs` is given, umbrella sub-sections whose heading isn't in it are
    dropped — used on apply to purge stale subs the latest deck no longer produces
    (e.g. a renamed section)."""
    public: list[dict] = []
    subs: list[dict] = []
    for s in sections:
        if s["heading"] in PUBLIC_SECTIONS:
            public.append(s)
        elif s["heading"] == UMBRELLA:
            subs.extend(_parse_subsections(s["body"]))
        else:
            subs.append(s)  # stray top-level deck section → fold into umbrella

    # de-dup sub-sections by heading (last write wins), then order
    by_sub: dict[str, dict] = {}
    for s in subs:
        by_sub[s["heading"]] = s
    if valid_subs is not None:
        by_sub = {h: s for h, s in by_sub.items() if h in valid_subs}
    ordered_subs = sorted(
        by_sub.values(),
        key=lambda s: (SUB_ORDER.index(s["heading"]) if s["heading"] in SUB_ORDER else len(SUB_ORDER)),
    )
    result = list(public)
    if ordered_subs:
        umb_body = "\n\n".join(f"### {s['heading']}\n{s['body']}".rstrip() for s in ordered_subs)
        result.append({"heading": UMBRELLA, "body": umb_body})
    # Order top-level sections so 營運綜覽 sits right under 業務概況.
    result.sort(key=lambda s: TOP_ORDER.index(s["heading"]) if s["heading"] in TOP_ORDER else len(TOP_ORDER))
    return result


def parse_sections(md: str) -> list[dict]:
    """Split a Markdown summary into ordered sections by `## heading`.
    Returns [{heading, body}]. Any preamble before the first `##` is dropped."""
    sections: list[dict] = []
    cur: dict | None = None
    for line in (md or "").split("\n"):
        m = _HEADING_RE.match(line.strip())
        if m:
            cur = {"heading": m.group(1).strip(), "body": []}
            sections.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for s in sections:
        s["body"] = "\n".join(s["body"]).strip("\n")
    return sections


def serialize_sections(sections: list[dict]) -> str:
    parts = []
    for s in sections:
        body = s["body"].strip("\n")
        parts.append(f"## {s['heading']}\n{body}".rstrip())
    return "\n\n".join(parts).strip() + "\n"
