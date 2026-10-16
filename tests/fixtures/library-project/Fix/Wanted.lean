module

public import Batteries.Util.ProofWanted

namespace Fix

/-- A result the library wants. -/
theorem_wanted wanted_theorem (n : Nat) : n = n

/-- A proof the library wants. -/
proof_wanted wanted_proof : (0 : Nat) = 0

/-- A definition the library wants. -/
def_wanted wanted_value : Nat

/-- A definition built on a wanted one. -/
def_wanted wanted_successor : Nat := ❰wanted_value❱ + 1

/-- An instance the library wants. -/
instance_wanted wantedInhabited : Inhabited Empty

/-- An anonymous instance the library wants. -/
instance_wanted : Nonempty Empty

private def handwritten : ProofWanted True := ⟨⟩

end Fix
