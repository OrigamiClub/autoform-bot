module

@[expose] public section

namespace Fix

notation "⟪" a "⟫" => Nat.succ a

macro "fix_zero" : term => `(0)

theorem after_notation : ⟪0⟫ = 1 := rfl

end Fix
