from app.core.agents import WriterAgent, CoderAgent
from app.core.llm import LLM, simple_chat
from app.models.model import CoderToWriter
from app.schemas.request import Problem
from typing import Optional
from app.schemas.response import SystemMessage
from app.utils.log_util import logger
from app.utils.common_utils import create_work_dir, get_config_template
from app.models.user_output import UserOutput
from app.config.setting import settings
from app.tools.interpreter_factory import create_interpreter
import json
from app.utils.redis_manager import redis_manager
from app.utils.notebook_serializer import NotebookSerializer
import hashlib
from app.tools.base_interpreter import BaseCodeInterpreter
from datetime import datetime

class WorkFlow:
    def __init__(self):
        pass

    def execute(self) -> str:
        pass

class MathModelWorkFlow(WorkFlow):
    def __init__(self):
        super().__init__()
        self.task_id: str = ""
        self.work_dir: str = ""
        self.ques_count: int = 0
        self.questions: dict[str, str | int] = {}
        logger.info(f"[DEBUG] MathModelWorkFlow instance created: {id(self)}, questions initialized: {hasattr(self, 'questions')}")

    async def execute(self, problem: Problem, resume_from_step: Optional[str] = None):
        self.task_id = problem.task_id
        self.work_dir = create_work_dir(self.task_id)

        async def update_status(current_step: str, status: str = "running", error_message: Optional[str] = None):
            status_key = f"status:{self.task_id}"
            try:
                current_status_str = await redis_manager.get(status_key)
                current_status = json.loads(current_status_str) if current_status_str else {}
                current_status["current_step"] = current_step
                current_status["status"] = status
                current_status["last_updated"] = datetime.utcnow().isoformat()
                if error_message:
                    current_status["error_message"] = error_message
                if status == "completed_step" and current_step not in current_status.get("completed_steps", []):
                    current_status.setdefault("completed_steps", []).append(current_step)
                await redis_manager.set(status_key, json.dumps(current_status))
            except Exception as e:
                logger.error(f"Failed to update Redis status for task {self.task_id}: {e}")

        llm_model = LLM(
            api_key=settings.API_KEY,
            model=settings.MODEL,
            base_url=settings.BASE_URL,
            task_id=self.task_id,
        )

        await redis_manager.publish_message(self.task_id, SystemMessage(content="正在拆解问题问题"))
        await self.format_questions(problem.ques_all, llm_model)
        logger.info(f"[DEBUG] After format_questions, questions attribute exists: {hasattr(self, 'questions')}, content: {self.questions}")

        user_output = UserOutput(work_dir=self.work_dir)
        notebook_serializer = NotebookSerializer(work_dir=self.work_dir)

        await redis_manager.publish_message(self.task_id, SystemMessage(content="正在创建代码沙盒环境"))
        code_interpreter = await create_interpreter(
            kind="local",
            task_id=self.task_id,
            work_dir=self.work_dir,
            notebook_serializer=notebook_serializer,
            timeout=3000,
        )
        await redis_manager.publish_message(self.task_id, SystemMessage(content="创建完成"))

        await redis_manager.publish_message(self.task_id, SystemMessage(content="初始化代码手"))
        coder_agent = CoderAgent(
            task_id=problem.task_id,
            model=llm_model,
            work_dir=self.work_dir,
            max_chat_turns=settings.MAX_CHAT_TURNS,
            max_retries=settings.MAX_RETRIES,
            code_interpreter=code_interpreter,
        )

        status_str = await redis_manager.get(f"status:{self.task_id}")
        completed_steps = json.loads(status_str).get("completed_steps", []) if status_str else []
        
        if resume_from_step and resume_from_step in completed_steps:
            logger.warning(f"Attempted to resume from an already completed step '{resume_from_step}'. Starting from the beginning.")
            resume_from_step = None
        
        start_processing = resume_from_step is None

        solution_steps = self.get_solution_steps()
        config_template = get_config_template(problem.comp_template)

        workspace_state = {
            "cleaned_data_path": None,
            "generated_charts": {}
        }

        for key, value in solution_steps.items():
            if not start_processing and key == resume_from_step:
                start_processing = True
            
            if not start_processing:
                logger.info(f"Skipping already completed step: {key}")
                continue

            # 为每个步骤创建一个新的、干净的CoderAgent实例
            coder_agent = CoderAgent(
                task_id=problem.task_id,
                model=llm_model,
                work_dir=self.work_dir,
                max_chat_turns=settings.MAX_CHAT_TURNS,
                max_retries=settings.MAX_RETRIES,
                code_interpreter=code_interpreter,
            )

            await update_status(current_step=key, status="running")

            # --- 1. 规划阶段 (Conditional Prompting) ---
            shared_context_prompt = ""
            if workspace_state["cleaned_data_path"]:
                shared_context_prompt += f"\n- 在之前的步骤中，已经完成了数据清洗，清洗后的数据保存在 `{workspace_state['cleaned_data_path']}`."
            if workspace_state["generated_charts"]:
                shared_context_prompt += f"\n- 已经生成了以下图表，你可以直接利用这些信息，无需重复生成:\n{json.dumps(workspace_state['generated_charts'], ensure_ascii=False, indent=2)}"
            if not shared_context_prompt:
                shared_context_prompt = "无"

            planning_prompt = ""
            if key == 'eda':
                await redis_manager.publish_message(self.task_id, SystemMessage(content=f"EDA专家开始为 {key} 制定详细分析计划"))
                planning_prompt = f'''你是一位顶尖的数据科学家和数学建模EDA（探索性数据分析）专家。你的任务是为提供的数据制定一个系统、全面且富有洞察力的EDA计划。你的分析必须为后续的建模工作奠定坚实的基础。
**问题背景**:{self.questions["background"]}
---
**你的EDA规划必须严格遵循以下四个侦查步骤**:
**第一步：数据质量评估与清洗策略 (Data Quality Assessment & Cleaning Strategy)**
- **首要任务是评估数据健康状况**。规划出检查以下方面的步骤：
    1.  **缺失值 (Missing Values)**：检查每列的缺失值数量和比例。
    2.  **数据类型 (Data Types)**：检查每列的数据类型是否正确（例如，日期是否为datetime，数字是否为numeric）。
    3.  **异常值与重复值 (Outliers & Duplicates)**：规划检测明显异常值（如通过箱型图或统计摘要）和完全重复行的步骤。
- 基于评估结果，**制定一个清晰的清洗策略**。例如：“计划删除重复行”、“计划使用中位数填充'年龄'列的缺失值”、“计划将'销售额'中的负数视为异常数据进行审查”。
- **重要限制**：此步骤**只进行文本总结和策略制定**，**禁止**为此阶段生成任何可视化图表（如缺失值热力图、重复值报告等）。所有图表都应在后续步骤中创建。
**第二步：单变量分析 (Univariate Analysis)**
- **深入理解每一个变量自身的特性**。规划出分析步骤：
    1.  **对于数值型变量 (Numerical)**：规划生成描述性统计（均值、方差、分位数等），并使用**直方图 (Histogram)** 或 **核密度图 (KDE Plot)** 来可视化其分布形态（是正态、偏态还是双峰？）。
    2.  **对于分类型变量 (Categorical)**：规划统计每个类别的频次和比例，并使用**条形图 (Bar Chart)** 进行可视化。
**第三步：双变量与多变量关系探索 (Bivariate & Multivariate Analysis)**
- **这是发现数据背后故事的关键**。规划出探索变量之间关系的步骤：
    1.  **数值 vs 数值**: 规划使用**散点图 (Scatter Plot)** 查看两个数值变量之间的线性或非线性关系，并使用**相关性热力图 (Correlation Heatmap)** 宏观地展示所有数值变量间的相关性矩阵。
    2.  **数值 vs 分类**: 规划使用**箱型图 (Box Plot)** 或 **小提琴图 (Violin Plot)** 来比较不同类别下数值变量的分布情况。这对于发现不同组间的差异至关重要。
    3.  **分类 vs 分类**: 规划使用**堆叠条形图 (Stacked Bar Chart)** 或**交叉表 (Crosstab)** 来分析两个分类变量之间的关系。
**第四步：综合洞察与最终计划制定 (Synthesize Insights & Formulate Final Plan)**
- 基于以上三步的分析策略，**总结你期望通过EDA回答的核心问题**。
- 最后，将上述所有分析步骤和可视化想法，整合成一份最终的、可执行的JSON计划。在图表的 `purpose` 描述中，必须清晰地阐明**该图表是为了验证哪个猜想或揭示哪个潜在规律**。
- **再次强调**：你的图表清单 (`charts`) 中**不应包含**任何用于展示数据缺失情况或重复值情况的图表。
---
**输出要求**:
请输出一份严格的JSON对象，包含`analysis_plan`, `charts`, `code_suggestions`三个键。
**示例 `charts` 条目**:
[  {{ 
    "type": "相关性热力图",
    "filename": "correlation_heatmap.png",
    "purpose": "为了宏观审视所有数值变量间的线性相关性，快速识别哪些变量之间可能存在多重共线性，并为后续特征选择提供依据。",
    "aesthetics": "使用'coolwarm'色系，数值显示在热力图格子中，保留两位小数。确保坐标轴标签清晰不重叠。"
  }},  {{ 
    "type": "箱型图",
    "filename": "price_by_category_boxplot.png",
    "purpose": "为了比较不同产品类别（category）下，销售价格（price）的分布情况，判断不同类别的产品是否存在显著的价格差异。",
    "aesthetics": "为每个箱体使用不同的柔和色调，清晰标注中位数、四分位数和异常值点。"
  }}]
'''
            else:
                await redis_manager.publish_message(self.task_id, SystemMessage(content=f"首席建模策略师开始为 {key} 制定解决方案"))
                planning_prompt = f'''你是一位顶级的数学建模竞赛首席建模策略师。你的核心任务是为下面的问题设计一个最优的、增量的解决方案。你必须遵循“简洁、高效、创新、可视化驱动”的原则。
**已知上下文信息**:{shared_context_prompt}
**当前待解决的问题**:{value["coder_prompt"]}
---
**你的工作流程必须严格遵循以下思考框架**:
**第一步：问题诊断 (Problem Diagnosis)**
- 深入分析这个问题的核心数学本质是什么？（例如：这是一个**优化问题**？**综合评价问题**？**预测问题**？**分类问题**？还是**动态模拟问题**？）
**第二步：方法头脑风暴 (Method Brainstorming)**
- 基于问题诊断，列出至少两种可以解决此问题的**不同**建模方法或技术路径。
- 对于每种方法，简要说明其优缺点。
**第三步：方案选择与论证 (Method Selection & Justification)**
- 从头脑风暴的多种方法中，选择一个你认为**最合适**的方案。
- **详细论证你做出此选择的原因**。请从数据量、问题复杂度、预期效果、以及竞赛时间的限制等角度进行说明。这部分是重点。
**第四步：制定最终执行计划 (包含专业级可视化策略)**
基于你选择并论证过的最佳方案，制定详细的执行步骤。在规划图表时，你必须像一个数据故事的导演，精心设计每一个视觉元素来增强说服力。
**A. 核心可视化策略与图表选择**:
**在你在【第一步】中诊断出问题类型后，你必须优先从下方对应类别的『高影响力图表库』中选择最合适的图表类型来呈现结果**，而不是仅仅使用基础的折线图和柱状图,中文字体用微软雅黑表示。
---
**[高影响力图表库]**
#### 1. 优化类问题 (Optimization Problems)
| 图表类型 | 适用场景 | 为何能“眼前一亮”？ |
| :--- | :--- | :--- |
| **三维曲面图/网格图** | 展示目标函数在两个连续变量下的三维形态。 | 提供直观空间感，清晰展示全局与局部最优点。 |
| **平行坐标图** | 处理多目标优化问题，展示不同方案在多目标下的权衡。 | 清晰展示不同方案的“优劣剖面”，是多目标决策可视化的利器。 |
| **甘特图** | 项目调度、任务排序、资源分配等优化。 | 行业标准，体现专业性，清晰展示任务时序与并行关系。 |
| **弦图 (Chord Diagram)** | 展示实体间的流量、转移或关系强度，适合网络流或分配问题。 | 视觉优美且信息密集，艺术化地展示复杂流转关系。 |
#### 2. 综合评价类问题 (Evaluation/Decision-Making Problems)
| 图表类型 | 适用场景 | 为何能“眼前一亮”？ |
| :--- | :--- | :--- |
| **雷达图 (蜘蛛图)** | 在多个评价指标下，对比不同方案或对象的综合表现。 | 直观展示方案的“能力形状”，看出其综合表现的均衡性与优劣。 |
| **旭日图 (Sunburst)** | 展示具有层级结构的评价指标及其权重。 | 视觉吸引力强，清晰展示从顶层目标到底层指标的权重分解。 |
| **Tornado 图** | 进行敏感性分析，按影响力大小对影响评价结果的变量排序。 | 一眼看出影响结果的**最关键因素**，体现对模型的深刻洞察。 |
#### 3. 预测与分类问题 (Prediction & Classification Problems)
| 图表类型 | 适用场景 | 为何能“眼前一亮”？ |
| :--- | :--- | :--- |
| **混淆矩阵热力图** | 分类问题中，可视化模型预测结果与真实标签的对应情况。 | 深入揭示模型“错在哪里”，而非简单给出准确率数字。 |
| **ROC 曲线 / P-R 曲线** | 分类问题中，全面评估一个二分类器在不同阈值下的性能。 | 评估分类器的“金标准”，体现了对模型评估方法的专业理解。 |
| **特征重要性图** | 展示不同输入特征对最终预测结果的贡献度大小。 | 清晰地告诉评委“哪些信息最有用”，体现对数据和模型的深入理解。 |
#### 4. 模拟与动态系统问题 (Simulation & Dynamic Systems)
| 图表类型 | 适用场景 | 为何能“眼前一亮”？ |
| :--- | :--- | :--- |
| **流场图/向量场图** | 模拟流体、交通或人群的运动方向和速度。 | 动态、直观地展示“流”的模式，如交通拥堵的形成。 |
| **相图 (Phase Portrait)** | 分析由微分方程描述的动态系统的长期行为与稳态。 | 动态系统分析的专业工具，体现扎实的理论功底。 |
| **山脊图 (Joy Plot)** | 展示一个变量的分布随时间或某个分类变量演变的情况。 | 优雅且信息丰富，能清晰地看出分布形态的演变过程。 |
---
**B. 输出格式要求**:
请将你的最终执行计划格式化为一个严格的 JSON 对象。
- `justification` 字段：填充你在第三步中的详细论证。
- `analysis_plan` 字段：填充详细的分析步骤。**其中包含一个明确的“结果呈现”步骤，指令代码手在代码执行的最后，使用`print()`函数清晰地输出所有核心数值结果（如最优值、预测值、评价得分等），并附带说明。** - `charts` 列表：
- `charts` 列表：
    - **必须**包含 `type` 字段，明确指出你从图表库中选择的图表类型。
    - `purpose` 字段必须清晰说明**这个图表是为了讲述什么故事或证明什么观点**。
    - `aesthetics` 字段必须包含对**美观性、配色和布局的专业级期望**，确保图表达到出版物级别。
格式如下:
{{ 
  "justification": "...",
  "analysis_plan": "...",
  "charts": [
    {{ 
      "type": "雷达图", 
      "filename": "solution_comparison_radar.png",
      "purpose": "为了在成本、效率、用户满意度、稳定性和创新性五个维度下，直观对比方案A、B、C的综合表现，从而论证我们选择的方案A在综合性能上的优越性。",
      "aesthetics": "使用三种高对比度的专业色系区分三个方案，图例清晰。雷达图的背景网格线使用淡灰色，标签字体清晰易读。"
     }}
  ],
  "code_suggestions": {{ ... }}
}}
'''
            
            plan_str = await simple_chat(llm_model, [{"role": "user", "content": planning_prompt}])
            try:
                # It's safer to find the JSON block than to just replace ```json
                json_start = plan_str.find('{')
                json_end = plan_str.rfind('}') + 1
                if json_start != -1 and json_end != 0:
                    plan = json.loads(plan_str[json_start:json_end])
                else:
                    raise json.JSONDecodeError("No JSON object found", plan_str, 0)

                if not isinstance(plan, dict):
                    logger.error(f"为 {key} 制定计划失败，LLM返回的不是一个有效的JSON对象，将使用无计划的通用提示。")
                    plan = {"analysis_plan": "", "charts": [], "code_suggestions": {}}
            except json.JSONDecodeError as e:
                logger.error(f"为 {key} 制定计划失败，JSON解析错误: {e}，将使用无计划的通用提示。")
                plan = {"analysis_plan": "", "charts": [], "code_suggestions": {}}

            # --- 2. 执行阶段 ---
            await redis_manager.publish_message(self.task_id, SystemMessage(content=f"代码手开始根据最终计划求解 {key}"))
            coder_prompt_with_plan = f'''{value["coder_prompt"]}\n\n重要指令：请严格遵循以下由首席建模策略师制定的最终计划进行分析，并生成所有要求的图表。\n最终计划详情：\n{json.dumps(plan, ensure_ascii=False, indent=2)}'''
            coder_response = await coder_agent.run(
                prompt=coder_prompt_with_plan, subtask_title=key
            )

            # --- 更新共享工作区状态 ---
            if coder_response.status == "success":
                await redis_manager.publish_message(self.task_id, SystemMessage(content=f"代码手求解成功{key}", type="success"))
                if key == 'eda' and not workspace_state["cleaned_data_path"]:
                    files_in_step = await code_interpreter.get_created_files(key)
                    for file in files_in_step:
                        if 'clean' in file.lower() and (file.endswith('.csv') or file.endswith('.xlsx')):
                            workspace_state["cleaned_data_path"] = file
                            logger.info(f"共享工作区状态更新：找到清洗后的数据文件 -> {file}")
                            break
                
                if plan.get('charts') and isinstance(plan['charts'], list):
                    for chart in plan['charts']:
                        if isinstance(chart, dict) and 'filename' in chart and 'purpose' in chart:
                            workspace_state['generated_charts'][chart['filename']] = chart['purpose']
                            logger.info(f"共享工作区状态更新：新增图表 -> {chart['filename']}")
                        else:
                            logger.warning(f"跳过无效的图表条目: {chart}")
            else:
                logger.error(f"代码手在任务 {key} 中失败，跳过写作阶段。")
                await update_status(current_step=key, status="failed", error_message=coder_response.summary)
                raise Exception(f"子任务 '{key}' 失败: {coder_response.summary}")

            # --- 3. 写作阶段 ---
            writer_prompt = self.get_writer_prompt(
                key, coder_response, config_template, plan
            )
            await update_status(current_step=f"write_{key}", status="running")
            await redis_manager.publish_message(self.task_id, SystemMessage(content=f"论文手开始写{key}部分"))

            writer_agent = WriterAgent(
                task_id=problem.task_id,
                model=llm_model,
                comp_template=problem.comp_template,
                format_output=problem.format_output,
            )
            writer_response = await writer_agent.run(
                writer_prompt,
                available_images=await code_interpreter.get_created_images(key),
                sub_title=key,
            )
            await redis_manager.publish_message(self.task_id, SystemMessage(content=f"论文手完成{key}部分"))
            user_output.set_res(key, writer_response)
            await update_status(current_step=f"write_{key}", status="completed_step")

        await code_interpreter.cleanup()
        logger.info(user_output.get_res())

        flows = self.get_write_flows(user_output, config_template, problem.ques_all)
        for key, value in flows.items():
            if not start_processing and key == resume_from_step:
                start_processing = True

            if not start_processing:
                logger.info(f"Skipping already completed step: {key}")
                continue

            await update_status(current_step=key, status="running")
            await redis_manager.publish_message(self.task_id, SystemMessage(content=f"论文手开始写{key}部分"))

            writer_agent = WriterAgent(
                task_id=problem.task_id,
                model=llm_model,
                comp_template=problem.comp_template,
                format_output=problem.format_output,
            )
            writer_response = await writer_agent.run(prompt=value, sub_title=key)
            user_output.set_res(key, writer_response)
            await update_status(current_step=key, status="completed_step")

        logger.info(user_output.get_res())

        user_output.save_result(ques_count=self.ques_count)

    async def format_questions(self, ques_all: str, model: LLM) -> None:
        """用户输入问题 使用LLM 格式化 questions"""
        hash_object = hashlib.sha256(ques_all.encode())
        cache_key = f"cache:format_questions:{hash_object.hexdigest()}"

        cached_result = await redis_manager.get(cache_key)
        if cached_result:
            logger.info(f"Cache hit for format_questions. Loading from cache key: {cache_key}")
            try:
                self.questions = json.loads(cached_result)
                if not isinstance(self.questions, dict):
                    logger.error(f"缓存的格式化问题不是一个有效的JSON对象，将使用空问题。")
                    self.questions = {}
                    self.ques_count = 0
                    return
                self.ques_count = self.questions["ques_count"]
                logger.info(f"questions (from cache):{self.questions}")
                return
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(f"Error loading from cache: {e}. Proceeding to fetch from LLM.")
        
        logger.info("Cache miss for format_questions. Calling LLM.")

        from app.core.prompts import FORMAT_QUESTIONS_PROMPT

        history = [
            {
                "role": "system",
                "content": FORMAT_QUESTIONS_PROMPT,
            },
            {"role": "user", "content": ques_all},
        ]
        json_str = await simple_chat(model, history)
        json_str = json_str.replace("```json", "").replace("```", "").strip()

        if not json_str:
            raise ValueError("返回的 JSON 字符串为空，请检查输入内容。")

        try:
            self.questions = json.loads(json_str)
            if not isinstance(self.questions, dict):
                logger.error(f"LLM返回的格式化问题不是一个有效的JSON对象，将使用空问题。")
                self.questions = {}
                self.ques_count = 0
                return
            self.ques_count = self.questions["ques_count"]
            logger.info(f"questions:{self.questions}")

            await redis_manager.set(cache_key, json_str, ex=86400)
            logger.info(f"Saved new result to cache key: {cache_key}")

        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析错误: {e}")

    def get_solution_steps(self):
        if not isinstance(self.questions, dict):
            logger.error(f"self.questions不是一个有效的字典，无法获取解决方案步骤。当前self.questions: {self.questions}")
            return {}
        questions_quesx = {
            key: value
            for key, value in self.questions.items()
            if key.startswith("ques") and key != "ques_count"
        }
        ques_flow = {
            key: {
                "coder_prompt": f"""
                        完成如下问题{value}
                    """,
            }
            for key, value in questions_quesx.items()
        }
        flows = {
            "eda": {
                "coder_prompt": f"""
                        对当前目录下数据进行EDA分析(数据清洗,可视化),清洗后的数据保存当前目录下,**不需要复杂的模型**
                    """,
            },
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": f"""
                        根据上面建立的模型，选择一个模型，完成敏感性分析
                    """,
            },
        }
        return flows

    def get_writer_prompt(
        self,
        key: str,
        coder_result: CoderToWriter,
        config_template: dict,
        plan: dict,
    ) -> str:
        """根据不同的key生成对应的writer_prompt

        Args:
            key: 任务类型
            coder_result: CoderAgent的完整返回结果
            config_template: 写作模板
            plan: 包含 justification, analysis_plan 和 charts 的规划字典

        Returns:
            str: 生成的writer_prompt
        """
        coder_summary = coder_result.summary
        code_execution_result = coder_result.code_execution_result

        # 将首席策略师的规划内容注入到提示中
        plan_prompt_addition = f"\n\n首席建模策略师的分析:\n方法论证: {plan.get('justification', '无')}\n分析思路: {plan.get('analysis_plan', '无')}\n图表清单:\n"
        if plan.get('charts') and isinstance(plan['charts'], list):
            for chart in plan['charts']:
                if isinstance(chart, dict):
                    plan_prompt_addition += f"- 文件名: {chart.get('filename', 'N/A')}, 目的: {chart.get('purpose', 'N/A')}\n"
                else:
                    logger.warning(f"在get_writer_prompt中跳过无效的图表条目: {chart}")
        else:
            plan_prompt_addition += "无图表生成计划。\n"

        questions_quesx_keys = self.get_questions_quesx_keys()
        bgc = ""
        if isinstance(self.questions, dict) and "background" in self.questions:
            bgc = self.questions["background"]
        else:
            logger.error(f"self.questions不是一个有效的字典或缺少'background'键，无法获取背景信息。当前self.questions: {self.questions}")
            bgc = "[背景信息缺失]"

        quesx_writer_prompt = {}
        for k in questions_quesx_keys:
            prompt_content = f"\n问题背景: {bgc}\n代码手执行总结: {coder_summary}\n代码输出摘要: {code_execution_result}\n{plan_prompt_addition}\n请根据以上所有信息,并严格按照如下模板撰写: {config_template.get(k, '')}\n"
            quesx_writer_prompt[k] = prompt_content

        writer_prompt = {
            "eda": f"\n问题背景: {bgc}\n代码手执行总结: {coder_summary}\n代码输出摘要: {code_execution_result}\n{plan_prompt_addition}\n请根据以上所有信息,并严格按照如下模板撰写: {config_template.get("eda", '')}\n",
            **quesx_writer_prompt,
            "sensitivity_analysis": f"\n问题背景: {bgc}\n代码手执行总结: {coder_summary}\n代码输出摘要: {code_execution_result}\n{plan_prompt_addition}\n请根据以上所有信息,并严格按照如下模板撰写: {config_template.get("sensitivity_analysis", '')}\n",
        }

        if key in writer_prompt:
            return writer_prompt[key]
        else:
            raise ValueError(f"未知的任务类型: {key}")

    def get_questions_quesx_keys(self) -> list[str]:
        """获取问题1,2...的键"""
        return list(self.get_questions_quesx().keys())

    def get_questions_quesx(self) -> dict[str, str]:
        """获取问题1,2,3...的键值对"""
        questions_quesx = {
            key: value
            for key, value in self.questions.items()
            if key.startswith("ques") and key != "ques_count"
        }
        return questions_quesx

    def get_write_flows(
        self, user_output: UserOutput, config_template: dict, bg_ques_all: str
    ):
        model_build_solve = user_output.get_model_build_solve()
        flows = {
            "firstPage": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["firstPage"]}，撰写标题，摘要，关键词""",
            "RepeatQues": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["RepeatQues"]}，撰写问题重述""",
            "analysisQues": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["analysisQues"]}，撰写问题分析""",
            "modelAssumption": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["modelAssumption"]}，撰写模型假设""",
            "symbol": f"""不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["symbol"]}，撰写符号说明部分""",
            "judge": f"""不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["judge"]}，撰写模型的评价部分""",
            "reference": f"""不需要编写代码,根据模型的求解的信息{model_build_solve}，可以生成参考文献,按照如下模板撰写：{config_template["reference"]}，撰写参考文献""",
        }
        return flows
