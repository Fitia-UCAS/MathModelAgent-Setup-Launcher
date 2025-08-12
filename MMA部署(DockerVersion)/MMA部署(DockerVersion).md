# [MathModelAgent](https://github.com/jihe520/MathModelAgent) 部署脚本 (Docker 版本)

欢迎体验 MathModelAgent 的自动化部署！这是原项目推荐的DOCKER布置流程的详细版本，但是据我体验依据于docker配置的体验不如本地配置的体验...比如py或者其他bat脚本...如果你将脚本粘贴给ai的话，你会得到你想要的答案...

## DOCKER部署流程

按以下步骤操作，确保部署顺畅：

1. **配置后端 `.env.dev`**：
   
   - 复制 `backend\.env.dev.example` 为 `backend\.env.dev`。
   - 编辑 `backend\.env.dev`，配置以下关键项（参考下图示例）：
     - `REDIS_URL`：Docker 使用 `redis://redis:6379/0`，本地使用 `redis://localhost:6379/0`。
     - 模型和 API 密钥：如 `COORDINATOR_MODEL`, `COORDINATOR_API_KEY`, `MODELER_MODEL`, `MODELER_API_KEY` 等。
     - 参考 [LiteLLM 文档](https://docs.litellm.ai/docs/) 获取模型选项。
   - 示例配置：
     ![后端 .env.dev 配置](../assets/docker%20env%20dev%E9%85%8D%E7%BD%AE.png)
   
2. **安装 Docker Desktop**：

    **参考资料**：

    - [Windows | Docker Docs](https://docs.docker.com/desktop/setup/install/windows-install/)
    - [如何优雅地变更 Docker Desktop 的镜像存储路径](https://cloud.tencent.com/developer/article/2414097)
    - [新版本 Docker Desktop 自定义安装路径和镜像地址修改](https://blog.csdn.net/hx2019626/article/details/145140014)
    
   - 指定安装和资源路径，避免占满 C 盘。示例命令（路径可自定义）：
     ```bash
     start /w "" "Docker Desktop Installer.exe" install --accept-license --installation-dir="E:\Docker\Docker"
     ```
   - 在 Docker Desktop 的 `设置 > 资源` 中设置存储路径，节省空间：
     ![Docker 资源设置](../assets/docker%20resources.png)

3. **配置镜像源**：
   - 编辑 `%USERPROFILE%\.docker\daemon.json` 或在 Docker Desktop 的 `设置 > Docker Engine` 中粘贴以下配置，加速镜像拉取：
     ```json
     {
       "builder": {
         "gc": {
           "defaultKeepStorage": "20GB",
           "enabled": true
         }
       },
       "experimental": false,
       "registry-mirrors": [
         "https://docker.1ms.run",
         "https://docker.xuanyuan.me",
         "https://hub.rat.dev",
         "https://dislabaiot.xyz",
         "https://doublezonline.cloud",
         "https://xdark.top"
       ]
     }
     ```
     ![Docker Engine 设置](../assets/doker%20engine.png)

4. **运行自动部署脚本**：
   
   - 将 `mma_setup_docker_win.bat` 放入 MathModelAgent 根目录，双击执行。脚本会：
     - 检查 Docker 是否运行。
     - 配置镜像源（若未配置）。
     - 通过 Docker Compose 启动服务。
   - **示例输出**：
     ```
     Checking if Docker is installed and running...
     Docker version 28.1.1, build 4eba377
     Verifying project directory...
     Configuring Docker registry mirrors...
     daemon.json already exists. Please ensure it contains valid registry mirrors
     Stopping and removing existing containers if any...
     Note: Data is persisted in volumes and will not be lost when containers are removed.
     Checking if buildx is installed...
     buildx is installed. Using buildx for optimized builds...
     
     Do you want to clear all Docker cache (including build cache, unused images, containers, networks, etc.)? (y/n, default n):
     y
     Clearing all Docker cache...
     Deleted build cache objects:
     m1lo2klxl0afvorolekpoc2jo
     27rlaiv5mdu8wi51u4bh69a1y
     i8vh5fje4xd45szesf34ha4jd
     
     ...
     
     Total reclaimed space: 7.641GB
     Clearing buildx build cache...
     Total:  0B
     Docker cache cleared.
     
     Do you want to build with cache? (y/n, default n):
     n
     Starting Docker Compose services...
     Building images with buildx...
     [+] Building 253.6s (14/14) FINISHED
     
     ...
     
     View build details: docker-desktop://dashboard/build/desktop-linux/desktop-linux/f2x8temy98g2djb9q7jjbmazi
     [+] Running 8/8
     
     ...
     
     Docker has been set up successfully
     Starting Docker containers for backend, frontend, and Redis...
     Press any key to exit...
     Press any key to continue . . .
     
     Microsoft Windows [版本 10.0.19045.5737]
     (c) Microsoft Corporation。保留所有权利。
     
     C:\Users\aFei>docker volume ls
     DRIVER    VOLUME NAME
     local     727e7b7bad6e3145f0c6ecc4af839d2fe769252595f8be0c8c8f87fbcffdd942
     local     mathmodelagent_backend_venv
     local     mathmodelagent_redis_data
     
     C:\Users\aFei>
     ```
   - 关闭命令行窗口。
   
5. **docker container点击映射网页访问MMA即可使用！**
   
   - **提示**：若未安装 buildx，构建可能极慢。建议从 [Docker buildx v0.24.0](https://github.com/docker/buildx/releases/tag/v0.24.0) 下载 `buildx-v0.24.0.windows-amd64.exe`（或根据系统选择版本）：

> - 移动至 `%USERPROFILE%\.docker\cli-plugins`（若无此文件夹则创建）。
>- 重命名为 `docker-buildx.exe`。
> - 在命令行验证：
>   ```
>   C:\Users\YourUser>docker buildx version
>   github.com/docker/buildx v0.24.0 d0e5e86c8b88ae4865040bc96917c338f4dd673c
>   ```

### 常见问题（避坑指南）

**坑点做法**：直接双击 `Docker Desktop Installer.exe` 默认安装。

- **问题**：
  - 默认安装将 Docker 文件和镜像存储在 `%USERPROFILE%\AppData\Local\Docker\wsl\`，C 盘空间告急！
  - 未配置镜像源，导致拉取镜像缓慢或失败。
- **解决办法**：指定安装路径并配置镜像源。

### 资源管理（保护你的硬盘）

- **存储占用**：部署约需 14GB 后续如果有其他镜像，会更大...，默认存储在 `%USERPROFILE%\AppData\Local\Docker\wsl\`。
  ![存储空间占用](../assets/space.png)

- **空间清理**：硬盘空间不足时，运行以下命令：
  
  ```bash
  docker system prune -a
  docker volume prune
  ```
  
- **优化存储**：
  1. 在 Docker Desktop 的 `设置 > 资源` 修改存储路径。
  2. 其他操作：通过符号链接迁移存储路径（**谨慎操作，回滚复杂，只推荐Hacker或者精通win系统的高手使用**）。
