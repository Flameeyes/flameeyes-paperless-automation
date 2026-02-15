# SPDX-FileCopyrightText: 2026 Diego Elio Pettenò
#
# SPDX-License-Identifier: 0BSD

# Development Instructions

## Tools

This project uses `uv` for dependency management and `prek` for pre-commit hooks.

## Before Committing

Run the type checker and linter before committing any changes:

```sh
uv run ty check
uvx prek run --all-files
```

Both must pass with no errors.
