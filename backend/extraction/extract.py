"""提取编排 — ar5iv 主 + LaTeX 备（D14）。

策略：先走 ar5iv，失败或渲染不全时自动回退 LaTeX 源码。
"""

from __future__ import annotations

import logging

from .ar5iv import extract_from_ar5iv
from .blocks import Block
from .quality import assess_extraction_quality

logger = logging.getLogger(__name__)


async def extract_paper(arxiv_id: str) -> tuple[list[Block], str]:
    """提取论文文本，返回 (blocks, source)。

    source: "ar5iv" | "latex" | "mineru" | "failed"
    """
    # 主路径：ar5iv
    blocks = await extract_from_ar5iv(arxiv_id)
    if blocks:
        report = assess_extraction_quality(blocks, "ar5iv")
        if report.acceptable:
            if report.findings:
                logger.warning(
                    "ar5iv 提取有质量告警: %s (%d findings, score=%.3f)",
                    arxiv_id,
                    len(report.findings),
                    report.score,
                )
            else:
                logger.info("ar5iv 提取成功: %s (%d blocks)", arxiv_id, len(blocks))
            return blocks, "ar5iv"
        logger.warning(
            "ar5iv 提取质量不合格，回退 LaTeX: %s (%d findings, score=%.3f)",
            arxiv_id,
            len(report.findings),
            report.score,
        )

    # 备路径：LaTeX
    logger.info("ar5iv 不可用或渲染不全，回退 LaTeX: %s", arxiv_id)
    from .latex import extract_from_latex  # 延迟导入，避免未装 pylatexenc 时报错

    blocks = await extract_from_latex(arxiv_id)
    if blocks:
        report = assess_extraction_quality(blocks, "latex")
        if report.acceptable:
            logger.info("LaTeX 提取成功: %s (%d blocks)", arxiv_id, len(blocks))
            return blocks, "latex"
        logger.warning(
            "LaTeX 提取质量不合格，继续尝试 MinerU: %s (%d findings, score=%.3f)",
            arxiv_id,
            len(report.findings),
            report.score,
        )

    mineru_config = _get_enabled_mineru_config()
    if mineru_config is not None:
        logger.info("ar5iv/LaTeX 不可用，尝试 MinerU 可选兜底: %s", arxiv_id)
        from .mineru import extract_from_mineru_url

        try:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            blocks = await extract_from_mineru_url(
                pdf_url,
                file_name=f"{arxiv_id}.pdf",
                config=mineru_config,
            )
        except Exception as e:
            logger.warning("MinerU 兜底提取失败: %s (%s)", arxiv_id, e)
            blocks = []
        if blocks:
            logger.info("MinerU 提取成功: %s (%d blocks)", arxiv_id, len(blocks))
            return blocks, "mineru"

    logger.warning("提取失败: %s", arxiv_id)
    return [], "failed"


def _get_enabled_mineru_config():
    try:
        from ..llm.config import resolve_mineru_config

        config = resolve_mineru_config()
    except Exception:
        return None
    if not config.enabled:
        return None
    if config.mode != "agent_lite":
        logger.warning("MinerU 提取兜底当前仅支持 agent_lite 模式，已跳过: %s", config.mode)
        return None
    return config
