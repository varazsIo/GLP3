#!/usr/bin/env bash
# =====================================================================
# The local CI entry point: every check, in order; any failure fails the run.
#
#     scripts/ci/run_all.sh                # every stage
#     scripts/ci/run_all.sh unit pg        # only these stages, in this order
#
# Stages:
#   lint      ruff, eslint, tsc, shellcheck; tofu fmt -check, validate and
#             test (mock providers) for every OpenTofu module; and a pinned
#             gitleaks scan of the working tree (tracked and untracked,
#             not ignored)
#   unit      backend tests that touch no database (pytest -m "not pg and
#             not hpdbldn and not db_write")
#   pg        the ephemeral PostgreSQL 18 cluster tests (tests/_pg.py)
#   hpdbldn   the tests that read the real hpdbldn (read-only sessions), and
#             the rolled-back revision and constraint checks
#   coverage  per-package floors over the three pytest stages
#             (scripts/ci/check_coverage.py)
#   jest      the frontend's unit tests
#   e2e       Playwright, against `cloud.local up --fake-google`
#   model     `models.artifact verify` on the served bundle
#   audit     pip-audit, npm audit --omit=dev, gitleaks
#
# Stages for a hosted runner (GitHub Actions, .github/workflows/ci.yml), not
# part of a plain run: everything that needs no server, no private data and
# no GPU. `scripts/ci/run_all.sh hosted jest e2e_static tofu` is the CI.
#   hosted      every pytest test except hpdbldn, gpu and tier_a (the
#               ephemeral-cluster tests included), then the coverage floors
#               for that selection (check_coverage.py --hosted)
#   e2e_static  Playwright's e2e/coverage.spec.ts alone: it lists e2e/api/
#               and needs neither the stack nor a browser
#   tofu        tofu test (mock providers) for every OpenTofu module
#
# The pg and hpdbldn stages set HPLDN_TEST_DB_WRITES=1, so the tests that
# write to a database (an initdb'd throwaway cluster, or a transaction on
# hpdbldn that is always rolled back) run here; a plain `uv run pytest`
# skips them. Any other skip fails the suite (tests/conftest.py).
#
# The from-empty replay of the whole migration chain is not here: it takes
# hours. It is scripts/ci/replay_chain.sh, the nightly job.
# =====================================================================
set -euo pipefail
CURRENT_SCRIPT="${BASH_SOURCE[0]:-${0}}"
REPO_ROOT="$(cd "$(dirname "${CURRENT_SCRIPT}")/../.." && pwd)"
FRONTEND="$REPO_ROOT/src/hpldn/frontend"
COVERAGE_FILE="$REPO_ROOT/tmp/ci/.coverage"
ALL_STAGES=(lint unit pg hpdbldn coverage jest e2e model audit)
SERVED_MODEL="${HPLDN_SERVED_MODEL:-$REPO_ROOT/tmp/models/model_anchored_0031}"
# Pinned with tofu and cosign in scripts/install_tofu.sh (--gitleaks installs it).
GITLEAKS_VERSION="$(sed -n 's/^GITLEAKS_VERSION=//p' "$REPO_ROOT/scripts/install_tofu.sh")"

log() { printf '\n==> [%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() {
  echo "run_all: $*" >&2
  exit 1
}
need() { command -v "$1" >/dev/null || die "missing prerequisite: $1 ($2)"; }

need_gitleaks() {
  need gitleaks "scripts/install_tofu.sh --gitleaks"
  [[ "$(gitleaks version | sed 's/^v//')" == "$GITLEAKS_VERSION" ]] ||
    die "gitleaks $(gitleaks version) is not the pinned $GITLEAKS_VERSION (scripts/install_tofu.sh --gitleaks)"
}

# The working tree as git sees it (tracked, and untracked but not ignored),
# copied aside so gitleaks reads neither tmp/ nor data/ nor node_modules/.
scan_working_tree() {
  local tree status=0
  tree="$(mktemp -d)"
  git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard |
    (cd "$REPO_ROOT" && xargs -0 -r cp --parents -t "$tree" 2>/dev/null) || true
  gitleaks dir --no-banner --redact "$tree" || status=$?
  # Explicit cleanup: a RETURN trap outlives this function and fires again
  # (with `tree` out of scope) on every later return.
  rm -rf "$tree"
  return "$status"
}

cd "$REPO_ROOT"
mkdir -p "$(dirname "$COVERAGE_FILE")"
export COVERAGE_FILE

pytest_stage() {
  # pytest_stage MARK_EXPRESSION [extra pytest args...]
  local marks="$1"
  shift
  uv run pytest -m "$marks" --cov=hpldn.backend --cov-append --cov-report= "$@"
}

# tofu_modules [--lint]: tofu init and test (mock providers) in every
# OpenTofu module; with --lint, fmt -check and validate as well.
tofu_modules() {
  local lint=0 dir
  [[ "${1:-}" == "--lint" ]] && lint=1
  mapfile -t tofu_dirs < <(find src/hpldn/backend/cloud -name '*.tf' -not -path '*/.terraform/*' -printf '%h\n' | sort -u)
  if ! ((${#tofu_dirs[@]})); then
    log "no OpenTofu configuration under src/hpldn/backend/cloud yet; nothing for tofu to check"
    return 0
  fi
  need tofu "scripts/install_tofu.sh"
  for dir in "${tofu_dirs[@]}"; do
    ((lint)) && tofu -chdir="$dir" fmt -check -recursive
    # -lockfile=readonly: the committed .terraform.lock.hcl must already
    # hold every provider's hashes (tofu providers lock).
    tofu -chdir="$dir" init -backend=false -input=false -lockfile=readonly >/dev/null
    ((lint)) && tofu -chdir="$dir" validate
    # Mock providers only: nothing is created and no credential is read.
    tofu -chdir="$dir" test
  done
}

stage_lint() {
  uv run ruff check src
  (cd "$FRONTEND" && npx eslint src && npx tsc --noEmit)
  mapfile -t scripts < <(find src/hpldn/backend/cloud src/hpldn/backend/db scripts -name '*.sh' | sort)
  uv run shellcheck -x --shell=bash "${scripts[@]}"
  tofu_modules --lint
  need_gitleaks
  scan_working_tree
}

stage_unit() {
  rm -f "$COVERAGE_FILE"
  pytest_stage "not pg and not hpdbldn and not db_write"
}

stage_pg() {
  HPLDN_TEST_DB_WRITES=1 pytest_stage "pg"
}

stage_hpdbldn() {
  HPLDN_TEST_DB_WRITES=1 pytest_stage "hpdbldn or (db_write and not pg)"
}

stage_coverage() {
  [[ -f "$COVERAGE_FILE" ]] || die "no coverage data; run the unit, pg and hpdbldn stages first"
  uv run coverage json -o "$REPO_ROOT/tmp/ci/coverage.json" >/dev/null
  uv run python scripts/ci/check_coverage.py "$REPO_ROOT/tmp/ci/coverage.json"
}

stage_jest() {
  (cd "$FRONTEND" && CI=true npm test --silent)
}

stage_hosted() {
  # One run, so the coverage file holds exactly this selection.
  rm -f "$COVERAGE_FILE"
  HPLDN_TEST_DB_WRITES=1 pytest_stage "not hpdbldn and not gpu and not tier_a" -n auto
  uv run coverage json -o "$REPO_ROOT/tmp/ci/coverage.json" >/dev/null
  uv run python scripts/ci/check_coverage.py --hosted "$REPO_ROOT/tmp/ci/coverage.json"
}

stage_e2e_static() {
  (cd "$FRONTEND" && CI=true npm run e2e:static)
}

stage_tofu() {
  tofu_modules
}

stage_e2e() {
  # The stack is always taken down, even when `up` or the tests fail. An
  # explicit teardown, not a RETURN trap, which would outlive this function.
  local status=0
  uv run python -m hpldn.backend.cloud.local up --fake-google --build || status=$?
  if ((status == 0)); then
    (cd "$FRONTEND" && CI=true npm run e2e) || status=$?
  fi
  uv run python -m hpldn.backend.cloud.local down || true
  return "$status"
}

stage_model() {
  uv run python -c "import hpldn.backend.models.artifact" 2>/dev/null ||
    die "hpldn.backend.models.artifact is missing: the verifiable model bundle code is not installed"
  uv run python -m hpldn.backend.models.artifact verify "$SERVED_MODEL"
}

stage_audit() {
  # hpldn itself is installed editable and is not on PyPI.
  uv run pip-audit --skip-editable --progress-spinner off
  (cd "$FRONTEND" && npm audit --omit=dev)
  need_gitleaks
  gitleaks git --no-banner --redact "$REPO_ROOT"
}

stages=("$@")
((${#stages[@]})) || stages=("${ALL_STAGES[@]}")
for stage in "${stages[@]}"; do
  declare -F "stage_$stage" >/dev/null || die "unknown stage $stage (stages: ${ALL_STAGES[*]} hosted e2e_static tofu)"
done
for stage in "${stages[@]}"; do
  log "$stage"
  "stage_$stage"
done
log "all stages passed: ${stages[*]}"
