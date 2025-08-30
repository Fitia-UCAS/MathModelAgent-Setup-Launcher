# app/tools/png_paths.py
import os
from pathlib import Path
from typing import List
from app.utils.common_utils import get_work_dir  # 用它拿到 work_dir

KEEP_FOLDERS = ["eda", "sensitivity_analysis"]

def collect_png_relative_paths(startpath: str, only_figures: bool = True) -> List[str]:
    """
    收集 .png 的相对路径（仅保留 eda、sensitivity_analysis、quesN）。
    默认仅限 figures 子目录（only_figures=True）。
    """
    png_paths: List[str] = []
    root_path = Path(startpath)

    for root, dirs, files in os.walk(root_path):
        rel_root = Path(root).relative_to(root_path)
        rel_root_str = rel_root.as_posix()

        ok = (
            rel_root_str.startswith("eda")
            or rel_root_str.startswith("sensitivity_analysis")
            or (rel_root_str.startswith("ques") and len(rel_root_str) >= 5 and rel_root_str[4].isdigit())
        )
        if not ok:
            continue

        if only_figures and "figures" not in rel_root.parts:
            # 只要 figures 子目录里的图（与你 Writer 约定一致）
            continue

        for f in files:
            if f.lower().endswith(".png"):
                rel_path = (rel_root / f).as_posix()  # 始终用 /
                png_paths.append(rel_path)

    # 排序去重
    return sorted(set(png_paths))

def collect_png_paths_by_task(task_id: str, only_figures: bool = True) -> List[str]:
    """给定 task_id，扫描该任务 work_dir 下的 png 相对路径"""
    work_dir = get_work_dir(task_id)  # e.g. project/work_dir/<task_id>
    return collect_png_relative_paths(work_dir, only_figures=only_figures)
