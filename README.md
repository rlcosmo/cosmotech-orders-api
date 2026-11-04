# cosmotech-orders-api

Orders API and its currently shared Orders library, including repository/processing contracts needed by the API. Worker extraction is deferred. CI tests, builds, scans and publishes SHA tags to the existing immutable ECR repository using OIDC. Cross-repository GitOps PR automation requires a repository-limited GitHub App: variable GITOPS_APP_ID and secret GITOPS_APP_PRIVATE_KEY. Until installed, the published digest is a proposed-gitops-image artifact. CI never writes a Deployment.

Extracted from [aws-serverless-lab](https://github.com/rlcosmo/aws-serverless-lab) at `6f3e67f9e93d626393b23b6884b031767a63fea0` with original commit ancestry preserved. Original files remain available there as migration snapshots. No infrastructure was recreated by extraction.
