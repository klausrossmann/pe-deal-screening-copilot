## 1. The Investment Question

We frame the application as:

An AI research assistant that helps a PE associate screen European software companies and identify attractive characteristics, risks, and potential buy-and-build opportunities from public information.

It should answer four classes of questions:

### Understand a Company

- What does it sell?
- Who are its customers?
- How does it make money?

### Identify Investment Characteristics

- Evidence of recurring revenue?
- Growth?
- Profitability?
- Vertical specialization?
- Mission-critical workflows?

### Identify Risks

- Customer concentration?
- Cyclicality?
- Competitive threats?
- AI disruption?
- Geographic concentration?

### Compare/Screen

- "Compare Nemetschek and TeamViewer."
- "Which companies show evidence of active M&A?"
- "Which companies have the strongest recurring-revenue characteristics?"
- "Find companies that could plausibly support a buy-and-build thesis."

The last category is what makes the RAG interesting.

## 2. Which 20 Companies?

Create a heterogeneous European B2B software universe, rather than calling all 20 "Vertical SaaS."

### DACH Core

| Company | Area | Why include it |
| --- | --- | --- |
| Nemetschek | Construction/design software | Excellent vertical software case |
| TeamViewer | Remote connectivity | Subscription software comparison |
| SUSE | Enterprise Linux | Enterprise infrastructure |
| SoftwareOne | Software/cloud services | Different business-model economics |
| Qt Group | Developer/embedded software | Specialized B2B software |
| ATOSS | Workforce management | Vertical/business workflow SaaS |
| Nexus AG | Healthcare software | Healthcare vertical |
| IVU Traffic Technologies | Public transport software | Very strong vertical niche |

### Nordic / Benelux

| Company | Area | Why include it |
| --- | --- | --- |
| Adyen | Payments technology | Embedded payments comparison |
| Wolters Kluwer | Professional information/software | Mission-critical workflows |
| Visma | Business software | PE-backed/private comparator |
| Basware | Procure-to-pay | PE-backed software case |
| Fortnox | SME accounting | Vertical-ish SME ecosystem |
| Vitec Software | Vertical market software | Fantastic buy-and-build example |

### UK / Rest of Europe

| Company | Area | Why include it |
| --- | --- | --- |
| Sage | Accounting/payroll | Mature recurring software |
| Darktrace | Cybersecurity | Acquired/private comparator |
| Kahoot! | Learning software | PE/private comparator |
| Dassault Systèmes | Engineering software | Large mature vertical software |
| Lectra | Fashion/manufacturing software | Niche vertical |
| Esker | Business automation | Automation/workflow software |

## 3. What Data Should We Collect?

No giant structured financial dataset. Raw data should primarily be documents.

For each company, we target three sources.

### Source A: Annual Report

This is the most valuable document.

It potentially gives:

- business description
- segments
- geographic footprint
- strategy
- revenue
- profitability
- KPIs
- employees
- risks
- acquisitions
- management commentary

### Source B: Investor Presentation / Results Presentation

Usually much denser than the annual report.

Useful for:

- strategic priorities
- market positioning
- SaaS KPIs
- customer metrics
- growth
- M&A strategy
- management's narrative

### Source C: Company Website

Limited to a few pages:

- About
- Products
- Industries
- Customers

This gives:

20 companies x ~3 sources = ~60 documents.

Perfectly sufficient for the MVP.

## 4. Optional Fourth Source: Acquisitions

This one gives us a nice PE angle.

For companies where M&A is important, we collect acquisition press releases.

For example, we want documents that let the application answer:

"Which companies demonstrate an established buy-and-build strategy?"

and retrieve evidence from actual acquisition announcements.

This makes a particularly strong demo because cross-company retrieval becomes useful rather than artificial.

## 5. Structured Company Metadata

Create one `companies.yaml` manually:

```yaml
companies:
  - id: nemetschek
    name: Nemetschek
    country: Germany
    category: construction_software
    ownership: public
    ticker: NEM
    website: ...

  - id: vitec
    name: Vitec Software Group
    country: Sweden
    category: vertical_software
    ownership: public
    ticker: VIT-B
    website: ...
```

We keep the company-level schema tiny:

- `id`
- `name`
- `country`
- `category`
- `ownership`
- `ticker`
- `website`

We don't manually encode financial metrics here, we want the RAG system to discover those from source documents.

## 6. Document Metadata

Every ingested document should get:

```json
{
  "company_id": "nemetschek",
  "company": "Nemetschek",
  "document_type": "annual_report",
  "publication_date": "YYYY-MM-DD",
  "source_url": "...",
  "year": 2025
}
```

Every chunk inherits this.

We additionally preserve:

- `page_number`
- `document_title`

That enables citations like:

> Nemetschek describes X as a strategic growth area.  
> Source: Annual Report, p. 73

rather than:

> Source 4

## 7. File Organization

Make provenance obvious directly from the filesystem:

```text
data/
└── raw/
    ├── nemetschek/
    │   ├── annual_report_2025.pdf
    │   ├── investor_presentation_2025.pdf
    │   └── company_overview.md
    │
    ├── teamviewer/
    │   ├── annual_report_2025.pdf
    │   ├── investor_presentation_2025.pdf
    │   └── company_overview.md
    │
    └── ...
```

Plus:

```text
data/
├── raw/
├── processed/
└── companies.yaml
```

## 8. What Not to Collect

We explicitly skip:

### Stock-price Histories

Not useful for demonstrating RAG.

### A Giant Excel Financial Model

Turns the exercise into data engineering.

### Hundreds of News Articles

Too noisy.

### LinkedIn / Glassdoor

Questionable signal and cumbersome provenance.

### Massive Web Crawls

Creates cleaning problems.

### 10 Years of Annual Reports

One recent annual report is enough initially.

### PitchBook/Crunchbase Scraping

Unnecessary dependency and licensing complexity for this experiment.

## 9. The PE Screening Framework

Before collecting documents, the PE characteristics the application looks for are defined:

```yaml
screening_dimensions:
  business_quality:
    - recurring_revenue
    - mission_criticality
    - customer_retention
    - market_position

  growth:
    - organic_growth
    - international_expansion
    - cross_sell
    - market_growth

  profitability:
    - gross_margin
    - ebitda_margin
    - cash_generation

  buy_and_build:
    - fragmented_market
    - acquisition_history
    - integration_capability

  risks:
    - customer_concentration
    - geographic_concentration
    - competition
    - ai_disruption
    - regulation
```

The model should not invent values for these.

Instead:

```text
Dimension: Recurring Revenue

Assessment:
Strong evidence

Evidence:
"..."

Sources:
Annual Report ...
Investor Presentation ...
```

If evidence isn't available:

```text
Evidence:
Insufficient information in available sources.
```

From a RAG-quality perspective, that last behavior is particularly important.

## 10. Example Interactions

Example interactions once the corpus and framework are in place:

### Question 1

Which companies in the portfolio have the strongest evidence for recurring revenue models?

The RAG searches across companies and returns evidence.

### Question 2

Find companies with evidence of an active acquisition strategy.

This tests cross-company retrieval.

### Question 3

Compare Vitec and Nemetschek as potential buy-and-build platforms.

This tests multi-entity RAG.

### Question 4

What could invalidate an investment thesis for ATOSS?

This tests risk retrieval.

### Question 5

Give me a one-page investment committee briefing on Nexus.

This tests synthesis.

These five interactions exercise cross-company retrieval, multi-entity comparison, risk retrieval, and synthesis — more than a generic document chatbot provides.