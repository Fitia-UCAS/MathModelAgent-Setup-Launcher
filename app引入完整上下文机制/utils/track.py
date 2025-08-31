import litellm
from app.utils.redis_manager import redis_manager


# track_cost_callback
def track_cost_callback(
    kwargs,  # kwargs to completion
    completion_response,  # response from completion
    start_time,
    end_time,  # start/end time
):
    try:
        response_cost = kwargs.get("response_cost", 0)
        total_tokens = kwargs.get("total_tokens", 0)

        # redis
        print("streaming response_cost", response_cost)
        print("streaming total_tokens", total_tokens)

        # 更新redis
        return
    except Exception as e:
        print(e)
        pass


# set callback
litellm.success_callback = [track_cost_callback]  # set custom callback function
