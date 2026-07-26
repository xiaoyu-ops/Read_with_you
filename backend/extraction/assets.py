"""论文源文件资产抽取：图片 / PDF 图转成本地可访问资源。"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from ..storage.files import paper_dir
from .latex import fetch_latex_source

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PDF_SUFFIXES = {".pdf"}


async def extract_source_assets(arxiv_id: str) -> dict[str, str]:
    """从 arXiv e-print 中抽取图像资产，返回 {basename: public_url}。"""
    raw = await fetch_latex_source(arxiv_id)
    if raw is None:
        return {}

    assets_dir = paper_dir(arxiv_id) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_map: dict[str, str] = {}

    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, EOFError):
        return {}

    with tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            suffix = Path(member.name).suffix.lower()
            if suffix not in IMAGE_SUFFIXES and suffix not in PDF_SUFFIXES:
                continue
            source = tar.extractfile(member)
            if source is None:
                continue

            stem = Path(member.name).stem
            basename = Path(member.name).name
            if suffix in IMAGE_SUFFIXES:
                out_name = basename
                out_path = assets_dir / out_name
                with open(out_path, "wb") as f:
                    shutil.copyfileobj(source, f)
                asset_map[basename] = f"/assets/{arxiv_id}/assets/{out_name}"
                continue

            pdf_path = assets_dir / basename
            with open(pdf_path, "wb") as f:
                shutil.copyfileobj(source, f)
            png_path = _convert_pdf_first_page(pdf_path, assets_dir / f"{stem}.png")
            if png_path:
                asset_map[basename] = f"/assets/{arxiv_id}/assets/{png_path.name}"
                asset_map[png_path.name] = f"/assets/{arxiv_id}/assets/{png_path.name}"

    return asset_map


def _convert_pdf_first_page(pdf_path: Path, png_path: Path) -> Path | None:
    """用 pdftoppm 把 PDF 第一页转成 PNG。"""
    if shutil.which("pdftoppm") is None:
        return None
    prefix = png_path.with_suffix("")
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-singlefile", str(pdf_path), str(prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return png_path if png_path.exists() else None
