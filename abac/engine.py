"""Policy compilation and decision engine.

Compilation: rules are hashed into buckets keyed by (action, resource.type).
Rules without a concrete action / resource.type condition go into the "*"
wildcard bucket. Each bucket is pre-sorted by the deterministic resolution
order:

    1. higher priority first
    2. tie -> deny before allow ("deny-overrides", fail-closed)
    3. tie -> lexicographic rule id (fully deterministic)

A lookup touches at most four buckets:
(action, rtype), (action, *), (*, rtype), (*, *), so the cost of a single
decision depends on the bucket sizes, not on the total rule count.
"""

import json

from .conditions import MISSING, AttributeProblem, match_spec
from .model import detect_conflicts, load_rules

ON_MISSING_POLICIES = ("error", "deny", "skip")
DEFAULT_EFFECTS = ("allow", "deny")

TIE_BREAK_DESCRIPTION = (
    "deny-overrides: same-priority ties resolve to deny (fail-closed), "
    "then to the lexicographically smaller rule id, so a decision never "
    "depends on file ordering"
)


def _sort_key(rule):
    return (-rule.priority, 0 if rule.effect == "deny" else 1, rule.rule_id)


class Policy:
    def __init__(self, rules, *, on_missing_attribute="error",
                 default_effect="deny"):
        if on_missing_attribute not in ON_MISSING_POLICIES:
            raise ValueError("on_missing_attribute must be one of %s"
                             % (ON_MISSING_POLICIES,))
        if default_effect not in DEFAULT_EFFECTS:
            raise ValueError("default_effect must be allow|deny")
        detect_conflicts(rules)
        self.rules = rules
        self.on_missing_attribute = on_missing_attribute
        self.default_effect = default_effect
        self._buckets = {}
        for rule in rules:
            for action in rule.action_keys:
                for rtype in rule.rtype_keys:
                    self._buckets.setdefault((action, rtype), []).append(rule)
        for bucket in self._buckets.values():
            bucket.sort(key=_sort_key)

    @classmethod
    def from_json(cls, data, **kwargs):
        """Build a policy from a parsed JSON list of rules."""
        return cls(load_rules(data), **kwargs)

    @classmethod
    def from_file(cls, path, **kwargs):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_json(json.load(fh), **kwargs)

    # -- candidate selection -------------------------------------------------

    def _candidates(self, action, rtype):
        """Candidate rules for a request.

        `action` / `rtype` are the request's index attributes, or None when
        the request does not provide a usable string: the index then degrades
        to a full scan on that dimension so the missing-attribute policy is
        applied by rule evaluation instead of silently pruning rules.
        """
        seen = set()
        out = []
        if action is not None and rtype is not None:
            # Fast path: four direct hash lookups, independent of rule count.
            keys = ((action, rtype), (action, "*"), ("*", rtype), ("*", "*"))
            buckets = (self._buckets.get(key, ()) for key in keys)
        else:
            actions = (action, "*") if action is not None else None
            rtypes = (rtype, "*") if rtype is not None else None
            buckets = (bucket for (a_key, r_key), bucket in self._buckets.items()
                       if (actions is None or a_key in actions)
                       and (rtypes is None or r_key in rtypes))
        for bucket in buckets:
            for rule in bucket:
                if rule.rule_id not in seen:
                    seen.add(rule.rule_id)
                    out.append(rule)
        out.sort(key=_sort_key)
        return out

    # -- rule matching -------------------------------------------------------

    @staticmethod
    def _match_rule(rule, request):
        for group, attrs in rule.conditions.items():
            if group == "action":
                if not match_spec(attrs, request.get("action", MISSING), "action"):
                    return False
                continue
            obj = request.get(group)
            if not isinstance(obj, dict):
                obj = {}
            for attr, spec in attrs.items():
                value = obj.get(attr, MISSING)
                if not match_spec(spec, value, "%s.%s" % (group, attr)):
                    return False
        return True

    # -- decision ------------------------------------------------------------

    def _base_explanation(self):
        return {
            "tie_break": TIE_BREAK_DESCRIPTION,
            "policies": {
                "on_missing_attribute": self.on_missing_attribute,
                "default_effect": self.default_effect,
            },
        }

    def _problem_decision(self, decision, problem, explanation):
        explanation["attribute_problem"] = {
            "attribute": problem.path,
            "kind": problem.kind,
            "detail": problem.detail,
            "policy_applied": self.on_missing_attribute,
        }
        return {
            "decision": decision,
            "deciding_rule": None,
            "reason": "attribute problem (%s) handled by policy %r"
                      % (problem.detail, self.on_missing_attribute),
            "explanation": explanation,
        }

    def decide(self, request, explain=True):
        explanation = self._base_explanation()
        explanation["total_rules"] = len(self.rules)

        action = request.get("action", MISSING)
        resource = request.get("resource")
        rtype = resource.get("type", MISSING) if isinstance(resource, dict) else MISSING
        action_key = action if isinstance(action, str) else None
        rtype_key = rtype if isinstance(rtype, str) else None
        if action_key is None or rtype_key is None:
            explanation["index_degraded"] = (
                "request lacks a usable action/resource.type; affected "
                "dimension scanned in full so the missing-attribute policy "
                "applies during rule evaluation")

        candidates = self._candidates(action_key, rtype_key)
        explanation["candidate_rules"] = len(candidates)
        explanation["pruned_by_index"] = len(self.rules) - len(candidates)

        matched = []
        unmatched = []
        for rule in candidates:
            try:
                ok = self._match_rule(rule, request)
            except AttributeProblem as problem:
                if self.on_missing_attribute == "skip":
                    unmatched.append({"id": rule.rule_id,
                                      "reason": "skipped: %s" % (problem,)})
                    continue
                decision = ("error" if self.on_missing_attribute == "error"
                            else "deny")
                explanation["matched_rules"] = matched
                explanation["unmatched_rules"] = unmatched
                return self._problem_decision(decision, problem, explanation)
            if ok:
                matched.append({"id": rule.rule_id, "effect": rule.effect,
                                "priority": rule.priority})
            else:
                unmatched.append({"id": rule.rule_id,
                                  "reason": "conditions not satisfied"})

        explanation["matched_rules"] = matched
        if explain:
            explanation["unmatched_rules"] = unmatched

        if not matched:
            explanation["default_applied"] = (
                "no rule matched; default_effect=%r applied" % self.default_effect)
            return {
                "decision": self.default_effect,
                "deciding_rule": None,
                "reason": "no matching rule; default policy %r applied"
                          % self.default_effect,
                "explanation": explanation,
            }

        # Candidates were processed in resolution order, so matched[0] wins.
        decider = matched[0]
        shadowed = []
        for entry in matched[1:]:
            if entry["priority"] < decider["priority"]:
                cause = "lower priority than %s" % decider["id"]
            elif entry["effect"] == decider["effect"]:
                cause = ("same priority and effect as %s; lost deterministic "
                         "id tie-break" % decider["id"])
            else:
                cause = ("same priority as %s; deny-overrides tie-break"
                         % decider["id"])
            shadowed.append({"id": entry["id"], "effect": entry["effect"],
                             "priority": entry["priority"],
                             "shadowed_by": decider["id"], "cause": cause})
        explanation["deciding_rule"] = decider["id"]
        explanation["shadowed_rules"] = shadowed
        return {
            "decision": decider["effect"],
            "deciding_rule": decider["id"],
            "reason": "rule %r decided by highest priority"
                      % decider["id"] if not shadowed else
                      "rule %r won over %d shadowed rule(s)"
                      % (decider["id"], len(shadowed)),
            "explanation": explanation,
        }
