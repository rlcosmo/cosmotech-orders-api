# cosmotech-orders-api

DEV pipeline: source push → tests/lint → Docker build → Trivy scan → immutable SHA image in ECR (AWS OIDC) → GitHub App proposes the GitOps image → automatic DEV merge → Argo CD sync → EKS rollout.

The owner explicitly authorized this first Orders API DEV pilot without human/GMUD approvals. `DEV_AUTO_PROMOTE=true` enables automatic merge only after successful CI and the exact image-only policy in cosmotech-gitops. The App token is limited to that repository. Actions never modifies the EKS Deployment directly. Authentication, scan and immutable image pinning remain enabled; no human approval is simulated.

Repository Secrets: GITOPS_APP_PRIVATE_KEY and GITOPS_APP_ID (the ID may alternatively be a repository Variable). Source changes build images; workflow-only corrections can dispatch `approved_run_id` to reuse a successful CI artifact. The promotion validates source run/branch, repository scope, SHA/digest and the exact Deployment image-only diff before a SHA-matched merge. An already merged promotion is a no-op.

Smoke test after rollout: port-forward `service/orders-api` service port 8000 to localhost 18080, then run `python scripts/smoke_dev_release.py`. It checks health/version, a random nonexistent order and an invalid POST; it never submits a valid order. Full business/SQS processing is outside this deployment smoke test.

To stop automatic promotion, set DEV_AUTO_PROMOTE=false. Also disable the versioned GitOps DEV policy and Application automated sync, then apply the Application configuration on the dedicated management K3s. Reinstating mandatory approvals is a separate governance change; this pilot does not claim that private GitHub protections became available.

Original commit ancestry is preserved. The currently shared Orders library remains here until Worker extraction is explicitly undertaken.

## Configuração e entrega DEV

`deploy/dev` neste repositório é a origem oficial de Deployment, ConfigMap, Service, HPA e host. Um push em `main` executa testes, valida essas configurações, constrói e verifica a imagem, publica no ECR e promove automaticamente o conjunto completo para o espelho gerado de entrega GitOps. Argo CD reconcilia o cluster e Kestra mostra as etapas reais por cluster. Não há aprovação humana nesta fase. Não edite o espelho gerado para configurar esta aplicação.

## Inicialização e disponibilidade no host

A startup probe controla a inicialização antes de habilitar readiness/liveness. No worker, ela só passa após a primeira consulta SQS bem-sucedida; a primeira verificação ocorre após a espera normal de long polling (20 segundos). Falhas reais de acesso à fila continuam retornando 503. O rollout mantém a réplica antiga até a nova ficar pronta, usando maxUnavailable=0, maxSurge=1 e a readiness gate do ALB habilitada no namespace pela configuração de infraestrutura `ops/kubernetes/orders-ingress-readiness.json`.

## Logs e conexões sintéticas

A configuração em `deploy/dev/configmap.json` habilita um ciclo a cada 300 segundos por pod. Cada ciclo registra heartbeat JSON, chama a rota de diagnóstico do serviço par e faz GET HTTPS em `example.com`. O endpoint GET `/diagnostics/ping` recebe chamadas internas/externas e registra um span de servidor e log correlacionado por trace ID. Os clientes propagam W3C traceparent. Não são criados pedidos ou alterados dados de negócio. Alvos são fixos, chamadas têm timeout de cinco segundos e não seguem redirects; logs incluem destino, direção, status e duração, sem corpo ou credenciais.

O APM exporta métricas OTLP reais de volume e duração por operação, com atributos limitados a operação, tipo e estado. Exportação assíncrona a cada 60 segundos; chamadas sintéticas continuam a cada 5 minutos. Logs estruturados seguem pelo Alloy para Loki e Zabbix.

## Capacidade DEV

A release 2.0.4-lab define duas réplicas estáveis pelo HPA (`minReplicas=2`, `maxReplicas=2`). O HPA mantém a propriedade da escala; não há `spec.replicas` concorrente no manifesto. A mudança passa por CI, GMUD e aprovação conforme a política de criticidade e alterações sensíveis. No Worker, o Canary continua limitado a1→2 consumidores temporários, com promoção, drenagem e restauração da capacidade aprovada.
