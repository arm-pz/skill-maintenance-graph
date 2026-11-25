"""Tests for the Descale graph spec and deterministic gate (tools/descale-graph/descale_gate.py)."""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "descale-graph"))
import descale_gate

BASE_SHA = "1" * 40
OLD_HEAD = "a" * 40
TUNED_HEAD = "b" * 40
RELEASE_HEAD = "c" * 40
RUN_ID = "20261125-1500"
REPO = "arm-pz/demo-skill"


def with_(base, **changes):
    out = copy.deepcopy(base)
    out.update(changes)
    return out


def good_state(head=TUNED_HEAD):
    return {
        "baseline_sha": BASE_SHA,
        "labels": [],
        "heads": {REPO: head},
        "files": {REPO: ["SKILL.md"]},
        "live_regressions": [],
    }


def good_triage():
    return {
        "run_id": RUN_ID,
        "issue_number": 42,
        "baseline_sha": BASE_SHA,
        "skills_checked": 60,
        "verdict": "regressions_found",
        "target_skills": ["demo-skill"],
        "regressions": [
            {
                "id": "R1",
                "skill": "demo-skill",
                "repo": REPO,
                "kind": "dangling",
                "message": "demo-skill -> missing-helper",
            }
        ],
    }


def good_tune(behavior_changed=False):
    return {
        "run_id": RUN_ID,
        "round": 1,
        "baseline_sha": BASE_SHA,
        "results": [
            {
                "id": "R1",
                "skill": "demo-skill",
                "repo": REPO,
                "branch": f"descale/{RUN_ID}",
                "base_sha": OLD_HEAD,
                "new_head_sha": TUNED_HEAD,
                "status": "fixed",
                "behavior_changed": behavior_changed,
                "files_changed": ["SKILL.md"],
                "commit": "abcdef1",
                "note": "Removed dangling handoff to missing-helper",
            }
        ],
    }


def good_verify():
    return {
        "run_id": RUN_ID,
        "round": 1,
        "baseline_sha": BASE_SHA,
        "verified_heads": {REPO: TUNED_HEAD},
        "regressions_remaining": [],
        "new_regressions": [],
        "check_skill_failures": [],
        "eval_check": {
            "status": "pass",
            "passed": 52,
            "total": 52,
            "newly_failing": [],
            "reason": None,
        },
        "verdict": "pass",
    }


def good_release():
    return {
        "run_id": RUN_ID,
        "verdict": "pass",
        "releases": [
            {
                "skill": "demo-skill",
                "repo": REPO,
                "branch": f"descale/{RUN_ID}",
                "verified_head_sha": TUNED_HEAD,
                "head_sha": RELEASE_HEAD,
                "version": "1.0.1",
                "changelog_updated": True,
                "trigger_cases": {"passed": 4, "failed": 0, "reviewed_only": 0},
                "status": "ready",
                "note": "Bumped patch version and recorded CHANGELOG",
            }
        ],
    }


def good_upstream(head=TUNED_HEAD):
    return {
        "run_id": RUN_ID,
        "repos": [
            {
                "repo": REPO,
                "branch": f"descale/{RUN_ID}",
                "head_sha": head,
                "git_verdict": "push-ready",
                "secret_hits": 0,
                "notes": [],
            }
        ],
        "drift": {"status": "clean", "details": []},
    }


# Each entry is (state, triage, tune, verify, release, upstream) and MUST fail.
KNOWN_BAD = {
    "labelled hold": (
        with_(good_state(), labels=["hold"]),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "labelled hands-off (mixed case)": (
        with_(good_state(), labels=["Hands-Off"]),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "labelled needs-owner": (
        with_(good_state(), labels=["needs-owner"]),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "stale baseline_sha": (
        with_(good_state(), baseline_sha="9" * 40),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "clean triage with live regressions": (
        with_(good_state(), live_regressions=["demo-skill: FAIL missing H1"]),
        with_(good_triage(), verdict="clean", regressions=[], target_skills=[]),
        None, None, None, None,
    ),
    "escalated finding in tune": (
        good_state(),
        good_triage(),
        with_(good_tune(), results=[with_(good_tune()["results"][0], status="escalate")]),
        good_verify(), None, good_upstream(),
    ),
    "round cap exceeded": (
        good_state(),
        good_triage(),
        with_(good_tune(), round=4),
        with_(good_verify(), round=4), None, good_upstream(),
    ),
    "regressions remaining after verify": (
        good_state(),
        good_triage(),
        good_tune(),
        with_(good_verify(), regressions_remaining=["R1"]),
        None, good_upstream(),
    ),
    "new regressions introduced": (
        good_state(),
        good_triage(),
        good_tune(),
        with_(good_verify(), new_regressions=["other-skill: FAIL over 1024 chars"]),
        None, good_upstream(),
    ),
    "check_skill failure": (
        good_state(),
        good_triage(),
        good_tune(),
        with_(good_verify(), check_skill_failures=["demo-skill: missing H1"]),
        None, good_upstream(),
    ),
    "eval regression": (
        good_state(),
        good_triage(),
        good_tune(),
        with_(good_verify(), eval_check={"status": "fail", "passed": 51, "total": 52, "newly_failing": ["demo-case"]}),
        None, good_upstream(),
    ),
    "stale verification head": (
        good_state(),
        good_triage(),
        good_tune(),
        with_(good_verify(), verified_heads={REPO: OLD_HEAD}),
        None, good_upstream(),
    ),
    "live head moved after verify": (
        good_state(head="d" * 40),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "behavior changed without release report": (
        good_state(),
        good_triage(), good_tune(behavior_changed=True), good_verify(), None, good_upstream(),
    ),
    "release did not chain from verified_head_sha": (
        good_state(head=RELEASE_HEAD),
        good_triage(),
        good_tune(behavior_changed=True),
        good_verify(),
        with_(good_release(), releases=[with_(good_release()["releases"][0], verified_head_sha=OLD_HEAD)]),
        good_upstream(head=RELEASE_HEAD),
    ),
    "release has failed trigger case": (
        good_state(head=RELEASE_HEAD),
        good_triage(),
        good_tune(behavior_changed=True),
        good_verify(),
        with_(good_release(), releases=[with_(good_release()["releases"][0], trigger_cases={"passed": 3, "failed": 1, "reviewed_only": 0})]),
        good_upstream(head=RELEASE_HEAD),
    ),
    "upstream diverged": (
        good_state(),
        good_triage(),
        good_tune(),
        good_verify(),
        None,
        with_(good_upstream(), repos=[with_(good_upstream()["repos"][0], git_verdict="diverged")]),
    ),
    "upstream behind": (
        good_state(),
        good_triage(),
        good_tune(),
        good_verify(),
        None,
        with_(good_upstream(), repos=[with_(good_upstream()["repos"][0], git_verdict="behind")]),
    ),
    "upstream secret hit": (
        good_state(),
        good_triage(),
        good_tune(),
        good_verify(),
        None,
        with_(good_upstream(), repos=[with_(good_upstream()["repos"][0], secret_hits=1)]),
    ),
    "touches site/health.json baseline": (
        with_(good_state(), files={REPO: ["SKILL.md"], "arm-pz/skills-site": ["site/health.json"]}),
        good_triage(), good_tune(), good_verify(), None, good_upstream(),
    ),
    "touches workflow file in tune": (
        good_state(),
        good_triage(),
        with_(good_tune(), results=[with_(good_tune()["results"][0], files_changed=[".github/workflows/health.yml"])]),
        good_verify(), None, good_upstream(),
    ),
    "run_id mismatch": (
        good_state(),
        good_triage(),
        with_(good_tune(), run_id="20261125-1501"),
        good_verify(), None, good_upstream(),
    ),
}


class DescaleGateTest(unittest.TestCase):
    def test_graph_spec_is_valid(self):
        graph = json.loads((ROOT / "tools" / "descale-graph" / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual(descale_gate.validate_graph(graph, root=ROOT), [])

    def test_clean_triage_passes_without_tune_or_verify(self):
        triage = with_(good_triage(), verdict="clean", regressions=[], target_skills=[])
        res = descale_gate.evaluate(good_state(head=OLD_HEAD), triage)
        self.assertTrue(res["pass"], res["reasons"])
        self.assertEqual(res["action"], "clean")

    def test_fixed_and_verified_without_behavior_change_passes(self):
        res = descale_gate.evaluate(
            good_state(),
            good_triage(),
            tune=good_tune(behavior_changed=False),
            verify=good_verify(),
            upstream=good_upstream(),
        )
        self.assertTrue(res["pass"], res["reasons"])
        self.assertEqual(res["action"], "ready_to_publish")
        self.assertEqual(res["summary"]["fixed"], 1)

    def test_behavior_changed_with_release_passes(self):
        res = descale_gate.evaluate(
            good_state(head=RELEASE_HEAD),
            good_triage(),
            tune=good_tune(behavior_changed=True),
            verify=good_verify(),
            release=good_release(),
            upstream=good_upstream(head=RELEASE_HEAD),
        )
        self.assertTrue(res["pass"], res["reasons"])
        self.assertEqual(res["action"], "ready_to_publish")
        self.assertEqual(res["summary"]["released"], 1)

    def test_require_evals_blocks_skipped_eval_check(self):
        verify = with_(good_verify(), eval_check={"status": "skipped", "passed": 0, "total": 0, "newly_failing": [], "reason": "no key"})
        allowed = descale_gate.evaluate(
            good_state(), good_triage(), tune=good_tune(), verify=verify, upstream=good_upstream(),
            require_evals=False,
        )
        self.assertTrue(allowed["pass"], allowed["reasons"])
        blocked = descale_gate.evaluate(
            good_state(), good_triage(), tune=good_tune(), verify=verify, upstream=good_upstream(),
            require_evals=True,
        )
        self.assertFalse(blocked["pass"])
        self.assertTrue(any("skipped" in r for r in blocked["reasons"]))

    def test_known_bad_cases_never_pass(self):
        for name, (state, triage, tune, verify, release, upstream) in KNOWN_BAD.items():
            with self.subTest(case=name):
                res = descale_gate.evaluate(
                    state, triage, tune=tune, verify=verify, release=release, upstream=upstream
                )
                self.assertFalse(res["pass"], f"KNOWN_BAD case passed: {name}")
                self.assertEqual(res["action"], "hold")
                self.assertTrue(res["reasons"])

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            state_path = run_dir / "state.json"
            state_path.write_text(json.dumps(good_state()), encoding="utf-8")
            (run_dir / "triage-report.json").write_text(json.dumps(good_triage()), encoding="utf-8")
            (run_dir / "tune-report.json").write_text(json.dumps(good_tune()), encoding="utf-8")
            (run_dir / "verify-report.json").write_text(json.dumps(good_verify()), encoding="utf-8")
            (run_dir / "upstream-report.json").write_text(json.dumps(good_upstream()), encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                code = descale_gate.main(["--run-dir", str(run_dir), "--state", str(state_path)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(buf.getvalue())["pass"])

            # Missing triage report yields exit 2 (gate error).
            (run_dir / "triage-report.json").unlink()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = descale_gate.main(["--run-dir", str(run_dir), "--state", str(state_path)])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
