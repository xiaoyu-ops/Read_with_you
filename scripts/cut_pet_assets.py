"""从 frontend/public/pet/source.png 裁切 Pet 状态素材。

source.png 是 5 个姿势横排的合集图，背景为灰色渐变（非透明）。流程：
1. 整图调用 macOS Vision 前景抠图（scripts/pet_foreground_mask.swift，按需编译）；
2. 在抠图结果的 alpha 通道上按列定位 5 个角色区间（背景辉光饱和度高，
   不能按颜色定位，只能信 alpha）；
3. 逐格 alpha 裁边后放到统一画布：宽高取各姿势最大值，底部居中对齐，
   保证人物脚底位置一致，前端切换状态时不跳位。

输出：frontend/public/pet/{idle,talking,thinking,working,confirm}.png
用法：python scripts/cut_pet_assets.py（需要 macOS 14+ 与 Xcode CLT）
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "frontend" / "public" / "pet" / "source.png"
OUT_DIR = ROOT / "frontend" / "public" / "pet"
SWIFT_SRC = ROOT / "scripts" / "pet_foreground_mask.swift"
MASK_BIN = Path(tempfile.gettempdir()) / "peinidu_pet_foreground_mask"

# source.png 中 5 个姿势从左到右对应的 Pet 状态：
# 1 微笑站立=idle，2 张嘴=talking，3 举拳=working，4 认真=confirm，5 微笑变体=thinking
POSE_STATES = ["idle", "talking", "working", "confirm", "thinking"]

ALPHA_THRESHOLD = 8  # 视为不透明的最小 alpha
MIN_COLUMN_HITS = 4  # 一列至少这么多不透明像素才算角色区
MERGE_GAP = 8  # 小于该间隔的区间视为同一角色（角色实际间距约 30-47px，不能设太大）
PAD_X = 10
PAD_TOP = 10
PAD_BOTTOM = 6


def ensure_mask_tool() -> Path:
    if MASK_BIN.exists() and MASK_BIN.stat().st_mtime >= SWIFT_SRC.stat().st_mtime:
        return MASK_BIN
    print(f"编译 Vision 抠图工具 -> {MASK_BIN}")
    subprocess.run(
        ["swiftc", "-O", str(SWIFT_SRC), "-o", str(MASK_BIN)],
        check=True,
    )
    return MASK_BIN


def run_mask(input_path: Path, output_path: Path) -> Image.Image:
    subprocess.run([str(MASK_BIN), str(input_path), str(output_path)], check=True)
    return Image.open(output_path).convert("RGBA")


def find_pose_spans(alpha: Image.Image) -> list[tuple[int, int]]:
    """在 alpha 通道上按列统计不透明像素，找出角色横向区间。"""
    width, height = alpha.size
    pixels = alpha.load()
    spans: list[tuple[int, int]] = []
    start = None
    for x in range(width + 1):
        count = 0
        if x < width:
            for y in range(height):
                if pixels[x, y] >= ALPHA_THRESHOLD:
                    count += 1
        if count >= MIN_COLUMN_HITS and start is None:
            start = x
        elif count < MIN_COLUMN_HITS and start is not None:
            spans.append((start, x - 1))
            start = None
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= MERGE_GAP:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return merged


def trim(image: Image.Image) -> Image.Image:
    solid = image.getchannel("A").point(lambda a: 255 if a >= ALPHA_THRESHOLD else 0)
    bbox = solid.getbbox()
    if bbox is None:
        raise RuntimeError("抠图结果为空")
    return image.crop(bbox)


def compose_on_shared_canvas(poses: list[Image.Image]) -> list[Image.Image]:
    canvas_w = max(p.width for p in poses) + PAD_X * 2
    canvas_h = max(p.height for p in poses) + PAD_TOP + PAD_BOTTOM
    results = []
    for pose in poses:
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        x = (canvas_w - pose.width) // 2
        y = canvas_h - PAD_BOTTOM - pose.height  # 底部对齐 = 脚底对齐
        canvas.paste(pose, (x, y), pose)
        results.append(canvas)
    return results


def main() -> int:
    if not SOURCE.exists():
        print(f"缺少素材源图: {SOURCE}", file=sys.stderr)
        return 1
    ensure_mask_tool()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        masked = run_mask(SOURCE, workdir / "full_masked.png")
        spans = find_pose_spans(masked.getchannel("A"))
        print(f"定位到 {len(spans)} 个角色区间: {spans}")
        if len(spans) != len(POSE_STATES):
            debug_path = OUT_DIR / "_debug_masked.png"
            masked.save(debug_path)
            print(
                f"期望 {len(POSE_STATES)} 个姿势，实际 {len(spans)} 个；"
                f"已保存调试图 {debug_path.relative_to(ROOT)}，请调整阈值后重试",
                file=sys.stderr,
            )
            return 1
        poses = [trim(masked.crop((x0, 0, x1 + 1, masked.height))) for x0, x1 in spans]

    for state, pose in zip(POSE_STATES, poses):
        print(f"{state}: 裁边后 {pose.width}x{pose.height}")

    for state, canvas in zip(POSE_STATES, compose_on_shared_canvas(poses)):
        out_path = OUT_DIR / f"{state}.png"
        canvas.save(out_path, optimize=True)
        print(f"写出 {out_path.relative_to(ROOT)} ({canvas.width}x{canvas.height}, {out_path.stat().st_size // 1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
