# CI

`github-actions.yml` is the CI pipeline (ruff → dataset‑reproducibility →
pytest → CLI smoke, plus a Docker build/run/health/reconcile job).

It lives here rather than `.github/workflows/` only because the token used to
create this repo lacks the GitHub `workflow` scope. To enable it:

```bash
mkdir -p .github/workflows
git mv ci/github-actions.yml .github/workflows/ci.yml
git commit -m "enable CI"
git push        # after: gh auth refresh -s workflow
```
