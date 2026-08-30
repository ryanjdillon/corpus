# GENERATED FILE — do not edit.
# Source: <base>/agents.just
# Regenerate with: just agents

# Pin the shared base here. "main" tracks latest; set a tag or SHA to freeze.
agents_rev := "main"

# Recompose AGENTS.md and agents.just from the shared base plus this repo's overlay.
# --refresh defeats nix's flake cache, which otherwise serves a stale base.
agents:
    nix run --refresh "github:ryanjdillon/agents/{{agents_rev}}#compose"

# Fail if the committed AGENTS.md or agents.just have drifted from their sources.
agents-check:
    nix run --refresh "github:ryanjdillon/agents/{{agents_rev}}#compose" -- --check
