from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.security.intent_drift import detect_diff_intent_drift, detect_executor_intent_drift


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_root() -> Path:
    override = os.getenv("MMO_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.mmo").expanduser()


@dataclass(slots=True)
class DelegationJobSpec:
    objective: str
    repo_root: str
    files: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    risk: str = "low"
    budget_usd: float = 0.25
    max_minutes: int = 10
    return_format: str = "patch"
    no_network: bool = True
    max_hops: int = 2
    max_jobs_spawned: int = 3
    executor_cmd: str | None = None


@dataclass(slots=True)
class DelegationWorkspacePolicy:
    context_roots: list[Path]
    allow_symlinks: bool = True
    symlink_policy: str = "resolve_and_verify"
    deny_globs: list[str] = field(default_factory=list)


class DelegationGateway:
    """Patch-first delegation gateway using ephemeral git worktrees."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (_state_root() / "delegate")
        self.jobs_dir = self.root / "jobs"
        self.worktrees_dir = self.root / "worktrees"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def submit(self, spec: DelegationJobSpec) -> dict[str, Any]:
        record = self._create_job_record(spec, status="queued")
        self._execute_job(record["job_id"], spec)
        return self.get_job(str(record["job_id"]))

    def submit_async(self, spec: DelegationJobSpec) -> dict[str, Any]:
        record = self._create_job_record(spec, status="queued")
        job_id = str(record["job_id"])
        worker = threading.Thread(target=self._execute_job, args=(job_id, spec), daemon=True)
        worker.start()
        return self.get_job(job_id)

    def read_events(self, job_id: str, *, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        event_path = self.jobs_dir / job_id / "events.jsonl"
        if not event_path.exists():
            return [], offset
        lines = event_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(0, offset)
        rows: list[dict[str, Any]] = []
        for line in lines[start:]:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows, len(lines)

    def wait_for_completion(self, job_id: str, *, timeout_s: int = 300, poll_s: float = 0.25) -> dict[str, Any]:
        deadline = time.time() + max(1, timeout_s)
        while time.time() < deadline:
            record = self.get_job(job_id)
            if str(record.get("status")) in {"completed", "failed"}:
                return record
            time.sleep(max(0.05, poll_s))
        raise TimeoutError(f"job timed out: {job_id}")

    def _create_job_record(self, spec: DelegationJobSpec, *, status: str) -> dict[str, Any]:
        repo_root = Path(spec.repo_root).expanduser().resolve()
        if not repo_root.exists():
            raise ValueError(f"repo root does not exist: {repo_root}")
        policy = self._load_workspace_policy(repo_root)
        self._validate_requested_files(spec, repo_root, policy)

        job_id = f"job-{uuid4().hex[:12]}"
        job_dir = self.jobs_dir / job_id
        artifacts_dir = job_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = self.worktrees_dir / job_id
        source_snapshot = self.worktrees_dir / f"{job_id}_base"
        use_git = (repo_root / ".git").exists()
        execution_mode = "git_worktree" if use_git else "temp_copy"
        base_commit = self._run_git(repo_root, ["rev-parse", "HEAD"]).strip() if use_git else None
        record: dict[str, Any] = {
            "job_id": job_id,
            "status": status,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "spec": asdict(spec),
            "repo_root": str(repo_root),
            "base_commit": base_commit,
            "worktree_path": str(worktree_path),
            "source_snapshot_path": str(source_snapshot),
            "execution_mode": execution_mode,
            "artifacts_dir": str(artifacts_dir),
            "error": None,
            "policy": {
                "context_roots": [str(path) for path in policy.context_roots],
                "allow_symlinks": policy.allow_symlinks,
                "symlink_policy": policy.symlink_policy,
                "deny_globs": policy.deny_globs,
            },
        }
        self._write_json(job_dir / "job.json", record)
        self._append_event(job_id, "submitted", "job submitted", {"status": status})
        return record

    def _execute_job(self, job_id: str, spec: DelegationJobSpec) -> None:
        record = self.get_job(job_id)
        repo_root = Path(str(record["repo_root"]))
        worktree_path = Path(str(record["worktree_path"]))
        source_snapshot = Path(str(record.get("source_snapshot_path", "")))
        execution_mode = str(record.get("execution_mode", "git_worktree"))
        artifacts_dir = Path(str(record["artifacts_dir"]))
        policy = self._load_workspace_policy(repo_root)
        self._set_job_status(job_id, "running")
        self._append_event(job_id, "running", "job execution started", {})

        checks_out: list[dict[str, Any]] = []
        changed_files: list[str] = []
        executor_log = ""
        try:
            self._append_event(
                job_id,
                "worktree",
                "creating isolated workspace",
                {"path": str(worktree_path), "mode": execution_mode},
            )
            if execution_mode == "git_worktree":
                self._run_git(repo_root, ["worktree", "add", "--detach", str(worktree_path), str(record["base_commit"])])
            else:
                self._copy_tree(repo_root, source_snapshot)
                self._copy_tree(source_snapshot, worktree_path)

            if spec.executor_cmd:
                cmd_drift = detect_executor_intent_drift(
                    objective=spec.objective,
                    executor_cmd=spec.executor_cmd,
                    requested_files=spec.files,
                    checks=spec.checks,
                )
                if cmd_drift.drifted:
                    raise RuntimeError(
                        "intent drift detected for executor command "
                        f"(score={cmd_drift.score:.2f}, overlap={cmd_drift.overlap[:6]})"
                    )
                self._append_event(job_id, "executor", "running executor command", {})
                executor_log = self._run_executor(spec.executor_cmd, worktree_path, spec.objective, max_minutes=spec.max_minutes)

            if execution_mode == "git_worktree":
                self._run_git(worktree_path, ["add", "-A"])
                changed_files = [
                    line.strip()
                    for line in self._run_git(worktree_path, ["diff", "--name-only", "--cached"]).splitlines()
                    if line.strip()
                ]
            else:
                changed_files = self._compute_changed_files(source_snapshot, worktree_path)
            self._validate_changed_files(changed_files, spec.files, policy)
            if not spec.files:
                diff_drift = detect_diff_intent_drift(
                    objective=spec.objective,
                    changed_files=changed_files,
                    requested_files=spec.files,
                )
                if diff_drift.drifted:
                    raise RuntimeError(
                        "intent drift detected for changed files "
                        f"(score={diff_drift.score:.2f}, overlap={diff_drift.overlap[:6]})"
                    )
            self._append_event(job_id, "diff", "captured changed files", {"count": len(changed_files)})

            for check_cmd in spec.checks:
                self._append_event(job_id, "check_start", "running check", {"command": check_cmd})
                check_result = self._run_shell(check_cmd, cwd=worktree_path, timeout_s=max(1, spec.max_minutes * 60))
                checks_out.append(check_result)
                self._append_event(
                    job_id,
                    "check_end",
                    "check completed",
                    {"command": check_cmd, "exit_code": int(check_result["exit_code"])},
                )

            if execution_mode == "git_worktree":
                patch_text = self._run_git(worktree_path, ["diff", "--binary", "--cached"])
            else:
                patch_text = self._build_unified_patch(source_snapshot, worktree_path, changed_files)
            (artifacts_dir / "changes.patch").write_text(patch_text, encoding="utf-8")
            (artifacts_dir / "summary.md").write_text(self._summary_markdown(spec, changed_files), encoding="utf-8")
            self._write_json(artifacts_dir / "checks.json", {"checks": checks_out})
            self._write_json(
                artifacts_dir / "risk_report.json",
                {
                    "risk": spec.risk,
                    "no_network": spec.no_network,
                    "max_hops": spec.max_hops,
                    "max_jobs_spawned": spec.max_jobs_spawned,
                    "files_touched": changed_files,
                    "executor_used": bool(spec.executor_cmd),
                },
            )
            if executor_log:
                (artifacts_dir / "executor.log").write_text(executor_log, encoding="utf-8")
            self._set_job_status(job_id, "completed")
            self._append_event(job_id, "completed", "job completed", {"files_touched": changed_files})
        except Exception as exc:
            self._set_job_status(job_id, "failed", error=str(exc))
            self._write_json(artifacts_dir / "checks.json", {"checks": checks_out})
            self._append_event(job_id, "failed", "job failed", {"error": str(exc)})
        finally:
            if execution_mode == "git_worktree":
                self._safe_remove_worktree(repo_root, worktree_path)
            else:
                self._safe_remove_copy_workspace(worktree_path, source_snapshot)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.jobs_dir.glob("job-*/job.json"), reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if len(rows) >= max(1, limit):
                break
        return rows

    def get_job(self, job_id: str) -> dict[str, Any]:
        path = self.jobs_dir / job_id / "job.json"
        if not path.exists():
            raise FileNotFoundError(f"job not found: {job_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def artifacts_path(self, job_id: str) -> Path:
        job = self.get_job(job_id)
        return Path(str(job["artifacts_dir"]))

    def delete_job(self, job_id: str, *, allow_running: bool = False) -> bool:
        job_dir = self.jobs_dir / job_id
        if not job_dir.exists():
            return False
        record: dict[str, Any] = {}
        job_path = job_dir / "job.json"
        if job_path.exists():
            try:
                record = json.loads(job_path.read_text(encoding="utf-8"))
            except Exception:
                record = {}
        status = str(record.get("status", "")).strip().lower()
        if status in {"queued", "running"} and not allow_running:
            raise ValueError(f"cannot delete active job without force: {job_id}")

        worktree_path = Path(str(record.get("worktree_path", ""))).expanduser() if record.get("worktree_path") else None
        source_snapshot_path = (
            Path(str(record.get("source_snapshot_path", ""))).expanduser() if record.get("source_snapshot_path") else None
        )

        shutil.rmtree(job_dir, ignore_errors=True)
        if worktree_path is not None:
            shutil.rmtree(worktree_path, ignore_errors=True)
        if source_snapshot_path is not None:
            shutil.rmtree(source_snapshot_path, ignore_errors=True)
        return True

    def delete_jobs(
        self,
        *,
        older_than_days: int | None = None,
        limit: int = 1000,
        allow_running: bool = False,
    ) -> dict[str, Any]:
        max_limit = max(1, min(int(limit), 5000))
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(older_than_days)))
            if older_than_days is not None
            else None
        )
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []

        for path in sorted(self.jobs_dir.glob("job-*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_dir():
                continue
            job_id = path.name
            job_path = path / "job.json"
            if cutoff is not None:
                ts: datetime | None = None
                if job_path.exists():
                    try:
                        record = json.loads(job_path.read_text(encoding="utf-8"))
                        raw_time = str(record.get("updated_at") or record.get("created_at") or "").strip()
                        if raw_time:
                            ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    except Exception:
                        ts = None
                if ts is None:
                    try:
                        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    except Exception:
                        ts = None
                if ts is None or ts >= cutoff:
                    continue
            try:
                ok = self.delete_job(job_id, allow_running=allow_running)
                if ok:
                    deleted.append(job_id)
            except Exception as exc:
                skipped.append({"job_id": job_id, "reason": str(exc)})
            if len(deleted) >= max_limit:
                break

        return {"deleted": deleted, "skipped": skipped}

    def apply_patch(self, job_id: str, *, check_only: bool = False, to_branch: str | None = None) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job.get("status") != "completed":
            raise ValueError(f"job is not completed: {job_id}")
        execution_mode = str(job.get("execution_mode", "git_worktree"))
        artifacts_dir = Path(str(job["artifacts_dir"]))
        patch_file = artifacts_dir / "changes.patch"
        if not patch_file.exists():
            raise FileNotFoundError(f"patch not found for job: {job_id}")
        repo_root = Path(str(job["repo_root"]))
        patch_text = patch_file.read_text(encoding="utf-8")
        if not patch_text.strip():
            return {"ok": True, "applied": False, "reason": "empty patch"}

        if execution_mode == "git_worktree":
            if to_branch:
                self._run_git(repo_root, ["checkout", "-B", to_branch])
            args = ["apply", "--check"] if check_only else ["apply"]
            proc = subprocess.run(
                ["git", "-C", str(repo_root), *args, str(patch_file)],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            if to_branch:
                raise ValueError("--to-branch is only supported for git repositories")
            args = ["patch", "-p1", "--dry-run"] if check_only else ["patch", "-p1"]
            proc = subprocess.run(
                [*args, "-i", str(patch_file)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
        return {
            "ok": proc.returncode == 0,
            "applied": (proc.returncode == 0 and not check_only),
            "stdout": (proc.stdout or "").strip()[:4000],
            "stderr": (proc.stderr or "").strip()[:4000],
            "repo_root": str(repo_root),
            "patch_file": str(patch_file),
        }

    def _run_executor(self, template: str, workspace: Path, objective: str, *, max_minutes: int) -> str:
        cmd = template.format(workspace=str(workspace), objective=objective)
        out = self._run_shell(cmd, cwd=workspace, timeout_s=max(1, max_minutes * 60))
        if int(out["exit_code"]) != 0:
            raise RuntimeError(f"executor command failed (exit={out['exit_code']}): {cmd}")
        return f"$ {cmd}\n\nstdout:\n{out['stdout']}\n\nstderr:\n{out['stderr']}\n\nexit_code={out['exit_code']}\n"

    def _run_shell(self, cmd: str, *, cwd: Path, timeout_s: int) -> dict[str, Any]:
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        return {
            "command": cmd,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "").strip()[:8000],
            "stderr": (proc.stderr or "").strip()[:8000],
        }

    def _load_workspace_policy(self, repo_root: Path) -> DelegationWorkspacePolicy:
        default = DelegationWorkspacePolicy(context_roots=[repo_root], allow_symlinks=True, symlink_policy="resolve_and_verify", deny_globs=[])
        path = repo_root / ".delegate.yaml"
        if not path.exists():
            return default
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return default
        roots_raw = raw.get("context_roots") if isinstance(raw.get("context_roots"), list) else [str(repo_root)]
        resolved_roots: list[Path] = []
        for item in roots_raw:
            p = Path(str(item)).expanduser()
            if not p.is_absolute():
                p = (repo_root / p).resolve()
            else:
                p = p.resolve()
            resolved_roots.append(p)
        return DelegationWorkspacePolicy(
            context_roots=resolved_roots or [repo_root],
            allow_symlinks=bool(raw.get("allow_symlinks", True)),
            symlink_policy=str(raw.get("symlink_policy", "resolve_and_verify")),
            deny_globs=[str(item) for item in list(raw.get("deny_globs", []))],
        )

    def _validate_requested_files(self, spec: DelegationJobSpec, repo_root: Path, policy: DelegationWorkspacePolicy) -> None:
        for rel_path in spec.files:
            cleaned = rel_path.strip()
            if not cleaned:
                continue
            self._validate_rel_path(cleaned, repo_root, policy)

    def _validate_changed_files(self, changed_files: list[str], allowed_files: list[str], policy: DelegationWorkspacePolicy) -> None:
        for rel_path in changed_files:
            self._enforce_deny_globs(rel_path, policy)
        if not allowed_files:
            return
        allowed = {item.strip() for item in allowed_files if item.strip()}
        unexpected = [path for path in changed_files if path not in allowed]
        if unexpected:
            raise RuntimeError(f"changed files outside requested scope: {unexpected}")

    def _validate_rel_path(self, rel_path: str, repo_root: Path, policy: DelegationWorkspacePolicy) -> None:
        self._enforce_deny_globs(rel_path, policy)
        target = (repo_root / rel_path)
        resolved = target.resolve() if target.exists() else (target.parent.resolve() / target.name)
        if not self._is_within_context_roots(resolved, policy.context_roots):
            raise RuntimeError(f"path outside context roots: {rel_path}")

    def _enforce_deny_globs(self, rel_path: str, policy: DelegationWorkspacePolicy) -> None:
        normalized = rel_path.replace("\\", "/")
        for pattern in policy.deny_globs:
            p = pattern.strip()
            alt = p[3:] if p.startswith("**/") else None
            if fnmatch.fnmatch(normalized, p) or (alt is not None and fnmatch.fnmatch(normalized, alt)):
                raise RuntimeError(f"path denied by policy glob '{pattern}': {rel_path}")

    def _is_within_context_roots(self, path: Path, roots: list[Path]) -> bool:
        resolved = path.resolve()
        for root in roots:
            root_resolved = root.resolve()
            try:
                resolved.relative_to(root_resolved)
                return True
            except ValueError:
                continue
        return False

    def _safe_remove_worktree(self, repo_root: Path, worktree_path: Path) -> None:
        try:
            self._run_git(repo_root, ["worktree", "remove", "--force", str(worktree_path)])
        except Exception:
            pass
        shutil.rmtree(worktree_path, ignore_errors=True)

    def _safe_remove_copy_workspace(self, workspace_path: Path, source_snapshot: Path) -> None:
        shutil.rmtree(workspace_path, ignore_errors=True)
        shutil.rmtree(source_snapshot, ignore_errors=True)

    def _summary_markdown(self, spec: DelegationJobSpec, changed_files: list[str]) -> str:
        touched = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- (none)"
        return (
            f"# Delegation Job Summary\n\n"
            f"Objective: {spec.objective}\n\n"
            f"Risk: {spec.risk}\n\n"
            f"Return format: {spec.return_format}\n\n"
            f"Files touched:\n{touched}\n"
        )

    def _run_git(self, repo: Path, args: list[str]) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip() or f"git {' '.join(args)} failed")
        return proc.stdout

    def _copy_tree(self, src: Path, dst: Path) -> None:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)

        def _ignore(_path: str, names: list[str]) -> set[str]:
            ignored = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"}
            return {name for name in names if name in ignored}

        shutil.copytree(src, dst, ignore=_ignore, symlinks=True)

    def _compute_changed_files(self, before: Path, after: Path) -> list[str]:
        rels: set[str] = set()
        for base in (before, after):
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_dir():
                    continue
                rel = str(path.relative_to(base)).replace("\\", "/")
                rels.add(rel)
        changed: list[str] = []
        for rel in sorted(rels):
            a = before / rel
            b = after / rel
            if not a.exists() or not b.exists():
                changed.append(rel)
                continue
            if a.read_bytes() != b.read_bytes():
                changed.append(rel)
        return changed

    def _build_unified_patch(self, before: Path, after: Path, changed_files: list[str]) -> str:
        chunks: list[str] = []
        for rel in changed_files:
            old_path = before / rel
            new_path = after / rel
            old_text = old_path.read_text(encoding="utf-8", errors="replace").splitlines() if old_path.exists() else []
            new_text = new_path.read_text(encoding="utf-8", errors="replace").splitlines() if new_path.exists() else []
            diff = difflib.unified_diff(
                old_text,
                new_text,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            rendered = "\n".join(list(diff))
            if rendered:
                chunks.append(rendered + "\n")
        return "".join(chunks)

    def _set_job_status(self, job_id: str, status: str, *, error: str | None = None) -> None:
        with self._lock:
            record = self.get_job(job_id)
            record["status"] = status
            record["updated_at"] = _utc_now()
            record["error"] = error
            self._write_json(self.jobs_dir / job_id / "job.json", record)

    def _append_event(self, job_id: str, event_type: str, message: str, payload: dict[str, Any]) -> None:
        path = self.jobs_dir / job_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": _utc_now(),
            "event_type": event_type,
            "message": message,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
