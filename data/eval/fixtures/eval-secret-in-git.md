---
id: eval-secret-in-git
title: API key committed to git history and rotated
kind: note
origin: personal
date: "2026-01-12"
tags:
  - security
  - secrets
  - git
  - rotation
concepts:
  - secret rotation
  - credential leak
  - git history
relates_to: []
summary: An API key was accidentally committed to a public repository; it was revoked, a new key was issued, and pre-commit hooks were added to block credential patterns.
---

# API key committed to git history and rotated

A developer committed a `.env` file containing a production API key while pushing a hotfix. The repository was public, so the key was visible in git history within minutes and could not be considered safe even after a follow-up commit deleted the file.

Fix: revoke the leaked key immediately and provision a new one. Do not rewrite public history — the exposure is already observable. Add a pre-commit hook that scans for high-entropy strings, and rotate any adjacent secrets sharing the same repository or CI context.

## Detection

A security scan of public repositories flagged the key within fifteen minutes of the push, not the team. The repository had already been cloned and mirrored, so removing the file from the working tree did not remove it from history downstream.

## Rotation steps

1. Revoke the old key in the provider console.
2. Generate a new key and update the production secret store.
3. Update CI environment variables and developer `.env` templates.
4. Audit access logs for the old key to confirm it was not used maliciously.
5. Add a pre-commit hook using `detect-secrets` or `gitleaks`.

## Lesson

Secrets in version control are unrecoverable in practice. The only safe response is immediate revocation and rotation, plus prevention at the commit boundary.
