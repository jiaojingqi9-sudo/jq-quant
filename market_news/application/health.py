from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from market_news.common import parse_datetime, utcnow


HEALTHY_STATUSES = {"ok", "idle"}


@dataclass(slots=True)
class HealthCheck:
    name: str
    status: str
    status_path: str
    last_update: str | None
    age_seconds: int | None
    max_age_seconds: int
    source_status: str | None
    report_path: str | None
    detail: str
    errors: list[str]
    modules: list[dict[str, object]]


@dataclass(slots=True)
class HealthSnapshot:
    timestamp: str
    overall_status: str
    checks: list[HealthCheck]


@dataclass(slots=True)
class HealthStateWriter:
    status_path: Path
    history_path: Path

    def write(self, snapshot: HealthSnapshot) -> dict[str, object]:
        payload = {
            "timestamp": snapshot.timestamp,
            "overall_status": snapshot.overall_status,
            "checks": [asdict(check) for check in snapshot.checks],
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload


def evaluate_status_files(
    *,
    status_files: list[tuple[str, Path]],
    max_age_seconds: int,
) -> HealthSnapshot:
    checks = [
        _evaluate_single_status(name=name, status_path=status_path, max_age_seconds=max_age_seconds)
        for name, status_path in status_files
    ]
    overall_status = _resolve_overall_status(checks)
    return HealthSnapshot(
        timestamp=utcnow().isoformat(),
        overall_status=overall_status,
        checks=checks,
    )


def render_health_screen(snapshot: HealthSnapshot, status_path: Path | None = None) -> str:
    lines = [
        "Market News Health",
        "==================",
        "",
        f"Updated: {snapshot.timestamp}",
        f"Overall: {snapshot.overall_status}",
        f"Checks: {len(snapshot.checks)}",
        "",
    ]
    for index, check in enumerate(snapshot.checks, start=1):
        lines.append(f"{index}. [{check.status.upper():8}] {check.name}")
        lines.append(f"   detail={check.detail}")
        lines.append(f"   status_file={check.status_path}")
        if check.last_update:
            lines.append(
                f"   last_update={check.last_update} age={check.age_seconds}s max_age={check.max_age_seconds}s"
            )
        if check.source_status:
            lines.append(f"   source_status={check.source_status}")
        if check.report_path:
            lines.append(f"   report={check.report_path}")
        if check.modules:
            module_summary = ", ".join(
                f"{module.get('name', 'module')}={module.get('status', 'unknown')}"
                for module in check.modules
            )
            lines.append(f"   modules={module_summary}")
        if check.errors:
            lines.append(f"   errors={' | '.join(check.errors)}")
    if status_path is not None:
        lines.extend(["", "Health State", "------------", f"Status file: {status_path}"])
    return "\n".join(lines) + "\n"


def exit_code_for(snapshot: HealthSnapshot) -> int:
    if snapshot.overall_status == "ok":
        return 0
    if snapshot.overall_status == "degraded":
        return 1
    return 2


def discover_status_files(candidates: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    active_lines = [
        (name, path) for name, path in candidates if name != "monitor" and path.exists()
    ]
    if active_lines:
        return active_lines

    legacy_line = [(name, path) for name, path in candidates if name == "monitor" and path.exists()]
    if legacy_line:
        return legacy_line

    expected_active_lines = [(name, path) for name, path in candidates if name != "monitor"]
    if expected_active_lines:
        return expected_active_lines
    return [candidates[0]]


def _evaluate_single_status(
    *,
    name: str,
    status_path: Path,
    max_age_seconds: int,
) -> HealthCheck:
    if not status_path.exists():
        return HealthCheck(
            name=name,
            status="missing",
            status_path=str(status_path),
            last_update=None,
            age_seconds=None,
            max_age_seconds=max_age_seconds,
            source_status=None,
            report_path=None,
            detail="status file is missing",
            errors=["missing-status-file"],
            modules=[],
        )

    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return HealthCheck(
            name=name,
            status="error",
            status_path=str(status_path),
            last_update=None,
            age_seconds=None,
            max_age_seconds=max_age_seconds,
            source_status=None,
            report_path=None,
            detail="failed to parse status file",
            errors=[str(exc)],
            modules=[],
        )

    timestamp = payload.get("timestamp")
    source_status = str(payload.get("overall_status") or "unknown")
    errors = [str(item) for item in payload.get("errors", [])]
    artifacts = payload.get("artifacts", {})
    report_path = str(artifacts.get("json_report") or "") or None
    modules = _normalize_modules(payload.get("modules"))
    age_seconds = None

    if timestamp:
        age_seconds = int((utcnow() - parse_datetime(timestamp)).total_seconds())

    if errors:
        return HealthCheck(
            name=name,
            status="error",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail="status file reports an error",
            errors=errors,
            modules=modules,
        )

    bad_modules = [module for module in modules if module.get("status") in {"error", "missing"}]
    degraded_modules = [module for module in modules if module.get("status") in {"degraded", "stale"}]
    if bad_modules:
        names = ", ".join(str(module.get("name", "module")) for module in bad_modules)
        return HealthCheck(
            name=name,
            status="degraded",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail=f"tracked modules are unhealthy: {names}",
            errors=[],
            modules=modules,
        )

    if degraded_modules:
        names = ", ".join(str(module.get("name", "module")) for module in degraded_modules)
        return HealthCheck(
            name=name,
            status="degraded",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail=f"tracked modules are degraded: {names}",
            errors=[],
            modules=modules,
        )

    if source_status not in HEALTHY_STATUSES:
        return HealthCheck(
            name=name,
            status="degraded",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail="upstream line is not healthy",
            errors=[],
            modules=modules,
        )

    if age_seconds is None:
        return HealthCheck(
            name=name,
            status="degraded",
            status_path=str(status_path),
            last_update=None,
            age_seconds=None,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail="status file has no timestamp",
            errors=[],
            modules=modules,
        )

    if age_seconds > max_age_seconds:
        return HealthCheck(
            name=name,
            status="stale",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail="heartbeat is stale",
            errors=[],
            modules=modules,
        )

    if report_path and not Path(report_path).exists():
        return HealthCheck(
            name=name,
            status="degraded",
            status_path=str(status_path),
            last_update=timestamp,
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            source_status=source_status,
            report_path=report_path,
            detail="referenced report artifact is missing",
            errors=[],
            modules=modules,
        )

    return HealthCheck(
        name=name,
        status="ok",
        status_path=str(status_path),
        last_update=timestamp,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        source_status=source_status,
        report_path=report_path,
        detail="heartbeat is fresh",
        errors=[],
        modules=modules,
    )


def _normalize_modules(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    modules = []
    for item in value:
        if not isinstance(item, dict):
            continue
        modules.append(
            {
                "name": str(item.get("name") or "module"),
                "status": str(item.get("status") or "unknown"),
                "detail": str(item.get("detail") or ""),
                "count": item.get("count"),
                "signal_count": item.get("signal_count"),
                "event_count": item.get("event_count"),
                "alert_count": item.get("alert_count"),
            }
        )
    return modules


def _resolve_overall_status(checks: list[HealthCheck]) -> str:
    if not checks:
        return "error"
    statuses = {check.status for check in checks}
    if statuses == {"ok"}:
        return "ok"
    if "error" in statuses or "missing" in statuses:
        return "error"
    return "degraded"
