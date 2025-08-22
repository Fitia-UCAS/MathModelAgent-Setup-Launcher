# [MathModelAgent](https://github.com/jihe520/MathModelAgent) 部署脚本

本项目提供多种自动部署脚本，方便用户快速部署 MathModelAgent。支持的部署方式包括：

1. **适用于 Bat    的脚本**
2. **适用于 Python 的脚本**
3. **适用于 Docker 的脚本**

## 部署方式对比

经过实际测试，不同脚本的部署体验和适用场景如下：

| 部署方式链接(Ctrl + 鼠标左键) | 易用性 | 自动化程度 | 适用场景 | 备注 |
|:---------|:-------|:-----------|:---------|:-----|
| [**Bat**](./MMA部署(BatVersion)/MMA部署(BatVersion).html) | ⭐⭐⭐⭐⭐ | 高 | 对电脑完全不懂，纯小白 | 无需任何步骤，双击即可 |
| [**Python**](./MMA部署(PythonVersion)/MMA部署(PythonVersion).html) | ⭐⭐⭐⭐ | 中 | 对 Python，Redis，Nodejs 有特定版本需求 | Python，Redis，Nodejs需手动安装并在项目下运行 |
| [**Docker**](./MMA部署(DockerVersion)/MMA部署(DockerVersion).html) | ⭐⭐⭐⭐ | 视配置而定 | 开发者和高配电脑用户 | 建议选择安装、存储目录，并配置镜像源 |

**其他说明**：如果你的电脑配置较高（磁盘读取速度，磁盘空间很大），则 Docker 的自动化程度最高，且能提供稳定的开发环境，最为推荐。
