"""Decision engine: deterministic evaluation with full explanations."""

from .conditions import AttributeMissingError, ConditionTypeError

TIE_BREAK = "deny-overrides"


class Decision(object):
    """The outcome of one evaluation, including the full explanation."""

    def __init__(self, verdict, decided_by, explanation):
        self.verdict = verdict  # "allow" | "deny" | "error"
        self.decided_by = decided_by  # rule id or None
        self.explanation = explanation

    def to_dict(self):
        out = {"decision": self.verdict, "decided_by": self.decided_by}
        out.update(self.explanation)
        return out


def _rule_brief(rule):
    return {"id": rule.id, "effect": rule.effect, "priority": rule.priority}


def evaluate(ruleset, request, use_index=True):
    """Evaluate a request against a compiled rule set.

    Deterministic order of business:
      1. Any candidate rule that cannot be evaluated (missing attribute or
         type mismatch) triggers the configured `on_attribute_error` policy
         ("error" default, or "deny"). It never falls through to allow.
      2. Otherwise the highest-priority matched rule decides; ties at the
         same priority are resolved deny-overrides (fixed strategy).
      3. No matched rule => configured `default_effect` (deny by default).
    """
    if not isinstance(request, dict):
        raise TypeError("request must be a dict with subject/resource/action/env")

    candidates = ruleset.candidates(request, use_index=use_index)
    matched, not_matched, errored = [], [], []
    for rule in sorted(candidates, key=lambda r: r.id):
        try:
            ok, reason = rule.matches(request)
        except (AttributeMissingError, ConditionTypeError) as exc:
            errored.append(
                {
                    "id": rule.id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue
        if ok:
            matched.append(rule)
        else:
            not_matched.append({"id": rule.id, "reason": reason})

    stats = {
        "rules_total": len(ruleset.rules),
        "candidates": len(candidates),
        "evaluated": len(matched) + len(not_matched) + len(errored),
    }
    explanation = {
        "matched": [_rule_brief(r) for r in matched],
        "not_matched": not_matched,
        "errored": errored,
        "shadowed": [],
        "tie_break": None,
        "default_policy_applied": None,
        "stats": stats,
    }

    if errored:
        policy = ruleset.on_attribute_error
        explanation["default_policy_applied"] = "on_attribute_error=%s" % policy
        if policy == "deny":
            return Decision("deny", None, explanation)
        return Decision("error", None, explanation)

    if matched:
        ordered = sorted(
            matched,
            key=lambda r: (-r.priority, 0 if r.effect == "deny" else 1, r.id),
        )
        decider = ordered[0]
        tied = [
            r
            for r in ordered[1:]
            if r.priority == decider.priority and r.effect != decider.effect
        ]
        if tied:
            explanation["tie_break"] = TIE_BREAK
        for rule in ordered[1:]:
            if rule.priority < decider.priority:
                reason = "lower priority (%d < %d) than deciding rule %r" % (
                    rule.priority,
                    decider.priority,
                    decider.id,
                )
            else:
                reason = (
                    "same priority %d as deciding rule %r; %s makes %s win"
                    % (rule.priority, decider.id, TIE_BREAK, decider.effect)
                )
            shadow = _rule_brief(rule)
            shadow.update({"shadowed_by": decider.id, "reason": reason})
            explanation["shadowed"].append(shadow)
        return Decision(decider.effect, decider.id, explanation)

    explanation["default_policy_applied"] = "default_effect=%s" % ruleset.default_effect
    return Decision(ruleset.default_effect, None, explanation)
