#!/usr/bin/env bash
# Run AutoformBot on a single mathematical statement instead of a whole paper.
#
#   ./formalize_statement.sh "6 is not a prime number"
#   ./formalize_statement.sh --roadmap-only "every finite group of prime order is cyclic"
#   ./formalize_statement.sh --project ~/my-lean-project "..."
#   ./formalize_statement.sh --prove2me Positive_definite_matrix_integral_inequality
#
# Statements go into one shared Lean/Mathlib workspace (default: ./statements,
# created on first use) so the Mathlib build cache is downloaded only once.
# Each statement becomes one roadmap article plus one Lean file, driven by a
# headless Claude Code session with this checkout loaded as the plugin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${AUTOFORM_STATEMENTS_DIR:-$ROOT/statements}"
PACKAGE="Statements"
ROADMAP_ONLY=0
MODEL=""
PROVE2ME=""
DRY_RUN=0
PROVE2ME_WORKSPACE="${PROVE2ME_WORKSPACE:-$HOME/prove2me_workspace}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] "<statement>"
       $(basename "$0") --prove2me MISSION [--dry-run] ["<hint>"]

Formalize and prove a single statement with AutoformBot, or contribute a
proof to a Prove2Me mission.

Options:
  --project DIR    Lean+Mathlib project to work in (default: $PROJECT).
                   Created with 'lake init' if it does not exist; an existing
                   project must already depend on Mathlib.
  --roadmap-only   Write the blueprint article only; do not touch Lean.
  --model MODEL    Claude model to use (default: Claude Code's default).

Prove2Me:
  --prove2me MISSION  Pick an open frontier theorem of this Prove2Me mission
                   (mission URL, URL slug, or name) and submit a proof or
                   proof sketch. An optional "<hint>" says which theorem or
                   approach to prefer. Works in \$PROVE2ME_WORKSPACE
                   (default: $PROVE2ME_WORKSPACE), which must already hold
                   credentials.json or PROVE2ME_API_KEY; see
                   https://prove2.me/start.md.
  --dry-run        Prove2Me only: compile locally but do not submit.
  -h, --help       Show this help.
EOF
}

STATEMENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --roadmap-only) ROADMAP_ONLY=1; shift ;;
    --model) MODEL="$2"; shift 2 ;;
    --prove2me) PROVE2ME="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; STATEMENT="$*"; break ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) STATEMENT="${STATEMENT:+$STATEMENT }$1"; shift ;;
  esac
done

if [[ -z "$STATEMENT" && -z "$PROVE2ME" ]]; then
  usage >&2
  exit 2
fi

for tool in claude uv lake git; do
  command -v "$tool" >/dev/null || { echo "error: '$tool' not found on PATH" >&2; exit 1; }
done

# Tools the headless session may use without prompting.
ALLOWED_TOOLS=(
  "Read" "Edit" "Write" "Glob" "Grep" "Skill"
  "Bash(lake:*)" "Bash(uv run:*)" "Bash(git:*)" "Bash(ls:*)" "Bash(cat:*)"
  "Bash(grep:*)" "Bash(find:*)" "Bash(mkdir:*)"
  "mcp__plugin_autoform_autoform-lsp" "mcp__plugin_autoform_autoform-repl"
)

run_claude() {  # run_claude <prompt> [extra allowed tools...]
  local prompt="$1"; shift
  local args=(
    -p "$prompt"
    --plugin-dir "$ROOT"
    --add-dir "$ROOT"
    --permission-mode acceptEdits
    --allowedTools "${ALLOWED_TOOLS[@]}" "$@"
  )
  [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
  claude "${args[@]}"
}

# --- Prove2Me mission mode ---------------------------------------------------
# The autoform:prove2me skill drives the work and `autoform prove2me` makes
# every API call, so this session never handles the key or needs curl.
if [[ -n "$PROVE2ME" ]]; then
  if ! uv run --quiet --project "$ROOT" autoform prove2me auth --workspace "$PROVE2ME_WORKSPACE"; then
    cat >&2 <<EOF
Fix the Prove2Me key first: mint one at prove2.me (account menu > API key) and
store it as api_key in $PROVE2ME_WORKSPACE/credentials.json, or export
PROVE2ME_API_KEY. New accounts: claude "Fetch https://prove2.me/start.md and follow it"
EOF
    exit 1
  fi

  git -C "$PROVE2ME_WORKSPACE" pull -q --ff-only \
    || echo "warning: could not update $PROVE2ME_WORKSPACE; continuing with local copy" >&2

  if [[ "$DRY_RUN" -eq 1 ]]; then
    SUBMIT="This is a dry run: take the work as far as passing
'autoform prove2me verify --dry-run' (and 'publish --dry-run' for any sketch
children), then stop. Do not submit, publish, or edit anything on Prove2Me.
Show the solution file, its target theorem id, and the explanation you would
attach."
  else
    SUBMIT="The user asks you to submit: once 'verify --dry-run' passes,
submit with an explanation, publishing sketch children first if you
decompose. Do not comment, vote, rate, accept licensing, or create missions."
  fi

  read -r -d '' PROMPT <<EOF || true
Use the autoform:prove2me skill to contribute to the Prove2Me mission
$PROVE2ME: pick an open frontier theorem and submit a proof or proof sketch.
${STATEMENT:+Hint from the user about what to target: "$STATEMENT"
}
You are running non-interactively, so nobody can answer questions; make
reasonable choices and explain them at the end. The Prove2Me workspace is
$PROVE2ME_WORKSPACE (pass --workspace or rely on its default). Run the CLI as
'uv run --project "$ROOT" autoform prove2me ...'.

$SUBMIT
EOF

  echo "==> Prove2Me mission: $PROVE2ME"
  echo "    workspace: $PROVE2ME_WORKSPACE"
  [[ "$DRY_RUN" -eq 1 ]] && echo "    dry run: nothing will be submitted"
  cd "$PROVE2ME_WORKSPACE"
  run_claude "$PROMPT" "WebFetch" "WebSearch"
  exit $?
fi

# "6 is not a prime number" -> slug "6-is-not-a-prime-number",
# Lean module "Stmt6IsNotAPrimeNumber" (identifiers cannot start with a digit).
SLUG="$(printf '%s' "$STATEMENT" | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-60 | sed -E 's/-+$//')"
[[ -n "$SLUG" ]] || SLUG="statement"
MODULE="$(printf '%s' "$SLUG" | awk -F- '{for (i = 1; i <= NF; i++) printf "%s", toupper(substr($i, 1, 1)) substr($i, 2)}')"
[[ "$MODULE" =~ ^[A-Za-z] ]] || MODULE="Stmt$MODULE"

autoform() { uv run --quiet --project "$ROOT" autoform "$@"; }

# --- Workspace ---------------------------------------------------------------
if [[ ! -d "$PROJECT" ]]; then
  echo "==> Creating Lean + Mathlib workspace at $PROJECT (first run only; this downloads Mathlib)"
  mkdir -p "$PROJECT"
  (cd "$PROJECT" && lake init "$PACKAGE" math)
fi
PROJECT="$(cd "$PROJECT" && pwd)"

if [[ "$ROADMAP_ONLY" -eq 0 ]]; then
  echo "==> Fetching Mathlib build cache"
  (cd "$PROJECT" && lake exe cache get)
fi

if [[ ! -d "$PROJECT/.git" ]]; then
  git -C "$PROJECT" init -q
fi
if [[ ! -d "$PROJECT/blueprint" ]]; then
  echo "==> Scaffolding blueprint"
  autoform init "$PROJECT" --title "Single statements"
fi
if ! git -C "$PROJECT" rev-parse -q --verify HEAD >/dev/null; then
  git -C "$PROJECT" add -A
  git -C "$PROJECT" commit -q -m "Initial Lean and blueprint scaffold"
fi

# Lean source directory: the package's own directory if lake made one.
LEAN_DIR="$PACKAGE"
[[ -d "$PROJECT/$LEAN_DIR" ]] || LEAN_DIR="$(cd "$PROJECT" && ls -d */ 2>/dev/null \
  | grep -vE '^(blueprint|\.lake|site|site-src|theme)/$' | grep -E '^[A-Z]' | head -1 | tr -d /)"
LEAN_FILE="${LEAN_DIR:-$PACKAGE}/$MODULE.lean"

# --- Prompt ------------------------------------------------------------------
AUTOFORM_CMD="uv run --project \"$ROOT\" autoform"
read -r -d '' PROMPT <<EOF || true
You are running AutoformBot non-interactively on a single statement, not a
paper. Nobody is available to answer questions: the scope below is already
confirmed and the one-node roadmap is pre-approved, so do not stop to ask.

Statement (verbatim from the user):
  "$STATEMENT"

Project root: $PROJECT
Autoform CLI: $AUTOFORM_CMD <subcommand> ...

1. Use the autoform:roadmap skill. The source is the statement itself: record
   it as a short project-authored specification in
   blueprint/sources/$SLUG.md, stating your precise mathematical reading of it
   (and any ambiguity you resolved). Write exactly one roadmap article at
   blueprint/roadmap/$SLUG.md with a single main result, a \`declaration:\`
   kind, a proof sketch, and an empty or genuine \`## Depends on\` section.
   Add one row for it to blueprint/coverage/README.md and update the landing
   pages so they no longer say nothing is planned.
EOF

if [[ "$ROADMAP_ONLY" -eq 0 ]]; then
  PROMPT+="
2. Formalize the statement in $LEAN_FILE (module ${LEAN_FILE%.lean}, with
   '/' read as '.') and import it from the package root file so 'lake build'
   compiles it. Prefer Mathlib's existing notions (e.g. Nat.Prime). Prove it
   with no sorry, admit, or new axioms. Use the autoform-lsp / autoform-repl
   MCP tools (pass project_dir=$PROJECT) or 'lake build' to check your work.
   If you cannot finish the proof, leave the statement compiled with sorry and
   say so plainly rather than weakening it.
3. Once 'lake build' succeeds, set \`statement: formalized\`, and
   \`proof: formalized\` only if the proof is complete, plus the
   fully-qualified \`lean:\` name in the article's frontmatter.
"
fi

PROMPT+="
Finally run '$AUTOFORM_CMD check $PROJECT/blueprint --lean-root $PROJECT',
fix anything it reports, and commit the result in $PROJECT (do not push).
Report the Lean statement you wrote, whether it is fully proved, and the paths
you changed."

# --- Run ---------------------------------------------------------------------
echo "==> Running AutoformBot on: $STATEMENT"
echo "    article: $PROJECT/blueprint/roadmap/$SLUG.md"
[[ "$ROADMAP_ONLY" -eq 1 ]] || echo "    lean:    $PROJECT/$LEAN_FILE"
cd "$PROJECT"
run_claude "$PROMPT"
