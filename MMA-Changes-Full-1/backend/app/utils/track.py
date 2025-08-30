from litellm.integrations.custom_logger import CustomLogger
import litellm


class AgentMetrics(CustomLogger):
    #### ASYNC ####

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            # response_cost = kwargs.get("response_cost", 0)
            # print("streaming response_cost", response_cost)
            print("agent_name", kwargs["litellm_params"]["metadata"]["agent_name"])
        except:
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        print(f"On Async Failure")


# 全局指标收集器实例
agent_metrics = AgentMetrics()
