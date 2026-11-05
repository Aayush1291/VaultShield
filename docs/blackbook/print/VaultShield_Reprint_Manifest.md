# VaultShield report: 10-page replacement manifest

- **Printed report (baseline):** `Vaultshield_Report.pdf`, 63 PDF pages.
- **Final report:** `VaultShield_Final_Report.pdf`, 63 PDF pages, the same pagination.
- **Packet:** `VaultShield_Minimal_Reprint_Packet_10pages.pdf`, exactly 10 pages = 5 sheets.
- **Print settings:** double-sided, flip on long edge, 100% scale.

"Printed page" is the footer number. "PDF page" is the 1-based index in the 63-page PDF.

## Sheets

| Sheet | Front side | Back side | PDF pages | Change |
|---|---|---|---|---|
| 1 | Original p.7 | Original p.8 | 19 / 20 | Research Gap (approved revision, vendor sources in a footnote) / unchanged partner |
| 2 | Original p.11 | Original p.12 | 23 / 24 | Figure 3.2 with the proposed context-preparation steps / unchanged partner |
| 3 | Original p.13 | Original p.14 | 25 / 26 | New 3.3.7 Proposed Agentic AI Orchestration / Figure 3.5 with the proposed context orchestrator |
| 4 | Original p.37 | Original p.38 | 49 / 50 | Implementation status (Sem VII vs Sem VIII), approval-gate and Antigravity sandbox captures |
| 5 | Original p.41 | Original p.42 | 53 / 54 | Sample Test Cases (Table 6.4) / both failed tests with screenshots, reliability impact, conclusion |

## Assembly

1. Remove these five sheets from the printed report (printed page numbers):
   - 7/8
   - 11/12
   - 13/14
   - 37/38
   - 41/42
2. Put packet sheets 1–5 into the same five positions.
3. Keep every other sheet, including the cover, declaration, abstract, Chapter 1, the Contents, the List of Figures and the List of Tables.

## Verification

- The packet has 10 pages, with footers 7, 8, 11, 12, 13, 14, 37, 38, 41 and 42 in that order. Every sheet starts on an odd PDF page, so the duplex pairs match the printed report.
- Printed pages 8 and 12 are reprinted only as duplex partners. They are pixel-identical to the printed originals.
- Final PDF pages outside the packet were compared with the printed PDF. All are pixel-identical except PDF page 10 (List of Tables, page ix); see below. Chapter 7 is unchanged.
- There is no 3.4.5 or 3.4.6. Subsection 3.3.7 is unnumbered in the Contents (`\subsection*`), so the printed Contents stays correct.
- The captions of Figures 3.2 and 3.5 are unchanged, so the printed List of Figures stays correct.

## Known inconsistencies in the kept printed pages

- **List of Tables (page ix, not reprinted).** The printed page still lists "6.3 Issues found and their resolution ... 41" and "6.4 Sample test cases ... 42". Table 6.3 no longer exists, and Table 6.4 is now on page 41. Table 2.2 (page 7) is kept out of the List of Tables on purpose. The final PDF's page ix shows the corrected list. Fixing the printed copy would need sheet ix/x (one more sheet).
- **New figures are unnumbered.** The new terminal captures on pages 38 and 42 carry descriptive captions but no figure numbers. Numbered figures would renumber Figure 5.16 on the kept page 39 and would be missing from the printed List of Figures.
