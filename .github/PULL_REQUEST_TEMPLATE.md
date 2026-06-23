<!-- Thanks for contributing to ProductsFactory! Please fill out the sections below. -->

## What does this PR do?

<!-- A clear, concise description of the change and the motivation for it. -->

## Related issue

<!-- e.g. Closes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] Other (describe):

## How was this tested?

<!-- Commands you ran and the result. Remember: TEST_DATABASE_URL must point at a
     database whose name contains "test". -->

```
TEST_DATABASE_URL=... pytest tests/ -v
```

- [ ] `pytest tests/ -v` passes locally
- [ ] Ran `pytest evals/ -v` (only if I touched anything under `orchestrator/prompts/`)
- [ ] Added/updated tests for my change

## Checklist

- [ ] My change follows the conventions in `CONTRIBUTING.md`
- [ ] Schema changes include both a model update **and** a new Alembic migration
- [ ] I documented any new environment variables in `.env.example`
- [ ] I noted any behavior/invariant I touched (see `orchestrator/INVARIANTS.md`)
- [ ] No secrets, credentials, or real product data are included in this PR
