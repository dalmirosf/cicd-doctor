# Webhook payload fixtures

Synthetic payloads shaped like GitHub's documented webhook bodies for `workflow_run`, `ping`,
`installation`, `installation_repositories` and `issue_comment`. They keep the fields CICD Doctor
reads plus some realistic context, and every name, id and e-mail address is invented
(`acme/widgets`, `example.com`). They are not recordings of real deliveries, so they contain no
real user data (D-56).

Tests derive the variants they need (another conclusion or action, a missing field) from these
files instead of keeping one file per variant.
