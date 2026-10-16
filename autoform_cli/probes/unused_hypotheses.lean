{imports}
-- Autoform unused-hypotheses probe. This file is a Python-format template:
-- `{{`/`}}` are literal braces and single-brace fields are filled by
-- autoform_cli.unused_hypotheses. It is written to a temporary file and run
-- with `lake env lean` inside the built project; it never modifies the project.
import Lean.Util.CollectFVars
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command Meta

namespace AutoformUnused

/-! Hypotheses a proof never uses. A proof is a term the kernel has checked. A
hypothesis the term never mentions can be deleted from the statement, and the
same term proves what remains. This reads the proof; it searches for nothing. -/

/-- Each propositional hypothesis of a proved theorem that could be deleted, with
its binder position and whether its proof uses it. A hypothesis that the
conclusion or a later binder mentions cannot be deleted and is left out. For
any other, a proof term that never mentions it, abstracted over the remaining
binders, proves the statement without it. `none` when the theorem's own proof
contains `sorry`, whose term uses nothing; a complete proof that rests on an
open statement is read like any other. -/
def hypotheses (info : TheoremVal) : MetaM (Option (Array Json)) := do
  if info.value.hasSorry then return none
  forallTelescope info.type fun xs conclusion => do
    -- The proof applied to the binders: its own lambdas are reduced, and a proof
    -- that is a bare constant stays an application that mentions every binder.
    let proof := info.value.beta xs
    -- One pass over the proof, and one over the statement, as graphs. Asking per
    -- hypothesis whether a term mentions it walks shared subterms once per path,
    -- which can take hours. A binder's type mentions only earlier binders, so a
    -- hypothesis in `stated` is one the conclusion or a later binder mentions.
    let used := (collectFVars {{}} proof).fvarSet
    let mut stated := collectFVars {{}} conclusion
    for x in xs do
      stated := collectFVars stated (← inferType x)
    let mut out := #[]
    for i in [0:xs.size] do
      let x := xs[i]!
      let id := x.fvarId!
      unless ← isProp (← inferType x) do continue
      if stated.fvarSet.contains id then continue
      let decl ← id.getDecl
      -- A hypothesis written as `p → …` or `[C]` has a hygienic name no reader wrote.
      let name := if decl.userName.hasMacroScopes then "_" else decl.userName.toString
      out := out.push <| Json.mkObj [("position", toJson (i + 1)), ("name", Json.str name),
        ("type", Json.str (toString (← ppExpr decl.type.cleanupAnnotations))),
        ("used", Json.bool (used.contains id))]
    return some out

end AutoformUnused

namespace AutoformUnusedProbe

/-- Write one record to the file the CLI names, where nothing else Lean prints
can split it. Standard output is only for running the probe by hand. -/
def emit (fields : List (String × Json)) : CommandElabM Unit := do
  let line := s!"{marker}{{(Json.mkObj fields).compress}}\n"
  match ← IO.getEnv "{output_env}" with
  | some path =>
    let out ← IO.FS.Handle.mk path .append
    out.putStr line
    out.flush
  | none => IO.print line

def probe (request : String) (root : Name) : CommandElabM Unit := do
  match (← getEnv).find? root with
  | none => emit [("root", Json.str request), ("found", Json.bool false)]
  | some info =>
    let (proof, hypotheses) ← match info with
      | .thmInfo v => do
        match ← liftTermElabM (AutoformUnused.hypotheses v) with
        | some found => pure ("proved", found)
        | none => pure ("sorry", #[])
      | _ => pure ("none", #[])
    emit [("root", Json.str request), ("found", Json.bool true), ("proof", Json.str proof),
      ("hypotheses", Json.arr hypotheses)]

/-- The requested roots, as the CLI passes them: JSON `[request, components]`
pairs, where a component is a string or a number. A list literal of names
would be compiled, and the code generator gives up on long ones. -/
def requestedRoots (text : String) : CommandElabM (Array (String × Name)) := do
  let decode (entry : Json) : Except String (String × Name) := do
    let request ← entry.getArrVal? 0 >>= (·.getStr?)
    let components ← entry.getArrVal? 1 >>= (·.getArr?)
    let root ← components.foldlM (init := Name.anonymous) fun name component =>
      match component with
      | .str part => pure (name.str part)
      | _ => name.num <$> component.getNat?
    pure (request, root)
  match _root_.Lean.Json.parse text >>= (·.getArr?) >>= (·.mapM decode) with
  | .ok roots => pure roots
  | .error e => throwError ("the requested roots are malformed: " ++ e)

end AutoformUnusedProbe

-- The command's own heartbeat limit would count every declaration together, so
-- a large project would hit it; the CLI's timeout bounds the run instead.
set_option maxHeartbeats 0 in
run_cmd do
  for (request, root) in (← AutoformUnusedProbe.requestedRoots {roots}) do
    AutoformUnusedProbe.probe request root
