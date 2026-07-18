# Environment constraints (measured, not assumed)

## Outbound port 25 is BLOCKED on this machine
Tested TCP connect to three MX servers on port 25 — all timed out:
- gmail-smtp-in.l.google.com:25  → timeout (17s)
- mxa-00176a02.gslb.pphosted.com:25 (Proofpoint/GE) → timeout (11s)
- aciworldwide-com.mail.protection.outlook.com:25 (Microsoft) → timeout (67s)

### Design implications (MUST be honored by architecture & implementation)
1. Live SMTP RCPT verification cannot run here, and almost certainly not on the
   user's home laptop either (residential ISPs block port 25 by default).
2. DEFAULT strategy = candidate generation + audit-KB prior + MX/provider
   reasoning + pattern confidence. SMTP verification is an OPTIONAL, off-by-default
   mode.
3. The SMTP verifier MUST detect a port-25 block quickly (short connect timeout)
   and return status `unknown` / `verification_unavailable` instead of hanging or
   crashing. Never block the whole run on a dead port.
4. Recommended high-confidence path when port 25 is blocked: optional HTTPS-based
   verifier API (ZeroBounce/Reoon/etc.) behind a pluggable interface.
5. End-to-end testing in THIS environment must exercise: DNS/MX lookup (works),
   provider classification, candidate generation, KB scoring, and graceful SMTP
   degradation — not a successful live RCPT probe.
