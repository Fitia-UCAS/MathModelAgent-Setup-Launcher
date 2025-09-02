# app/utils/track.py

# 1 导入依赖
from litellm.integrations.custom_logger import CustomLogger
import litellm


# 2 指标收集器
class AgentMetrics(CustomLogger):
    # 2.1 成功事件
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            print("agent_name", kwargs["litellm_params"]["metadata"]["agent_name"])
        except:
            pass

    # 2.2 失败事件
    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        print(f"On Async Failure")


# 3 全局实例
agent_metrics = AgentMetrics()
