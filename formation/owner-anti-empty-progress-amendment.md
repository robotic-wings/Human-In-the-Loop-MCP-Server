# Owner Amendment: Outcome Before Procedure

By the owner's decision of 2026-11-09, this amendment applies only to the
`Human-in-the-loop-MCP-server` Formation. It replaces the named provisions of
the Basic TDD Charter below. Every other Charter provision remains in force.

## 1. Replacement for “Continuity and round maintenance”

An unfinished failure is not a transition. When an acceptance condition is
known to fail, the Leader must retain that failure as the current work until an
assigned role changes the work object or the failure is genuinely blocked by an
external dependency. The Leader must not turn the same failure into a new
stage, a new handoff, a new plan, a review request, an inspection request, a
restart, a test run, a status record, or a fresh physical run merely because
the previous activity ended.

A role may be assigned only when the assignment names one concrete work object
and one accountable role. The object is a source artifact, a deployed behavior,
or a specific external dependency; “coordination”, “investigation”,
“verification”, “follow-up”, and “continue the work” are not work objects.
No role receives credit for activity that leaves its assigned object unchanged.

The Leader must not use parallel roles to distribute an unresolved failure.
Before starting another role, the Leader must identify why that role is needed
to change its assigned object. If it cannot do so, it must leave the role
unstarted. A role that cannot act on an object ends without a replacement
handoff.

The only valid dispositions of an unresolved acceptance condition are:
`SUCCESS`, when the condition no longer fails; `FAILURE`, when it still fails;
or `EXTERNAL BLOCK`, naming the external dependency and the person or system
that must change it. “In progress”, “reviewed”, “checked”, “delivered”,
“restarted”, and “awaiting the next stage” are not dispositions.

## 2. Replacement for the Inspector intervention provision

When the Inspector finds repeated handoffs, reports, checks, restarts, test
runs, or role activity without a changed work object, it must treat the cycle
as a failure of responsibility, not as a liveness signal. It must require the
Leader to stop the cycle and retain the original failed condition. It must not
create another report, watch event, correction chain, or role assignment as the
remedy.

The Inspector may not call an outcome “unverified” in a way that hides a known
failure. A known `FAILURE` remains a failure until changed; the absence of a
new check never converts it into an open-ended procedural state.

## 3. Owner enforcement

The owner may terminate a role turn or project process that is producing
procedure without changing an assigned object. Such termination voids the
turn's claimed progress. A terminated turn is neither partial completion nor
evidence of diligence. The Leader and every role remain responsible for the
unresolved condition after termination.

This amendment does not grant the owner, Leader, Inspector, Reviewer, or any
other role authority to prescribe the technical means of repair. The team
chooses those means. It changes only what counts as work, progress, and a
permissible next action.
