"""Tests for S5: CI workflows and GitHub rulesets validation (AC-8, AC-15, NFR-2, NFR-4)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS_DIR = _REPO_ROOT / ".github" / "workflows"
_RULESETS_DIR = _REPO_ROOT / ".github" / "rulesets"


def _read_yaml(path: Path) -> dict[object, object]:
    content = path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    assert isinstance(data, dict), f"Expected YAML dict in {path}"
    return data


def test_pr_workflow_structure_and_security() -> None:
    pr_yml = _WORKFLOWS_DIR / "pr-check.yml"
    assert pr_yml.is_file(), "pr-check.yml must exist"

    data = _read_yaml(pr_yml)

    # 1. Triggers
    on_triggers = data.get("on") if "on" in data else data.get(True)
    assert on_triggers is not None
    assert isinstance(on_triggers, dict)
    assert "pull_request" in on_triggers
    assert "pull_request_target" not in on_triggers

    pr_config = on_triggers["pull_request"]
    assert isinstance(pr_config, dict)
    branches = pr_config.get("branches", [])
    for b in ("develop", "release/**", "master"):
        assert b in branches

    # 2. Permissions: top-level contents: read
    perms = data.get("permissions")
    assert perms == {"contents": "read"}

    # 3. Jobs validation
    jobs = data.get("jobs", {})
    assert isinstance(jobs, dict)

    expected_job_names = [
        "Python Quality & Tests",
        "UI Typecheck & Tests",
        "Conventional Commits",
        "Gitflow Branch Policy",
        "Version Consistency",
        "Build Debian Package",
    ]
    actual_job_names = [j.get("name") for j in jobs.values() if isinstance(j, dict)]

    for exp in expected_job_names:
        assert exp in actual_job_names, f"Expected job name '{exp}' in pr-check.yml"

    # 4. Check Debian artifact upload job
    deb_job = next(
        j for j in jobs.values() if isinstance(j, dict) and j.get("name") == "Build Debian Package"
    )
    steps = deb_job.get("steps", [])
    upload_step = next(
        s for s in steps if isinstance(s, dict) and "upload-artifact" in str(s.get("uses", ""))
    )
    with_args = upload_step.get("with", {})
    assert with_args.get("retention-days") == 7 or with_args.get("retention-days") == "7"

    # 5. Check timeouts, step timeouts, and SHA pinning across all jobs
    sha40_re = re.compile(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+@[0-9a-f]{40}$")
    raw_content = pr_yml.read_text(encoding="utf-8")
    assert "secrets." not in raw_content or "secrets.GITHUB_TOKEN" in raw_content

    # Verified action SHAs
    assert "a26af69be951a213d495a4c3e4e4022e16d87065" in raw_content, (
        "Must use verified setup-python SHA"
    )

    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        assert "timeout-minutes" in job, f"Job {jid} missing timeout-minutes"
        assert job["timeout-minutes"] <= 30
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            assert "timeout-minutes" in step, f"Step in job {jid} missing timeout-minutes: {step}"
            assert step["timeout-minutes"] <= 15
            run_script = str(step.get("run", ""))
            # Anti-script-injection: untrusted context must not be interpolated in run
            for dangerous in (
                "${{ github.event.pull_request.title",
                "${{ github.head_ref",
                "${{ github.base_ref",
                "${{ github.event.pull_request.base.sha",
                "${{ github.event.pull_request.head.sha",
            ):
                assert dangerous not in run_script, (
                    f"Script injection hazard in job {jid}: {dangerous} found in run: {run_script}"
                )
            uses = step.get("uses")
            if uses:
                assert sha40_re.match(uses.split("#")[0].strip()), (
                    f"Action in job {jid} not pinned by 40-char SHA: {uses}"
                )


def test_release_workflow_structure_and_security() -> None:
    rel_yml = _WORKFLOWS_DIR / "release.yml"
    assert rel_yml.is_file(), "release.yml must exist"

    data = _read_yaml(rel_yml)
    raw_content = rel_yml.read_text(encoding="utf-8")
    assert "a26af69be951a213d495a4c3e4e4022e16d87065" in raw_content, (
        "Must use verified setup-python SHA"
    )
    assert "extract-release-notes.py" in raw_content, "Must use extract-release-notes.py script"

    # 1. Triggered on push to master
    on_triggers = data.get("on") if "on" in data else data.get(True)
    assert on_triggers is not None
    assert isinstance(on_triggers, dict)
    assert "push" in on_triggers
    assert on_triggers["push"].get("branches") == ["master"]

    # 2. Concurrency
    concurrency = data.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("cancel-in-progress") is False

    # 3. Top-level permissions: contents: read
    perms = data.get("permissions")
    assert perms == {"contents": "read"}

    # 4. Release job elevates permissions to contents: write
    jobs = data.get("jobs", {})
    assert isinstance(jobs, dict)
    rel_job = next(iter(jobs.values()))
    assert isinstance(rel_job, dict)
    assert rel_job.get("permissions") == {"contents": "write"}
    assert "timeout-minutes" in rel_job
    assert rel_job["timeout-minutes"] <= 30

    for step in rel_job.get("steps", []):
        if not isinstance(step, dict):
            continue
        assert "timeout-minutes" in step, f"Step in release job missing timeout-minutes: {step}"
        run_script = str(step.get("run", ""))
        assert "${{ github.sha" not in run_script, "github.sha should be passed via env"


def test_ruleset_structure_and_status_checks() -> None:
    ruleset_json = _RULESETS_DIR / "gitflow-ruleset.json"
    assert ruleset_json.is_file(), "gitflow-ruleset.json must exist"

    with open(ruleset_json, encoding="utf-8") as f:
        data = json.load(f)

    assert data.get("enforcement") == "active"
    conditions = data.get("conditions", {}).get("ref_name", {}).get("include", [])
    for ref in ("refs/heads/master", "refs/heads/develop", "refs/heads/release/**"):
        assert ref in conditions

    # Collect status checks required by ruleset
    rules = data.get("rules", [])
    checks_rule = next(r for r in rules if r.get("type") == "required_status_checks")
    required_contexts = [
        c["context"] for c in checks_rule.get("parameters", {}).get("required_status_checks", [])
    ]

    # Verify matching jobs in pr-check.yml
    pr_yml = _WORKFLOWS_DIR / "pr-check.yml"
    pr_data = _read_yaml(pr_yml)
    raw_jobs = pr_data.get("jobs", {})
    assert isinstance(raw_jobs, dict)
    pr_jobs = [j.get("name") for j in raw_jobs.values() if isinstance(j, dict)]

    for ctx in required_contexts:
        assert ctx in pr_jobs, f"Ruleset check '{ctx}' has no matching job in pr-check.yml"

    # Bypass actor must be Repository Admin (actor_id 5)
    bypass = data.get("bypass_actors", [])
    assert len(bypass) == 1
    assert bypass[0].get("actor_id") == 5
    assert bypass[0].get("actor_type") == "RepositoryRole"
    assert bypass[0].get("bypass_mode") == "pull_request"


def test_actionlint_if_available() -> None:
    if shutil.which("actionlint"):
        res = subprocess.run(["actionlint"], cwd=_REPO_ROOT, capture_output=True, text=True)
        assert res.returncode == 0, f"actionlint failed:\n{res.stdout}\n{res.stderr}"
