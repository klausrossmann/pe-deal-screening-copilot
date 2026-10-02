The PE screening framework is the analytical layer between your RAG and the user. Instead of asking the LLM to vaguely “analyze this company,” you define in advance what an attractive PE/software business looks like and make the application retrieve evidence for those dimensions.

For your project, I’d use it in three places: retrieval, output structure, and evaluation.

1. Define the framework

Create something like config/screening_framework.yaml:

screening_dimensions:

  business_quality:
    description: Quality and defensibility of the business
    criteria:
      recurring_revenue:
        question: "What evidence is there of recurring or subscription revenue?"
      mission_criticality:
        question: "How important is the product to customers' core operations?"
      customer_retention:
        question: "What evidence is available regarding customer retention?"
      market_position:
        question: "What evidence describes the company's competitive position?"

  growth:
    description: Potential for continued revenue growth
    criteria:
      organic_growth:
        question: "What evidence is there of organic growth?"
      international_expansion:
        question: "What opportunities or evidence exist for geographic expansion?"
      cross_sell:
        question: "What evidence is there for cross-selling or upselling?"
      market_growth:
        question: "What does management say about underlying market growth?"

  profitability:
    description: Profitability and cash generation
    criteria:
      margin:
        question: "What evidence is available regarding margins and profitability?"
      cash_generation:
        question: "What evidence is available regarding cash generation?"
      operating_leverage:
        question: "What evidence suggests operating leverage?"

  buy_and_build:
    description: Suitability for an acquisition-led growth strategy
    criteria:
      acquisition_history:
        question: "What acquisitions has the company completed?"
      fragmented_market:
        question: "Is there evidence that the company's market is fragmented?"
      integration_capability:
        question: "What evidence shows the company can integrate acquisitions?"
      acquisition_strategy:
        question: "Does management explicitly describe M&A as part of its strategy?"

  risks:
    description: Material risks to the investment thesis
    criteria:
      customer_concentration:
        question: "Is there evidence of customer concentration?"
      geographic_concentration:
        question: "Is there evidence of geographic concentration?"
      competition:
        question: "What competitive risks are described?"
      technology_disruption:
        question: "What technology disruption risks are described?"
      regulation:
        question: "What regulatory risks are described?"


The questions are surprisingly important. They become reusable retrieval queries.

2. Use it when analyzing a company

Suppose the user selects Nemetschek and clicks:

Generate PE Screening

Instead of doing one giant RAG query:

Analyze Nemetschek as a PE investment.


I'd have your application execute several targeted retrievals.

Conceptually:

for dimension in screening_framework:
    for criterion in dimension:
        evidence = retrieve(
            query=criterion["question"],
            filters={"company_id": "nemetschek"},
            top_k=3
        )


You now have:

                        Nemetschek documents
                               │
                ┌──────────────┴──────────────┐
                │       Vector Search         │
                └──────────────┬──────────────┘
                               │
        ┌──────────────────────┼───────────────────────┐
        ▼                      ▼                       ▼
 recurring revenue       acquisition history          risks
        │                      │                       │
   evidence                  evidence                evidence
        └──────────────────────┼───────────────────────┘
                               ▼
                       PE Screening Report


This is much more robust than asking the LLM to create everything in one shot.

3. Have the LLM assess evidence, not invent a score

I would not ask:

"Score recurring revenue from 1–10."

Instead, use an evidence classification:

assessment:
  - strong_evidence
  - moderate_evidence
  - weak_evidence
  - insufficient_evidence


For example:

Recurring Revenue
-----------------

Assessment: Strong evidence

Rationale:
The company reports...

Evidence:
"...."

Sources:
- Annual Report 2025, p. 34
- Investor Presentation 2025, p. 12


Or:

Customer Concentration
----------------------

Assessment: Insufficient evidence

Rationale:
The retrieved sources do not provide sufficient
information to assess customer concentration.


That second outcome is good RAG behavior.

You're explicitly testing whether the system can say:

"I don't know based on the documents I have."

4. Turn it into your main application view

I'd make the company page look roughly like this:

--------------------------------------------------
Nemetschek
Germany | Construction Software
--------------------------------------------------

PE SCREENING

Business Quality
✓ Recurring Revenue         Strong evidence
✓ Mission Criticality       Strong evidence
○ Customer Retention        Moderate evidence
✓ Market Position           Strong evidence

Growth
✓ Organic Growth            Strong evidence
○ International Expansion   Moderate evidence
○ Cross-sell                Moderate evidence
✓ Market Growth             Strong evidence

Profitability
✓ Margins                   Strong evidence
✓ Cash Generation           Strong evidence
○ Operating Leverage        Moderate evidence

Buy & Build
✓ Acquisition History       Strong evidence
○ Fragmented Market         Moderate evidence
✓ Integration Capability    Strong evidence
✓ Acquisition Strategy      Strong evidence

Risks
! Competition               Evidence found
! Technology disruption     Evidence found
? Customer concentration    Insufficient evidence


Clicking Acquisition History then exposes the underlying citations/chunks.

That would be a very nice Streamlit demo.

5. It also enables portfolio screening

This is where the framework becomes especially useful.

Without the framework your application can answer:

"Tell me about ATOSS."

Fine, but fairly generic.

With the framework, the user can ask:

Find companies with strong evidence for buy-and-build characteristics.

Your backend can then run the same criteria against each company:

                 acquisition_history
                         +
                  fragmented_market
                         +
                acquisition_strategy
                         +
               integration_capability
                         │
                         ▼
                    20 companies
                         │
                         ▼
               evidence per company


And produce something like:

Companies with substantial supporting evidence

Company A
  Acquisition history: Strong
  Acquisition strategy: Strong
  Fragmented market: Moderate

  Evidence:
  ...
  Sources:
  ...

Company B
  Acquisition history: Strong
  Acquisition strategy: Moderate
  Fragmented market: Insufficient

  Evidence:
  ...
  Sources:
  ...


Crucially, I'd avoid presenting this as “Company A is better than Company B.” The application is finding and organizing evidence against explicit screening criteria, not making an investment decision.

6. Store the results separately from the documents

I'd distinguish three things:

companies.yaml
      │
      │ Who are we analyzing?
      ▼
Company master data


screening_framework.yaml
      │
      │ What are we looking for?
      ▼
PE investment criteria


Vector DB
      │
      │ What evidence do we have?
      ▼
Document chunks + metadata


Then optionally cache generated analyses:

data/
├── raw/
│   └── ...
│
├── processed/
│   └── ...
│
└── analysis/
    ├── nemetschek.json
    ├── atoss.json
    └── ...


For example:

{
  "company_id": "nemetschek",
  "criteria": {
    "recurring_revenue": {
      "assessment": "strong_evidence",
      "rationale": "...",
      "evidence": [
        {
          "chunk_id": "nem_ar2025_00142",
          "source": "annual_report_2025.pdf",
          "page": 34
        }
      ]
    }
  }
}


That separation is architecturally clean.

7. It also gives you your Golden Questions almost for free

This is probably the part you'd appreciate most given your evaluation background.

Your screening framework effectively becomes an evaluation taxonomy.

For example:

golden_questions:

  - company: nemetschek
    dimension: business_quality
    criterion: recurring_revenue
    question: >
      What evidence indicates that Nemetschek
      generates recurring revenue?

  - company: atoss
    dimension: growth
    criterion: international_expansion
    question: >
      What evidence exists for ATOSS's
      international expansion?

  - company: nexus
    dimension: buy_and_build
    criterion: acquisition_history
    question: >
      What evidence of historical acquisitions
      exists for Nexus?


Then evaluate:

Question
   ↓
Retrieval
   ├─ Did we retrieve the right evidence?
   │
   ↓
Answer
   ├─ Is every claim grounded?
   │
   ↓
Citation
   └─ Does the cited passage support the claim?


You now have an end-to-end RAG evaluation story.

8. I would make this the central concept of the project

I'd actually slightly rename your project:

PE Deal Screening RAG
 Evidence-based screening of European B2B software companies using public company information.

Its architecture becomes:

Public company documents
          ↓
     Ingestion
          ↓
 Chunk + metadata
          ↓
      Vector DB
          ↓
 ┌────────┴─────────┐
 │ Screening        │
 │ Framework        │
 │                  │
 │ Business Quality │
 │ Growth           │
 │ Profitability    │
 │ Buy & Build      │
 │ Risks            │
 └────────┬─────────┘
          ↓
 Targeted Retrieval
          ↓
   LLM Synthesis
          ↓
 Evidence + Citations
          ↓
  Streamlit UI


That's the twist: the framework isn't extra data you manually research. It's a lens through which your RAG interrogates the documents.

For the weekend MVP, I'd implement just 12–15 criteria across those five dimensions, with strong / moderate / weak / insufficient evidence. That gives you enough structure to make the application feel PE-specific without turning the project into an investment-analysis platform.