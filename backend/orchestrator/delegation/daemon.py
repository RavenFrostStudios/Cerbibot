from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .gateway import DelegationGateway, DelegationJobSpec


class DelegationBrokerDaemon:
    def __init__(self, gateway: DelegationGateway, socket_path: Path) -> None:
        self.gateway = gateway
        self.socket_path = socket_path
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        async with self.server:
            await self.server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            req = json.loads(raw.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
            op = str(req.get("op", "")).strip()
            if op == "follow":
                await self._stream_follow(writer, req)
                return
            payload = await asyncio.to_thread(self._dispatch, req)
            writer.write((json.dumps({"ok": True, "data": payload}) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as exc:
            writer.write((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _dispatch(self, req: dict[str, Any]) -> Any:
        op = str(req.get("op", "")).strip()
        if op == "submit":
            spec_raw = req.get("spec", {})
            if not isinstance(spec_raw, dict):
                raise ValueError("spec must be an object")
            spec = DelegationJobSpec(**spec_raw)
            async_run = bool(req.get("async_run", True))
            return self.gateway.submit_async(spec) if async_run else self.gateway.submit(spec)
        if op == "list":
            return self.gateway.list_jobs(limit=int(req.get("limit", 20)))
        if op == "show":
            return self.gateway.get_job(str(req.get("job_id", "")))
        if op == "fetch":
            artifacts = self.gateway.artifacts_path(str(req.get("job_id", "")))
            files = [p.name for p in sorted(artifacts.iterdir())] if artifacts.exists() else []
            return {"artifacts_dir": str(artifacts), "files": files}
        if op == "apply":
            return self.gateway.apply_patch(
                str(req.get("job_id", "")),
                check_only=bool(req.get("check_only", False)),
                to_branch=(str(req["to_branch"]) if req.get("to_branch") else None),
            )
        if op == "health":
            return {"status": "ok", "socket": str(self.socket_path)}
        raise ValueError(f"unsupported op: {op}")

    async def _stream_follow(self, writer: asyncio.StreamWriter, req: dict[str, Any]) -> None:
        job_id = str(req.get("job_id", "")).strip()
        if not job_id:
            raise ValueError("job_id required")
        offset = int(req.get("offset", 0))
        poll_s = float(req.get("poll_s", 0.5))
        terminal_seen = False
        while True:
            events, offset = await asyncio.to_thread(self.gateway.read_events, job_id, offset=offset)
            for event in events:
                writer.write((json.dumps({"ok": True, "event": event, "offset": offset}) + "\n").encode("utf-8"))
            await writer.drain()
            job = await asyncio.to_thread(self.gateway.get_job, job_id)
            status = str(job.get("status", ""))
            if status in {"completed", "failed"}:
                if terminal_seen:
                    writer.write((json.dumps({"ok": True, "done": True, "status": status}) + "\n").encode("utf-8"))
                    await writer.drain()
                    return
                terminal_seen = True
            await asyncio.sleep(max(0.1, poll_s))
