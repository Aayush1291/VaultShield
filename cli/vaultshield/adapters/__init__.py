"""Agent_Adapter implementations.

Holds the common `Agent_Adapter` interface (`base.py`) and the concrete
adapters for each supported AI coding agent: `Mock_Adapter` (`mock.py`),
`Codex_Adapter` (`codex.py`), `Kiro_Adapter` (`kiro.py`), and
`Antigravity_Adapter` (`antigravity.py`). Each concrete adapter lives in
its own module and is imported directly (e.g.
`from vaultshield.adapters.base import Agent_Adapter`); this package
itself re-exports nothing, matching the rest of the `vaultshield` package.
"""
