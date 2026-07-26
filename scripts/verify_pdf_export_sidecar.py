#!/usr/bin/env python3
"""Verify the optional PDF export sidecar's pinned and isolated deployment."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "sidecar/pdf_export/Dockerfile"
BACKEND_DOCKERFILE = ROOT / "backend/Dockerfile"
NGINX = ROOT / "deploy/nginx.conf"
SIDECAR_NOTICE = ROOT / "sidecar/pdf_export/THIRD_PARTY.md"
PUBLIC_NOTICE = ROOT / "docs/third-party/pdf-export-sidecar.md"
PINNED_BASE = (
    "awwaawwa/pdfmathtranslate-next@"
    "sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a"
)
DERIVED_IMAGE = "peinidu-pdf-export:2.9.0-f8dffcf4"
PDFMATH_SOURCE = "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0"
PDFMATH_LICENSE = "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/blob/v2.9.0/LICENSE"
BABELDOC_SOURCE = "https://github.com/funstory-ai/BabelDOC/tree/v0.6.2"
BABELDOC_LICENSE = "https://github.com/funstory-ai/BabelDOC/blob/v0.6.2/LICENSE"
WRAPPER_RUNTIME_FILES = (
    "app.py",
    "Dockerfile",
    "entrypoint.sh",
    "healthcheck.py",
    "runtime_probe.py",
    "README.md",
    "THIRD_PARTY.md",
    "tests/__init__.py",
    "tests/test_app.py",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wrapper_source_sha256(root: Path = ROOT / "sidecar/pdf_export") -> str:
    """Mirror the sidecar's ordered, framed public-source attestation."""
    resolved_root = root.resolve(strict=True)
    require(not root.is_symlink(), "wrapper source root must not be a symlink")
    digest = hashlib.sha256()
    for relative_name in WRAPPER_RUNTIME_FILES:
        current = root
        for part in Path(relative_name).parts:
            current /= part
            require(not current.is_symlink(), "wrapper source contains a symlink")
        source = (root / relative_name).resolve(strict=True)
        require(
            source.is_relative_to(resolved_root) and source.is_file(),
            f"wrapper source missing: {relative_name}",
        )
        path_bytes = relative_name.encode("utf-8")
        content = source.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _yaml_block(text: str, key: str, indent: int) -> str:
    """Extract one indentation-delimited block from this fixed Compose file."""
    marker = f"{' ' * indent}{key}:"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() != marker:
            continue
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.lstrip()
            if not stripped or stripped.startswith("#"):
                body.append(candidate)
                continue
            candidate_indent = len(candidate) - len(stripped)
            if candidate_indent <= indent:
                break
            body.append(candidate)
        return "\n".join(body)
    raise AssertionError(f"Compose block missing: {key}")


def _yaml_optional_block(text: str, key: str, indent: int) -> str:
    try:
        return _yaml_block(text, key, indent)
    except AssertionError:
        return ""


def _yaml_scalar(text: str, key: str, indent: int) -> str | None:
    prefix = f"{' ' * indent}{key}:"
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def _yaml_list(text: str, key: str, indent: int) -> list[str]:
    block = _yaml_optional_block(text, key, indent)
    item_prefix = f"{' ' * (indent + 2)}- "
    return [
        line[len(item_prefix) :].strip()
        for line in block.splitlines()
        if line.startswith(item_prefix)
    ]


def _yaml_has_key(text: str, key: str, indent: int) -> bool:
    prefix = f"{' ' * indent}{key}:"
    return any(line.startswith(prefix) for line in text.splitlines())


def static_checks() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    services = _yaml_block(compose, "services", 0)
    sidecar = _yaml_block(services, "pdf-export", 2)
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    backend_dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    require(f"FROM {PINNED_BASE}" in dockerfile, "sidecar base image digest drifted")
    require(_yaml_scalar(sidecar, "image", 4) == DERIVED_IMAGE, "derived image tag drifted")
    build = _yaml_block(sidecar, "build", 4)
    require(
        _yaml_scalar(build, "dockerfile", 6) == "sidecar/pdf_export/Dockerfile",
        "sidecar Dockerfile missing",
    )
    require(
        _yaml_scalar(sidecar, "profiles", 4) == '["pdf-export"]',
        "sidecar must remain opt-in",
    )
    require(not _yaml_has_key(sidecar, "ports", 4), "sidecar must not publish a host port")
    require(
        _yaml_scalar(sidecar, "read_only", 4) == "true",
        "sidecar root filesystem must be read-only",
    )
    require(
        _yaml_scalar(sidecar, "user", 4) == "10001:10001",
        "sidecar must use fixed non-root uid",
    )
    require(_yaml_list(sidecar, "cap_drop", 4) == ["ALL"], "sidecar must drop all capabilities")
    require(
        "no-new-privileges:true" in _yaml_list(sidecar, "security_opt", 4),
        "no-new-privileges missing",
    )
    tmpfs = _yaml_list(sidecar, "tmpfs", 4)
    require(any(item.startswith("/work:") for item in tmpfs), "/work must be tmpfs")
    require(
        any("uid=10001" in item and "mode=0700" in item for item in tmpfs),
        "tmpfs ownership/mode missing",
    )
    require(
        not _yaml_has_key(sidecar, "volumes", 4),
        "sidecar source must be immutable in the image",
    )
    for owner in ("backend", "nginx"):
        owner_service = _yaml_block(services, owner, 2)
        dependencies = _yaml_optional_block(owner_service, "depends_on", 4)
        require(
            not _yaml_has_key(dependencies, "pdf-export", 6),
            f"{owner} must not depend on optional sidecar",
        )

    networks = _yaml_block(compose, "networks", 0)
    internal_network = _yaml_block(networks, "pdf-export-internal", 2)
    require(
        _yaml_scalar(internal_network, "internal", 4) == "true",
        "PDF export network must be internal",
    )

    def service_networks(name: str) -> set[str]:
        service = _yaml_block(services, name, 2)
        return set(_yaml_list(service, "networks", 4))

    require(
        service_networks("backend")
        == {"default", "pdf-export-internal", "browser-control-internal"},
        "backend must join default, PDF export, and browser control networks",
    )
    require(service_networks("pdf-export") == {"pdf-export-internal"}, "sidecar must only join the internal network")
    require(service_networks("frontend") == {"default"}, "frontend must only join the default network")
    require(service_networks("nginx") == {"default"}, "nginx must only join the default network")

    backend = _yaml_block(services, "backend", 2)
    require(
        set(_yaml_list(backend, "volumes", 4))
        == {"./data:/app/data", "./config:/app/config"},
        "backend must not bind-mount application or wrapper source",
    )

    environment = _yaml_block(sidecar, "environment", 4)
    require(
        _yaml_scalar(environment, "PEINIDU_PDF_EXPORT_INTERNAL_BASE_URL", 6)
        == "http://backend:8000/internal/llm/v1",
        "internal LiteLLM URL drifted",
    )
    require(_yaml_scalar(environment, "HF_HUB_OFFLINE", 6) == "1", "offline model guard missing")
    require(_yaml_scalar(environment, "TRANSFORMERS_OFFLINE", 6) == "1", "offline transformer guard missing")
    require(
        _yaml_has_key(environment, "PEINIDU_PDF_EXPORT_INTERNAL_TOKEN", 6),
        "internal token missing",
    )
    banned = (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "RELAY_API_KEY",
        "MINERU_API_TOKEN",
        "AZURE_OPENAI_API_KEY",
    )
    require(
        not any(_yaml_has_key(environment, name, 6) for name in banned),
        "upstream provider credential leaked into sidecar",
    )

    require("USER 10001:10001" in dockerfile, "derived image must end as non-root")
    require("/opt/pdf-export-cache" in dockerfile, "preloaded cache was not preserved")
    require(
        "COPY --chown=10001:10001 sidecar/pdf_export/ /pet-sidecar/"
        in dockerfile,
        "sidecar image must copy the public wrapper source",
    )
    require(
        "COPY sidecar/pdf_export/ pdf_export_wrapper_source/"
        in backend_dockerfile,
        "backend image must bake the attested wrapper source tree",
    )
    require(
        "COPY deploy/nginx.conf deploy/nginx.conf" in backend_dockerfile,
        "backend public bundle is missing the Nginx verifier input",
    )
    require(
        '"--no-proxy-headers"' in backend_dockerfile
        and '"--proxy-headers"' not in backend_dockerfile,
        "backend must not let Uvicorn rewrite the socket peer from proxy headers",
    )
    nginx = NGINX.read_text(encoding="utf-8")
    deny = nginx.index("location ^~ /api/internal/")
    public_api = nginx.index("location /api/")
    require(deny < public_api, "internal route deny must precede public API proxy")
    deny_body = nginx[deny:public_api]
    require("return 404;" in deny_body, "internal route must return 404 publicly")
    require(
        "$proxy_add_x_forwarded_for" not in nginx,
        "Nginx must not append attacker-controlled X-Forwarded-For values",
    )
    require(
        "proxy_set_header X-Forwarded-For $remote_addr;" in nginx,
        "Nginx must overwrite X-Forwarded-For with one client address",
    )

    backend_environment = _yaml_block(backend, "environment", 4)
    for name in (
        "PEINIDU_TRUSTED_PROXY_IPS",
        "PEINIDU_PDF_EXPORT_RATE_LIMIT_PER_MINUTE",
        "PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS",
    ):
        require(
            _yaml_has_key(backend_environment, name, 6),
            f"backend environment missing {name}",
        )

    wrapper_hash = wrapper_source_sha256()
    require(
        len(wrapper_hash) == 64
        and wrapper_hash == wrapper_hash.lower()
        and all(character in "0123456789abcdef" for character in wrapper_hash),
        "wrapper source hash is invalid",
    )

    required_notice_fragments = (
        "PDFMathTranslate-next",
        "BabelDOC",
        "0.6.2",
        PDFMATH_SOURCE,
        PDFMATH_LICENSE,
        BABELDOC_SOURCE,
        BABELDOC_LICENSE,
        "sidecar/pdf_export",
        "scripts/verify_pdf_export_sidecar.py",
        "AGPL",
    )
    for notice_path in (SIDECAR_NOTICE, PUBLIC_NOTICE):
        notice = notice_path.read_text(encoding="utf-8")
        for fragment in required_notice_fragments:
            require(fragment in notice, f"{notice_path.relative_to(ROOT)} missing disclosure: {fragment}")


def runtime_checks() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "pdf-export",
            "build",
            "pdf-export",
        ],
        cwd=ROOT,
        check=True,
    )
    inspect = subprocess.run(
        ["docker", "inspect", DERIVED_IMAGE, "--format", "{{.Config.User}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    require(inspect.stdout.strip() == "10001:10001", "built image is not non-root")
    source_hash = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "pdf-export",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "pdf-export",
            "-c",
            (
                "import sys; sys.path.insert(0, '/pet-sidecar'); "
                "from app import _wrapper_source_sha256; "
                "print(_wrapper_source_sha256())"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    require(
        source_hash.stdout.strip() == wrapper_source_sha256(),
        "built sidecar wrapper source differs from the public source tree",
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "pdf-export",
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "PEINIDU_PDF_EXPORT_INTERNAL_TOKEN=runtime-probe-token",
            "pdf-export",
            "python",
            "/pet-sidecar/runtime_probe.py",
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true", help="build and probe the read-only container")
    args = parser.parse_args()
    static_checks()
    if args.runtime:
        runtime_checks()
    print("PDF export sidecar verification passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, subprocess.CalledProcessError) as error:
        print(f"PDF export sidecar verification failed: {error}", file=sys.stderr)
        raise SystemExit(1)
