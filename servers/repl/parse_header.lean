import Lean.Elab.Import

open Lean Elab

/-!
Print the header of the Lean source on standard input, in the JSON shape of
`lean --deps-json`, as `Lean.Parser.parseHeader` reads it. The REPL imports
what that parser reports. `lean --deps-json` uses a separate fast parser that
can report different imports for the same header.
-/

def main : IO Unit := do
  let input ← IO.FS.readFile "/dev/stdin"
  let (header, _, messages) ← Parser.parseHeader (Parser.mkInputContext input "<stdin>")
  let mut errors := #[]
  for message in messages.toList do
    if message.severity == .error then
      errors := errors.push s!"{message.pos.line}:{message.pos.column}: {← message.data.toString}"
  let mut entry := Json.mkObj [("errors", toJson errors)]
  if errors.isEmpty then
    let moduleHeader := HeaderSyntax.toModuleHeader header
    -- `toJson` prints a module with `Name.toString`, which does not escape names such as
    -- `«Mathlib.Unsafe».«x✝»` or `«Mathlib.Unsafe»._hyg`, so their root would read as
    -- `Mathlib`. Escape each component that needs it instead.
    let imports := moduleHeader.imports.map fun imp =>
      (toJson imp).setObjVal! "module" (toJson (Name.toStringWithSep "." true imp.module))
    -- Before v4.30 an ordinary header and `prelude` followed by `import Init`
    -- both report one plain `Init` import, so also say whether `prelude` is there.
    -- `header` is `module? prelude? import*`, so `header.raw[1]` is the optional `prelude`.
    let result := (toJson moduleHeader).setObjVal! "imports" (toJson imports)
      |>.setObjVal! "isPrelude" (toJson !header.raw[1].isNone)
    entry := entry.setObjVal! "result" result
  IO.println (Json.mkObj [("imports", Json.arr #[entry])]).compress
