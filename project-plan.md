# Project definition
PE Deal Screening RAG

Goal: Build a small RAG application that helps a PE investment professional screen European B2B software companies using publicly available company documents.

Instead of generic document Q&A, the application retrieves evidence against a predefined PE Screening Framework and generates traceable company assessments.

Core principle

The system does not make investment recommendations or calculate an investment score.

It answers:

What evidence do the available sources provide for or against specific PE screening criteria?

Each conclusion should be backed by retrieved evidence and citations.

## Scope
### Company universe

Target:

~20 European B2B software companies
Start development with 5 companies
Expand toward 20 only after the pipeline works

### Documents

Target 2 primary documents per company:

Latest annual report / equivalent company report
Latest investor/results presentation

Optionally add important acquisition announcements where useful.

That gives approximately:

20 companies × 2 documents = ~40 documents

## PE Screening Framework

This becomes a first-class project artifact:

config/
├── companies.yaml
└── screening_framework.yaml


Keep the first version to around 12 criteria.

Business Quality
Recurring revenue
Mission criticality
Customer retention
Market position
Growth
Organic growth
International expansion
Market growth
Profitability
Margin / profitability
Cash generation
Buy & Build
Acquisition history
Acquisition strategy
Fragmented market
Risks

I'd treat risks slightly differently: retrieve whatever material risks the company reports rather than trying to exhaustively score a predefined list.

The assessment vocabulary stays very simple:

Strong evidence
Moderate evidence
Weak evidence
Insufficient evidence

## Use cases

### UC1: Ask questions about a company

Example:

What evidence is there that ATOSS has recurring revenues?

The RAG:

filters documents to ATOSS
retrieves relevant chunks
generates an answer
cites the evidence

Purpose: basic RAG capability.

### UC2: PE Company Screening 

This is now the hero feature.

User selects:

Nemetschek → Generate PE Screening

The application evaluates each screening criterion using targeted retrieval.

Output:

NEMETSCHEK
PE Screening

BUSINESS QUALITY

Recurring Revenue
Assessment: Strong evidence

Rationale:
...

Evidence:
...

Source:
Annual Report 2025, p. XX


Mission Criticality
Assessment: Moderate evidence

Rationale:
...

Source:
...


CUSTOMER RETENTION

Assessment: Insufficient evidence

No sufficient information was found in
the available sources.


This is what distinguishes the project from a normal "chat with your PDFs" application.

### UC3: Compare companies

User selects:

Nemetschek
vs.
ATOSS


And optionally a dimension:

[All]
Business Quality
Growth
Profitability
Buy & Build
Risks


The system applies the same criteria to both companies.

For example:

Criterion	Nemetschek	ATOSSRecurring revenue	Strong evidence	Strong evidence
International expansion	Strong evidence	Moderate evidence
Acquisition history	Strong evidence	Weak evidence
Customer retention	Insufficient	Moderate evidence

Clicking into a result shows the underlying evidence.

The point is consistent comparison against a common framework, not declaring a winner.

### UC4: Screen the company universe

This should be the stretch feature because technically it is mostly reuse of UC2.

Example:

Show companies with evidence of an active acquisition strategy.

or:

Find companies with strong evidence of recurring revenue.

The system performs retrieval across company metadata and shows supporting evidence for matching companies.

You are essentially turning your RAG corpus into a miniature research universe.

## Architecture

The architecture stays deliberately small:

             Public Documents
                    │
                    ▼
            Document Loader
                    │
                    ▼
            Chunk + Metadata
                    │
                    ▼
              Embeddings
                    │
                    ▼
              Vector Store
                    │
             ┌──────┴──────┐
             │             │
             ▼             ▼
        User query    PE Screening
                     Framework
             │             │
             └──────┬──────┘
                    ▼
             Retrieval Layer
                    │
                    ▼
                  LLM
                    │
                    ▼
         Answer / Assessment
                    │
                    ▼
         Evidence + Citations
                    │
                    ▼
             Streamlit UI


## Project plan

### DAY 1: Corpus + RAG
#### Phase 1: Project setup

Create:

pe-screening-rag/

├── app/
├── config/
│   ├── companies.yaml
│   └── screening_framework.yaml
├── data/
│   ├── raw/
│   └── processed/
├── ingestion/
├── evaluation/
└── README.md


Define the ~12 screening criteria.

Exit condition: scope and framework frozen.

Do not keep tweaking the PE methodology afterwards.

#### Phase 2: Data collection

Start with only:

Nemetschek
ATOSS
Nexus
IVU
TeamViewer

We collect two strong primary sources per company.

Organize:

data/raw/
  nemetschek/
  atoss/
  nexus/
  ivu/
  teamviewer/


Capture metadata:

company_id
document_type
title
date
source_url

#### Phase 3: Ingestion

Build:

PDF
 ↓
text extraction
 ↓
chunk
 ↓
metadata
 ↓
embedding
 ↓
vector index


Critical metadata:

{
  "company_id": "atoss",
  "document_type": "annual_report",
  "year": 2025,
  "page": 42,
  "source": "..."
}


#### Phase 4: Basic RAG

Implement:

retrieve(
    query,
    company_id=None,
    top_k=5
)


Then:

question
    ↓
retrieval
    ↓
context
    ↓
LLM
    ↓
answer + citations


Test manually:

What evidence is there of recurring revenue at ATOSS?

What acquisitions has Nemetschek made?

What risks does Nexus report?

Day 1 finish line

If those kinds of questions return good evidence and citations, Day 1 is successful.

Don't build UI polish yet.

### DAY 2: Screening + UI + deployment
#### Phase 5: Implement PE Screening

Load:

screening_framework.yaml


For a selected company:

for criterion in framework:
    retrieve evidence
    assess evidence
    store result


Return structured JSON:

{
  "criterion": "recurring_revenue",
  "assessment": "strong_evidence",
  "rationale": "...",
  "sources": [...]
}


This is the most important development task of Day 2.

#### Phase 6: Streamlit UI

Build just three tabs.

1. Company Screening
Company: [Nemetschek ▼]

[Generate PE Screening]

Business Quality
Growth
Profitability
Buy & Build
Risks

2. Ask
Company: [ATOSS ▼]

Question:
[...]

[Ask]

3. Compare
Company A: [...]
Company B: [...]

[Compare]


Portfolio-wide screening is optional if you're making good progress.

#### Phase 7: Golden Questions

Keep it small.

Create perhaps 10 manually verified questions covering different criteria and companies.

Example:

- id: GQ001
  company: atoss
  criterion: recurring_revenue
  question: >
    What evidence supports recurring revenue
    at ATOSS?


For the weekend project, evaluate just:

Retrieval

Did the correct source passage get retrieved?

Groundedness

Is the claim supported by the retrieved evidence?

Citation

Does the citation point to the supporting source?

Don't build an evaluation platform.

A YAML file plus a small script/output is enough.

#### Phase 8: Expand corpus

Only at this point expand from 5 toward 20 companies.

This ordering matters.

I'd rather have:

12 companies with excellent ingestion + screening

than:

20 companies where six PDFs failed extraction.

"~20 companies" is the dataset target, not the core technical success criterion.

#### Phase 9: Docker + AWS

Containerize the already-working application.

Docker
├── Streamlit
├── application
├── config
└── vector index


Then deploy that container to an appropriate AWS container runtime.

Treat AWS as the final packaging/deployment step, not as part of the RAG architecture.

If deployment starts consuming disproportionate effort, stop at a working Docker image. The intellectually interesting part of this project is the screening RAG, not cloud plumbing.


## The final story

The finished project should not be described as:

"A RAG chatbot for PE."

That's too generic.

We position it as:

PE Deal Screening RAG

An evidence-based research application for screening European B2B software companies against a consistent private-equity investment framework.

The system retrieves evidence from public company documents to assess business quality, growth, profitability, buy-and-build characteristics and investment risks, with source-level citations and explicit handling of insufficient evidence.
