# CosmoTech — Orders API

API de pedidos do laboratório, integrada à AWS e entregue no Amazon EKS por GitOps.

## Fluxo de entrega contínua (DEV)

Um push de código em `main` inicia testes e lint, build Docker, análise Trivy e publicação da imagem imutável (SHA) no Amazon ECR por credenciais temporárias OIDC. O GitHub App propõe a atualização da imagem no repositório GitOps; após validar o CI, SHA/digest e a alteração exclusiva de imagem, a proposta pode ser mesclada automaticamente. O Argo CD sincroniza e atualiza o EKS. O GitHub Actions **não modifica diretamente** os Deployments.

A entrega automática do piloto DEV foi autorizada sem aprovação humana/GMUD. `DEV_AUTO_PROMOTE=true` habilita a promoção apenas quando todas as verificações são aprovadas; isso não simula aprovação humana. Autenticação, análise de segurança e imagens imutáveis continuam obrigatórias.

## Configurações necessárias

- Secret `GITOPS_APP_PRIVATE_KEY`: chave privada do GitHub App.
- `GITOPS_APP_ID`: ID do App, como Secret ou Variable do repositório.
- `DEV_AUTO_PROMOTE`: habilita ou desabilita a promoção automática.

O token do App fica restrito ao repositório GitOps. Alterações de código geram imagens; correções apenas no workflow podem usar `approved_run_id` para reaproveitar um artefato de CI já aprovado tecnicamente. A promoção confere execução, branch, repositório, SHA/digest e diff exato. Propostas já mescladas não geram nova mudança.

## Como validar uma publicação

Após o rollout, encaminhe a porta 8000 do serviço `orders-api` para a porta local 18080 com `kubectl port-forward`, no namespace apropriado, e execute:

```bash
python scripts/smoke_dev_release.py
```

O teste verifica saúde/versão, consulta um pedido inexistente e envia um POST inválido. **Não cria pedidos reais**; o processamento completo com SQS deve ser validado separadamente.

## Como pausar ou reverter

Para interromper a promoção automática, configure `DEV_AUTO_PROMOTE=false`, desabilite também `auto_promote` na política DEV do GitOps e `syncPolicy.automated.enabled=false` na Application, aplicando a configuração no K3s de gerenciamento. Para rollback, utilize a imagem imutável anterior validada via GitOps, sem editar diretamente o Deployment EKS. Aprovações obrigatórias futuras exigem mudança separada de governança.

O histórico original foi preservado; a biblioteca compartilhada Orders permanece neste repositório até a separação formal do Worker.

## Configuração e entrega DEV

`deploy/dev` neste repositório é a origem oficial de Deployment, ConfigMap, Service, HPA e host. Um push em `main` executa testes, valida essas configurações, constrói e verifica a imagem, publica no ECR e promove automaticamente o conjunto completo para o espelho gerado de entrega GitOps. Argo CD reconcilia o cluster e Kestra mostra as etapas reais por cluster. Não há aprovação humana nesta fase. Não edite o espelho gerado para configurar esta aplicação.

## Inicialização e disponibilidade no host

A startup probe controla a inicialização antes de habilitar readiness/liveness. No worker, ela só passa após a primeira consulta SQS bem-sucedida; a primeira verificação ocorre após a espera normal de long polling (20 segundos). Falhas reais de acesso à fila continuam retornando 503. O rollout mantém a réplica antiga até a nova ficar pronta, usando maxUnavailable=0, maxSurge=1 e a readiness gate do ALB habilitada no namespace pela configuração de infraestrutura `ops/kubernetes/orders-ingress-readiness.json`.

## Logs e conexões sintéticas

A configuração em `deploy/dev/configmap.json` habilita um ciclo a cada 300 segundos por pod. Cada ciclo registra heartbeat JSON, chama a rota de diagnóstico do serviço par e faz GET HTTPS em `example.com`. O endpoint GET `/diagnostics/ping` recebe chamadas internas/externas e registra um span de servidor e log correlacionado por trace ID. Os clientes propagam W3C traceparent. Não são criados pedidos ou alterados dados de negócio. Alvos são fixos, chamadas têm timeout de cinco segundos e não seguem redirects; logs incluem destino, direção, status e duração, sem corpo ou credenciais.

O APM exporta métricas OTLP reais de volume e duração por operação, com atributos limitados a operação, tipo e estado. Exportação assíncrona a cada 60 segundos; chamadas sintéticas continuam a cada 5 minutos. Logs estruturados seguem pelo Alloy para Loki e Zabbix.

## Capacidade DEV

A release 2.0.4-lab define duas réplicas estáveis pelo HPA (`minReplicas=2`, `maxReplicas=2`). O HPA mantém a propriedade da escala; não há `spec.replicas` concorrente no manifesto. A mudança passa por CI, GMUD e aprovação conforme a política de criticidade e alterações sensíveis. No Worker, o Canary continua limitado a1→2 consumidores temporários, com promoção, drenagem e restauração da capacidade aprovada.
