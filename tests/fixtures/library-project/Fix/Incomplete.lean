namespace Fix

theorem open_claim : (1 : Nat) = 1 := by sorry

private theorem gap : (2 : Nat) = 2 := by sorry

theorem rests_on_gap : (2 : Nat) = 2 := gap

axiom assumed : (3 : Nat) = 3

theorem rests_on_axiom : (3 : Nat) = 3 := assumed

theorem plain : (4 : Nat) = 4 := rfl

end Fix
