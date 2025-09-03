import os
import subprocess
import shutil
import fnmatch
from pathlib import Path
from typing import Iterable, List, Optional


# -------- 工具：基于相对路径的 ignore 函数 --------
def make_path_aware_ignore(root_dir: Path, patterns: Iterable[str]):
    """
    返回一个可传给 shutil.copytree(ignore=...) 的函数。
    支持多级通配（**、*/xxx、xxx/**、*.pyc 等），既匹配“相对路径”，也兜底匹配“basename”。
    """
    root_dir = Path(root_dir)

    # 归一化模式：用 / 作为分隔，便于跨平台匹配
    pats: List[str] = [p.replace("\\", "/") for p in patterns]

    def _ignore(dirpath: str, names: List[str]) -> set[str]:
        rel_dir = Path(dirpath).resolve().relative_to(root_dir.resolve())
        rel_dir_str = "" if str(rel_dir) == "." else str(rel_dir).replace("\\", "/")
        ignored = set()
        for name in names:
            # 组合成相对路径（dir/name）
            rel_path = (rel_dir_str + "/" + name) if rel_dir_str else name
            rel_path = rel_path.replace("\\", "/")
            for pat in pats:
                # 路径匹配或 basename 匹配，命中任意一个就忽略
                if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(name, pat):
                    ignored.add(name)
                    break
        return ignored

    return _ignore


# -------- 复制 git diff 文件（可选排除）--------
def copy_changed_files(diff_dir: str, base_ref: str = "HEAD", exclude_globs: Optional[List[str]] = None):
    """
    复制 git diff 出来的变动文件到指定目录（支持基于相对路径的排除）
    """
    repo_path = os.getcwd()
    result = subprocess.run(
        ["git", "diff", "--name-only", base_ref],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    if result.returncode != 0:
        print("git diff 执行失败：", result.stderr.strip())
        return

    changed_files = [f for f in result.stdout.strip().split("\n") if f]
    if not changed_files:
        print("没有检测到变动文件。")
        return

    # 归一化排除模式
    exclude_globs = [(g or "").replace("\\", "/") for g in (exclude_globs or [])]

    os.makedirs(diff_dir, exist_ok=True)
    for file_path in changed_files:
        # 路径统一用 / 方便匹配
        rel_path = file_path.replace("\\", "/")
        # 过滤
        if any(
            fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(os.path.basename(rel_path), pat) for pat in exclude_globs
        ):
            # 可打印：被排除
            # print(f"[排除] {rel_path}")
            continue

        src = Path(repo_path) / file_path
        if src.is_file():
            dest_path = Path(diff_dir) / file_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_path)
            print(f"已复制改动文件: {file_path} → {dest_path}")


# -------- 复制项目子目录（多级排除）--------
def copy_project_subdir(subdir_name: str, full_dir: str, ignore_extra_patterns: Optional[List[str]] = None):
    """
    复制项目下的某个子目录（如 backend / ques / tools），支持多级排除
    """
    repo_path = os.getcwd()
    src_dir = Path(repo_path) / subdir_name

    if not src_dir.exists():
        print(f"警告: {subdir_name} 目录不存在。")
        return

    dest_dir = Path(full_dir) / subdir_name
    if dest_dir.exists():
        shutil.rmtree(dest_dir)

    os.makedirs(full_dir, exist_ok=True)

    # 这些模式现在都支持多级路径：**/example/**、project/** 等
    default_ignores = [
        ".venv/**",
        "venv/**",
        "**/__pycache__/**",
        ".mypy_cache/**",
        ".pytest_cache/**",
        ".ruff_cache/**",
        "**/*.pyc",
        "**/*.pyo",
        "**/*.pyd",
        "**/*.log",
        "logs/**",
        ".DS_Store",
        "Thumbs.db",
        "**/example/**",  # 忽略任意层级的 example 目录
        "project/**",  # 忽略当前 subdir 下的 project 整个子树
        # 你也可以加更多：
        # "project/work_dir/**",
        # "launcher/**",
    ]
    if ignore_extra_patterns:
        default_ignores.extend(ignore_extra_patterns)

    ignore_func = make_path_aware_ignore(src_dir, default_ignores)
    shutil.copytree(src_dir, dest_dir, ignore=ignore_func)

    print(f"已复制整个 {subdir_name} → {dest_dir}")
    print(f"（忽略模式：{default_ignores}）")


if __name__ == "__main__":

    # 完整目录复制目标
    full_target = r"E:/repo1/MathModelAgent-Setup-Launcher/MMA-Changes-1"

    # 分别复制 backend / ques / tools
    for subdir in ["backend", "ques", "tools"]:
        # 这里也可以按需加更细的忽略规则
        copy_project_subdir(
            subdir,
            full_target,
            ignore_extra_patterns=[
                "project/work_dir/**",  # 仅忽略 backend/project/work_dir
                "logs/**",  # 忽略日志目录
                "**/*.log",  # 忽略所有 .log
            ],
        )
