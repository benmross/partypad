# Security policy

PartyPad is an early-stage local-network application and does not currently
publish versioned security-support guarantees.

Please report vulnerabilities privately through GitHub's **Report a
vulnerability** feature instead of opening a public issue. Include reproduction
steps and the affected platform. Reports involving the privileged AP helper,
command construction, certificate/private-key handling, WebSocket input, or
network exposure are particularly important.

PartyPad is intended for trusted local networks. Its self-signed TLS certificate
protects transport after manual browser acceptance, but the controller service
does not authenticate players beyond network access.
