"""Streamlit UI for the PE Deal Screening copilot (project-plan.md Phase 6).

Thin client over the FastAPI app: every tab just calls an existing endpoint
(/screen, /ask, /compare, /screen/universe, /companies, /ingestion/*) and renders the JSON response. Run with:

    streamlit run app/ui/streamlit_app.py

Needs the FastAPI app running separately (`uvicorn app.main:app --reload`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
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
DOCUMENT_TYPES = [
    "annual_report",
    "investor_presentation",
    "investor_relations",
    "company_profile",
    "product_overview",
    "industry_overview",
    "customer_references",
    "acquisition_announcement",
    "acquisition_overview",
    "other",
]
UPLOAD_METADATA_COLUMNS = ["company_id", "document_type", "year", "title", "source_url"]


def assessment_label(assessment: str, polarity: str = "positive") -> str:
    labels = RISK_LABELS if polarity == "risk" else ASSESSMENT_LABELS
    return labels.get(assessment, assessment)


def side_label(side: dict[str, Any], polarity: str = "positive") -> str:
    """Like assessment_label, but shows a failed assessment (compare's per-company result) distinctly."""
    return "⚠️ Failed" if side.get("error") else assessment_label(side["assessment"], polarity)


@st.cache_data
def load_screening_structure(path: str = "config/screening_config.yaml") -> dict[str, Any]:
    """Returns the raw screening_dimensions mapping, used to populate the dimension/criterion pickers."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return payload.get("screening_dimensions") or {}


def call_api(method: str, path: str, base_url: str, **kwargs: Any) -> Any:
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


def load_companies(base_url: str) -> dict[str, str]:
    """Returns {company_id: display_name} from the API, so companies added in the Data tab show up right away."""
    return {company["id"]: company["name"] for company in call_api("GET", "/companies", base_url) or []}


def render_sources(sources: list[dict[str, Any]], cached: bool = False) -> None:
    if cached:
        st.caption("💾 Saved result (not re-assessed). Tick *Recompute* in the sidebar to refresh it.")
    if not sources:
        st.caption("No sources.")
        return
    for source in sources:
        st.caption(f"{source['document']} — {source['file_name']}, p. {source['page']}")


def render_criterion(result: dict[str, Any]) -> None:
    if result.get("error"):
        with st.expander(f"**{result['criterion'].replace('_', ' ').title()}** — ⚠️ Assessment failed"):
            st.warning(result["rationale"])
        return
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
                    label_a = side_label(criterion["company_a"], polarity)
                    label_b = side_label(criterion["company_b"], polarity)
                    title = criterion["criterion"].replace("_", " ").title()
                    with st.expander(f"**{title}** — {companies[company_a]}: {label_a} | {companies[company_b]}: {label_b}"):
                        tab_a, tab_b = st.tabs([companies[company_a], companies[company_b]])
                        with tab_a:
                            if criterion["company_a"].get("error"):
                                st.warning(criterion["company_a"]["rationale"])
                            else:
                                st.write(criterion["company_a"]["rationale"])
                                render_sources(criterion["company_a"]["sources"], criterion["company_a"].get("cached", False))
                        with tab_b:
                            if criterion["company_b"].get("error"):
                                st.warning(criterion["company_b"]["rationale"])
                            else:
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
            if row.get("error"):
                line[criterion["criterion"].replace("_", " ").title()] = "⚠️ Failed"
                continue
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
                title = criterion["criterion"].replace("_", " ").title()
                if row.get("error"):
                    st.markdown(f"**{title}** — ⚠️ Assessment failed")
                    st.warning(row["rationale"])
                    continue
                st.markdown(f"**{title}** — {assessment_label(row['assessment'], row['polarity'])}")
                st.write(row["rationale"])
                render_sources(row["sources"], row.get("cached", False))


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or not str(value).strip()


def add_company_form(base_url: str) -> None:
    st.subheader("1. Add a company")
    with st.form("add_company", clear_on_submit=True):
        col_left, col_right = st.columns(2)
        with col_left:
            name = st.text_input("Name *", placeholder="Acme Software AG")
            company_id = st.text_input(
                "ID *", placeholder="acme_software", help="Lowercase letters, digits and underscores. Cannot be changed later."
            )
            website = st.text_input("Website *", placeholder="https://www.acme.com")
            country = st.text_input("Country *", placeholder="Germany")
        with col_right:
            category = st.text_input("Category *", placeholder="workforce_management_software")
            ownership = st.selectbox("Ownership *", options=["public", "private"])
            ticker = st.text_input("Ticker", help="Leave empty for private companies.")
            tags = st.text_input("Screening tags *", placeholder="vertical_software, enterprise_software")
        submitted = st.form_submit_button("Add company")

    if not submitted:
        return
    payload = {
        "id": company_id,
        "name": name,
        "website": website,
        "country": country,
        "category": category,
        "ownership": ownership,
        "ticker": ticker,
        "screening_tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
    }
    if any(_is_blank(payload[field]) for field in ("id", "name", "website", "country", "category")) or not payload[
        "screening_tags"
    ]:
        st.warning("Fill in all fields marked with *.")
        return
    if call_api("POST", "/companies", base_url, json=payload):
        st.session_state["data_flash"] = f"Added company **{name}** to companies.yaml."
        st.rerun()


def upload_documents_form(base_url: str, companies: dict[str, str]) -> None:
    st.subheader("2. Upload documents")
    files = st.file_uploader(
        "PDF or HTML files",
        type=["pdf", "html", "htm"],
        accept_multiple_files=True,
        key=f"upload_files_{st.session_state.get('upload_nonce', 0)}",
    )
    if not files:
        return
    if not companies:
        st.info("Add a company first.")
        return

    st.caption("Every field is required. Double-click a cell to edit it.")
    rows = pd.DataFrame(
        [{"file": file.name, **{column: None for column in UPLOAD_METADATA_COLUMNS}} for file in files]
    ).astype({"company_id": object, "document_type": object, "year": "float64", "title": object, "source_url": object})
    edited = st.data_editor(
        rows,
        column_config={
            "file": st.column_config.TextColumn("File", disabled=True),
            "company_id": st.column_config.SelectboxColumn("Company", options=list(companies), required=True),
            "document_type": st.column_config.SelectboxColumn("Document type", options=DOCUMENT_TYPES, required=True),
            "year": st.column_config.NumberColumn("Year", min_value=1900, max_value=2100, step=1, format="%d", required=True),
            "title": st.column_config.TextColumn("Title", required=True),
            "source_url": st.column_config.TextColumn("Source URL", help="http(s) link to the original document", required=True),
        },
        hide_index=True,
        num_rows="fixed",
        key="upload_metadata_" + "|".join(f"{file.name}:{file.size}" for file in files),
    )

    if not st.button("Upload files", type="primary"):
        return
    records = edited.to_dict("records")
    incomplete = [record["file"] for record in records if any(_is_blank(record[c]) for c in UPLOAD_METADATA_COLUMNS)]
    if incomplete:
        st.warning("Missing metadata for: " + ", ".join(incomplete))
        return
    metadata = [
        {**{c: str(record[c]).strip() for c in UPLOAD_METADATA_COLUMNS}, "year": int(record["year"])} for record in records
    ]
    with st.spinner(f"Uploading {len(files)} file(s)..."):
        result = call_api(
            "POST",
            "/ingestion/upload",
            base_url,
            files=[("files", (file.name, file.getvalue(), file.type or "application/octet-stream")) for file in files],
            data={"metadata": json.dumps(metadata)},
        )
    if result:
        names = ", ".join(source["file_name"] for source in result["registered"])
        st.session_state["data_flash"] = f"Added {names} to sources.yaml. Run the ingestion below to make them searchable."
        st.session_state["upload_nonce"] = st.session_state.get("upload_nonce", 0) + 1
        st.rerun()


def run_ingestion_section(base_url: str) -> None:
    st.subheader("3. Run ingestion")
    st.caption("Parses, chunks and embeds every document in sources.yaml. Already ingested files are skipped.")
    if not st.button("Run ingestion"):
        return
    with st.spinner("Ingesting documents..."):
        result = call_api("POST", "/ingestion/all", base_url)
    if result:
        st.success(f"Ingested {result['ingested']} new document(s), skipped {result['skipped']} already ingested.")
        new_documents = [doc for doc in result["documents"] if doc["ingested"]]
        if new_documents:
            st.dataframe(new_documents, hide_index=True)


def data_tab(base_url: str, companies: dict[str, str]) -> None:
    st.header("Add your own data")
    if flash := st.session_state.pop("data_flash", None):
        st.success(flash)
    add_company_form(base_url)
    st.divider()
    upload_documents_form(base_url, companies)
    st.divider()
    run_ingestion_section(base_url)


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

    companies = load_companies(base_url)
    dimensions = load_screening_structure()

    tab_screening, tab_ask, tab_compare, tab_universe, tab_data = st.tabs(
        ["Company Screening", "Ask", "Compare", "Universe", "Data"]
    )
    with tab_screening:
        screening_tab(base_url, companies, refresh)
    with tab_ask:
        ask_tab(base_url, companies)
    with tab_compare:
        compare_tab(base_url, companies, dimensions, refresh)
    with tab_universe:
        universe_tab(base_url, companies, dimensions, refresh)
    with tab_data:
        data_tab(base_url, companies)


if __name__ == "__main__":
    main()
