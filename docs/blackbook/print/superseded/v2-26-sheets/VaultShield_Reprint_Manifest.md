# VaultShield report: reprint manifest

- **Printed report:** `Vaultshield_Report.pdf`, 63 PDF pages on 32 double-sided sheets. The last sheet has a blank back.
- **Final report:** `VaultShield_Final_Report.pdf`, 75 PDF pages on 38 sheets.
- **Packet:** `VaultShield_Minimal_Reprint_Packet.pdf`, 52 PDF pages = **26 sheets** (51 report pages plus 1 blank back).
- **Print settings:** double-sided, flip on long edge, 100% scale.

"Original page" and "final page" are 1-based PDF page indices, counting the cover as 1. Printed footer numbers are given in brackets.

## Summary

| | Sheets |
|---|---|
| Original sheets kept untouched | 12 |
| Original sheets removed | 20 |
| Packet sheets inserted | 26 |
| Final report | 38 |

Of the 51 report pages in the packet, two are unchanged pages reprinted only because their duplex partner changed:
- final page 20 (Chapter 3 opening, footer 8)
- final page 31 (sequence diagram, Figure 3.9, footer 19)

Both were checked to be identical to the printed originals.

From final page 32 to the end, every page is reprinted. The new Chapter 3 material adds 5 pages after the sequence diagram, so every later printed page number moves up by 5 or more. Continuous integer numbering (no 19a, 19b) makes this unavoidable.

## Page-by-page table

| Original PDF page [footer] | Final PDF page [footer] | Required action | Reason |
|---|---|---|---|
| 1–2 [cover, approval] | 1–2 | Retain | Unchanged (pixel-identical, including the cover logo) |
| 3–4 [certificate, acknowledgement] | 3–4 | Retain | Unchanged |
| 5–6 [declaration, abstract] | 5–6 | Retain | Unchanged, as instructed |
| 7–8 [vi–vii] | 7–8 [vi–vii] | Replace | Contents: new subsections and new page numbers |
| 9–10 [viii–ix] | 9–10 [viii–ix] | Replace | List of Figures and List of Tables: new entries and page numbers |
| 11–12 [x–xi] | 11–12 | Retain | Abbreviations unchanged |
| 13–18 [1–6] | 13–18 | Retain | Chapter 1 and Sections 2.0–2.1 unchanged |
| 19 [7] | 19 [7] | Replace | Strengthened 2.2 Research Gap, with new Table 2.2 inside the section |
| 20 [8] | 20 [8] | Reprint (unchanged duplex partner) | Back of page 19 |
| 21–30 [9–18] | 21–30 | Retain | Chapter 3, Sections 3.1–3.4.3, unchanged |
| 31 [19] | 31 [19] | Reprint (unchanged duplex partner) | Sequence diagram; unchanged, but page 32 changes |
| — | 32–36 [20–24] | Insert (new) | 3.4.5 Proposed Agentic AI Orchestrator Workflow (Fig 3.10, Table 3.2); 3.4.6 Threat Model and Attack Paths (Fig 3.11, Table 3.3) |
| 32 [20] | 37 [25] | Replace | Chapter 4 opening: new Semester VIII paragraph in the former blank space |
| 33 [21] | 38 [26] | Replace | Table 4.1: Orchestrator row added, Semester VIII rows aligned with the four enhancements |
| 34–37 [22–25] | 39–42 [27–30] | Replace (renumbered only) | Content unchanged; footer number +5 |
| 38–49 [26–37] | 43–54 [31–42] | Replace (renumbered only) | Chapter 5 content unchanged; footer number +5 |
| 50 [38] | 55 [43] | Replace | 5.3.6 Implementation Summary: Tables 5.1 (Semester VII) and 5.2 (Semester VIII); 5.4 follows |
| 51 [39] | 56 [44] | Replace (renumbered only) | Gantt chart unchanged |
| 52–54 [40–42] | 57–61 [45–49] | Replace | Chapter 6: references fixed, new rows in Table 6.3, new 6.3.1 test environment, new 6.3.2 failed-test analysis, updated 6.5 conclusion |
| 55–59 [43–47] | 62–71 [50–59] | Replace | Chapter 7: mock-agent scope box, 7.1.1 benchmark configuration (40 + 20 runs), qualified overhead claims, 7.7.1 live-run evidence, 7.8.1 comparison with existing approaches |
| 60 [48] | 72 [60] | Replace | Chapter 8: Objective 2 planned, design in Section 3.4.5 |
| 61 [49] | 73 [61] | Replace | Chapter 9: four Semester VIII enhancements separated from long-term work |
| 62–63 [50–51] | 74–75 [62–63] | Replace | Bibliography: references 16–21 added |
| — | (packet page 52) | Blank back | Back of the last sheet |

## Assembly order

Take sheets out of the printed binder and rebuild it in this order. "Packet p" means pages of the packet PDF.

| Final sheet | Source | Front / back (final PDF pages) |
|---|---|---|
| 1 | Keep original sheet 1 | 1 / 2 |
| 2 | Keep original sheet 2 | 3 / 4 |
| 3 | Keep original sheet 3 | 5 / 6 |
| 4 | **Packet sheet 1** (packet p1–2) | 7 / 8 |
| 5 | **Packet sheet 2** (packet p3–4) | 9 / 10 |
| 6 | Keep original sheet 6 | 11 / 12 |
| 7–9 | Keep original sheets 7–9 | 13 / 14, 15 / 16, 17 / 18 |
| 10 | **Packet sheet 3** (packet p5–6) | 19 / 20 |
| 11–15 | Keep original sheets 11–15 | 21 / 22 … 29 / 30 |
| 16–38 | **Packet sheets 4–26** (packet p7–52) | 31 / 32 … 73 / 74, 75 / blank |

Original sheets to discard:
- 4 (pages 7/8)
- 5 (pages 9/10)
- 10 (pages 19/20)
- 16–32 (pages 31/32 to 63)

That is 20 sheets.

## Notes

- **Kept pages were verified against the printed PDF.** Final PDF pages 1–6, 11–18 and 20–31 have identical extracted text and identical renders to the printed PDF.
- **Pagination is continuous.**
  - Roman i–xi in the front matter.
  - Arabic 1–63 in the body.
  - No alphabetic suffixes and no duplicate numbers.
- **Front matter still fits its original pages.** The Contents and lists still occupy exactly pages 7–10, so the front matter keeps its original 12 pages and no later duplex pairing shifts.
