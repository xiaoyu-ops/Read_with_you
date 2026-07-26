"""Read-only Docker/runtime footprint audit for the default Pet deployment.

The script never builds, starts, stops, or removes containers. It inspects the
images and containers that already exist, measures the configured data
directory, and optionally merges browser timing metrics.

Examples:
  python scripts/audit_runtime_footprint.py
  python scripts/audit_runtime_footprint.py --experience output/pet-timings.json
  python scripts/audit_runtime_footprint.py --enforce
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "output" / "runtime-footprint.json"
DEFAULT_DATA_DIR = ROOT / "data"

MIB = 1024**2
GIB = 1024**3

DEFAULT_BUDGETS = {
    "images": {
        "backend": 800 * MIB,
        "frontend": 400 * MIB,
        "nginx": 80 * MIB,
        "default_total": int(1.3 * GIB),
    },
    "runtime": {"idle_memory_total": 250 * MIB},
    "experience": {
        "first_readable_page_ms": 2500,
        "selection_translation_regression_ratio": 1.10,
        "pet_first_sse_regression_ratio": 1.10,
        "agent_first_sse_regression_ratio": 1.10,
    },
}

EXPERIENCE_FIELDS = (
    "startup_ms",
    "first_readable_page_ms",
    "selection_translation_ms",
    "pet_first_sse_ms",
    "agent_first_sse_ms",
)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def parse_size(value: str) -> int:
    """Parse Docker-style sizes such as ``517.5MiB`` or ``1.2 GB``."""

    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b|b)\s*",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"unsupported size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).casefold()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": MIB,
        "gib": GIB,
        "tib": 1024**4,
    }
    return int(number * multipliers[unit])


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _compose_config() -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    result = _run(["docker", "compose", "config", "--format", "json"])
    if result.returncode:
        return ROOT.name.replace("_", ""), {}, [result.stderr.strip() or "compose_config_failed"]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ROOT.name.replace("_", ""), {}, ["compose_config_invalid_json"]
    project = str(payload.get("name") or ROOT.name).strip()
    services = payload.get("services")
    if not isinstance(services, dict):
        return project, {}, ["compose_services_missing"]
    return project, services, []


def _default_service_images(
    project: str,
    services: dict[str, dict[str, Any]],
) -> dict[str, str]:
    references: dict[str, str] = {}
    for name in ("backend", "frontend", "nginx"):
        service = services.get(name)
        if not isinstance(service, dict):
            continue
        profiles = service.get("profiles")
        if profiles:
            continue
        references[name] = str(service.get("image") or f"{project}-{name}")
    return references


def _inspect_image(reference: str) -> dict[str, Any]:
    inspect = _run(["docker", "image", "inspect", reference])
    if inspect.returncode:
        return {
            "reference": reference,
            "available": False,
            "error": (inspect.stderr or inspect.stdout).strip()[:500],
        }
    try:
        payload = json.loads(inspect.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        return {"reference": reference, "available": False, "error": "invalid_image_inspect"}
    history = _run(
        [
            "docker",
            "history",
            "--no-trunc",
            "--format",
            "{{json .}}",
            reference,
        ]
    )
    layers: list[dict[str, Any]] = []
    if history.returncode == 0:
        for line in history.stdout.splitlines():
            try:
                item = json.loads(line)
                size_text = str(item.get("Size") or "0B")
                size_bytes = parse_size(size_text)
            except (json.JSONDecodeError, ValueError):
                continue
            if size_bytes <= 0:
                continue
            layers.append(
                {
                    "size_bytes": size_bytes,
                    "created_by": str(item.get("CreatedBy") or "")[:500],
                }
            )
    layers.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
    return {
        "reference": reference,
        "available": True,
        "id": str(payload.get("Id") or ""),
        "size_bytes": int(payload.get("Size") or 0),
        "top_layers": layers[:12],
    }


def _inspect_running_containers(service_names: list[str]) -> dict[str, Any]:
    containers: dict[str, Any] = {}
    ids: list[str] = []
    id_to_service: dict[str, str] = {}
    for service in service_names:
        result = _run(["docker", "compose", "ps", "-q", service])
        container_id = result.stdout.strip() if result.returncode == 0 else ""
        if not container_id:
            containers[service] = {"running": False}
            continue
        ids.append(container_id)
        id_to_service[container_id] = service
        containers[service] = {"running": True, "container_id": container_id}
    if not ids:
        return containers
    stats = _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *ids,
        ]
    )
    if stats.returncode:
        for container in containers.values():
            if container.get("running"):
                container["stats_error"] = stats.stderr.strip()[:500]
        return containers
    for line in stats.stdout.splitlines():
        try:
            item = json.loads(line)
            container_id = str(item.get("ID") or "")
            memory_usage = str(item.get("MemUsage") or "").split("/", 1)[0].strip()
            memory_bytes = parse_size(memory_usage)
        except (json.JSONDecodeError, ValueError):
            continue
        service = id_to_service.get(container_id)
        if service is None:
            service = next(
                (
                    candidate
                    for candidate_id, candidate in id_to_service.items()
                    if candidate_id.startswith(container_id) or container_id.startswith(candidate_id)
                ),
                None,
            )
        if service:
            containers[service]["memory_bytes"] = memory_bytes
            containers[service]["cpu_percent"] = str(item.get("CPUPerc") or "")
            containers[service]["memory_source"] = "docker_stats"
    for service, container in containers.items():
        if not container.get("running") or "memory_bytes" in container:
            continue
        memory_bytes = _cgroup_memory_bytes(str(container["container_id"]))
        if memory_bytes is not None:
            container["memory_bytes"] = memory_bytes
            container["memory_source"] = "cgroup_current_minus_inactive_file"
        else:
            container["stats_error"] = "docker_stats_and_cgroup_unavailable"
    return containers


def _cgroup_memory_bytes(container_id: str) -> int | None:
    current = _run(
        ["docker", "exec", container_id, "cat", "/sys/fs/cgroup/memory.current"]
    )
    stats = _run(
        ["docker", "exec", container_id, "cat", "/sys/fs/cgroup/memory.stat"]
    )
    if current.returncode or stats.returncode:
        return None
    try:
        current_bytes = int(current.stdout.strip())
        inactive_file = next(
            int(line.split()[1])
            for line in stats.stdout.splitlines()
            if line.startswith("inactive_file ")
        )
    except (ValueError, IndexError, StopIteration):
        return None
    return max(0, current_bytes - inactive_file)


def _load_experience(path: Path | None) -> tuple[dict[str, float], list[str]]:
    if path is None:
        return {}, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"experience_metrics_unreadable:{exc}"]
    if not isinstance(payload, dict):
        return {}, ["experience_metrics_must_be_object"]
    metrics: dict[str, float] = {}
    warnings: list[str] = []
    for field in EXPERIENCE_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            warnings.append(f"experience_metric_invalid:{field}")
            continue
        if parsed < 0:
            warnings.append(f"experience_metric_negative:{field}")
            continue
        metrics[field] = parsed
    return metrics, warnings


def evaluate_budgets(
    snapshot: dict[str, Any],
    *,
    baseline_experience: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    images = snapshot.get("images") or {}
    for role in ("backend", "frontend", "nginx"):
        image = images.get(role) if isinstance(images, dict) else None
        actual = image.get("size_bytes") if isinstance(image, dict) and image.get("available") else None
        limit = DEFAULT_BUDGETS["images"][role]
        checks.append(
            {
                "name": f"image_{role}",
                "actual": actual,
                "limit": limit,
                "status": "pass" if actual is not None and actual <= limit else "fail",
            }
        )
    total = snapshot.get("default_image_total_bytes")
    total_limit = DEFAULT_BUDGETS["images"]["default_total"]
    checks.append(
        {
            "name": "image_default_total",
            "actual": total,
            "limit": total_limit,
            "status": "pass" if isinstance(total, int) and total <= total_limit else "fail",
        }
    )
    idle = (snapshot.get("runtime") or {}).get("idle_memory_total_bytes")
    idle_limit = DEFAULT_BUDGETS["runtime"]["idle_memory_total"]
    checks.append(
        {
            "name": "runtime_idle_memory",
            "actual": idle,
            "limit": idle_limit,
            "status": "pass" if isinstance(idle, int) and idle <= idle_limit else "fail",
        }
    )
    experience = snapshot.get("experience") or {}
    first_page = experience.get("first_readable_page_ms")
    first_page_limit = DEFAULT_BUDGETS["experience"]["first_readable_page_ms"]
    checks.append(
        {
            "name": "experience_first_readable_page",
            "actual": first_page,
            "limit": first_page_limit,
            "status": (
                "pass"
                if isinstance(first_page, (int, float)) and first_page <= first_page_limit
                else "fail"
            ),
        }
    )
    baseline = baseline_experience or {}
    for field in (
        "selection_translation_ms",
        "pet_first_sse_ms",
        "agent_first_sse_ms",
    ):
        actual = experience.get(field)
        baseline_value = baseline.get(field)
        ratio_limit = DEFAULT_BUDGETS["experience"][f"{field.removesuffix('_ms')}_regression_ratio"]
        limit = baseline_value * ratio_limit if baseline_value is not None else None
        checks.append(
            {
                "name": f"experience_{field.removesuffix('_ms')}",
                "actual": actual,
                "baseline": baseline_value,
                "limit": limit,
                "status": (
                    "pass"
                    if isinstance(actual, (int, float))
                    and isinstance(limit, (int, float))
                    and actual <= limit
                    else "fail"
                ),
            }
        )
    return checks


def build_snapshot(
    *,
    data_dir: Path,
    experience_path: Path | None = None,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    project, services, warnings = _compose_config()
    service_images = _default_service_images(project, services)
    images = {role: _inspect_image(reference) for role, reference in service_images.items()}
    available_sizes = [
        int(item["size_bytes"])
        for item in images.values()
        if item.get("available") and isinstance(item.get("size_bytes"), int)
    ]
    all_images_available = len(available_sizes) == len(service_images) and bool(service_images)
    containers = _inspect_running_containers(list(service_images))
    memory_values = [
        int(item["memory_bytes"])
        for item in containers.values()
        if item.get("running") and isinstance(item.get("memory_bytes"), int)
    ]
    all_containers_running = (
        len(memory_values) == len(service_images) and bool(service_images)
    )
    experience, experience_warnings = _load_experience(experience_path)
    baseline_experience, baseline_warnings = _load_experience(baseline_path)
    snapshot: dict[str, Any] = {
        "version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "compose_project": project,
        "images": images,
        "default_image_total_bytes": sum(available_sizes) if all_images_available else None,
        "runtime": {
            "containers": containers,
            "idle_memory_total_bytes": sum(memory_values) if all_containers_running else None,
        },
        "data": {
            "path": str(data_dir.resolve()),
            "size_bytes": _directory_size(data_dir),
        },
        "experience": experience,
        "baseline_experience": baseline_experience,
        "budgets": DEFAULT_BUDGETS,
        "warnings": [*warnings, *experience_warnings, *baseline_warnings],
    }
    snapshot["checks"] = evaluate_budgets(
        snapshot,
        baseline_experience=baseline_experience,
    )
    snapshot["acceptable"] = all(item["status"] == "pass" for item in snapshot["checks"])
    return snapshot


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--experience",
        type=Path,
        help="Optional JSON containing startup/PDF/translation/Pet/Agent timings.",
    )
    parser.add_argument(
        "--baseline-experience",
        type=Path,
        help="Baseline timing JSON used for the 10%% regression checks.",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Return non-zero unless every image, runtime, and experience budget passes.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    snapshot = build_snapshot(
        data_dir=args.data_dir,
        experience_path=args.experience,
        baseline_path=args.baseline_experience,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": os.fspath(args.output),
                "default_image_total_bytes": snapshot["default_image_total_bytes"],
                "idle_memory_total_bytes": snapshot["runtime"]["idle_memory_total_bytes"],
                "data_size_bytes": snapshot["data"]["size_bytes"],
                "acceptable": snapshot["acceptable"],
                "failed_checks": [
                    item["name"]
                    for item in snapshot["checks"]
                    if item["status"] != "pass"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0 if snapshot["acceptable"] or not args.enforce else 1


if __name__ == "__main__":
    sys.exit(main())
