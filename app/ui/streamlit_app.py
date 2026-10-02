"""Streamlit UI for the PE Deal Screening copilot (project-plan.md Phase 6).

Thin client over the FastAPI app: every tab just POSTs to an existing endpoint
(/screen, /ask, /compare, /screen/universe) and renders the JSON response. Run with:

    streamlit run app/ui/streamlit_app.py

Needs the FastAPI app running separately (`uvicorn app.main:app --reload`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
import streamlit as st
import yaml

DEFAULT_API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT_SECONDS = 600  # a first (unsaved) screening run is bounded by the LLM's free-tier rate limit

ASSESSMENT_LABELS = {
    "strong_evidence": "🟢 Strong evidence",
    "moderate_evidence": "🟡 Moderate evidence",
    "weak_evidence": "🟠 Weak evidence",
    "insufficient_evidence": "⚪ Insufficient evidence",
}
# Risk criteria use the same evidence levels, but strong evidence of a risk is a red flag, not a strength.
RISK_LABELS = {
    "strong_evidence": "🔴 Risk clearly present",
    "moderate_evidence": "🟠 Risk indicated",
    "weak_evidence": "🟡 Weak risk signal",
    "insufficient_evidence": "⚪ Insufficient evidence",
}


def assessment_label(assessment: str, polarity: str = "positive") -> str:
    labels = RISK_LABELS if polarity == "risk" else ASSESSMENT_LABELS
    return labels.get(assessment, assessment)


@st.cache_data
def load_companies(path: str = "config/companies.yaml") -> dict[str, str]:
    """Returns {company_id: display_name}, read directly from config since there's no /companies endpoint."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {company["id"]: company["name"] for company in payload.get("companies", [])}


@st.cache_data
def load_screening_structure(path: str = "config/screening_config.yaml") -> dict[str, Any]:
    """Returns the raw screening_dimensions mapping, used to populate the dimension/criterion pickers."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return payload.get("screening_dimensions") or {}


def call_api(method: str, path: str, base_url: str, **kwargs: Any) -> dict[str, Any] | None:
    """POST/GET against the FastAPI app, surfacing connection/HTTP errors as st.error instead of raising."""
    try:
        response = requests.request(method, f"{base_url}{path}", timeout=REQUEST_TIMEOUT_SECONDS, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        st.error(f"Could not reach the API at {base_url}. Is `uvicorn app.main:app --reload` running?")
    except requests.exceptions.HTTPError as exc:
        detail = str(exc)
        if exc.response is not None:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except ValueError:
                detail = exc.response.text or detail
        st.error(f"API error: {detail}")
    except requests.exceptions.RequestException as exc:
        st.error(f"Request failed: {exc}")
    return None


def render_sources(sources: list[dict[str, Any]], cached: bool = False) -> None:
    if cached:
        st.caption("💾 Saved result (not re-assessed). Tick *Recompute* in the sidebar to refresh it.")
    if not sources:
        st.caption("No sources.")
        return
    for source in sources:
        st.caption(f"{source['document']} — {source['file_name']}, p. {source['page']}")


def render_criterion(result: dict[str, Any]) -> None:
    label = assessment_label(result["assessment"], result.get("polarity", "positive"))
    with st.expander(f"**{result['criterion'].replace('_', ' ').title()}** — {label}"):
        st.write(result["rationale"])
        render_sources(result["sources"], result.get("cached", False))


def render_dimensions(dimensions: list[dict[str, Any]]) -> None:
    for dimension in dimensions:
        st.subheader(dimension["dimension"].replace("_", " ").title())
        st.caption(dimension["description"])
        for criterion in dimension["criteria"]:
            render_criterion(criterion)


def screening_tab(base_url: str, companies: dict[str, str], refresh: bool) -> None:
    st.header("Company Screening")
    company_id = st.selectbox(
        "Company", options=list(companies), format_func=lambda cid: companies[cid], key="screening_company"
    )
    top_k = st.slider("Chunks per criterion (top_k)", min_value=1, max_value=10, value=3, key="screening_top_k")
    if st.button("Generate PE Screening", type="primary"):
        with st.spinner(f"Screening {companies[company_id]}..."):
            result = call_api(
                "POST", "/screen", base_url, json={"company_id": company_id, "top_k": top_k, "refresh": refresh}
            )
        if result:
            render_dimensions(result["dimensions"])


def ask_tab(base_url: str, companies: dict[str, str]) -> None:
    st.header("Ask a question")
    company_options = ["(all companies)"] + list(companies)
    company_choice = st.selectbox(
        "Company", options=company_options, format_func=lambda cid: companies.get(cid, cid), key="ask_company"
    )
    question = st.text_area("Question", placeholder="What evidence is there of recurring revenue?")
    top_k = st.slider("Chunks (top_k)", min_value=1, max_value=10, value=5, key="ask_top_k")
    if st.button("Ask", type="primary"):
        if not question.strip():
            st.warning("Enter a question first.")
            return
        company_id = None if company_choice == "(all companies)" else company_choice
        with st.spinner("Retrieving evidence and generating an answer..."):
            result = call_api(
                "POST", "/ask", base_url, json={"question": question, "company_id": company_id, "top_k": top_k}
            )
        if result:
            st.write(result["answer"])
            render_sources(result["sources"])


def compare_tab(base_url: str, companies: dict[str, str], dimensions: dict[str, Any], refresh: bool) -> None:
    st.header("Compare two companies")
    col_a, col_b = st.columns(2)
    with col_a:
        company_a = st.selectbox(
            "Company A", options=list(companies), format_func=lambda cid: companies[cid], key="compare_a"
        )
    with col_b:
        company_b = st.selectbox(
            "Company B",
            options=list(companies),
            format_func=lambda cid: companies[cid],
            index=min(1, len(companies) - 1),
            key="compare_b",
        )
    dimension_options = ["(all dimensions)"] + list(dimensions)
    dimension_choice = st.selectbox(
        "Dimension",
        options=dimension_options,
        format_func=lambda d: d if d == "(all dimensions)" else d.replace("_", " ").title(),
        key="compare_dimension",
    )
    top_k = st.slider("Chunks per criterion (top_k)", min_value=1, max_value=10, value=3, key="compare_top_k")
    if st.button("Compare", type="primary"):
        if company_a == company_b:
            st.warning("Pick two different companies.")
            return
        dimension = None if dimension_choice == "(all dimensions)" else dimension_choice
        with st.spinner(f"Comparing {companies[company_a]} vs {companies[company_b]}..."):
            result = call_api(
                "POST",
                "/compare",
                base_url,
                json={
                    "company_a": company_a,
                    "company_b": company_b,
                    "top_k": top_k,
                    "dimension": dimension,
                    "refresh": refresh,
                },
            )
        if result:
            for dim in result["dimensions"]:
                st.subheader(dim["dimension"].replace("_", " ").title())
                st.caption(dim["description"])
                for criterion in dim["criteria"]:
                    polarity = criterion.get("polarity", "positive")
                    label_a = assessment_label(criterion["company_a"]["assessment"], polarity)
                    label_b = assessment_label(criterion["company_b"]["assessment"], polarity)
                    title = criterion["criterion"].replace("_", " ").title()
                    with st.expander(f"**{title}** — {companies[company_a]}: {label_a} | {companies[company_b]}: {label_b}"):
                        tab_a, tab_b = st.tabs([companies[company_a], companies[company_b]])
                        with tab_a:
                            st.write(criterion["company_a"]["rationale"])
                            render_sources(criterion["company_a"]["sources"], criterion["company_a"].get("cached", False))
                        with tab_b:
                            st.write(criterion["company_b"]["rationale"])
                            render_sources(criterion["company_b"]["sources"], criterion["company_b"].get("cached", False))


def universe_tab(base_url: str, companies: dict[str, str], dimensions: dict[str, Any], refresh: bool) -> None:
    st.header("Screen the company universe")
    criterion_options = {
        criterion_id: f"{dimension_id.replace('_', ' ').title()} — {criterion['question']}"
        for dimension_id, dimension in dimensions.items()
        for criterion_id, criterion in (dimension.get("criteria") or {}).items()
    }
    selected = st.multiselect(
        "Criteria (pick one or more, e.g. all four Buy & Build criteria)",
        options=list(criterion_options),
        default=[next(iter(criterion_options))],
        format_func=lambda cid: criterion_options[cid],
        key="universe_criteria",
    )
    min_assessment = st.selectbox(
        "Minimum evidence level",
        options=["strong_evidence", "moderate_evidence", "weak_evidence"],
        format_func=lambda a: ASSESSMENT_LABELS[a],
        index=1,
        key="universe_min_assessment",
    )
    top_k = st.slider("Chunks per company (top_k)", min_value=1, max_value=10, value=3, key="universe_top_k")
    if st.button("Screen universe", type="primary"):
        if not selected:
            st.warning("Pick at least one criterion.")
            return
        with st.spinner("Screening every ingested company..."):
            result = call_api(
                "POST",
                "/screen/universe",
                base_url,
                json={"criteria": selected, "top_k": top_k, "min_assessment": min_assessment, "refresh": refresh},
            )
        if result:
            render_universe(result, companies)


def render_universe(result: dict[str, Any], companies: dict[str, str]) -> None:
    """One row per company, one column per criterion; no overall ranking or verdict (framework.md section 5)."""
    criteria = result["criteria"]
    if any(c["polarity"] == "risk" for c in criteria):
        st.info("Risk criteria are included: for those, ✅ means the risk was **flagged**, not that it is absent.")

    by_company: dict[str, dict[str, dict[str, Any]]] = {}
    for row in result["results"]:
        by_company.setdefault(row["company_id"], {})[row["criterion"]] = row

    table = []
    for company_id, rows in by_company.items():
        line = {"Company": companies.get(company_id, company_id)}
        for criterion in criteria:
            row = rows[criterion["criterion"]]
            mark = "✅ " if row["meets_threshold"] else ""
            line[criterion["criterion"].replace("_", " ").title()] = mark + assessment_label(
                row["assessment"], row["polarity"]
            )
        table.append(line)
    st.dataframe(table, hide_index=True)

    st.subheader("Evidence per company")
    for company_id, rows in by_company.items():
        with st.expander(f"**{companies.get(company_id, company_id)}**"):
            for criterion in criteria:
                row = rows[criterion["criterion"]]
                st.markdown(
                    f"**{criterion['criterion'].replace('_', ' ').title()}** — "
                    f"{assessment_label(row['assessment'], row['polarity'])}"
                )
                st.write(row["rationale"])
                render_sources(row["sources"], row.get("cached", False))


def main() -> None:
    st.set_page_config(page_title="PE Deal Screening Copilot", layout="wide")
    st.title("PE Deal Screening Copilot")

    with st.sidebar:
        st.subheader("Settings")
        base_url = st.text_input("API base URL", value=DEFAULT_API_BASE_URL)
        refresh = st.checkbox(
            "Recompute (ignore saved results)",
            help="Screening results are saved under data/analysis/ and reused while nothing relevant changed. "
            "Tick this to force a fresh assessment.",
        )

    companies = load_companies()
    dimensions = load_screening_structure()

    tab_screening, tab_ask, tab_compare, tab_universe = st.tabs(
        ["Company Screening", "Ask", "Compare", "Universe"]
    )
    with tab_screening:
        screening_tab(base_url, companies, refresh)
    with tab_ask:
        ask_tab(base_url, companies)
    with tab_compare:
        compare_tab(base_url, companies, dimensions, refresh)
    with tab_universe:
        universe_tab(base_url, companies, dimensions, refresh)


if __name__ == "__main__":
    main()
