import os
import subprocess
import shutil


def copy_changed_files(diff_dir, base_ref="HEAD"):
    """
    复制 git diff 出来的变动文件到指定目录
    :param diff_dir: 目标路径
    :param base_ref: 对比基准（默认 HEAD）
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

    for file_path in changed_files:
        if os.path.isfile(file_path):
            dest_path = os.path.join(diff_dir, file_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            shutil.copy2(file_path, dest_path)
            print(f"已复制改动文件: {file_path} → {dest_path}")


def copy_project_subdir(subdir_name, full_dir, ignore_extra_patterns=None):
    """
    复制项目下的某个子目录（如 backend / ques / tools）
    :param subdir_name: 子目录名称
    :param full_dir: 目标路径
    :param ignore_extra_patterns: 额外忽略的通配符列表
    """
    repo_path = os.getcwd()
    src_dir = os.path.join(repo_path, subdir_name)

    if not os.path.exists(src_dir):
        print(f"警告: {subdir_name} 目录不存在。")
        return

    dest_dir = os.path.join(full_dir, subdir_name)
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)

    os.makedirs(full_dir, exist_ok=True)

    default_ignores = [
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        "*.log",
        "logs",
        ".DS_Store",
        "Thumbs.db",
        "*/example",  # 忽略 example 目录本身
        "*/example/*",  # 忽略 example 目录下所有文件
        "*/project",
        "*/project/*",
    ]
    if ignore_extra_patterns:
        default_ignores.extend(ignore_extra_patterns)

    ignore = shutil.ignore_patterns(*default_ignores)
    shutil.copytree(src_dir, dest_dir, ignore=ignore)

    # 删除残留的 example 目录（防止之前已复制过）
    example_path = os.path.join(dest_dir, "app", "example")
    if os.path.exists(example_path):
        shutil.rmtree(example_path)
        print(f"已删除残留的 example 目录: {example_path}")

    print(f"已复制整个 {subdir_name} → {dest_dir}")
    print(f"（忽略模式：{default_ignores}）")


if __name__ == "__main__":
    # 改动文件复制目录
    diff_target_1 = r"E:/repo1/MathModelAgent-DeploymentScript/MMA-Changes-Diff-1"
    copy_changed_files(diff_target_1)

    # diff_target_2 = r"tools/MMA-Changes-Diff-1"
    # copy_changed_files(diff_target_2)

    # 完整目录复制目标
    full_target = r"E:/repo1/MathModelAgent-DeploymentScript/MMA-Changes-Full-1"

    # 分别复制 backend / ques / tools
    for subdir in ["backend", "ques", "tools"]:
        copy_project_subdir(subdir, full_target, ignore_extra_patterns=None)
