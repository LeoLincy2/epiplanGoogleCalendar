# Contributing

epiplan is meant to stay small and easy to install. Before starting a large change, open an issue to discuss it.

## Pull requests

- One topic per PR. Don't mix a feature, a refactor and a behaviour change.
- Don't change existing CLI or tool behaviour without calling it out in the description.
- Keep optional, platform-specific features out of `main` unless they are small and self-contained.
- Run `python -m unittest discover -s tests` before pushing.

## Commit messages

Format: `type: short description`

- `type` is one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.
- Lowercase, imperative mood, no trailing period, 72 characters max.
- Add a body when the *why* isn't obvious from the diff, separated by a blank line.
- One logical change per commit. No "wip", "fix typo" or "address review" commits: squash them before opening the PR.

Examples:

```
fix: add tzdata to Dockerfile
feat: register detected MCP clients during setup
docs: document the --read-only flag
```
