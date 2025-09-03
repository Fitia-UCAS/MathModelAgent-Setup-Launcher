import os
import re
import shutil

# 根目录（按实际情况调整）
BASE_DIR = os.path.abspath("backend")

# 目标路径
launcher_dir = os.path.join(BASE_DIR, "logs", "launcher")
messages_dir = os.path.join(BASE_DIR, "logs", "messages")
work_dir = os.path.join(BASE_DIR, "project", "work_dir")

# 正则匹配模式
log_pattern = re.compile(r"^\d+\.log$")
txt_pattern = re.compile(r"^源码快照_\d+\.txt$")
json_pattern = re.compile(r"^\d+\.json$")
workdir_pattern = re.compile(r"^\d+$")


def remove_files_in_dir(path, pattern):
    if not os.path.exists(path):
        return
    for fname in os.listdir(path):
        if pattern.match(fname):
            fpath = os.path.join(path, fname)
            print(f"删除文件: {fpath}")
            os.remove(fpath)


def remove_dirs_in_dir(path, pattern):
    if not os.path.exists(path):
        return
    for dname in os.listdir(path):
        if pattern.match(dname):
            dpath = os.path.join(path, dname)
            print(f"删除目录: {dpath}")
            shutil.rmtree(dpath)


if __name__ == "__main__":
    # 删除 .log
    remove_files_in_dir(launcher_dir, log_pattern)

    # 删除 后端现有的项目源码_{数字}.txt
    remove_files_in_dir(launcher_dir, txt_pattern)

    # 删除 .json
    remove_files_in_dir(messages_dir, json_pattern)

    # 删除 work_dir/{数字}
    remove_dirs_in_dir(work_dir, workdir_pattern)

    print("清理完成 ✅")
