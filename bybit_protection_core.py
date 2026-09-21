"""Pure, paper-only Fib close candidate evaluation."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

FIB_CLOSE_FRACTIONS = {"fib_1272": 0.25, "fib_1618": 0.35}

def evaluate_fib_close_candidate(position: dict[str, Any], fib: dict[str, Any], state: dict[str, Any] | None = None, *, now: datetime | None = None, max_age_seconds: int = 900) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    side = str(position.get("side", "")).lower()
    if side not in {"long", "short"}: return {"status":"FIB_CLOSE_BLOCKED","reason":"POSITION_SIDE_INVALID","execution":"PAPER_ONLY"}
    if fib.get("valid") is not True: return {"status":"FIB_CLOSE_BLOCKED","reason":fib.get("reason","FIB_INVALID"),"execution":"PAPER_ONLY"}
    now = now or datetime.now(timezone.utc)
    if not fib.get("as_of"): return {"status":"FIB_CLOSE_BLOCKED","reason":"FIB_EVIDENCE_TIME_MISSING","execution":"PAPER_ONLY"}
    try:
        mark, size = float(position["mark_price"]), float(position["size"])
        liq = float(position["liquidation_price"]) if position.get("liquidation_price") not in (None, "") else None
        levels = {name: float(fib[name]) for name in FIB_CLOSE_FRACTIONS}
        as_of = datetime.fromisoformat(str(fib["as_of"]).replace("Z", "+00:00"))
        if as_of.tzinfo is None: as_of = as_of.replace(tzinfo=timezone.utc)
        if not -30 <= (now - as_of).total_seconds() <= max_age_seconds: raise ValueError("stale")
    except (KeyError, TypeError, ValueError): return {"status":"FIB_CLOSE_BLOCKED","reason":"FIB_OR_POSITION_INVALID","execution":"PAPER_ONLY"}
    if mark <= 0 or size <= 0 or any(level <= 0 for level in levels.values()): return {"status":"FIB_CLOSE_BLOCKED","reason":"FIB_OR_POSITION_INVALID","execution":"PAPER_ONLY"}
    if liq is not None and ((side == "short" and mark >= liq) or (side == "long" and mark <= liq)): return {"status":"FIB_CLOSE_BLOCKED","reason":"LIQUIDATION_BOUNDARY_BREACHED","execution":"PAPER_ONLY"}
    if (side == "short" and levels["fib_1272"] <= levels["fib_1618"]) or (side == "long" and levels["fib_1272"] >= levels["fib_1618"]): return {"status":"FIB_CLOSE_BLOCKED","reason":"FIB_STRUCTURE_INVALID","execution":"PAPER_ONLY"}
    for stage in ("fib_1272", "fib_1618"):
        if state.get(f"{stage}_closed") is True: continue
        target = levels[stage]
        if not (mark <= target if side == "short" else mark >= target): return {"status":"NO_TRIGGER","stage":stage,"target":target,"execution":"PAPER_ONLY"}
        fraction = FIB_CLOSE_FRACTIONS[stage]
        close_size = round(size * fraction, 8)
        if close_size <= 0 or close_size > size: return {"status":"FIB_CLOSE_BLOCKED","reason":"CLOSE_SIZE_INVALID","execution":"PAPER_ONLY"}
        return {"status":"FIB_CLOSE_CANDIDATE","stage":stage,"target":target,"close_fraction":fraction,"close_size":close_size,"execution":"PAPER_ONLY"}
    return {"status":"NO_TRIGGER","stage":"COMPLETE","execution":"PAPER_ONLY"}
