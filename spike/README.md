# VaultShield spike — can Antigravity read .env *values*?

Throwaway experiment (no product code) measuring which protections stop Google
Antigravity (CLI `agy`, and the App/IDE GUI) from revealing the **value** of a
secret in `.env`. Product goal being probed: agents may see variable **names**
but never **values**.

```
spike/
  run_cli_spike.sh     Part 2 — automated CLI test (C0–C3). Already run; see results/ + RESULT.md.
  project/             CLI test workspace (fake .env). Rewritten per config by the harness.
  results/             per-config agent response, raw NDJSON stream, stderr, grades.
  ide_test/            Part 3 — manual GUI kit (Kiro can't drive the GUI):
    apply.sh           applies I0/I1/I2/I3/cross to the real config, with backup+restore.
    CHECKLIST.md       ~10-min runbook: prompts to paste, where to paste answers.
    score.sh           grades answers/*.txt (LEAKED/SAFE + tool heuristic).
    project/           GUI test workspace (fake .env).
    answers/           you paste the GUI agent's replies here.
  RESULT.md            findings, tables, recommendation, what's automated vs pending.
```

All secret values here are fake. Nothing is committed. Config files touched on
the machine are backed up and restored by the scripts; `.backups/` is gitignored
because it holds a verbatim copy of your real `~/.gemini/config/config.json`.

Start with `RESULT.md`.
