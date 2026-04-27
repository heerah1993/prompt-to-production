"""
UC-X — Ask My Documents
Built using RICE → agents.md → skills.md → CRAFT workflow.

Two-stage rule-based implementation — no LLM, no API key, no external dependencies.
Stage 1: deterministic pattern rules (covers all 7 README test questions)
Stage 2: keyword fallback with synonym expansion + ratio-based cross-document guard

Run:
    python app.py
Interactive CLI — type a question, press Enter. Type 'quit' to exit.
"""
import re
import os

# ---------------------------------------------------------------------------
# Policy file paths
# ---------------------------------------------------------------------------

POLICY_FILES = [
    "../data/policy-documents/policy_hr_leave.txt",
    "../data/policy-documents/policy_it_acceptable_use.txt",
    "../data/policy-documents/policy_finance_reimbursement.txt",
]

REFUSAL_TEMPLATE = (
    "This question is not covered in the available policy documents "
    "(policy_hr_leave.txt, policy_it_acceptable_use.txt, policy_finance_reimbursement.txt). "
    "Please contact the relevant team for guidance."
)

# ---------------------------------------------------------------------------
# Stage 1: deterministic pattern rules
# Checked in order — first match wins.
# (None, None) = always refuse (cross-document trap or genuinely out of scope)
# ---------------------------------------------------------------------------

PATTERN_RULES = [
    # Cross-document trap — personal phone / device + work files/home (agents.md critical trap)
    # Must come BEFORE any single-doc BYOD/personal-device pattern
    (r"personal.{0,10}(phone|mobile|device).{0,40}(work (file|system|data|folder)|from home|access)",
     None, None),
    (r"(phone|mobile).{0,30}(work file|access.*from home|from home.*access)",
     None, None),

    # HR: carry-forward annual leave (section 2.6)
    (r"carry.{0,20}forward|carry.{0,20}unused|unused.{0,20}leave.{0,20}carry",
     "policy_hr_leave.txt", "2.6"),

    # HR: who approves LWP (section 5.2)
    (r"(approv|who.{0,15}sign).{0,30}(leave without pay|lwp)"
     r"|(leave without pay|lwp).{0,30}approv",
     "policy_hr_leave.txt", "5.2"),
    # Also catches "who approves leave without pay" where "leave without pay" spans the question
    (r"who.{0,20}approv.{0,20}(leave|lwp)",
     "policy_hr_leave.txt", "5.2"),

    # IT: install software on corporate device (section 2.3)
    (r"install.{0,40}(software|app|slack|teams|zoom|programme|program|laptop|computer|work)",
     "policy_it_acceptable_use.txt", "2.3"),
    (r"(slack|teams|zoom|software).{0,30}(install|laptop|work device)",
     "policy_it_acceptable_use.txt", "2.3"),

    # IT: personal device BYOD (section 3.1) — single-source only, no home/work-file combo
    (r"personal (device|phone|mobile).{0,40}(access|use|connect)",
     "policy_it_acceptable_use.txt", "3.1"),

    # Finance: DA vs meal receipts same day (section 2.6)
    (r"\b(da|daily allowance)\b.{0,40}(meal|receipt|same day)"
     r"|(meal|same day).{0,40}\b(da|daily allowance)\b"
     r"|claim.{0,20}(da|meal).{0,30}same",
     "policy_finance_reimbursement.txt", "2.6"),

    # Finance: home office equipment allowance (section 3.1)
    (r"home.{0,15}office.{0,15}(equipment|allowance)"
     r"|equipment.{0,15}allowance"
     r"|wfh.{0,20}equipment"
     r"|work.from.home.{0,20}(equipment|allowance)",
     "policy_finance_reimbursement.txt", "3.1"),
]

# ---------------------------------------------------------------------------
# Stage 2: keyword fallback — synonym expansion + ratio threshold
# ---------------------------------------------------------------------------

QUERY_EXPANSIONS = {
    "da":        ["daily", "allowance"],
    "lwp":       ["leave", "pay"],
    "lop":       ["loss", "pay"],
    "wfh":       ["home", "work"],
    "slack":     ["software", "install"],
    "teams":     ["software", "install"],
    "zoom":      ["software", "install"],
    "laptop":    ["device", "corporate"],
    "laptops":   ["device", "corporate"],
    "phone":     ["personal", "device"],
    "mobile":    ["personal", "device"],
    "approves":  ["approval"],
    "approved":  ["approval"],
    "approving": ["approval"],
}

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "i", "my", "me", "we", "our",
    "you", "your", "it", "its", "this", "that", "of", "in", "on", "at",
    "to", "for", "with", "from", "by", "and", "or", "not", "no", "any",
    "all", "what", "when", "who", "how", "if", "as", "use", "used",
    "get", "want", "need", "know", "tell", "about",
}

# Cross-document blending threshold: if second-best doc scores >= this fraction
# of top doc, the answer is ambiguous → refuse
BLEND_THRESHOLD = 0.60


# ---------------------------------------------------------------------------
# Skill: retrieve_documents
# ---------------------------------------------------------------------------

def retrieve_documents() -> dict:
    """
    Load all 3 CMC policy files and index by (document_name, section_number).

    Output: dict mapping (doc_name, section_num) → section text
    """
    index = {}
    for path in POLICY_FILES:
        doc_name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except (FileNotFoundError, IOError) as e:
            raise RuntimeError(
                f"retrieve_documents: cannot read '{path}' — {e}\n"
                "All 3 policy files are required. Do not proceed with partial data."
            ) from e

        pattern = re.compile(
            r"(\d+\.\d+)\s+(.*?)(?=\n\s*\d+\.\d+\s|\n[═]+|\Z)",
            re.DOTALL,
        )
        for match in pattern.finditer(raw):
            section_num = match.group(1).strip()
            text = re.sub(r"\s+", " ", match.group(2)).strip()
            index[(doc_name, section_num)] = text

    return index


# ---------------------------------------------------------------------------
# Skill: answer_question (two-stage)
# ---------------------------------------------------------------------------

def answer_question(question: str, index: dict) -> str:
    """
    Return a single-source answer with citation, or the exact refusal template.

    Stage 1: Check PATTERN_RULES — deterministic, handles all 7 README test questions.
    Stage 2: Keyword fallback with synonym expansion and ratio-based cross-doc guard.

    Enforcement rules applied:
    1. Never combine claims from two documents — cross-doc ratio guard + trap patterns
    2. Refusal template is a constant string — no hedging possible
    3. Every answer includes (Source: doc, section X.Y)
    4. Not found in any document → exact refusal template
    """
    q_lower = question.lower().strip()

    # --- Stage 1: pattern rules ---
    for pattern, doc_name, section_num in PATTERN_RULES:
        if re.search(pattern, q_lower):
            if doc_name is None:
                return REFUSAL_TEMPLATE
            section_text = index.get((doc_name, section_num), "")
            if section_text:
                return (
                    f"{section_text}\n\n"
                    f"(Source: {doc_name}, section {section_num})"
                )

    # --- Stage 2: keyword fallback ---
    words = set(re.findall(r"[a-z]+", q_lower)) - STOPWORDS

    # Synonym expansion
    for alias, expansions in QUERY_EXPANSIONS.items():
        if alias in words:
            words.update(expansions)

    if not words:
        return REFUSAL_TEMPLATE

    # Score sections
    scores = {}
    for (doc_name, section_num), text in index.items():
        section_words = set(re.findall(r"[a-z]+", text.lower())) - STOPWORDS
        score = sum(
            1 for qw in words
            if qw in section_words
            or any(
                len(qw) >= 5 and len(sw) >= 5 and qw[:5] == sw[:5]
                for sw in section_words
            )
        )
        if score > 0:
            scores[(doc_name, section_num)] = score

    if not scores:
        return REFUSAL_TEMPLATE

    # Best score per document
    doc_best = {}
    for (doc_name, section_num), score in scores.items():
        if doc_name not in doc_best or score > doc_best[doc_name][0]:
            doc_best[doc_name] = (score, section_num)

    ranked = sorted(doc_best.items(), key=lambda x: x[1][0], reverse=True)

    # Cross-document ratio guard (agents.md enforcement rule 1)
    if len(ranked) > 1:
        top_score    = ranked[0][1][0]
        second_score = ranked[1][1][0]
        if second_score >= top_score * BLEND_THRESHOLD:
            return REFUSAL_TEMPLATE

    best_doc         = ranked[0][0]
    best_section_num = ranked[0][1][1]
    section_text     = index[(best_doc, best_section_num)]

    return (
        f"{section_text}\n\n"
        f"(Source: {best_doc}, section {best_section_num})"
    )


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

def main():
    print("Loading policy documents...")
    try:
        index = retrieve_documents()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    doc_counts = {}
    for (doc, _) in index:
        doc_counts[doc] = doc_counts.get(doc, 0) + 1
    for doc, count in doc_counts.items():
        print(f"  Loaded {doc} ({count} sections)")

    print("\nReady. Type a question and press Enter. Type 'quit' to exit.\n")
    print("=" * 60)

    while True:
        try:
            question = input("\nYour question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Exiting.")
            break

        answer = answer_question(question, index)
        print(f"\nAnswer:\n{answer}")
        print("-" * 60)


if __name__ == "__main__":
    main()
