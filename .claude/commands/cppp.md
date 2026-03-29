---
name: commit-pr
description: Create commits and PRs with K3s deployment awareness. Use when committing code or creating pull requests.
---

Follows the standard cppp commit/PR workflow. Additionally, be aware of the match-scraper-agent deployment pipeline below.

## match-scraper-agent Deployment Pipeline

**Merging a PR to main triggers automatic deployment — no manual steps needed:**

1. GitHub Actions builds Docker image with `--platform linux/amd64` → pushes to GHCR
2. GHA updates `k3s/cronjob.yaml` with new image tag (committed with `[skip ci]`)
3. K3s cluster pulls updated manifest → new CronJob image active within minutes

**Never manually push images or edit `k3s/cronjob.yaml`** — CI owns that file.

## Verify deployment

```bash
kubectl get cronjob -n match-scraper
kubectl get pods -n match-scraper --sort-by=.metadata.creationTimestamp
```

## Check current image version

```bash
kubectl get cronjob match-scraper-agent -n match-scraper \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}'
```

## View recent run logs

```bash
kubectl logs -n match-scraper -l app=match-scraper-agent --tail=50
```
