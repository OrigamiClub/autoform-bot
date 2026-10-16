module

namespace Fix

public theorem shown : True := trivial

theorem unmarked : True := trivial

private theorem secret : True := trivial

public def outer (n : Nat) : Nat := helper n
where
  helper (n : Nat) : Nat := n

end Fix
