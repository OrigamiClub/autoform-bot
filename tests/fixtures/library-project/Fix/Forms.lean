module

/-! Declarations whose name or keyword is not written the plain way. -/

@[expose] public section

namespace Fix

theorem _root_.Nat.fix_root (n : Nat) : n + 0 = n := rfl

structure Pair where
  left : Nat
  right : Nat

@[ext] theorem Pair.ext {p q : Pair} (left : p.left = q.left) (right : p.right = q.right) : p = q := by
  cases p; cases q; simp_all

def countdown (n : Nat) : Nat :=
  let rec loop : Nat → Nat
    | 0 => 0
    | k + 1 => loop k
  loop n

theorem «odd name» : True := trivial

theorem «plainquoted» : True := trivial

/-- Rebuilding a pair. -/
@[simp] protected theorem Pair.eta (p : Pair) : Pair.mk p.left p.right = p := rfl

set_option maxRecDepth 600 in
theorem with_option : (6 : Nat) = 6 := rfl

end Fix
