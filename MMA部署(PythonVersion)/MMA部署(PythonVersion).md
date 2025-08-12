# [MathModelAgent](https://github.com/jihe520/MathModelAgent) 部署脚本 （Python 版本）

## 配置好 python、redis、nodejs![image-20250812232025238](./MMA部署(PythonVersion).assets/image-20250812232025238.png)![image-20250812231948821](./MMA部署(PythonVersion).assets/image-20250812231948821.png)

![image-20250812231919254](./MMA部署(PythonVersion).assets/image-20250812231919254.png)

## 目标脚本复制到MathModelAgent目录下![image-20250812232055977](./MMA部署(PythonVersion).assets/image-20250812232055977.png)

![image-20250812232127129](./MMA部署(PythonVersion).assets/image-20250812232127129.png)

## 填好配置文件（以deepseek为例）

![image-20250812232401901](./MMA部署(PythonVersion).assets/image-20250812232401901.png)

如果使用中转 provider 需填写两遍（`provider/provider/your-ai-model`）
在上述示例中 `deepseek` 就是 `provider`
使用中转需要填写 `DEFAULT_BASE_URL` 参数

## 运行python脚本

运行情况请观看视频：[MathModelAgent部署视频](../assets/mma_setup_run(python).mp4)

