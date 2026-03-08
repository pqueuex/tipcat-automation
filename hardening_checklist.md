Do First (today)

Rotate all API keys/tokens now (Gemini, Shopify, Printify), since they were exposed in prior terminal/chat context.
Stop making GCS objects public in product_automation_script.py:212-221; switch to signed URLs with short expiry for external fetches.
Lock bucket access: enforce Uniform bucket-level access, remove allUsers/allAuthenticatedUsers grants, and verify with gcloud storage buckets get-iam-policy.
Do Next (this week)

Use least-privilege IAM for the Cloud Run service account: only storage.objectViewer/objectCreator, Secret Manager accessor, and exact APIs needed.
Add network/data controls: disable unauthenticated invocations (for services), use CMEK if required, and enable Data Access audit logs.
Add dependency and secret scanning in repo (pip-audit, gitleaks) and block pushes on findings.
Code/Process Hardening

Remove any default production identifiers from code (store/shop IDs) and keep all runtime config in Secret Manager + env vars.
Add output validation + fail-closed behavior before pushing to Shopify (prevents bad/malicious metadata from publishing).
If you want, I can implement the biggest code hardening now: replace public GCS URLs with signed URLs and push the patch.