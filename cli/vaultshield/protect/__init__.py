"""VaultShield Protect: keep secret VALUES out of project files.

Real values live in the macOS Keychain, project files hold placeholders, and
apps get the real values only through `vaultshield exec`. See README.md in this
directory for usage and the honest limits.
"""

# The value every protected variable holds on disk.
PLACEHOLDER = "vs_protected"
