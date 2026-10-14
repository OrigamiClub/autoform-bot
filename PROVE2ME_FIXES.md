# Prove2Me fixes (October 2026)

## Statements workspace could not be edited
- **Symptom:** the headless run stopped with "allow edits under …/statements". `chmod` did not help.
- **Cause:** `formalize_statement.sh` loads this checkout with `--plugin-dir`, and Claude Code refuses Write/Edit on any file inside a plugin directory ("a sensitive file"). The default workspace, `./statements`, was inside it.
- **Fix:** the default is now `~/autoform_statements` (`$AUTOFORM_STATEMENTS_DIR` overrides it). The script refuses a workspace inside the checkout and prints the `mv` command to move it.

## Session could not find the API key
- **Symptom:** in a `--propose` run, the session reported "no Prove2Me API key", but `setup` had just used it.
- **Cause:** the script set `PROVE2ME_WORKSPACE` to the per-environment checkout (`prove2me-0df444a`), which hid the home workspace that holds the key. A relative `PROVE2ME_WORKSPACE` also broke once the script changed directory.
- **Fix:** workspace paths are now absolute. `setup` links each extra checkout's `credentials.json` to the home key (gitignored, still one copy). The script runs `prove2me auth` before starting Claude, so a key problem fails fast.

## Proposing a mission (new)
- `autoform prove2me propose spec.json` checks a proposal spec and builds every draft item in the pinned Prove2Me workspace. It then creates the draft proposal, or updates it in place with `--proposal ID`, and sets the goal, item order and milestones. `--dry-run` stops before uploading.
- `./formalize_statement.sh --propose "<statement>"` formalizes the statement and then drafts the proposal. The agent picks the mission type and fields, checks for duplicates, and gets blind read-backs from a sub-agent. `--mission-type`, `--mission-title` and `--dry-run` are optional.
- Only you can submit: confirm each item under **My missions** on prove2.me and click **Submit Proposal**. Until then the draft is private.
