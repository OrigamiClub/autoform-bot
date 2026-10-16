module

/-! Public declarations of the fixture library. -/

@[expose] public section

namespace Fix

/-- Doubling. -/
def double (n : Nat) : Nat := n + n

/-- Doubling is addition. -/
theorem double_eq (n : Nat) : double n = n + n := rfl

abbrev Count := Nat

@[ext] structure Point where
  x : Nat
  y : Nat
deriving DecidableEq

class Sized (α : Type) where
  size : α → Nat

inductive Colour where
  | red
  | green

instance pointSized : Sized Point := ⟨fun p => p.x + p.y⟩

instance : Sized Colour := ⟨fun _ => 1⟩

theorem Point.swap (p : Point) : (Point.mk p.y p.x).y = p.x := rfl

def total (xs : List Nat) : Nat := go xs 0
where
  go : List Nat → Nat → Nat
    | [], acc => acc
    | x :: rest, acc => go rest (acc + x)

theorem fn_id : (fun n : Nat => n) = id := rfl

end Fix
