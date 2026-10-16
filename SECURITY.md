# Security

- Report vulnerabilities privately to the repository maintainers (GitHub private vulnerability reporting).
- Authentication uses Google Sign-In ID tokens, verified server-side (audience = `GOOGLE_CLIENT_ID`). Only verified accounts in the Workspace domain `ALLOWED_DOMAIN` (`hd` claim and email suffix) are accepted. No long-lived secrets are stored.
- There are no roles: every authenticated domain user has the same access. Check-ins record the user's email (`checked_in_by`, and column E of the Live Log) for audit.
- Cloud Run allows unauthenticated invocation but only accepts traffic from the load balancer (`internal-and-cloud-load-balancing`); Cloud Armor enforces the IP allowlist, WAF and rate limit. Set `ALLOWED_IP` when running `deploy.sh`.
- Never commit `students.csv` (contains PII).
