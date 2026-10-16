# Security

- Report vulnerabilities privately to the repository maintainers (GitHub private vulnerability reporting).
- Authentication uses Google Sign-In ID tokens, verified server-side (audience = `GOOGLE_CLIENT_ID`). Only verified accounts in the Workspace domain `ALLOWED_DOMAIN` (`hd` claim and email suffix) are accepted. No long-lived secrets are stored.
- Only emails in `ADMIN_EMAILS` may call `/api/trigger_export`.
- Cloud Run allows unauthenticated invocation but only accepts traffic from the load balancer (`internal-and-cloud-load-balancing`); Cloud Armor enforces the IP allowlist, WAF and rate limit. Set `ALLOWED_IP` when running `deploy.sh`.
- Never commit `students.csv` (contains PII).
