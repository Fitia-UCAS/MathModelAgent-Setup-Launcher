# app/tools/png_paths.py

# 1 导入依赖
import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Set, Dict
from app.utils.common_utils import get_work_dir
from app.tools.text_sanitizer import TextSanitizer as TS

# 2 常量
# 2.1 默认允许的图片扩展名
DEFAULT_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg")


# 3 内部工具函数
# 3.1 判断路径是否在允许的根路径下（eda / sensitivity_analysis / quesN）
def _is_under_allowed_root(rel_root: Path) -> bool:
    parts = rel_root.parts
    if not parts:
        return False
    head = parts[0]
    if head in ("eda", "sensitivity_analysis"):
        return True
    if head.startswith("ques"):
        return len(head) > 4 and head[4:].isdigit()
    return False


# 3.2 判断是否保留该目录进行文件扫描
def _should_keep_dir(rel_root: Path, only_figures: bool) -> bool:
    if not _is_under_allowed_root(rel_root):
        return False
    return ("figures" in rel_root.parts) if only_figures else True


# 3.3 归一化相对路径为 POSIX 格式
def _norm_rel(p: Path) -> str:
    return p.as_posix().lstrip("./").lstrip("/")


# 3.4 判断文件扩展名是否允许
def _has_allowed_ext(name: str, extensions: Sequence[str]) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in extensions)


# 4 图片收集函数
# 4.1 收集相对路径图片
def collect_image_relative_paths(
    startpath: str,
    only_figures: bool = True,
    extensions: Sequence[str] = DEFAULT_EXTS,
) -> List[str]:
    out: Set[str] = set()
    root_path = Path(startpath)

    for root, _, files in os.walk(root_path):
        rel_root = Path(root).relative_to(root_path)
        if not _should_keep_dir(rel_root, only_figures=only_figures):
            continue
        for f in files:
            if not _has_allowed_ext(f, extensions):
                continue
            rel_path = _norm_rel(rel_root / f)
            if TS.is_allowed_image_prefix(rel_path, allow_ques_prefix=True):
                out.add(rel_path)

    return sorted(out)


# 4.2 仅收集 .png 文件（兼容旧接口）
def collect_png_relative_paths(startpath: str, only_figures: bool = True) -> List[str]:
    return collect_image_relative_paths(startpath, only_figures=only_figures, extensions=(".png",))


# 4.3 按 task_id 收集图片路径
def collect_image_paths_by_task(
    task_id: str,
    only_figures: bool = True,
    extensions: Sequence[str] = DEFAULT_EXTS,
) -> List[str]:
    work_dir = get_work_dir(task_id)
    return collect_image_relative_paths(work_dir, only_figures=only_figures, extensions=extensions)


# 4.4 按 task_id 收集 .png 文件
def collect_png_paths_by_task(task_id: str, only_figures: bool = True) -> List[str]:
    return collect_image_paths_by_task(task_id, only_figures=only_figures, extensions=(".png",))


# 5 URL 映射工具
# 5.1 相对路径映射为前端可访问 URL
def to_public_url(task_id: str, rel_path: str) -> str:
    rel = TS.normalize_relpath(rel_path)
    return f"/static/{task_id}/{rel}"


# 5.2 批量映射为 URL
def to_public_urls_by_task(task_id: str, rel_paths: Iterable[str]) -> List[str]:
    return [to_public_url(task_id, p) for p in rel_paths]


# 6 校验与修正工具
# 6.1 校验 Markdown 图片引用合法性
def validate_markdown_image_refs(
    md_text: str,
    available_paths: Iterable[str],
    allow_ques_prefix: bool = True,
) -> Tuple[List[str], List[str]]:
    refs = TS.extract_markdown_image_paths(md_text or "")
    avail_set = {TS.normalize_relpath(p) for p in available_paths}

    valid: List[str] = []
    invalid: List[str] = []

    for r in refs:
        nr = TS.normalize_relpath(r)
        if not TS.is_allowed_image_prefix(nr, allow_ques_prefix=allow_ques_prefix):
            invalid.append(nr)
            continue
        if nr in avail_set:
            valid.append(nr)
        else:
            invalid.append(nr)

    def _uniq_keep_order(lst: List[str]) -> List[str]:
        seen = set()
        out = []
        for x in lst:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return _uniq_keep_order(valid), _uniq_keep_order(invalid)


# 6.2 将 Markdown 中的裸文件名替换为规范路径
def rewrite_image_paths_by_basename(md_text: str, available_paths: Iterable[str]) -> str:
    paths = list(available_paths)
    by_basename: Dict[str, List[str]] = {}
    for p in paths:
        base = os.path.basename(p)
        by_basename.setdefault(base, []).append(p)

    def _replace(match):
        alt = match.group(1)
        url = match.group(2).strip()
        if "/" in url or "\\" in url:
            return match.group(0)
        cands = by_basename.get(url)
        if not cands:
            return match.group(0)
        best = sorted(cands, key=len)[0]
        return f"![{alt}]({best})"

    import re

    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    return pattern.sub(_replace, md_text or "")
