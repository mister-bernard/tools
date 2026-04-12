# Contributing

This repo is a book club for CLI tools. Bring something useful, take what you need.

## Adding a tool

1. Create a directory with your tool name (lowercase, hyphenated)
2. Include at minimum:
   - `README.md` — what it does, how to install, how to use
   - The tool itself (script, binary, whatever)
3. Optional but nice:
   - `install.sh` — one-command setup
   - Screenshots or examples
   - Tests

### Structure

```
your-tool/
  README.md          # required
  your-tool.py       # the actual tool
  install.sh         # optional
  requirements.txt   # optional (Python deps)
  package.json       # optional (Node deps)
```

### Rules

- **Self-contained.** Each tool lives in its own directory. No cross-dependencies between tools.
- **No secrets.** No API keys, tokens, passwords, or hardcoded credentials. Use environment variables or config files.
- **No binaries.** Source code only. Build instructions if compilation is needed.
- **Works on Linux.** macOS support is a bonus. Windows is aspirational.
- **Document it.** If someone can't figure out what it does from the README in 30 seconds, it needs work.

## Agent submission protocol

AI agents (Claude Code, Cursor, Copilot, etc.) can submit tools programmatically.

### Authentication

Set the `TOOLS_SUBMIT_TOKEN` environment variable to the shared passphrase before pushing.

### Automated workflow

```bash
# 1. Fork + clone
gh repo fork mister-bernard/tools --clone
cd tools

# 2. Create your tool
mkdir my-tool
# ... add files ...

# 3. Submit
git checkout -b add/my-tool
git add my-tool/
git commit -m "add: my-tool — one-line description"
git push -u origin add/my-tool

# 4. Open PR with the token in the body for auto-merge
gh pr create \
  --title "add: my-tool" \
  --body "## Summary
- What: one-line description
- Lang: Python/Bash/Node/etc

## Agent submission
token: ${TOOLS_SUBMIT_TOKEN}

## Checklist
- [x] README.md included
- [x] No hardcoded secrets
- [x] Self-contained directory
- [x] Tested locally"
```

### Auto-merge

PRs that include a valid `token:` line in the body are auto-labeled and fast-tracked for review. The CI pipeline validates:

1. Tool has a `README.md`
2. No obvious secrets (scans for API keys, tokens, passwords)
3. Directory is self-contained
4. Passes shellcheck/lint where applicable

### Branch naming

Use `add/tool-name` for new tools, `fix/tool-name` for fixes, `update/tool-name` for improvements.
