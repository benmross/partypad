# Signing and release plan

PartyPad's tag workflow produces draft **unsigned alpha** archives, a Windows
installer, SHA-256 checksums, and CycloneDX runtime SBOMs on native runners.
Those artifacts are suitable for technical pilot testing only.

Before a broad nontechnical launch:

- Windows artifacts and the installer must be Authenticode-signed with a
  protected organization/code-signing certificate. CI should import the
  certificate only into an ephemeral runner store, sign both executable and
  installer with SHA-256 plus a trusted timestamp, verify with `signtool`, and
  remove the key material before artifact upload.
- macOS Intel and Apple Silicon artifacts must use a Developer ID Application
  identity, hardened runtime, and appropriate entitlements. CI must verify with
  `codesign`, submit the final archive/app to Apple's notary service, wait for a
  successful result, staple the ticket where the artifact format permits, and
  validate with `spctl`.
- Protected signing credentials must be environment-scoped GitHub secrets with
  required reviewer approval. They must never be placed in repository files,
  caches, build logs, agent prompts, or downloadable unsigned artifacts.
- Release publication should remain a reviewed draft until native smoke tests,
  SBOM/checksum generation, signature verification, malware scanning, and the
  clean-laptop first-run checklist pass.

The initial PyInstaller one-file CLI is an evidence-gathering packaging choice,
not a permanent architecture decision. Retain it only if aiortc, PyAV,
cryptography, keyring, qrcode, and platform backends work reliably on the native
matrix. Windows installer technology is currently Inno Setup; macOS remains an
unsigned archive until an app bundle/signing implementation is validated.
