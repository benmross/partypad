# Security policy

PartyPad is an early-stage local and hosted controller application and does not
currently publish versioned security-support guarantees.

Please report vulnerabilities privately through GitHub's **Report a
vulnerability** feature instead of opening a public issue. Include reproduction
steps and the affected platform. Reports involving the privileged AP helper,
command construction, certificate/private-key handling, WebSocket input, or
network exposure are particularly important.

Local mode is intended for trusted networks. Its self-signed TLS certificate
protects transport after manual browser acceptance, but local controllers are
not authenticated beyond network access. Hosted sessions use independent host
and controller bearer secrets; treat a QR/link as an invitation to control the
game until the session ends. Desktop authorization uses per-device revocable
credentials and must never be replaced with a shared embedded host token.
