# [MathModelAgent](https://github.com/jihe520/MathModelAgent) 部署脚本 (Bat 版本)

## 配置好python

![image-20250812231948821](./MMA部署(BatVersion).assets/image-20250812231948821.png)

## bat脚本复制到MathModelAgent目录下![image-20250812234251025](./MMA部署(BatVersion).assets/image-20250812234251025.png)

![image-20250812234354873](./MMA部署(BatVersion).assets/image-20250812234354873.png)

## 填好配置文件（以deepseek为例）

![image-20250812232401901](./MMA部署(BatVersion).assets/image-20250812232401901.png)

如果使用中转 provider 需填写两遍（`provider/provider/your-ai-model`）
在上述示例中 `deepseek` 就是 `provider`
使用中转需要填写 `DEFAULT_BASE_URL` 参数

## 双击 bat 脚本运行

运行情况请查看视频:[MathModelAgent部署视频](../assets/mma_setup_run(bat).mp4)

## 后续再次启动 MathModelAgent

将如下脚本![image-20250813000647796](./MMA部署(BatVersion).assets/image-20250813000647796.png)

粘贴到MathModelAgent目录下

![image-20250813000731178](./MMA部署(BatVersion).assets/image-20250813000731178.png)

双击即可。

运行情况查看视频：[MathModelAgent再次运行](../assets/mma_run(bat).mp4)
