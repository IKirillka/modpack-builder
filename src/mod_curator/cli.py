from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .engine import Engine
from .lifecycle import CleanupPatch, DEFAULT_STATUSES, JobLifecycle, run_on_startup
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="CurseForge catalogue curation and local MCP")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Register a local destination. No model can register arbitrary folders.")
    init.add_argument("--destination", required=True)
    init.add_argument("--alias", default="mods")
    init.add_argument("--transport", choices=["http", "browser", "cdp"], default="http")
    init.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    init.add_argument("--headed", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--port", type=int, default=8765)
    worker = sub.add_parser("worker")
    worker.add_argument("job_id")
    start = sub.add_parser("start")
    start.add_argument("filters_json", type=Path)
    status = sub.add_parser("status")
    status.add_argument("job_id")
    resume = sub.add_parser("resume")
    resume.add_argument("job_id")
    audit = sub.add_parser("verify", help="Audit saved catalogue pages or a specific Prism instance; never launch Minecraft")
    audit.add_argument("kind", choices=["collection", "prism"])
    audit.add_argument("job_id")
    audit.add_argument("--instance", type=Path)
    audit.add_argument("--log", type=Path)
    extraction = sub.add_parser("extract", help="Schedule/inspect factual extraction; runs only with --run")
    extraction.add_argument("job_id")
    extraction.add_argument("--prepare", action="store_true")
    extraction.add_argument("--run", action="store_true")
    extraction.add_argument("--batches", type=int, default=1)
    extraction.add_argument("--effort", choices=["medium","high"], default="medium")
    ew = sub.add_parser("extract-worker")
    ew.add_argument("job_id")
    ew.add_argument("--batches", type=int, default=1)
    ew.add_argument("--effort", choices=["medium","high"], default="medium")
    economy = sub.add_parser("economy", help="Настройки анализа и предпросмотр расхода")
    economy.add_argument("job_id", nargs="?")
    economy.add_argument("--menu", action="store_true", help="Открыть меню выбора режима")
    economy.add_argument("--preset", choices=["economy","balanced","faster"])
    economy.add_argument("--batches", type=int)
    economy.add_argument("--model", help="Модель первого прохода")
    economy.add_argument("--effort", choices=["low","medium","high"], help="Глубина первого прохода")
    economy.add_argument("--retry-model", help="Модель повторной проверки или inherit")
    economy.add_argument("--retry-effort", choices=["inherit","low","medium","high"], help="Глубина повторной проверки")
    economy.add_argument("--reset", action="store_true")
    economy.add_argument("--preview", action="store_true")
    economy.add_argument("--run", action="store_true")
    economy.add_argument("--run-id")
    economy.add_argument("--stop", action="store_true")
    economy.add_argument("--resume", action="store_true")
    economy_worker = sub.add_parser("economy-worker")
    economy_worker.add_argument("run_id")
    sub.add_parser("jobs")
    opened = sub.add_parser("open", help="Open a job and refresh its last-opened time")
    opened.add_argument("job_id")
    deletion = sub.add_parser("delete", help="Preview deletion or confirm the exact ID")
    deletion.add_argument("job_id")
    deletion.add_argument("--confirm-id")
    deletion.add_argument("--preview-token")
    deletion.add_argument("--permanent", action="store_true", help="Permanently delete a job from trash")
    sub.add_parser("trash", help="List jobs in trash")
    restore = sub.add_parser("restore", help="Restore the original job and its reports")
    restore.add_argument("job_id")
    pin = sub.add_parser("pin", help="Protect a job from automatic cleanup")
    pin.add_argument("job_id")
    pin.add_argument("--off", action="store_true")
    cleanup = sub.add_parser("cleanup", help="Inspect or apply the job retention policy").add_subparsers(
        dest="cleanup_action", required=True)
    cleanup.add_parser("settings")
    cleanup.add_parser("run")
    for action in ("preview", "configure"):
        rule = cleanup.add_parser(action)
        enabled = rule.add_mutually_exclusive_group()
        enabled.add_argument("--enable", dest="enabled", action="store_true", default=argparse.SUPPRESS)
        enabled.add_argument("--disable", dest="enabled", action="store_false", default=argparse.SUPPRESS)
        rule.add_argument("--days", type=int, default=argparse.SUPPRESS)
        rule.add_argument("--statuses", nargs="+", choices=DEFAULT_STATUSES, default=argparse.SUPPRESS)
        retention = rule.add_mutually_exclusive_group()
        retention.add_argument("--purge-after-days", nargs="?", const=7, type=int, default=argparse.SUPPRESS)
        retention.add_argument("--keep-trash", dest="purge_after_days", action="store_const",
                               const=None, default=argparse.SUPPRESS)
        if action == "configure":
            rule.add_argument("--preview-token", required=True)
    args = parser.parse_args()
    lifecycle_commands = {"delete", "trash", "restore", "pin", "cleanup"}
    if args.action in lifecycle_commands:
        try:
            result = lifecycle_command(args, JobLifecycle(Store()))
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action not in ("serve", "worker", "extract-worker", "economy-worker"):
        store = Store()
        if args.action == "open":
            from .composition import Composition
            result = Composition(Engine(store)).info(args.job_id)
            run_on_startup(store)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        run_on_startup(store)
    if args.action == "serve":
        from .server import make_server
        make_server(port=args.port).run(transport="stdio" if args.transport == "stdio" else "streamable-http")
    elif args.action == "worker":
        asyncio.run(Engine().run(args.job_id))
    elif args.action == "init":
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.alias):
            parser.error("Invalid destination alias")
        store = Store()
        path = Path(args.destination).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if store.root == path or store.root.is_relative_to(path) or path.is_relative_to(store.root):
            parser.error("Destination and service state must be separate directory trees")
        config = store.config()
        config["destinations"][args.alias] = str(path)
        config.update(transport=args.transport, cdp_url=args.cdp_url, headless=not args.headed, browser_profile=str(store.root / "browser-profile"))
        target = store.root / "config.json"
        target.write_text(json.dumps(config, indent=2))
        target.chmod(0o600)
        print(json.dumps(config, indent=2))
    elif args.action == "start":
        print(json.dumps(Engine().start(json.loads(args.filters_json.read_text()))))
    elif args.action == "status":
        print(json.dumps(Engine().status(args.job_id), ensure_ascii=False, indent=2))
    elif args.action == "resume":
        print(json.dumps(Engine().resume(args.job_id)))
    elif args.action == "economy-worker":
        from .economy import Economy
        print(json.dumps(Economy(Store()).work(args.run_id), ensure_ascii=False))
    elif args.action == "economy":
        from .economy import Economy, SettingsPatch, interactive
        service = Economy(Store())
        has_model_options = any((args.model,args.effort,args.retry_model,args.retry_effort))
        if has_model_options and (args.menu or args.run_id or args.preview or args.run):
            parser.error('Set worker models first, then start or preview separately')
        if args.menu:
            result = interactive(service.store)
        elif args.run_id:
            result = service.stop(args.run_id) if args.stop else service.resume(args.run_id) if args.resume else service.status(args.run_id)
        elif args.preview or args.run:
            if not args.job_id:
                parser.error("economy --preview/--run requires a job ID")
            if args.preview and args.run:
                parser.error("Choose either --preview or --run")
            result = service.preview(args.job_id, batches=args.batches) if args.preview else service.start(args.job_id, batches=args.batches)
        else:
            values = {key: getattr(args, key) for key in ('preset','batches','model','effort','retry_model','retry_effort')
                      if getattr(args,key) is not None}
            patch = SettingsPatch(**values) if values else None
            result = service.settings(args.job_id, patch, args.reset)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.action == "extract-worker":
        from .extraction_worker import run
        print(json.dumps(run(args.job_id,args.batches,args.effort),ensure_ascii=False))
    elif args.action == "extract":
        from .facts import Facts
        store=Store();facts=Facts(store)
        if args.prepare:
            print(json.dumps(facts.prepare(args.job_id),ensure_ascii=False))
        if args.run:
            from .extraction_worker import launch
            print(json.dumps(launch(store,args.job_id,args.batches,args.effort)))
        else:
            print(json.dumps(facts.progress(args.job_id)))
    elif args.action == "verify":
        from .verification import audit_collection, audit_prism
        if args.kind == "prism" and args.instance is None:
            parser.error("verify prism requires --instance pointing to the imported test instance")
        result = audit_collection(Store(), args.job_id) if args.kind == "collection" else audit_prism(Store(), args.job_id, args.instance, args.log)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("issues") or result.get("errors"):
            raise SystemExit(1)
    else:
        print(json.dumps([{k: d[k] for k in ("id", "status", "stage")} for d in Store().jobs()], indent=2))


def lifecycle_command(args, service):
    if args.action == "delete":
        if args.confirm_id is None and args.preview_token is None:
            return service.preview_delete(args.job_id, permanent=args.permanent)
        if not args.confirm_id or not args.preview_token:
            raise ValueError("Both --confirm-id and --preview-token are required")
        return asyncio.run(service.delete(args.job_id, args.confirm_id, args.preview_token, permanent=args.permanent))
    if args.action == "trash":
        return service.trash()
    if args.action == "restore":
        return service.restore(args.job_id)
    if args.action == "pin":
        return service.pin(args.job_id, not args.off)
    if args.cleanup_action == "settings":
        return service.settings()
    if args.cleanup_action == "run":
        return service.sweep()
    patch = CleanupPatch.model_validate({key: value for key, value in vars(args).items()
                                         if key in CleanupPatch.model_fields})
    if args.cleanup_action == "preview":
        return service.preview_cleanup(patch if patch.model_fields_set else None)
    return service.configure(patch, args.preview_token)
