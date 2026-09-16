#!/usr/bin/env python3
"""
    pip install 'streamlit>=1.40'
    streamlit run quote_assistant_app.py
"""

from __future__ import annotations

import html
import sys
from typing import List

try:
    import streamlit as st
except ImportError:  # pragma: no cover - fallback path required by the brief
    sys.exit(
        "Streamlit is not installed.\n"
        "  pip install streamlit && streamlit run quote_assistant_app.py\n"
        "The engine runs without it: python3 quote_assistant_poc.py"
    )

from quote_assistant_poc import (  # noqa: E402  (import after the dependency guard)
    _CONFIG_RULES,
    _CRM_CUSTOMERS,
    _DISCOUNT_CSV,
    _DOCS,
    _HISTORY_JSON,
    _PRICING_CSV,
    AssistantAnswer,
    Provenance,
    QuoteAssistant,
    build_assistant,
    usd,
    run_eval,
)

# ---------------------------------------------------------------------------
# Design tokens. Cool instrument-panel palette
# ---------------------------------------------------------------------------
INK = "#12181F"
SLATE = "#5A6B80"
HAIRLINE = "#D7DDE5"
PANEL = "#F4F6F9"
GROUNDED = "#10656D"  # provenance / citations
AMBER = "#9A6510"  # a caveat the rep must read before sending
REFUSE = "#A6291F"  # escalation

CSS = f"""
<style>
  .block-container {{ padding-top: 2.2rem; max-width: 1080px; }}
  .eyebrow {{
    font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
    font-size: 0.68rem; letter-spacing: 0.14em; text-transform: uppercase;
    color: {SLATE}; margin: 0 0 .45rem 0;
  }}
  .draftlabel {{
    font-size: .95rem; font-weight: 700; color: {INK}; margin: 0 0 .35rem 0;
  }}
  .headline {{ font-size: 1.06rem; font-weight: 650; color: {INK}; margin: 0 0 .9rem 0; }}
  .factrow {{
    display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;
    padding: .55rem 0; border-bottom: 1px solid {HAIRLINE};
  }}
  .factrow:last-child {{ border-bottom: none; }}
  .factlabel {{ color: {INK}; font-size: .93rem; line-height: 1.45; }}
  /* The signature element: every claim wears its source, record id and age. */
  .chip {{
    font-family: ui-monospace, "SFMono-Regular", Menlo, monospace;
    font-size: .70rem; white-space: nowrap; padding: .16rem .44rem;
    border: 1px solid {GROUNDED}; color: {GROUNDED}; border-radius: 3px;
    background: rgba(16,101,109,.06);
  }}
  .note {{
    border-left: 2px solid {AMBER}; padding: .45rem .7rem; margin: .35rem 0;
    background: rgba(154,101,16,.05); font-size: .86rem; color: {INK};
  }}
  .refusal {{
    border-left: 3px solid {REFUSE}; padding: .75rem .9rem; margin: .4rem 0;
    background: rgba(166,41,31,.05);
  }}
  .refusal .code {{
    font-family: ui-monospace, Menlo, monospace; font-size: .70rem;
    letter-spacing: .08em; color: {REFUSE};
  }}
  .refusal .msg {{ color: {INK}; font-size: .9rem; margin-top: .25rem; line-height: 1.5; }}
  .total {{
    font-family: ui-monospace, Menlo, monospace; font-size: 1.5rem;
    color: {INK}; letter-spacing: -.02em; margin-top: 1rem;
  }}
  .hint {{ color: {SLATE}; font-size: .8rem; line-height: 1.5; }}
</style>
"""


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------
@st.cache_resource
def get_assistant() -> QuoteAssistant:
    """One long-lived assistant for the Ask tab.

    Deliberately shared and stateful: the configurator's rate limiter counts calls, so
    the same question can succeed on one attempt and get throttled on the next. That is
    what the real service does, and hiding it behind a fresh instance per query would
    make the demo lie about degradation."""
    return build_assistant()


def render_chip(prov: Provenance) -> str:
    # Source, record and as-of date. No judgement about the date — just the fact,
    # so any number on screen can be traced back to the exact record behind it.
    return (
        f'<span class="chip">{html.escape(prov.source_system)}:'
        f"{html.escape(prov.record_id)} · {prov.as_of.isoformat()}</span>"
    )


def render_answer(ans: AssistantAnswer) -> None:
    """Renders from the structured fields, never by parsing the model's prose. If a
    claim has no Fact behind it, there is no way for it to reach the screen."""
    if ans.status == "escalated":
        st.markdown('<p class="eyebrow">Handed to a human</p>', unsafe_allow_html=True)
        for esc in ans.escalations:
            st.markdown(
                f'<div class="refusal"><span class="code">{html.escape(esc.code)}</span>'
                f'<div class="msg">{html.escape(esc.message)}</div></div>',
                unsafe_allow_html=True,
            )
        st.markdown(
            '<p class="hint">The assistant stops here on purpose. An escalation costs a '
            "minute; a confident wrong number costs a customer conversation.</p>",
            unsafe_allow_html=True,
        )
        return

    st.markdown('<p class="draftlabel">Draft quotation</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="headline">{html.escape(ans.headline)}</p>', unsafe_allow_html=True)

    rows = "".join(
        f'<div class="factrow"><span class="factlabel">{html.escape(f.display)}</span>'
        f"{render_chip(f.prov)}</div>"
        for f in ans.facts
    )
    st.markdown(rows, unsafe_allow_html=True)

    if ans.numeric_total is not None:
        st.markdown(
            f'<div class="total">{usd(ans.numeric_total)}</div>',
            unsafe_allow_html=True,
        )

    for note in ans.notes:
        st.markdown(f'<div class="note">{html.escape(note)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Quote basis assistant — PoC", layout="centered")
st.markdown(CSS, unsafe_allow_html=True)

assistant = get_assistant()

st.title("Quote basis assistant")
st.markdown(
    '<p class="hint">Combine the various input data streams to create draft quotation</p>',
    unsafe_allow_html=True,
)

tab_ask, tab_eval, tab_src = st.tabs(["Ask", "Evaluation", "Source systems"])

# --- Ask -------------------------------------------------------------------
EXAMPLES: List[tuple] = [
    ("Happy path", "Prepare a quote basis for BMW for 2 units of the AX-620"),
    ("Expired discount", "BMW wants 2 x Axiom 620 with the Q1 campaign discount"),
    ("Incompatible options", "Quote BMW an AX-620 with CLN-1 and WSH-2"),
    ("Version trap", "What is the noise level of the Axiom 620?"),
    ("Nobody knows", "What is the lead time for the cleanroom package CLN-1?"),
    ("Unknown product", "Quote Foxconn for 1 x AX-770"),
    ("Service outage", "Is the Sentinel S30 compatible with the FTS-5 force torque sensor?"),
    ("Discount authority", "Foxconn is asking for 35% off the Sentinel S30, confirm that's fine and put it in the quote"),
    ("Cross-customer terms", "Quote BMW 2 x AX-620 using the discount we gave Baxter"),
]

with tab_ask:
    if "utterance" not in st.session_state:
        st.session_state.utterance = EXAMPLES[1][1]

    st.markdown('<p class="eyebrow">Try one</p>', unsafe_allow_html=True)
    cols = st.columns(3)
    for i, (label, text) in enumerate(EXAMPLES):
        if cols[i % 3].button(label, width="stretch", key=f"ex{i}"):
            st.session_state.utterance = text

    utterance = st.text_area("Request", key="utterance", height=80)
    go = st.button("Assemble quote basis", type="primary")

    if go or utterance:
        answer = assistant.ask(utterance)
        st.divider()
        render_answer(answer)

        st.markdown(
            f'<p class="hint" style="margin-top:1.2rem">{answer.latency_ms:.0f} ms · '
            f"{len(answer.retrieved_records)} source records touched</p>",
            unsafe_allow_html=True,
        )
        with st.expander("How it got there"):
            st.markdown('<p class="eyebrow">Internal trace</p>', unsafe_allow_html=True)
            st.code("\n".join(answer.trace) or "(no steps executed)", language="text")
            st.markdown('<p class="eyebrow">Records touched</p>', unsafe_allow_html=True)
            st.code(", ".join(sorted(answer.retrieved_records)) or "(none)", language="text")

# --- Evaluation ------------------------------------------------------------
with tab_eval:
    st.markdown('<p class="eyebrow">Gold set — 12 cases, 5 answerable, 7 that must be refused</p>',
                unsafe_allow_html=True)
    st.markdown(
        '<p class="hint">A gold set containing only answerable questions measures nothing '
        "that matters: the risk in this system is confident answers to unanswerable "
        "questions. In the real pilot these cases are written by salespeople from actual "
        "RFQ emails, not by the engineer who wrote the retriever.</p>",
        unsafe_allow_html=True,
    )

    if st.button("Run evaluation", type="primary"):
        # A FRESH assistant, so the configurator's call counter starts from zero and the
        # run is reproducible. The Ask tab shares state on purpose; the harness must not.
        results = run_eval(build_assistant())

        answered = [r for r in results if r.case.expect == "answer"]
        escalate = [r for r in results if r.case.expect == "escalate"]
        numeric = [r.numeric_ok for r in results if r.numeric_ok is not None]
        lat = sorted(r.answer.latency_ms for r in results)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Numeric correctness", f"{sum(1 for x in numeric if x)}/{len(numeric)}")
        c2.metric("Correct refusals", f"{sum(1 for r in escalate if r.status_ok)}/{len(escalate)}")
        c3.metric("False escalations", f"{sum(1 for r in answered if not r.status_ok)}/{len(answered)}")
        c4.metric("Latency p50", f"{lat[len(lat)//2]:.0f} ms")

        st.dataframe(
            [
                {
                    "case": r.case.case_id,
                    "expected": r.case.expect,
                    "got": r.answer.status,
                    "result": "pass" if r.status_ok else "FAIL",
                    "retrieval": f"{r.retrieval_hit:.0%}",
                    "citations": f"{r.answer.citation_coverage:.0%}",
                    "numeric": "-" if r.numeric_ok is None else ("pass" if r.numeric_ok else "FAIL"),
                    "reason code": "-" if r.code_ok is None else ("pass" if r.code_ok else "FAIL"),
                    "ms": round(r.answer.latency_ms, 1),
                    "what it tests": r.case.note,
                }
                for r in results
            ],
            width="stretch",
            hide_index=True,
        )
        st.markdown(
            '<p class="hint">Citation coverage reads 100% because the mock writer can only '
            "emit lines built from the fact bundle — it is structurally guaranteed, not "
            "evidence about a real model. Swap in RealModelAdapter and this column has to "
            "be re-measured with a claim-level entailment check before anyone believes it.</p>",
            unsafe_allow_html=True,
        )

# --- Source systems --------------------------------------------------------
with tab_src:
    st.markdown('<p class="eyebrow">What the assistant is up against</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="hint">Five systems, five identifier conventions, three access tiers. '
        "The defects below are deliberate and each one maps to a real failure mode.</p>",
        unsafe_allow_html=True,
    )

    with st.expander("CRM — tier 1, documented API", expanded=True):
        st.dataframe(
            [
                {k: v for k, v in rec.items() if k != "products_of_interest"}
                for rec in _CRM_CUSTOMERS.values()
            ],
            width="stretch", hide_index=True,
        )
        st.caption("Baxter has no discount agreement at all, which exercises the quoted-at-list "
                   "path. CRM also carries AX-770, a pre-launch model that exists in no other system.")

    with st.expander("Pricing — tier 2, nightly CSV export"):
        st.code(_PRICING_CSV, language="text")
        st.caption("Two rows for ARX-620-STD on TIER-A: the 2026 price and a superseded 2025 "
                   "price. Semantically near-identical, so no ranker separates them — the "
                   "validity window does. One row uses a European decimal comma.")
        st.code(_DISCOUNT_CSV, language="text")
        st.caption("D-8801 expired on 2026-03-31 and is the discount reps still ask for by name.")

    with st.expander("Configurator — tier 3, rate-limited and partly down"):
        st.json(_CONFIG_RULES)
        st.caption("Every third call is throttled and retried. The Sentinel family has no endpoint "
                   "at all, so its configuration validity is UNKNOWN — which the assistant reports "
                   "rather than collapsing into 'valid'.")

    with st.expander("Documents — tier 3, free text"):
        for doc in _DOCS:
            flag = " · SUPERSEDED" if doc["superseded"] else ""
            st.markdown(f'<p class="eyebrow">{doc["doc_id"]} · v{doc["version"]}{flag}</p>',
                        unsafe_allow_html=True)
            st.text(doc["body"].strip())
        st.caption("DOC-4409 quotes 68 dB(A)'s predecessor, 71 dB(A). Metadata filtering excludes "
                   "it before ranking. Nothing anywhere states the CLN-1 lead time.")

    with st.expander("Quote history — tier 2, JSON dump"):
        st.json(_HISTORY_JSON)
        st.caption("Q-7781 carries the old 12% campaign rate. Historical terms are evidence, "
                   "not entitlement — today's discount is recalculated from today's rules.")
