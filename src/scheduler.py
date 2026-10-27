"""DeRT 截止期调度器 v1：双档路由 + 收尾时限制（周 3 计划提前落地核心）。

v0（agent.py 内嵌）只有一条规则：剩余 < 0.5s 强制收尾。
v1 依据 2026-08-25 粗版 Pareto 数据（eval/results/pareto_20260825_1519.json）：

  ① 强档任务实测总耗时均值 ≈ 3.0s（P50≈2.4s，最慢 6.5s）
     → 路由规则：任务首步 remaining >= STRONG_REMAINING_MIN(=3.5s) 用强档，
       否则快档。宽预算吃强档 100% 正确率，紧预算吃快档速度。
  ② v0 病灶：wrap_up 触发后收尾 LLM 调用本身耗 2–3s（qwen 生成长答案）
     → 收尾改造：max_tokens 上限 + 触发阈值提到 MIN_STEP_BUDGET_V1(=1.2s)。

v1.1 两处修订（出处：eval/results/pareto_20260825_1845.json 的失败模式分析）：

  ① 粘性路由（治 4s 档）：v1 逐步判档，4s 任务首步走强档后剩余 ~2.9s < 3.5s，
     第二步被降级到快档——中途换档质量崩塌（T2-1 第二步 qwen 泄漏 tool_call 原文），
     该档 v1 仅 40% vs 纯强档 80%。路由本意是任务级决策（整个强档任务塞不塞得进
     剩余预算）→ 档位首步一次定死，中途不降级（wrap_up 兜底防超时）。
  ② 自适应收尾上限（治 2s 档）：固定 150 tokens 在 qwen（实测 ~30 tok/s 生成）
     要 3–5s，紧预算下收尾调用自身超时（T3-1 收尾耗 ~4.5s）。
     → max_tokens = clamp(剩余秒数 × 40 tok/s, 30, 150)。

EMA 钩子已留（observe_task 在线更新强档任务耗时估计），v2 已启用参与路由。

v2 = v1.1 + 量化路由 + 收尾数值优先（依据 50 任务 × 3 重复数据，
出处 eval/results/pareto_20260825_2035.json）：

  ① 量化路由（治"死常数"）：强档任务实测 P50=2.98s / P75=3.89s / P95=7.83s，
     v1.1 固定阈值 3.5 恰为 P50+0.5（本晚数据下校准良好），但它是常数——
     deepseek TTFT 单晚可漂移（0.4–1.6s），后端变慢时固定阈值会持续把塞不下
     的任务推上强档然后超时。v2 用滑窗 P60 在线估计替代：后端变慢 → 估计
     上升 → 紧预算档自动回落快档。冷启动（窗口样本 < 8）退化为 EMA 先验
     3.0 + 0.5 边际 = 3.5，与 v1.1 行为一致（优雅退化）。
  ② 收尾数值优先（治 2s 档内容截断）：v1.1 收尾 max_tokens 下限 30，实测
     2s 档 64/150 内容错且全部是 2 步任务（step1 工具 + step2 收尾）——
     30 tokens 装不下"数值+判断词"完整句，截断丢判分点。v2 两手：
     下限提到 40 + wrap_up_hint() 给 loop 追加"第一行先给数值与判断结论"
     指令——截断只伤解释、不伤判分点（成功判定只查数值与判断词）。

与 loop 的接线（见 agent.run 的 scheduler 参数）：
    scheduler.route(remaining, step) -> "fast_model名" | "strong_model名"
    scheduler.min_step_budget()      -> 本次的 wrap_up 触发阈值
    scheduler.wrap_up_max_tokens(remaining) -> 收尾调用 max_tokens（None=不限）
    scheduler.wrap_up_hint()         -> 收尾指令追加语（None=不追加，v1.1 行为）
    scheduler.observe_call(model, total)   -- 由 DualTierLLM 每次调用后回报
    scheduler.observe_task(model, total)   -- 由 loop 在 run 结束时回报
"""
from __future__ import annotations

from collections import deque

# ---- v1 路由常数（出处：2026-08-25 实测，见模块 docstring）----
STRONG_REMAINING_MIN = 3.5   # 任务首步剩余预算高于此值 → 整任务用强档。
# 取值依据：强档任务 P50≈2.4s + 1s 余量。曾取 4.0，实测 4s 预算档因首步
# 流逝数毫秒而落到 3.99…s 被判快档（边界效应），该档强档实测 80% vs 快档 50% → 下调。
MIN_STEP_BUDGET_V1 = 1.2     # wrap_up 触发阈值（v0 是 0.5，实测收尾调用要 1~2.5s）
WRAP_UP_MAX_TOKENS = 150     # 收尾答案 token 上限（宽余量时的天花板）
WRAP_UP_TOK_PER_SEC = 40     # 收尾生成速度估计（qwen 实测 ~30 tok/s，留 25% 余量）
WRAP_UP_MIN_TOKENS = 30      # 下限：低于此装不下"数值+判断"的最短合格答案

_EMA_ALPHA = 0.3
_STRONG_TASK_PRIOR = 3.0     # 强档任务耗时 EMA 初值 = 粗版实测均值

# ---- v2 常数（出处：eval/results/pareto_20260825_2035.json，50 任务 × 3 重复）----
ROUTE_MARGIN = 0.5           # 路由安全边际：est + margin <= remaining 才上强档
# 与 v1.1 对齐：先验 3.0 + 0.5 = 3.5（本晚数据下 P50=2.98，恰为 v1.1 阈值）
_ROUTE_WINDOW = 40           # 强档任务耗时滑窗长度
_ROUTE_MIN_SAMPLES = 8       # 窗口样本不足时退化为 EMA 先验（冷启动）
_ROUTE_QUANTILE = 0.60       # 路由分位数：P60（P50 的中心性 + P75 的稳健性折中）
WRAP_UP_MIN_TOKENS_V2 = 40   # v2 收尾 token 下限（v1.1=30 实测截断丢数值；
# 取 40 而非 45：小子集实测 floor 45 的生成耗时使边界任务系统性迟到 ~0.3s，
# hint（数值优先）才是内容修复主力，floor 只做保险不主动赌迟到）


class DeadlineScheduler:
    """预算感知路由策略（纯决策，不持有客户端）。"""

    def __init__(self, fast_model: str, strong_model: str):
        self.fast_model = fast_model
        self.strong_model = strong_model
        self._ema_strong_task = _STRONG_TASK_PRIOR
        self._tier: str | None = None   # 粘性路由：本任务的档位（首步定死）

    # ---- 决策接口（agent.run 每步调用）----

    def route(self, remaining: float | None, step: int = 1) -> str:
        """返回本步应使用的模型名（任务级决策：首步定档，中途不降级）。"""
        if step == 1 or self._tier is None:
            if remaining is None or remaining >= STRONG_REMAINING_MIN:
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier

    def min_step_budget(self) -> float:
        return MIN_STEP_BUDGET_V1

    def wrap_up_max_tokens(self, remaining: float | None = None) -> int | None:
        """收尾调用的 max_tokens：随剩余预算自适应，保证收尾按时完成。"""
        if remaining is None:
            return WRAP_UP_MAX_TOKENS
        return max(WRAP_UP_MIN_TOKENS,
                   min(WRAP_UP_MAX_TOKENS, int(remaining * WRAP_UP_TOK_PER_SEC)))

    def wrap_up_hint(self) -> str | None:
        """收尾指令追加语：基类不追加（v0/v1.1 行为不变），v2 覆写。"""
        return None

    # ---- 观测接口（EMA 在线更新，后续参与路由）----

    def observe_call(self, model: str, total: float) -> None:
        """单次 LLM 调用耗时回报（由 DualTierLLM 调用）。暂只积累，不影响决策。"""

    def observe_task(self, model: str, total: float, wrapped: bool = False) -> None:
        """整任务耗时回报（由 loop 结束时调用）。wrapped=收尾截断（v3d 过滤用）。"""
        if model == self.strong_model:
            self._ema_strong_task = (
                _EMA_ALPHA * total + (1 - _EMA_ALPHA) * self._ema_strong_task)
class DeadlineSchedulerV2(DeadlineScheduler):
    """v2 = v1.1 + 量化路由 + 收尾数值优先（设计依据见模块 docstring）。

    路由从"固定阈值开环"变为"在线估计闭环"：强档任务耗时滑窗 P60 + 0.5s
    边际决定是否上强档；后端变慢 → 估计上升 → 紧预算自动回落快档。
    收尾从"截断听天由命"变为"判分点前置"：token 下限 30→45 +
    数值优先指令，截断只伤解释、不伤判分点。
    """

    def __init__(self, fast_model: str, strong_model: str):
        super().__init__(fast_model, strong_model)
        self._strong_window: deque[float] = deque(maxlen=_ROUTE_WINDOW)

    # ---- 决策接口 ----

    def _strong_task_estimate(self) -> float:
        """强档任务耗时在线估计：窗口够用取 P60，冷启动退化为 EMA 先验。"""
        if len(self._strong_window) >= _ROUTE_MIN_SAMPLES:
            xs = sorted(self._strong_window)
            k = min(len(xs) - 1, int(_ROUTE_QUANTILE * (len(xs) - 1)))
            return xs[k]
        return self._ema_strong_task

    def route(self, remaining: float | None, step: int = 1) -> str:
        """任务级粘性路由（同 v1.1），阈值由在线估计替代固定常数。"""
        if step == 1 or self._tier is None:
            if (remaining is None
                    or remaining >= self._strong_task_estimate() + ROUTE_MARGIN):
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier

    def wrap_up_max_tokens(self, remaining: float | None = None) -> int | None:
        """v2 收尾上限：下限 30→40（30 实测装不下"数值+判断词"完整句）。"""
        if remaining is None:
            return WRAP_UP_MAX_TOKENS
        return max(WRAP_UP_MIN_TOKENS_V2,
                   min(WRAP_UP_MAX_TOKENS, int(remaining * WRAP_UP_TOK_PER_SEC)))

    def wrap_up_hint(self) -> str | None:
        """数值优先：让判分点（数值 + 判断词）出现在生成最前部。"""
        return ("答案格式要求：第一行直接给出关键数值与判断结论"
                "（如：延迟P95=42.3ms，未超过50ms阈值），之后再补充解释。")

    # ---- 观测接口 ----

    def observe_task(self, model: str, total: float) -> None:
        super().observe_task(model, total)
        if model == self.strong_model:
            self._strong_window.append(total)


class DeadlineSchedulerV21(DeadlineSchedulerV2):
    """v2.1 = v2 的自适应路由 + v1.1 的收尾（路由-only 变体，2026-10-23）。

    依据 sweep2 死区发现（eval/results/budget_sweep2/report.md）：固定阈值
    STRONG_REMAINING_MIN=3.5 按旧强档 deepseek-chat（~2-3s/次）标定，换慢强档
    v4.1-flash（4.5-8.6s/次）后预算 ∈ [3.5s, 强档延迟] 时确定性选择注定迟到的
    模型（4s 档 B 100% 上强、on_time 3%，惨败快档基线 0/56 p<0.001）。

    离线回放（eval/replay_threshold.py，模型校验最大偏差 3.7pp）表明：
    最优固定阈值 8.5s（+15.2pp vs 3.5s），且 6.5-10s 为平坦高原——阈值无需
    精确校准，在线估计落进高原即可拿到几乎全部收益。v2 的滑窗 P60+0.5 边际
    在 v4.1-flash 实测窗口（强档任务 ~4.5-8.6s）下自然落在 ~7s，恰在高原内。

    收尾保持 v1.1 行为：数值优先（wrap_up_hint + token 下限 40）在 50 任务
    时代净 -5pp（失败模式从内容截断搬到迟到），不随路由实验重测——本类只
    验证一个假设：自适应阈值能填平死区。
    """

    def wrap_up_max_tokens(self, remaining: float | None = None) -> int | None:
        return DeadlineScheduler.wrap_up_max_tokens(self, remaining)

    def wrap_up_hint(self) -> str | None:
        return None


# ---- v3 常数（出处：eval/results/budget_sweep2/v3_replay.json，2026-10-23）----
STRONG_REMAINING_MIN_V3 = 8.0  # 难任务上强档的时间门槛（A 基线 on_time@8s=71%
# 才勉强可用；易任务永远快档故不受死区影响——旧死区病理是"易任务被强制上强"，
# v3 结构性规避。固定常数而非在线估计：v2.1 的教训——估计器反馈回路会棘轮锁死）


class DeadlineSchedulerV3(DeadlineScheduler):
    """v3 = 难度感知路由（2026-10-23，"又快又准"的可行形态）。

    依据（eval/replay_v3.py 离线回放，sweep2 实测数据）：快档错误高度集中——
    问句含数值对比措辞的任务快档错 48%，不含的只错 16%。规则：
      难任务（问句含对比措辞）→ 剩余 ≥ 8s 时走强档，否则快档+收尾兜底；
      易任务 → 永远快档。
    回放预测：正确率 88.9%（全快档 73.7）、强档用量 31.3%（全强档省 69%）、
    平均耗时 5.48s（全强档 7.59）。

    与 v2.1 的关系：v2.1 估计"后端快不快"（时间维度，反馈回路会锁死），
    v3 判断"任务难不难"（难度维度，静态规则无回路可锁）。两者正交：
    时间调度管"来不来得及"，难度路由管"值不值得请"。
    """

    CMP_WORDS = ("是否超过", "SLO", "红线", "对照", "是否达标", "超过")

    def __init__(self, fast_model: str, strong_model: str):
        super().__init__(fast_model, strong_model)
        self._prompt = ""

    def set_prompt(self, text: str) -> None:
        """由采集 harness 在每任务开始前注入问句（loop 不感知难度）。"""
        self._prompt = text or ""

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = any(w in self._prompt for w in self.CMP_WORDS)
            if hard and (remaining is None
                         or remaining >= STRONG_REMAINING_MIN_V3):
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier


class DeadlineSchedulerV3B(DeadlineSchedulerV3):
    """v3b = 纯难度路由：难任务永远走强档，易任务永远走快档（不看时间）。

    2026-10-23 在线教训：v3 把"难→强"与"剩余≥8s"耦合后，强档用量从预测的
    31% 被时间门槛砍到 8%，且快档路径在 deadline 压力下被迫交草稿——全局
    correct 62.4%，低于全快档基线 73%（McNemar 107/192, p=1e-06）。
    教训：难度路由回答"值不值得"，时间调度回答"来不来得及"，两者必须解耦——
    质量主张应在宽松预算下与全强档对标，紧预算的质量让时间调度兜底。
    v3b 即解耦后的形态：难度唯一决定档位。
    """

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = any(w in self._prompt for w in self.CMP_WORDS)
            self._tier = self.strong_model if hard else self.fast_model
        return self._tier


# ---- v3c 常数（2026-10-24；冷启动先验 = v3 手拍门 8.0 − 边际 0.5）----
_STRONG_TASK_PRIOR_V3C = 7.5
_ROUTE_MIN_SAMPLES_V3C = 5     # 窗口样本不足时用 EMA 先验（v2 用 8；v3c 的
# 窗口只由强档任务构成、纯度高，5 个样本即可用）
_ROUTE_WINDOW_V3C = 40


class DeadlineSchedulerV3C(DeadlineSchedulerV3):
    """v3c = v3 的难度路由 + 自适应时间门（2026-10-24）。

    把 v3 手拍的 STRONG_REMAINING_MIN_V3=8.0 换成对强档任务耗时的在线
    P60 + 0.5s 边际（窗口机制同 v2）。动因：sweep2 已实证强档延迟漂移会让
    手拍门失准（换慢强档后边界 8s→10s）；固定常数需要人工重标定，在线估计
    不需要——这是"部署形态"的差，不是"峰值性能"的差。

    对 v2.1 两个教训的修正：
    ① 任务构成污染：v2 的窗口被"哪些任务被路由上强"反馈扭曲（估计器把
       任务难度和后端速度混一锅）；v3c 的难度信号前置——易任务永远不进
       强档 → 窗口只由真强档任务构成，污染被难度信号结构性滤除。
    ② 棘轮锁死：v2.1 死锁路径是"est 升 → 强档调用归零 → 窗口断粮 → est
       永远回落不了"；v3c 里宽预算档（30/60s 类）门恒过、持续喂养窗口，
       相当于自带探针——后端恢复时估计能回落（生产混合负载同理）。
    冷启动：窗口 <5 样本时用 EMA 先验 7.5 + 边际 0.5 = 8.0，恰为 v3 的
    手拍值——退化行为与 v3 一致，随后被实测分位数替换。
    """

    def __init__(self, fast_model: str, strong_model: str):
        super().__init__(fast_model, strong_model)
        self._strong_window: deque[float] = deque(maxlen=_ROUTE_WINDOW_V3C)
        self._ema_strong_task = _STRONG_TASK_PRIOR_V3C

    def _gate(self) -> float:
        """强档时间门：窗口 P60 + 边际；冷启动退化为先验（= v3 手拍值）。"""
        if len(self._strong_window) >= _ROUTE_MIN_SAMPLES_V3C:
            xs = sorted(self._strong_window)
            k = min(len(xs) - 1, int(_ROUTE_QUANTILE * (len(xs) - 1)))
            return xs[k] + ROUTE_MARGIN
        return self._ema_strong_task + ROUTE_MARGIN

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = any(w in self._prompt for w in self.CMP_WORDS)
            if hard and (remaining is None or remaining >= self._gate()):
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier

    def observe_task(self, model: str, total: float, wrapped: bool = False) -> None:
        super().observe_task(model, total)   # EMA 更新（先验通道，永不冻结）
        if model == self.strong_model:
            self._strong_window.append(total)


# ---- v3d 常数（2026-10-24；出处 v3c_analysis.json 失败诊断 + 离线门回放网格）----
GATE_MARGIN_V3D = 0.8         # 不对称边际（v2/v3c 用 0.5）：放行一个注定迟到的
# 强档任务的代价（迟到+白花钱）大于误拦一个可行任务（回落快档+收尾仍可用）。
_GATE_STEP_V3D = 0.3          # 门值每任务最大移动量（变速率限制/滞回）：v3c 的
# 失败是门的方差（早期窗口不成熟，P60 在 5-8s 摆动 → 低谷放行 8s 档、尖峰
# 误拦 10s 档）。门移动慢于窗口漂移，抖动被结构性地滤掉。
_GATE_FLOOR_V3D = 8.5         # 门的地板 = 冷启动先验：数据只允许把门从先验
# **向上**推（后端比标定慢），向下回调需要比滑动分位数更强的证据（换更快
# 模型的场景交给标定卡 eval/calibrate_gate.py）。离线回放：无地板时门下探
# 7.88 → 8s 档放行 9 个注定迟到的调用（v3c 同款失败复现）。
_ROUTE_MIN_SAMPLES_V3D = 10   # 信任窗口前需要更多样本（v3c 用 5 太激进：
# 5 个样本的 P60 方差大，正是低谷/尖峰的来源）；不足时门停在地板。
_ROUTE_WINDOW_V3D = 40
_GATE_TRIM_X_V3D = 3.0        # 离群截断：量化前剔除 > 3×窗口中位数的样本
# （强档耗时重尾至 26s，尖峰会把 P60 顶过 9.9 误拦 10s 档）


class DeadlineSchedulerV3D(DeadlineSchedulerV3):
    """v3d = v3 的难度路由 + 稳态自适应门（2026-10-24，v3c 的修复版）。

    v3c 证明了两件事：①门可以在线收敛到手拍值附近（8.6 vs 8.0，无棘轮）；
    ②但门的方差本身是成本——预算≈门界的档位上，门在 7.1-8.6s 间摆动，
    低谷放行注定迟到的强档调用（8s 档 66 vs 78）、尖峰误拦来得及的任务
    （10s 档 70 vs 84）。v3d 四处修复（估计对象不变，全部作用于方差）：
      ①删失过滤：wrap_up 截断的任务耗时（时长被预算截断，v3c 实测截断样本
        P50=13.1s vs 自然完成 7.2s）不进窗口；
      ②离群截断 + 不对称边际 0.8s：重尾被 3×中位数截断，边际向"少放行"偏置；
      ③门的变速率限制：每任务最多移动 ±0.3s——窗口可以抖，门不许追着抖；
      ④先验地板：门不低于 8.5s（=先验 7.5+1.0 对齐 v3 的有效行为：8s 档
        首步剩余 ≈7.9s 被先验直接拦住）。**门只向上自适应（后端变慢→上调），
        向下回调交给标定卡**——滑动分位数不足以安全地下调。
    离线门回放（对 v3c 执行序列反事实重放）：8s 档 0 放行、12s+ 满放行、
    门稳态 ≈8.5-8.6s。

    判定标准（10-24 定）：在线 1000 格与 v3 配对不劣（8-12s 档），8s 档强档
    调用为 0，门轨迹无深谷/尖峰。达成则"自适应门达 v3 水平"。
    """

    def __init__(self, fast_model: str, strong_model: str):
        super().__init__(fast_model, strong_model)
        self._strong_window: deque[float] = deque(maxlen=_ROUTE_WINDOW_V3D)
        self._ema_strong_task = _STRONG_TASK_PRIOR_V3C
        self._gate_val = _GATE_FLOOR_V3D  # 冷启动门 = 先验地板 8.5（≈v3 手拍的有效行为）

    def _update_gate(self) -> None:
        """观察一个（自然完成的）强档任务后更新门值。

        target = 截断后窗口 P60 + 边际；门以 ±_GATE_STEP_V3D 的最大步长向
        target 移动，且永不低于 _GATE_FLOOR_V3D（向上自适应、向下走标定卡）。
        """
        if len(self._strong_window) >= _ROUTE_MIN_SAMPLES_V3D:
            xs = sorted(self._strong_window)
            med = xs[len(xs) // 2]
            xs = [x for x in xs if x <= med * _GATE_TRIM_X_V3D] or xs
            k = min(len(xs) - 1, int(_ROUTE_QUANTILE * (len(xs) - 1)))
            target = xs[k] + GATE_MARGIN_V3D
        else:
            target = self._gate_val   # 样本不足：门停在原地（地板兜底）
        delta = max(-_GATE_STEP_V3D, min(_GATE_STEP_V3D, target - self._gate_val))
        self._gate_val = max(_GATE_FLOOR_V3D, self._gate_val + delta)

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = any(w in self._prompt for w in self.CMP_WORDS)
            if hard and (remaining is None or remaining >= self._gate_val):
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier

    def observe_task(self, model: str, total: float, wrapped: bool = False) -> None:
        super().observe_task(model, total)
        if model == self.strong_model and not wrapped:
            # 删失过滤：被收尾截断的耗时（≈预算值）不反映真实分布，不进窗口。
            # EMA 先验通道仍更新（保留信息，且永不冻结）。
            self._strong_window.append(total)
            self._update_gate()


class DeadlineSchedulerV3Emp(DeadlineSchedulerV3D):
    """v3emp = v3d 机制 + 经验难度信号（2026-10-25，难度评分通用化的在线形态）。

    难度信号从手写中文词表（CMP_WORDS）换成**影子流量失败史**：历史会话中
    快档答错的任务集合（心理测量学 1−p 难度指数的最简形态）。离线依据
    （analyze_difficulty.py，sweep2 N=100 回放）：留出夜次失败史 F1 0.87
    完胜词表 0.53，回放 97% @ 强档 27%（词表 89% @ 31%），几乎贴住 oracle。
    新发现：快档错误跨夜次高度可复现——难度是「任务×快档」的稳定属性。

    边界（如实交底）：
      ①绑定快档模型——换快档=历史作废需重积累（P4 门标定同理）；
      ②新任务冷启动无历史 → 未知任务默认 easy（保守：省成本，质量靠
        结构分层兜底——部署形态 = 分层冷启动 + 影子流量预热后切换）；
      ③回放→在线两步纪律：本类即在线确认步，替换词表前须过对照。
    时间门机制逐字继承 v3d（删失过滤+截断+限速+地板 8.5 单向适应）。
    """

    def __init__(self, fast_model: str, strong_model: str,
                 history: dict[str, bool] | None = None):
        super().__init__(fast_model, strong_model)
        self._history = dict(history or {})   # {task_id: 快档曾答错}
        self._task_id = ""

    def set_task_id(self, tid: str) -> None:
        """由采集 harness 在每任务开始前注入任务标识（loop 不感知难度）。"""
        self._task_id = tid or ""

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = bool(self._history.get(self._task_id, False))
            if hard and (remaining is None or remaining >= self._gate_val):
                self._tier = self.strong_model
            else:
                self._tier = self.fast_model
        return self._tier


class DualTierLLM:
    """双档客户端：把 scheduler 给出的模型名分派到对应后端。

    实现 chat() -> ModelResponse 协议（鸭子类型），可直接注入 loop。
    每次调用后把真实耗时回报给 scheduler。
    """

    def __init__(self, fast, strong, scheduler: DeadlineScheduler):
        self.fast = fast
        self.strong = strong
        self.scheduler = scheduler
        self.base_url = f"dual({fast.base_url}|{strong.base_url})"

    @property
    def model(self) -> str:
        return self.fast.model  # 无 scheduler 时的缺省档

    def chat(self, messages: list, model: str | None = None,
             tools: list | None = None, temperature: float = 0.0,
             max_tokens: int | None = None):
        if model == self.strong.model:
            backend = self.strong
        else:
            backend = self.fast
        resp = backend.chat(messages, model=backend.model, tools=tools,
                            temperature=temperature, max_tokens=max_tokens)
        self.scheduler.observe_call(resp.model, resp.total)
        return resp


# ---- v3e 常数（2026-10-24 晚；出处 eval/results/mid_probe_full.json + 公开价目）----
MID_GATE_V3E = 6.6   # 中档时间门 = 中档 hard 任务耗时 P60 5.8 + 0.8 边际
# （probe_mid 20 难题自然完成分布，与强档标定卡同款公式）。中档 = deepseek-v4-
# flash-0731：hard 20/20 与强档逐题一致、P50 5.8s 快于强档 6.15s、公开价约六折。
# 中档门为标定卡常数（非在线估计）：v3d 的教训是"门的方差在预算≈门时是成本"，
# 中档窗口在线样本更少、方差更大——部署形态 = 每档一张标定卡 + 每档一个门。


class DeadlineSchedulerV3E(DeadlineSchedulerV3D):
    """v3e = 三档阶梯路由（2026-10-24，v3d 的多档扩展）。

    依据 probe_mid 实测：中档（v4-flash-0731）能接住 hard 族且比强档快，
    强档门（8.5s）与中档门（6.6s）之间的预算段此前无解——强档塞不进、
    快档答不对（hard 快档 correct 天花板 74%）。阶梯规则（hard 任务）：
      剩余 ≥ 强档门 → 强档（预算管够时用最好的）；
      中档门 ≤ 剩余 < 强档门 → 中档（服务窗口扩展段）；
      剩余 < 中档门 → 快档 + 收尾兜底。
    易任务永远快档（v3 结构不变）。强档门机制完全继承 v3d（删失过滤+
    截断+限速+地板 8.5 只向上适应）；中档门 = 标定卡常数。
    """

    def __init__(self, fast_model: str, strong_model: str, mid_model: str):
        super().__init__(fast_model, strong_model)
        self.mid_model = mid_model

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = any(w in self._prompt for w in self.CMP_WORDS)
            if hard:
                if remaining is None or remaining >= self._gate_val:
                    self._tier = self.strong_model
                elif remaining >= MID_GATE_V3E:
                    self._tier = self.mid_model
                else:
                    self._tier = self.fast_model
            else:
                self._tier = self.fast_model
        return self._tier


class DeadlineSchedulerV3EEmp(DeadlineSchedulerV3E):
    """v3emp 三档形态 = v3e 中档阶梯 + 经验难度信号换影子流量失败史。

    与 V3Emp（双档）的差异仅在难度判定后的阶梯：hard 任务按剩余预算
    ≥强档门→强档 / ≥中档门→中档 / 否则快档；easy（含冷启动未知任务）
    永远快档。难度来源 {task_id: 快档曾答错}——1−p 最简形态。
    这是服务化部署形态（版本号 v3emp 以此类为主角）。
    """

    def __init__(self, fast_model: str, strong_model: str, mid_model: str,
                 history: dict[str, bool] | None = None):
        super().__init__(fast_model, strong_model, mid_model)
        self._history = dict(history or {})
        self._task_id = ""

    def set_task_id(self, tid: str) -> None:
        self._task_id = tid or ""

    def route(self, remaining: float | None, step: int = 1) -> str:
        if step == 1 or self._tier is None:
            hard = bool(self._history.get(self._task_id, False))
            if not hard:
                self._tier = self.fast_model
            elif remaining is None or remaining >= self._gate_val:
                self._tier = self.strong_model
            elif remaining >= MID_GATE_V3E:
                self._tier = self.mid_model
            else:
                self._tier = self.fast_model
        return self._tier


class TripleTierLLM:
    """三档客户端：按 scheduler 给出的模型名分派到快/中/强后端。

    协议与 DualTierLLM 相同（chat -> ModelResponse，调用后回报耗时）。
    未知名回退快档（保守：错分派宁可慢档也不误上付费档）。
    """

    def __init__(self, fast, mid, strong, scheduler: DeadlineScheduler):
        self.fast = fast
        self.mid = mid
        self.strong = strong
        self.scheduler = scheduler
        self.base_url = f"triple({fast.base_url}|{mid.base_url}|{strong.base_url})"

    @property
    def model(self) -> str:
        return self.fast.model

    def chat(self, messages: list, model: str | None = None,
             tools: list | None = None, temperature: float = 0.0,
             max_tokens: int | None = None):
        backend = {self.mid.model: self.mid,
                   self.strong.model: self.strong}.get(model, self.fast)
        resp = backend.chat(messages, model=backend.model, tools=tools,
                            temperature=temperature, max_tokens=max_tokens)
        self.scheduler.observe_call(resp.model, resp.total)
        return resp
