#!/usr/bin/env python3
"""The Descale graph's deterministic gate: pass-or-hold in Python, not by the model.

    python tools/descale-graph/descale_gate.py --run-dir .descale-graph/runs/<id> --state state.json
    python tools/descale-graph/descale_gate.py --run-dir .descale-graph/runs/<id> --state state.json --require-evals

A Descale run passes only when ALL deterministic invariants hold:
  1. No blocking labels (`hold`, `hands-off`, `needs-owner`) on the health issue / target PRs.
  2. Triage report is valid and matches the live `site/health.json` baseline SHA.
     If triage is `clean`, zero live regressions may exist.
  3. If regressions were found:
     - Tune report is valid, round is 1..3, every regression ID has an entry, and none escalated.
     - Verify report is valid, matches the same round and run, has verdict `pass`, zero remaining
       regressions, zero new regressions, zero check_skill failures, zero eval regressions, and
       matches every fixed repo's `new_head_sha`.
     - If any tuned skill had `behavior_changed: true`, a valid release report must be present
       with verdict `pass`, chained from `verified_head_sha`, changelog updated, and 0 failed
       trigger cases.
     - Upstream report is valid, covers every touched repo, shows `push-ready` or `level`
       (never `behind`, `diverged`, `no-upstream`, `no-remote`, `unreadable`), has 0 secret
       hits, and matches the live head SHA.
     - No touched repo or state file list modifies a protected path (CI, agent, gate, or baseline).

Prints one JSON verdict. Exit 0 = pass, 1 = hold, 2 = error.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
SHORT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{4}(-[0-9]+)?$")
REG_ID = re.compile(r"^R[0-9]+$")

MAX_ROUNDS = 3
BLOCKING_LABELS = {"hold", "hands-off", "needs-owner"}
PROTECTED_PREFIXES = (
    ".github/workflows/",
    ".claude/",
    "tools/descale-graph/",
    "tools/pr-graph/",
)
PROTECTED_FILES = {"CLAUDE.md", "site/health.json", "site/deny.json"}
OK_GIT_VERDICTS = {"push-ready", "level"}
ALL_GIT_VERDICTS = OK_GIT_VERDICTS | {
    "behind",
    "diverged",
    "no-upstream",
    "no-remote",
    "unreadable",
}
REGRESSION_KINDS = {"fail", "warn", "note", "dangling", "collision", "missing"}


class GateError(Exception):
    pass


def is_protected(path):
    norm = str(path).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    return norm in PROTECTED_FILES or norm.startswith(PROTECTED_PREFIXES)


def _is_sha(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _is_nonempty_str(value):
    return isinstance(value, str) and bool(value.strip())


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def check_triage_shape(report):
    problems = []
    if not isinstance(report, dict):
        return ["triage report is not a JSON object"]
    required = ("run_id", "baseline_sha", "skills_checked", "verdict", "regressions", "target_skills")
    for key in required:
        if key not in report:
            problems.append(f"triage is missing `{key}`")
    if problems:
        return problems
    if not isinstance(report["run_id"], str) or not RUN_ID.fullmatch(report["run_id"]):
        problems.append("triage `run_id` is invalid")
    if not _is_sha(report["baseline_sha"]):
        problems.append("triage `baseline_sha` is not a full 40-character SHA")
    if not isinstance(report["skills_checked"], int) or isinstance(report["skills_checked"], bool) or report["skills_checked"] < 0:
        problems.append("triage `skills_checked` must be a non-negative integer")
    if report["verdict"] not in ("clean", "regressions_found"):
        problems.append("triage `verdict` must be clean or regressions_found")
    if not _is_str_list(report["target_skills"]):
        problems.append("triage `target_skills` must be a list of strings")
    regs = report["regressions"]
    if not isinstance(regs, list):
        problems.append("triage `regressions` must be a list")
    else:
        for i, item in enumerate(regs):
            if not isinstance(item, dict):
                problems.append(f"triage regression {i} is not an object")
                continue
            for k in ("id", "skill", "repo", "kind", "message"):
                if k not in item:
                    problems.append(f"triage regression {i} missing `{k}`")
            if "id" in item and (not isinstance(item["id"], str) or not REG_ID.fullmatch(item["id"])):
                problems.append(f"triage regression {i} has invalid `id`")
            if "skill" in item and not _is_nonempty_str(item["skill"]):
                problems.append(f"triage regression {i} has invalid `skill`")
            if "repo" in item and item["repo"] is not None and (
                not isinstance(item["repo"], str) or not REPO_NAME.fullmatch(item["repo"])
            ):
                problems.append(f"triage regression {i} has invalid `repo`")
            if "kind" in item and item["kind"] not in REGRESSION_KINDS:
                problems.append(f"triage regression {i} has invalid `kind`")
            if "message" in item and not _is_nonempty_str(item["message"]):
                problems.append(f"triage regression {i} has empty `message`")
    return problems


def check_tune_shape(report):
    problems = []
    if not isinstance(report, dict):
        return ["tune report is not a JSON object"]
    for key in ("run_id", "round", "baseline_sha", "results"):
        if key not in report:
            problems.append(f"tune is missing `{key}`")
    if problems:
        return problems
    if not isinstance(report["run_id"], str) or not RUN_ID.fullmatch(report["run_id"]):
        problems.append("tune `run_id` is invalid")
    rnd = report["round"]
    if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
        problems.append("tune `round` must be a positive integer")
    if not _is_sha(report["baseline_sha"]):
        problems.append("tune `baseline_sha` is not a full 40-character SHA")
    results = report["results"]
    if not isinstance(results, list) or not results:
        problems.append("tune `results` must be a non-empty list")
        return problems
    req = (
        "id", "skill", "repo", "branch", "base_sha", "new_head_sha",
        "status", "behavior_changed", "files_changed", "commit", "note",
    )
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            problems.append(f"tune result {i} is not an object")
            continue
        for k in req:
            if k not in item:
                problems.append(f"tune result {i} missing `{k}`")
        if "id" in item and (not isinstance(item["id"], str) or not REG_ID.fullmatch(item["id"])):
            problems.append(f"tune result {i} has invalid `id`")
        if "skill" in item and not _is_nonempty_str(item["skill"]):
            problems.append(f"tune result {i} has invalid `skill`")
        if "repo" in item and (not isinstance(item["repo"], str) or not REPO_NAME.fullmatch(item["repo"])):
            problems.append(f"tune result {i} has invalid `repo`")
        if "branch" in item and not _is_nonempty_str(item["branch"]):
            problems.append(f"tune result {i} has invalid `branch`")
        if "base_sha" in item and not _is_sha(item["base_sha"]):
            problems.append(f"tune result {i} has invalid `base_sha`")
        if "new_head_sha" in item and not _is_sha(item["new_head_sha"]):
            problems.append(f"tune result {i} has invalid `new_head_sha`")
        if "status" in item and item["status"] not in ("fixed", "wont_fix", "escalate"):
            problems.append(f"tune result {i} has invalid `status`")
        if "behavior_changed" in item and not isinstance(item["behavior_changed"], bool):
            problems.append(f"tune result {i} `behavior_changed` must be boolean")
        if "files_changed" in item and not _is_str_list(item["files_changed"]):
            problems.append(f"tune result {i} `files_changed` must be a list of strings")
        if "commit" in item and item["commit"] is not None and (
            not isinstance(item["commit"], str) or not SHORT_SHA.fullmatch(item["commit"])
        ):
            problems.append(f"tune result {i} has invalid `commit`")
        if "note" in item and not _is_nonempty_str(item["note"]):
            problems.append(f"tune result {i} has empty `note`")
    return problems


def check_verify_shape(report):
    problems = []
    if not isinstance(report, dict):
        return ["verify report is not a JSON object"]
    required = (
        "run_id", "round", "baseline_sha", "verified_heads",
        "regressions_remaining", "new_regressions", "check_skill_failures",
        "eval_check", "verdict",
    )
    for key in required:
        if key not in report:
            problems.append(f"verify is missing `{key}`")
    if problems:
        return problems
    if not isinstance(report["run_id"], str) or not RUN_ID.fullmatch(report["run_id"]):
        problems.append("verify `run_id` is invalid")
    rnd = report["round"]
    if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
        problems.append("verify `round` must be a positive integer")
    if not _is_sha(report["baseline_sha"]):
        problems.append("verify `baseline_sha` is not a full 40-character SHA")
    heads = report["verified_heads"]
    if not isinstance(heads, dict) or not all(
        isinstance(k, str) and REPO_NAME.fullmatch(k) and _is_sha(v) for k, v in heads.items()
    ):
        problems.append("verify `verified_heads` must map owner/repo to 40-char SHA")
    for field in ("regressions_remaining", "new_regressions", "check_skill_failures"):
        if not _is_str_list(report[field]):
            problems.append(f"verify `{field}` must be a list of strings")
    ev = report["eval_check"]
    if not isinstance(ev, dict):
        problems.append("verify `eval_check` must be an object")
    else:
        for k in ("status", "passed", "total", "newly_failing"):
            if k not in ev:
                problems.append(f"verify `eval_check` missing `{k}`")
        if "status" in ev and ev["status"] not in ("pass", "fail", "skipped"):
            problems.append("verify `eval_check.status` must be pass, fail, or skipped")
        for count_key in ("passed", "total"):
            val = ev.get(count_key)
            if count_key in ev and (not isinstance(val, int) or isinstance(val, bool) or val < 0):
                problems.append(f"verify `eval_check.{count_key}` must be non-negative integer")
        if "newly_failing" in ev and not _is_str_list(ev["newly_failing"]):
            problems.append("verify `eval_check.newly_failing` must be a list of strings")
    if report["verdict"] not in ("pass", "fail"):
        problems.append("verify `verdict` must be pass or fail")
    return problems


def check_release_shape(report):
    problems = []
    if not isinstance(report, dict):
        return ["release report is not a JSON object"]
    for key in ("run_id", "verdict", "releases"):
        if key not in report:
            problems.append(f"release is missing `{key}`")
    if problems:
        return problems
    if not isinstance(report["run_id"], str) or not RUN_ID.fullmatch(report["run_id"]):
        problems.append("release `run_id` is invalid")
    if report["verdict"] not in ("pass", "fail"):
        problems.append("release `verdict` must be pass or fail")
    rels = report["releases"]
    if not isinstance(rels, list):
        problems.append("release `releases` must be a list")
        return problems
    req = (
        "skill", "repo", "branch", "verified_head_sha", "head_sha",
        "version", "changelog_updated", "trigger_cases", "status", "note",
    )
    for i, item in enumerate(rels):
        if not isinstance(item, dict):
            problems.append(f"release entry {i} is not an object")
            continue
        for k in req:
            if k not in item:
                problems.append(f"release entry {i} missing `{k}`")
        if "skill" in item and not _is_nonempty_str(item["skill"]):
            problems.append(f"release entry {i} has invalid `skill`")
        if "repo" in item and (not isinstance(item["repo"], str) or not REPO_NAME.fullmatch(item["repo"])):
            problems.append(f"release entry {i} has invalid `repo`")
        if "branch" in item and not _is_nonempty_str(item["branch"]):
            problems.append(f"release entry {i} has invalid `branch`")
        if "verified_head_sha" in item and not _is_sha(item["verified_head_sha"]):
            problems.append(f"release entry {i} has invalid `verified_head_sha`")
        if "head_sha" in item and not _is_sha(item["head_sha"]):
            problems.append(f"release entry {i} has invalid `head_sha`")
        if "version" in item and not _is_nonempty_str(item["version"]):
            problems.append(f"release entry {i} has invalid `version`")
        if "changelog_updated" in item and not isinstance(item["changelog_updated"], bool):
            problems.append(f"release entry {i} `changelog_updated` must be boolean")
        if "status" in item and item["status"] not in ("ready", "blocked"):
            problems.append(f"release entry {i} has invalid `status`")
        if "note" in item and not _is_nonempty_str(item["note"]):
            problems.append(f"release entry {i} has empty `note`")
        tc = item.get("trigger_cases")
        if "trigger_cases" in item:
            if not isinstance(tc, dict):
                problems.append(f"release entry {i} `trigger_cases` must be an object")
            else:
                for k in ("passed", "failed", "reviewed_only"):
                    v = tc.get(k)
                    if k not in tc or not isinstance(v, int) or isinstance(v, bool) or v < 0:
                        problems.append(f"release entry {i} `trigger_cases.{k}` must be non-negative integer")
    return problems


def check_upstream_shape(report):
    problems = []
    if not isinstance(report, dict):
        return ["upstream report is not a JSON object"]
    for key in ("run_id", "repos", "drift"):
        if key not in report:
            problems.append(f"upstream is missing `{key}`")
    if problems:
        return problems
    if not isinstance(report["run_id"], str) or not RUN_ID.fullmatch(report["run_id"]):
        problems.append("upstream `run_id` is invalid")
    repos = report["repos"]
    if not isinstance(repos, list):
        problems.append("upstream `repos` must be a list")
    else:
        for i, item in enumerate(repos):
            if not isinstance(item, dict):
                problems.append(f"upstream repo {i} is not an object")
                continue
            for k in ("repo", "branch", "head_sha", "git_verdict", "secret_hits", "notes"):
                if k not in item:
                    problems.append(f"upstream repo {i} missing `{k}`")
            if "repo" in item and (not isinstance(item["repo"], str) or not REPO_NAME.fullmatch(item["repo"])):
                problems.append(f"upstream repo {i} has invalid `repo`")
            if "branch" in item and not _is_nonempty_str(item["branch"]):
                problems.append(f"upstream repo {i} has invalid `branch`")
            if "head_sha" in item and not _is_sha(item["head_sha"]):
                problems.append(f"upstream repo {i} has invalid `head_sha`")
            if "git_verdict" in item and item["git_verdict"] not in ALL_GIT_VERDICTS:
                problems.append(f"upstream repo {i} has invalid `git_verdict`")
            hits = item.get("secret_hits")
            if "secret_hits" in item and (not isinstance(hits, int) or isinstance(hits, bool) or hits < 0):
                problems.append(f"upstream repo {i} `secret_hits` must be non-negative integer")
            if "notes" in item and not _is_str_list(item["notes"]):
                problems.append(f"upstream repo {i} `notes` must be a list of strings")
    drift = report["drift"]
    if not isinstance(drift, dict):
        problems.append("upstream `drift` must be an object")
    else:
        if drift.get("status") not in ("clean", "drift_detected", "inconclusive"):
            problems.append("upstream `drift.status` must be clean, drift_detected, or inconclusive")
        if not _is_str_list(drift.get("details")):
            problems.append("upstream `drift.details` must be a list of strings")
    return problems


def validate_graph(graph, root=None):
    """Validate a declarative Descale graph.json structure."""
    problems = []
    if not isinstance(graph, dict):
        return ["graph is not a JSON object"]
    for key in ("id", "nodes", "edges", "limits", "protected_paths"):
        if key not in graph:
            problems.append(f"graph missing `{key}`")
    if problems:
        return problems
    if graph["limits"].get("max_rounds") != MAX_ROUNDS:
        problems.append(f"graph `limits.max_rounds` must be {MAX_ROUNDS}")
    node_ids = set()
    for node in graph["nodes"]:
        nid = node.get("id")
        if not nid or nid in node_ids:
            problems.append(f"duplicate or missing node id: {nid!r}")
        else:
            node_ids.add(nid)
        out = node.get("output")
        if out and root is not None:
            schema = Path(root) / out["schema"]
            if not schema.is_file():
                problems.append(f"missing schema file: {out['schema']}")
    valid_targets = node_ids | {"halt_needs_owner"}
    has_loop = False
    for edge in graph["edges"]:
        if edge.get("from") not in node_ids:
            problems.append(f"edge `from` unknown node: {edge.get('from')!r}")
        if edge.get("to") not in valid_targets:
            problems.append(f"edge `to` unknown target: {edge.get('to')!r}")
        if edge.get("loop_back"):
            has_loop = True
    if not has_loop:
        problems.append("graph must have at least one `loop_back` edge")
    if "gate" not in node_ids:
        problems.append("graph must include a `gate` node")
    return problems


def evaluate(state, triage, tune=None, verify=None, release=None, upstream=None, require_evals=False):
    """Decide pass or hold from plain data so the gate can be tested without GitHub or an agent.

    state: {
      "baseline_sha": 40-char SHA of site/health.json,
      "labels": [str, ...],                     # live labels on the health issue / target PRs
      "heads": {repo: 40-char SHA, ...},        # current head SHA of each touched repo branch
      "files": {repo: [path, ...], ...},        # changed files per touched repo
      "live_regressions": [str, ...],           # optional current regress.py failures
    }
    Returns {"pass": bool, "action": "clean"|"ready_to_publish"|"hold", "reasons": [...], "summary": {...}}.
    """
    reasons = []
    for label in sorted(BLOCKING_LABELS & {str(x).casefold() for x in state.get("labels", [])}):
        reasons.append(f"labelled `{label}`")

    triage_problems = check_triage_shape(triage)
    reasons.extend(triage_problems)
    if triage_problems:
        return {"pass": False, "action": "hold", "reasons": reasons, "summary": {}}

    if state.get("baseline_sha") and triage["baseline_sha"] != state["baseline_sha"]:
        reasons.append("stale triage: site/health.json baseline changed")

    if triage["verdict"] == "clean":
        if triage["regressions"]:
            reasons.append("triage verdict is clean but lists regressions")
        if state.get("live_regressions"):
            reasons.append("triage says clean but live regressions exist")
        passed = not reasons
        return {
            "pass": passed,
            "action": "clean" if passed else "hold",
            "reasons": reasons,
            "summary": {"run_id": triage["run_id"], "skills_checked": triage["skills_checked"], "regressions": 0},
        }

    # Regressions were found: require tune, verify, upstream (and release if behavior_changed).
    if not triage["regressions"]:
        reasons.append("triage verdict is regressions_found but regressions list is empty")

    if tune is None:
        reasons.append("missing tune report")
    else:
        reasons.extend(check_tune_shape(tune))
    if verify is None:
        reasons.append("missing verify report")
    else:
        reasons.extend(check_verify_shape(verify))
    if upstream is None:
        reasons.append("missing upstream report")
    else:
        reasons.extend(check_upstream_shape(upstream))

    if reasons:
        return {"pass": False, "action": "hold", "reasons": reasons, "summary": {"run_id": triage["run_id"]}}

    run_id = triage["run_id"]
    baseline_sha = triage["baseline_sha"]
    for name, rep in (("tune", tune), ("verify", verify), ("upstream", upstream)):
        if rep["run_id"] != run_id:
            reasons.append(f"{name} run_id `{rep['run_id']}` does not match triage `{run_id}`")
    for name, rep in (("tune", tune), ("verify", verify)):
        if rep["baseline_sha"] != baseline_sha:
            reasons.append(f"{name} baseline_sha does not match triage")

    if tune["round"] > MAX_ROUNDS:
        reasons.append(f"round cap exceeded: round {tune['round']} > {MAX_ROUNDS}")
    if verify["round"] != tune["round"]:
        reasons.append(f"verify round {verify['round']} does not match tune round {tune['round']}")

    # Every regression from triage must be accounted for in tune.
    triage_ids = {r["id"] for r in triage["regressions"]}
    tune_by_id = {r["id"]: r for r in tune["results"]}
    missing_ids = sorted(triage_ids - set(tune_by_id))
    if missing_ids:
        reasons.append("regressions missing from tune report: " + ", ".join(missing_ids))

    escalated = [r["id"] for r in tune["results"] if r["status"] == "escalate"]
    if escalated:
        reasons.append("escalated to owner: " + ", ".join(escalated))

    fixed = [r for r in tune["results"] if r["status"] == "fixed"]
    touched_repos = {}
    behavior_changed = []
    for r in fixed:
        repo = r["repo"]
        if r["commit"] is None:
            reasons.append(f"{r['id']} marked fixed without a commit")
        if r["new_head_sha"] == r["base_sha"]:
            reasons.append(f"{r['id']} marked fixed but new_head_sha equals base_sha")
        for path in r["files_changed"]:
            if is_protected(path):
                reasons.append(f"{r['id']} touches protected path `{path}` (human only)")
        if repo in touched_repos and touched_repos[repo] != r["new_head_sha"]:
            reasons.append(f"conflicting new_head_sha for `{repo}` in tune report")
        touched_repos[repo] = r["new_head_sha"]
        if r["behavior_changed"]:
            behavior_changed.append(r)

    # Check live state changed files against protected paths too (defense in depth).
    for repo, paths in state.get("files", {}).items():
        for path in paths:
            if is_protected(path):
                reasons.append(f"`{repo}` touches protected path `{path}` (human only)")

    # Verify invariants.
    if verify["verdict"] != "pass":
        reasons.append("verify verdict is fail")
    if verify["regressions_remaining"]:
        reasons.append("regressions remaining: " + ", ".join(verify["regressions_remaining"]))
    if verify["new_regressions"]:
        reasons.append("new regressions introduced: " + ", ".join(verify["new_regressions"]))
    if verify["check_skill_failures"]:
        reasons.append("check_skill failures: " + ", ".join(verify["check_skill_failures"]))
    if verify["eval_check"]["status"] == "fail" or verify["eval_check"]["newly_failing"]:
        failing = verify["eval_check"]["newly_failing"] or ["eval suite failed"]
        reasons.append("eval regressions: " + ", ".join(failing))
    elif require_evals and verify["eval_check"]["status"] == "skipped":
        reasons.append("evals required but eval_check was skipped")

    for repo, tuned_sha in touched_repos.items():
        verified_sha = verify["verified_heads"].get(repo)
        if verified_sha != tuned_sha:
            reasons.append(f"stale verification for `{repo}`: tune head {tuned_sha[:7]}, verified {str(verified_sha)[:7]}")

    # Release invariants (required when behavior_changed is true).
    expected_final_heads = dict(touched_repos)
    if behavior_changed or release is not None:
        if release is None:
            reasons.append("missing release report for behavior-changed skill(s)")
        else:
            rel_problems = check_release_shape(release)
            reasons.extend(rel_problems)
            if not rel_problems:
                if release["run_id"] != run_id:
                    reasons.append(f"release run_id `{release['run_id']}` does not match triage `{run_id}`")
                if release["verdict"] != "pass":
                    reasons.append("release verdict is fail")
                rel_by_key = {(e["skill"], e["repo"]): e for e in release["releases"]}
                for r in behavior_changed:
                    entry = rel_by_key.get((r["skill"], r["repo"]))
                    if entry is None:
                        reasons.append(f"missing release entry for behavior-changed skill `{r['skill']}`")
                        continue
                    if entry["verified_head_sha"] != verify["verified_heads"].get(r["repo"]):
                        reasons.append(f"release for `{r['skill']}` did not chain from verified_head_sha")
                    if entry["status"] != "ready":
                        reasons.append(f"release for `{r['skill']}` is `{entry['status']}`")
                    if not entry["changelog_updated"]:
                        reasons.append(f"release for `{r['skill']}` did not update CHANGELOG")
                    if entry["trigger_cases"]["failed"] > 0:
                        reasons.append(f"release for `{r['skill']}` has failed trigger cases")
                    expected_final_heads[r["repo"]] = entry["head_sha"]

    # Upstream & live head freshness invariants.
    upstream_by_repo = {u["repo"]: u for u in upstream["repos"]}
    for repo, final_sha in expected_final_heads.items():
        up = upstream_by_repo.get(repo)
        if up is None:
            reasons.append(f"missing upstream check entry for `{repo}`")
            continue
        if up["head_sha"] != final_sha:
            reasons.append(f"stale upstream report for `{repo}`: expected {final_sha[:7]}, saw {up['head_sha'][:7]}")
        if up["git_verdict"] not in OK_GIT_VERDICTS:
            reasons.append(f"`{repo}` git state is `{up['git_verdict']}`")
        if up["secret_hits"] > 0:
            reasons.append(f"`{repo}` has {up['secret_hits']} secret hit(s) in outgoing diff")
        live_sha = state.get("heads", {}).get(repo)
        if live_sha is not None and live_sha != final_sha:
            reasons.append(f"stale run for `{repo}`: live head {live_sha[:7]}, expected {final_sha[:7]}")

    for up in upstream["repos"]:
        if up["git_verdict"] not in OK_GIT_VERDICTS and up["repo"] not in expected_final_heads:
            reasons.append(f"`{up['repo']}` git state is `{up['git_verdict']}`")
        if up["secret_hits"] > 0 and up["repo"] not in expected_final_heads:
            reasons.append(f"`{up['repo']}` has {up['secret_hits']} secret hit(s) in outgoing diff")

    passed = not reasons
    return {
        "pass": passed,
        "action": "ready_to_publish" if passed else "hold",
        "reasons": reasons,
        "summary": {
            "run_id": run_id,
            "round": tune["round"],
            "fixed": len(fixed),
            "wont_fix": sum(1 for r in tune["results"] if r["status"] == "wont_fix"),
            "escalated": len(escalated),
            "released": len(release["releases"]) if release and isinstance(release.get("releases"), list) else 0,
            "repos": sorted(expected_final_heads),
        },
    }


def _load_optional(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise GateError(f"cannot read {path}: {err}") from err


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", required=True, type=Path, help="directory holding node JSON reports")
    parser.add_argument("--state", required=True, type=Path, help="JSON file describing live state (baseline_sha, labels, heads, files)")
    parser.add_argument("--require-evals", action="store_true", help="reject runs where eval_check.status is skipped")
    args = parser.parse_args(argv)

    try:
        state = _load_optional(args.state)
        if state is None:
            raise GateError(f"missing state file: {args.state}")
        triage = _load_optional(args.run_dir / "triage-report.json")
        if triage is None:
            raise GateError(f"missing triage report: {args.run_dir / 'triage-report.json'}")
        tune = _load_optional(args.run_dir / "tune-report.json")
        verify = _load_optional(args.run_dir / "verify-report.json")
        release = _load_optional(args.run_dir / "release-report.json")
        upstream = _load_optional(args.run_dir / "upstream-report.json")
        verdict = evaluate(
            state,
            triage,
            tune=tune,
            verify=verify,
            release=release,
            upstream=upstream,
            require_evals=args.require_evals,
        )
    except GateError as err:
        print(json.dumps({"pass": False, "action": "error", "reasons": [str(err)]}))
        return 2

    print(json.dumps(verdict, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
