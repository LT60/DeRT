"""评估任务集（100 任务，对齐 benchmarks/tasks-design.md）+ 规则判分。

分层（tasks-design.md，2026-10-22 由 60 扩到 100）：
    T1 简单查询 ×34：单工具、单轮问答（config 查询 / 单统计值查询）
    T2 单步诊断 ×33：1–2 工具 + 推理判断（数值 vs 阈值 / 双数值）
    T3 复杂诊断 ×33：多工具依赖链（≥2 工具 / 跨窗口对比 / 渲染产物）

判分原则（AGENTS.md §6 定案）：内容判分，不依赖 stop_reason / FINAL 前缀。

判分原理：工具输出为种子随机（同参数恒定），故期望值可预先计算——
judge 只需检查模型回答是否包含正确的数值 / 路径 / 判断词。

原 10 任务（粗版 Pareto 用）的 id 与语义完整保留（T1-1..T1-4、
T2-1..T2-4、T3-1..T3-2），保证新旧数据可对照。
"""
from __future__ import annotations

import re

from src.tools import (_latency_stats, _detection_stats,
                       _ALARM_LOG, _SEGMENTS, _segment_latency, _gpu_stats,
                       _get_config, _alarm_log, _list_segments, _render_report)


# ---------- 判分辅助 ----------

def num_in(answer: str, value: float, pct: bool = False) -> bool:
    """检查数值是否出现在回答中（容忍 .2f / .1f / % 三种写法）。"""
    variants = {f"{value:g}", f"{value:.2f}", f"{value:.1f}"}
    if pct:  # 0.94 也接受 "94%"
        variants.add(f"{value * 100:g}%")
    return any(v in answer for v in variants)


# 双否定写法（实际表达"达到/超过"），先替换成占位符，避免被否定词表误伤
_DOUBLE_NEG = ("不低于", "未低于", "未跌破", "未破线")
# 否定式（表达"未达到/未超过"）
_NEG_WORDS = ("未超过", "不超过", "没有超过", "不超", "低于", "以内", "之下",
              "未突破", "没突破", "没有突破", "未达标", "未达到",
              "未及", "未到", "跌破", "破线")
# 肯定式（表达"超过/达到"）
_POS_WORDS = ("超过", "高于", "超出", "之上", "突破", "达标", "达到")


def above_verdict(answer: str, expected_above: bool) -> bool:
    """判断词校验：expected_above=True 期许"超过/不低于"，False 期许"未超过/低于"。

    两处子串陷阱：
      ① 双否定："未超过"含"超过"、"不低于"含"低于"——先把双否定写法替换成
         占位符再查否定式；
      ② 词表覆盖面：模型会换同义说法（"未突破""未达标""跌破"），2026-09-28
         任务措辞改写为运维口吻后这类说法显著变多。原词表只认 7 个词，会
         **把内容全对的答案判成错**（如问句写"是否突破 15ms 红线"、模型答
         "未突破"就落榜）。此处只做增量扩表，原有判定结果不受影响。
    """
    t = answer
    for w in _DOUBLE_NEG:
        t = t.replace(w, "§POS§")
    neg = any(w in t for w in _NEG_WORDS)
    if not expected_above:
        return neg
    pos = "§POS§" in t or any(w in t for w in _POS_WORDS)
    return pos and not neg


# ---------- 判分工厂（参数化生成，期望值预计算） ----------

def _mk_single(stat: str, n: int, field: str, label: str, pct: bool = False):
    """单数值查询判分：answer 须含该字段值。"""
    stats = _latency_stats(n) if stat == "latency" else _detection_stats(n)
    val = stats[field]

    def check(a: str, _val=val, _pct=pct, _label=label):
        return num_in(a, _val, pct=_pct), f"需含 {_label}={_val:g}"
    return check


def _mk_threshold(stat: str, n: int, field: str, threshold: float,
                  unit: str, label: str, pct: bool = False):
    """阈值判断判分：answer 须含数值 + 正确方向判断词。"""
    stats = _latency_stats(n) if stat == "latency" else _detection_stats(n)
    val = stats[field]
    above = val > threshold

    def check(a: str, _val=val, _above=above, _thr=threshold, _unit=unit,
              _pct=pct, _label=label):
        ok = num_in(a, _val, pct=_pct) and above_verdict(a, _above)
        thr_disp = f"{_thr * 100:g}%" if _pct else f"{_thr:g}{_unit}"
        return ok, (f"需含 {_label}={_val:g} 且判断 "
                    f"{'超过' if _above else '未超过'}{thr_disp}")
    return check


def _mk_pair(stat: str, n: int, f1: str, f2: str, l1: str, l2: str,
             pct1: bool = False, pct2: bool = False):
    """双数值判分（同一次工具调用即可取到两个字段）。"""
    stats = _latency_stats(n) if stat == "latency" else _detection_stats(n)
    v1, v2 = stats[f1], stats[f2]

    def check(a: str, _v1=v1, _v2=v2, _l1=l1, _l2=l2, _p1=pct1, _p2=pct2):
        ok = num_in(a, _v1, pct=_p1) and num_in(a, _v2, pct=_p2)
        return ok, f"需含 {_l1}={_v1:g} 与 {_l2}={_v2:g}"
    return check


def _mk_cross(stat: str, n1: int, n2: int, field: str, label: str,
              pct: bool = False):
    """跨窗口双数值判分（两次工具调用，各窗口一个值）。"""
    s1 = _latency_stats(n1) if stat == "latency" else _detection_stats(n1)
    s2 = _latency_stats(n2) if stat == "latency" else _detection_stats(n2)
    v1, v2 = s1[field], s2[field]

    def check(a: str, _v1=v1, _v2=v2, _label=label, _pct=pct):
        ok = num_in(a, _v1, pct=_pct) and num_in(a, _v2, pct=_pct)
        return ok, f"需含 {_label}：{n1}帧={_v1:g} 与 {n2}帧={_v2:g}"
    return check


# ---------- config 类任务（get_config 恒定输出） ----------

def _norm(s: str) -> str:
    """判分用归一化：小写 + 去掉连字符/空格/下划线。

    避免模型把 rt-detect-v2 写成 RT-Detect V2 / rt_detect_v2 时被判成错。
    只用于"照抄某个固定字符串"类的判分，数值类判分不走这里。
    """
    return re.sub(r"[\s\-_]", "", s.lower())


def t1_config_res_fps(a):
    ok = "1280" in a and re.search(r"(?<!\d)60(?!\d)", a) is not None
    return ok, "需含 1280 与 60"


def t1_detector(a):
    return "rtdetectv2" in _norm(a), "需含 rt-detect-v2"


def t1_stages(a):
    return re.search(r"(?<!\d)4(?!\d)", a) is not None, "需含 4"


def t1_mode(a):
    # 2026-09-28 修正假阴性：模型答「实时流模式」内容完全正确，原判分死等英文
    # 原词 realtime-stream，会把正确答案判成错（实测 T1-5）。判分原则是看内容
    # 不看表述，故中英文写法都收。同类风险已在判分器自测里用复述变体覆盖。
    return ("realtimestream" in _norm(a) or "实时流" in a,
            "需含 realtime-stream（或「实时流」）")


# ---------- 新增覆盖：alarm_log / gpu_stats / list_segments / segment_latency ----------
# 2026-09-28 补齐：原 50 任务只覆盖 8 个工具中的 4 个，这 4 个工具的判分器在此定义。
# 数据与 src/tools.py 同源（固定表 / 种子随机），期望值同样是预计算得到的。

def _n_in(a: str, v) -> bool:
    """独立数字匹配：要求该数字不与其前后数字粘连（避免 "87" 命中 "187"）。"""
    return re.search(rf"(?<!\d){re.escape(f'{v:g}')}(?!\d)", a) is not None


def t_alarm_errors(a: str):
    """报警日志：error 级条数与来源模块（固定数据：2 条，均来自 detector）。"""
    n = sum(1 for x in _ALARM_LOG if x["level"] == "error")
    return (_n_in(a, n) and "detector" in a.lower(),
            f"需含 error 报警 {n} 条且提到来源 detector")


def t_alarm_timeout(a: str):
    """报警日志：是否存在推理超时及其来源模块。"""
    return (("inference timeout" in a.lower() or "推理超时" in a)
            and "detector" in a.lower(),
            "需提到推理超时（inference timeout）与模块 detector")


def t_gpu_util(a: str):
    """GPU 利用率。"""
    g = _gpu_stats()
    return num_in(a, g["gpu_util_pct"]), f"需含 GPU 利用率 {g['gpu_util_pct']:g}%"


def t_gpu_pair(a: str):
    """GPU 资源：利用率 + 显存占用。"""
    g = _gpu_stats()
    return (num_in(a, g["gpu_util_pct"]) and num_in(a, g["mem_used_gb"]),
            f"需含利用率 {g['gpu_util_pct']:g}% 与显存 {g['mem_used_gb']:g}GB")


def t_segment_list(a: str):
    """片段目录：片段总数与首个 id（依赖链入口）。"""
    return (_n_in(a, len(_SEGMENTS)) and "seg-001" in a,
            f"需含片段数 {len(_SEGMENTS)} 且列出 seg-001")


def _mk_segment(seg: str, need_report: bool = False):
    """指定片段的延迟 P95——依赖链下游：segment_id 只能来自 list_segments 的输出。"""
    s = _segment_latency(seg)
    report = f"reports/{seg}.png"

    def check(a: str, _v=s["p95_ms"], _r=report, _seg=seg, _nr=need_report):
        ok = num_in(a, _v) and (_r in a if _nr else _seg in a)
        return ok, (f"需含 {_seg} 的 P95={_v:g}" + (f" 与 {_r}" if _nr else ""))
    return check


def _mk_seg_pair(s1: str, s2: str):
    """两个片段的 P95 对比（两次 segment_latency 调用）。"""
    v1, v2 = _segment_latency(s1)["p95_ms"], _segment_latency(s2)["p95_ms"]

    def check(a: str):
        return (num_in(a, v1) and num_in(a, v2),
                f"需含 {s1} 的 P95={v1:g} 与 {s2} 的 P95={v2:g}")
    return check


def _mk_combo(specs: list, why: str):
    """多工具关联诊断：specs = [(value, pct, label), ...]，要求答案同时含全部字段值。

    用意：逼模型真的把几个工具都跑一遍——只跑其中一个，答案里就缺值，判不过。
    """
    def check(a: str, _specs=specs, _why=why):
        return all(num_in(a, v, pct=p) for v, p, _ in _specs), f"需同时含 {_why}"
    return check


# ---------- 任务定义 ----------

def _t(tid, ttype, prompt, expect_tools, check):
    return {"id": tid, "type": ttype, "prompt": prompt,
            "expect_tools": expect_tools, "check": check}


TASKS = [
    # ==== T1 简单查询 ×20（配置核对 / 单指标采样）====
    # 措辞口径（2026-09-28 统一）：场景前缀 + 「N 帧（约 X 秒）」双单位 + 术语给英文原词。
    # 判分完全不受影响：check 只看答案里的数值，与问句措辞无关。
    # 配置类问句保留「配置」二字是措辞需要（明确问的是配置项，不是运行指标），
    # 但**不要以为它能约束模型行为**：2026-09-28 两轮全 60 条验证里，带「配置」
    # 的 T1-1 依旧先调 get_config 再调 latency_stats、把配置问答成延迟（11.06s /
    # 3 步 / 判分✗）。跑题是 7b 的随机行为，与这两个字无关。
    # 教训：措辞效果不能用单次 A/B 定案——同一 prompt 两次跑可能 1.16s 对、
    # 10.67s 错，看起来像"措辞修好了/搞坏了"，实际是采样噪声。
    _t("T1-1", "T1", "【配置核对】查询当前检测服务的配置：输出分辨率和帧率分别是多少？",
       ["get_config"], t1_config_res_fps),
    _t("T1-2", "T1", "【配置核对】当前服务配置的检测器模型是哪个版本？",
       ["get_config"], t1_detector),
    _t("T1-3", "T1", "【配置核对】系统配置的推理流水线（pipeline）一共分几个阶段？",
       ["get_config"], t1_stages),
    _t("T1-4", "T1", "【巡检采样】取最近 100 帧（约 1.7 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 100, "p50_ms", "P50")),
    _t("T1-5", "T1", "【配置核对】当前服务配置的运行模式是什么？",
       ["get_config"], t1_mode),
    _t("T1-6", "T1", "【巡检采样】取最近 200 帧（约 3.3 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 200, "p50_ms", "P50")),
    _t("T1-7", "T1", "【巡检采样】取最近 500 帧（约 8.3 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 500, "p50_ms", "P50")),
    _t("T1-8", "T1", "【巡检采样】最近 100 帧（约 1.7 秒）的 P95 延迟是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 100, "p95_ms", "P95")),
    _t("T1-9", "T1", "【巡检采样】最近 300 帧（约 5.0 秒）的 P95 延迟是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 300, "p95_ms", "P95")),
    _t("T1-10", "T1", "【巡检采样】最近 200 帧（约 3.3 秒）的 P99 延迟是多少毫秒？",
        ["latency_stats"], _mk_single("latency", 200, "p99_ms", "P99")),
    _t("T1-11", "T1", "【巡检采样】最近 400 帧（约 6.7 秒）的 P99 延迟是多少毫秒？",
        ["latency_stats"], _mk_single("latency", 400, "p99_ms", "P99")),
    _t("T1-12", "T1", "【质量采样】最近 100 帧（约 1.7 秒）的目标检出率是多少？",
        ["detection_stats"], _mk_single("detection", 100, "detection_rate",
                                        "检出率", pct=True)),
    _t("T1-13", "T1", "【质量采样】最近 300 帧（约 5.0 秒）的目标检出率是多少？",
        ["detection_stats"], _mk_single("detection", 300, "detection_rate",
                                        "检出率", pct=True)),
    _t("T1-14", "T1", "【质量采样】最近 100 帧（约 1.7 秒）平均每张图检出多少个目标？",
        ["detection_stats"], _mk_single("detection", 100,
                                        "mean_targets_per_frame", "目标数")),
    _t("T1-15", "T1", "【质量采样】最近 200 帧（约 3.3 秒）平均每张图检出多少个目标？",
        ["detection_stats"], _mk_single("detection", 200,
                                        "mean_targets_per_frame", "目标数")),
    _t("T1-16", "T1",
       "【质量采样】最近 300 帧（约 5.0 秒）的定位抖动（localization jitter）是多少像素？",
        ["detection_stats"], _mk_single("detection", 300,
                                        "localization_jitter_px", "抖动")),
    _t("T1-17", "T1",
       "【质量采样】最近 500 帧（约 8.3 秒）的定位抖动（localization jitter）是多少像素？",
        ["detection_stats"], _mk_single("detection", 500,
                                        "localization_jitter_px", "抖动")),
    _t("T1-18", "T1", "【巡检采样】最近 800 帧（约 13.3 秒）的 P95 延迟是多少毫秒？",
        ["latency_stats"], _mk_single("latency", 800, "p95_ms", "P95")),
    _t("T1-19", "T1", "【巡检采样】最近 300 帧（约 5.0 秒）的 P50 延迟是多少毫秒？",
        ["latency_stats"], _mk_single("latency", 300, "p50_ms", "P50")),
    _t("T1-20", "T1", "【质量采样】最近 500 帧（约 8.3 秒）的目标检出率是多少？",
        ["detection_stats"], _mk_single("detection", 500, "detection_rate",
                                        "检出率", pct=True)),

    # ==== T2 单步诊断 ×20（告警/巡检语境下的单指标 + 阈值判断）====
    # 措辞纪律：阈值判断类必须保留「是否超过 / 是否低于」这类触发词——判分器的
    # above_verdict 靠答案里的方向词判对错，问句换了说法模型就会换说法（详见
    # above_verdict 的 docstring）。SLO/告警/值班等包装词可以加，触发词不能换。
    _t("T2-1", "T2",
       "【告警·延迟】最近 200 帧（约 3.3 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 200, "p95_ms", 15, "ms", "P95")),
    _t("T2-2", "T2",
       "【告警·质量】最近 200 帧（约 3.3 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
       ["detection_stats"],
       _mk_threshold("detection", 200, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-3", "T2",
       "【巡检】最近 500 帧（约 8.3 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 500, "p99_ms", 25, "ms", "P99")),
    _t("T2-4", "T2",
       "【质量采样】最近 300 帧（约 5.0 秒）的平均每张图目标数与定位抖动（像素）分别是多少？",
       ["detection_stats"],
       _mk_pair("detection", 300, "mean_targets_per_frame",
                "localization_jitter_px", "目标数", "抖动")),
    _t("T2-5", "T2",
       "【告警·延迟】最近 300 帧（约 5.0 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 300, "p95_ms", 15, "ms", "P95")),
    _t("T2-6", "T2",
       "【告警·延迟】最近 500 帧（约 8.3 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 500, "p95_ms", 15, "ms", "P95")),
    _t("T2-7", "T2",
       "【巡检】最近 200 帧（约 3.3 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 200, "p99_ms", 25, "ms", "P99")),
    _t("T2-8", "T2",
       "【巡检】最近 300 帧（约 5.0 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 300, "p99_ms", 25, "ms", "P99")),
    _t("T2-9", "T2",
       "【告警·质量】最近 300 帧（约 5.0 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
       ["detection_stats"],
       _mk_threshold("detection", 300, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-10", "T2",
        "【告警·质量】最近 500 帧（约 8.3 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
        ["detection_stats"],
        _mk_threshold("detection", 500, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-11", "T2",
        "【容量核对】最近 100 帧（约 1.7 秒）的 P50 延迟是多少毫秒？是否超过 10ms 基线？",
        ["latency_stats"], _mk_threshold("latency", 100, "p50_ms", 10, "ms", "P50")),
    _t("T2-12", "T2",
        "【容量核对】最近 200 帧（约 3.3 秒）的 P50 延迟是多少毫秒？是否超过 10ms 基线？",
        ["latency_stats"], _mk_threshold("latency", 200, "p50_ms", 10, "ms", "P50")),
    _t("T2-13", "T2",
        "【容量核对】最近 500 帧（约 8.3 秒）的 P50 延迟是多少毫秒？是否超过 10ms 基线？",
        ["latency_stats"], _mk_threshold("latency", 500, "p50_ms", 10, "ms", "P50")),
    _t("T2-14", "T2",
        "【质量采样】最近 200 帧（约 3.3 秒）的定位抖动是多少像素？是否超过 1.5px 上限？",
        ["detection_stats"],
        _mk_threshold("detection", 200, "localization_jitter_px", 1.5, "px", "抖动")),
    _t("T2-15", "T2",
        "【质量采样】最近 300 帧（约 5.0 秒）的定位抖动是多少像素？是否超过 1.5px 上限？",
        ["detection_stats"],
        _mk_threshold("detection", 300, "localization_jitter_px", 1.5, "px", "抖动")),
    _t("T2-16", "T2", "【巡检】最近 200 帧（约 3.3 秒）的 P95 与 P99 延迟分别是多少毫秒？",
        ["latency_stats"],
        _mk_pair("latency", 200, "p95_ms", "p99_ms", "P95", "P99")),
    _t("T2-17", "T2", "【巡检】最近 400 帧（约 6.7 秒）的 P95 与 P99 延迟分别是多少毫秒？",
        ["latency_stats"],
        _mk_pair("latency", 400, "p95_ms", "p99_ms", "P95", "P99")),
    _t("T2-18", "T2",
        "【质量采样】最近 300 帧（约 5.0 秒）的目标检出率与平均每张图目标数分别是多少？",
        ["detection_stats"],
        _mk_pair("detection", 300, "detection_rate", "mean_targets_per_frame",
                 "检出率", "目标数", pct1=True)),
    _t("T2-19", "T2",
        "【质量采样】最近 500 帧（约 8.3 秒）的平均每张图目标数与定位抖动分别是多少？",
        ["detection_stats"],
        _mk_pair("detection", 500, "mean_targets_per_frame",
                 "localization_jitter_px", "目标数", "抖动")),
    _t("T2-20", "T2", "【巡检】最近 300 帧（约 5.0 秒）的 P50 与 P95 延迟分别是多少毫秒？",
        ["latency_stats"],
        _mk_pair("latency", 300, "p50_ms", "p95_ms", "P50", "P95")),

    # ==== T3 复杂诊断 ×20 ====
    # 前 10 条 = 原有多工具任务（改写措辞，判分逻辑不动）；
    # 后 10 条 = 2026-09-28 新增，补齐 alarm_log / gpu_stats / list_segments /
    # segment_latency 四个此前基准从未覆盖的工具，并刻意构造两个调度实验场景：
    #   依赖链（list_segments → segment_latency，必须先查目录才有 segment_id）
    #   并行批（多个互不依赖的工具，逼执行器同批并发）
    _t("T3-1", "T3",
       "【值班工单】疑似线上质量下降：核查最近 200 帧（约 3.3 秒）的延迟分布与目标检出率，"
       "逐项对照 SLO（P95<15ms、检出率>92%）给出达标结论。",
       ["latency_stats", "detection_stats"],
       (lambda a, _L=_latency_stats(200), _D=_detection_stats(200):
        (num_in(a, _L["p50_ms"]) and num_in(a, _D["detection_rate"], pct=True),
         f"需含 P50={_L['p50_ms']:g} 与检出率 {_D['detection_rate']:g}"))),
    _t("T3-2", "T3",
       "【值班工单】诊断最近 200 帧（约 3.3 秒）的延迟分布，并渲染 latest 片段的"
       "可视化报告。报告保存路径是什么？", ["latency_stats", "render_report"],
       (lambda a, _L=_latency_stats(200):
        ("reports/latest.png" in a and num_in(a, _L["p50_ms"]),
         "需含 reports/latest.png 与 P50 值"))),
    _t("T3-3", "T3",
       "【值班工单】疑似线上质量下降：核查最近 300 帧（约 5.0 秒）的延迟分布与目标检出率，"
       "逐项对照 SLO（P95<15ms、检出率>92%）给出达标结论。",
       ["latency_stats", "detection_stats"],
       (lambda a, _L=_latency_stats(300), _D=_detection_stats(300):
        (num_in(a, _L["p50_ms"]) and num_in(a, _D["detection_rate"], pct=True),
         f"需含 P50={_L['p50_ms']:g} 与检出率 {_D['detection_rate']:g}"))),
    _t("T3-4", "T3",
       "【值班工单】疑似线上质量下降：核查最近 500 帧（约 8.3 秒）的延迟分布与目标检出率，"
       "逐项对照 SLO（P95<15ms、检出率>92%）给出达标结论。",
       ["latency_stats", "detection_stats"],
       (lambda a, _L=_latency_stats(500), _D=_detection_stats(500):
        (num_in(a, _L["p50_ms"]) and num_in(a, _D["detection_rate"], pct=True),
         f"需含 P50={_L['p50_ms']:g} 与检出率 {_D['detection_rate']:g}"))),
    _t("T3-5", "T3",
       "【值班工单】检查最近 200 帧（约 3.3 秒）的检测质量，并渲染 latest 片段的"
       "可视化报告。报告保存路径是什么？", ["detection_stats", "render_report"],
       (lambda a, _D=_detection_stats(200):
        ("reports/latest.png" in a and num_in(a, _D["detection_rate"], pct=True),
         "需含 reports/latest.png 与检出率值"))),
    _t("T3-6", "T3",
       "【值班工单·全面排查】对最近 200 帧（约 3.3 秒）做延迟与检测质量核查，"
       "最后渲染 latest 片段的可视化报告，给出结论与报告路径。",
       ["latency_stats", "detection_stats", "render_report"],
       (lambda a, _L=_latency_stats(200), _D=_detection_stats(200):
        (num_in(a, _L["p95_ms"]) and num_in(a, _D["detection_rate"], pct=True)
         and "reports/latest.png" in a,
         "需含 P95、检出率与 reports/latest.png"))),
    _t("T3-7", "T3",
       "【值班工单·全面排查】对最近 500 帧（约 8.3 秒）做延迟与检测质量核查，"
       "最后渲染 latest 片段的可视化报告，给出结论与报告路径。",
       ["latency_stats", "detection_stats", "render_report"],
       (lambda a, _L=_latency_stats(500), _D=_detection_stats(500):
        (num_in(a, _L["p95_ms"]) and num_in(a, _D["detection_rate"], pct=True)
         and "reports/latest.png" in a,
         "需含 P95、检出率与 reports/latest.png"))),
    _t("T3-8", "T3",
       "【值班工单】诊断最近 200 帧（约 3.3 秒）的延迟分布，并渲染 morning 片段的"
       "可视化报告。报告保存路径是什么？", ["latency_stats", "render_report"],
       (lambda a, _L=_latency_stats(200):
        ("reports/morning.png" in a and num_in(a, _L["p50_ms"]),
         "需含 reports/morning.png 与 P50 值"))),
    _t("T3-9", "T3",
       "【对比分析】分别统计最近 100 帧（约 1.7 秒）与最近 500 帧（约 8.3 秒）的处理延迟，"
       "两段窗口的 P50 分别是多少毫秒？", ["latency_stats", "latency_stats"],
       _mk_cross("latency", 100, 500, "p50_ms", "P50")),
    _t("T3-10", "T3",
        "【对比分析】分别检查最近 200 帧（约 3.3 秒）与最近 500 帧（约 8.3 秒）的检测质量，"
        "两段窗口的检出率分别是多少？", ["detection_stats", "detection_stats"],
        _mk_cross("detection", 200, 500, "detection_rate", "检出率", pct=True)),

    _t("T3-11", "T3",
       "【录像排查·依赖链】先列出系统中所有已录制片段，再查询其中第一个片段的延迟 P95"
       "（毫秒）——片段 id 只能从目录里取，不要凭空猜。",
       ["list_segments", "segment_latency"],
       _mk_segment("seg-001")),
    # 注（2026-09-28 实测）：本条与 T3-18 的问句直接点名了片段 id，模型不必先查
    # 目录即可作答，故 expect_tools 不再列 list_segments（列了会误报"缺工具"）。
    # 依赖链（必须先 list_segments 才知道 segment_id）由 T3-11 与 T3-19 承担，
    # 两条实测均已真实触发；本条的价值是"两次 segment_latency 并行批"。
    _t("T3-12", "T3",
       "【对比分析·依赖链】分别查询 seg-001 与 seg-003 两个片段的延迟 P95，"
       "判断哪一段更慢。",
       ["segment_latency", "segment_latency"],
       _mk_seg_pair("seg-001", "seg-003")),
    # 以下三条在措辞上刻意收窄范围（点名工具 + 限定回答项）：开放式问句会让本地
    # 7b 模型发散——8 步用完仍不收敛、顺带乱调无关工具（实测单条跑出 395s）。
    # 收窄后才是"能不能按时答对"的考题，而不是"会不会跑偏"的考题。
    _t("T3-13", "T3",
       "【告警排查】拉取系统报警日志（alarm_log），统计其中 error 级别的报警有几条、"
       "分别来自哪个模块。只回答这两项，不需要调用其它工具。",
       ["alarm_log"], t_alarm_errors),
    _t("T3-14", "T3",
       "【告警排查】查询系统报警日志（alarm_log），确认是否存在推理超时"
       "（inference timeout）记录，说明来自哪个模块。只回答这两项，不需要调用其它工具。",
       ["alarm_log"], t_alarm_timeout),
    _t("T3-15", "T3",
       "【资源排查】查询 GPU 状态（gpu_stats）：当前 GPU 利用率与显存占用（GB）分别是多少？"
       "据此说明是否存在资源瓶颈。只回答这三项，不需要调用其它工具。",
       ["gpu_stats"], t_gpu_pair),
    _t("T3-16", "T3",
       "【关联诊断】告警称推理超时：请同时拉取报警日志与 GPU 状态，并给出最近 200 帧"
       "（约 3.3 秒）的 P95 延迟，判断超时是否与资源相关。"
       "回答包含上述各项数值与一句结论即可。",
       ["alarm_log", "gpu_stats", "latency_stats"],
       _mk_combo([(_latency_stats(200)["p95_ms"], False, "P95"),
                  (_gpu_stats()["gpu_util_pct"], False, "GPU利用率")],
                 "200帧 P95 与 GPU 利用率")),
    _t("T3-17", "T3",
       "【关联诊断】最近 300 帧（约 5.0 秒）检出率异常：请同时核查检测质量与 GPU 资源，"
       "判断质量下降是否由资源瓶颈导致。回答包含两项数值与一句结论即可。",
       ["detection_stats", "gpu_stats"],
       _mk_combo([(_detection_stats(300)["detection_rate"], True, "检出率"),
                  (_gpu_stats()["gpu_util_pct"], False, "GPU利用率")],
                 "300帧检出率与 GPU 利用率")),
    _t("T3-18", "T3",
       "【定位并出报告】定位到 seg-002 片段后，查询它的延迟 P95，并渲染该片段的"
       "可视化报告，给出报告路径。",
       ["segment_latency", "render_report"],
       _mk_segment("seg-002", need_report=True)),
    _t("T3-19", "T3",
       "【分段 vs 全局】列出所有已录制片段，查询最新片段 seg-006 的延迟 P95，"
       "同时核对最近 500 帧（约 8.3 秒）的整体 P95，说明分段与全局是否一致。"
       "回答包含两个 P95 数值与一句结论即可。",
       ["list_segments", "segment_latency", "latency_stats"],
       _mk_combo([(_segment_latency("seg-006")["p95_ms"], False, "seg-006 的 P95"),
                  (_latency_stats(500)["p95_ms"], False, "500帧 P95")],
                 "seg-006 的 P95 与 500 帧 P95")),
    _t("T3-20", "T3",
       "【日常巡检·三项并行】同时核最近 200 帧（约 3.3 秒）的延迟 P95、目标检出率"
       "与 GPU 利用率，给出三项指标的健康结论。",
       ["latency_stats", "detection_stats", "gpu_stats"],
       _mk_combo([(_latency_stats(200)["p95_ms"], False, "P95"),
                  (_detection_stats(200)["detection_rate"], True, "检出率"),
                  (_gpu_stats()["gpu_util_pct"], False, "GPU利用率")],
                 "200帧 P95、检出率与 GPU 利用率")),

    # ==== 2026-10-22 扩展：60 → 100（评估体系重建，配对设计 N≥100）====
    # 扩题纪律（五条，缺一不可）：
    #   ① 全部复用既有判分工厂（_mk_single/_mk_threshold/_mk_pair/_mk_cross/
    #      _mk_combo/_mk_seg_pair），期望值仍由种子随机预计算——零新判分逻辑，
    #      self_test 的可达/空判/压缩覆盖/复述检查自动覆盖新题；
    #   ② 参数表显式逐条列出（不是循环里隐式生成），每条可单独审计与删改；
    #   ③ 窗口 n 取与原 60 条不重复的新值（150/250/350/450/600/700/900/1000），
    #      避免同 n 同指标的语义重复任务；
    #   ④ T2 阈值题保留「是否超过 / 是否低于」触发词（above_verdict 依赖，见上）；
    #   ⑤ 问句沿用「最近 N 帧（约 X 秒）」双单位口径（golden_answer 正则依赖，
    #      X = N/60 保留 1 位）。

    # ---- T1 简单查询 +14（T1-21..T1-34，新窗口单指标）----
    _t("T1-21", "T1", "【巡检采样】取最近 150 帧（约 2.5 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 150, "p50_ms", "P50")),
    _t("T1-22", "T1", "【巡检采样】取最近 250 帧（约 4.2 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 250, "p50_ms", "P50")),
    _t("T1-23", "T1", "【巡检采样】取最近 350 帧（约 5.8 秒）的处理延迟，P95 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 350, "p95_ms", "P95")),
    _t("T1-24", "T1", "【巡检采样】取最近 450 帧（约 7.5 秒）的处理延迟，P95 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 450, "p95_ms", "P95")),
    _t("T1-25", "T1", "【巡检采样】取最近 600 帧（约 10.0 秒）的处理延迟，P99 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 600, "p99_ms", "P99")),
    _t("T1-26", "T1", "【巡检采样】取最近 700 帧（约 11.7 秒）的处理延迟，P50 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 700, "p50_ms", "P50")),
    _t("T1-27", "T1", "【巡检采样】取最近 900 帧（约 15.0 秒）的处理延迟，P95 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 900, "p95_ms", "P95")),
    _t("T1-28", "T1", "【巡检采样】取最近 1000 帧（约 16.7 秒）的处理延迟，P99 是多少毫秒？",
       ["latency_stats"], _mk_single("latency", 1000, "p99_ms", "P99")),
    _t("T1-29", "T1", "【质量采样】最近 150 帧（约 2.5 秒）的目标检出率是多少？",
       ["detection_stats"], _mk_single("detection", 150, "detection_rate",
                                       "检出率", pct=True)),
    _t("T1-30", "T1", "【质量采样】最近 250 帧（约 4.2 秒）的目标检出率是多少？",
       ["detection_stats"], _mk_single("detection", 250, "detection_rate",
                                       "检出率", pct=True)),
    _t("T1-31", "T1", "【质量采样】最近 350 帧（约 5.8 秒）平均每张图检出多少个目标？",
       ["detection_stats"], _mk_single("detection", 350,
                                       "mean_targets_per_frame", "目标数")),
    _t("T1-32", "T1", "【质量采样】最近 450 帧（约 7.5 秒）的定位抖动是多少像素？",
       ["detection_stats"], _mk_single("detection", 450,
                                       "localization_jitter_px", "抖动")),
    _t("T1-33", "T1", "【质量采样】最近 600 帧（约 10.0 秒）的定位抖动是多少像素？",
       ["detection_stats"], _mk_single("detection", 600,
                                       "localization_jitter_px", "抖动")),
    _t("T1-34", "T1", "【质量采样】最近 700 帧（约 11.7 秒）平均每张图检出多少个目标？",
       ["detection_stats"], _mk_single("detection", 700,
                                       "mean_targets_per_frame", "目标数")),

    # ---- T2 单步诊断 +13（T2-21..T2-33，新窗口阈值判断/双数值）----
    _t("T2-21", "T2",
       "【告警·延迟】最近 150 帧（约 2.5 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 150, "p95_ms", 15, "ms", "P95")),
    _t("T2-22", "T2",
       "【告警·延迟】最近 450 帧（约 7.5 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 450, "p95_ms", 15, "ms", "P95")),
    _t("T2-23", "T2",
       "【告警·延迟】最近 700 帧（约 11.7 秒）的 P95 延迟是多少毫秒？是否超过 15ms 的 SLO 红线？",
       ["latency_stats"], _mk_threshold("latency", 700, "p95_ms", 15, "ms", "P95")),
    _t("T2-24", "T2",
       "【巡检】最近 350 帧（约 5.8 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 350, "p99_ms", 25, "ms", "P99")),
    _t("T2-25", "T2",
       "【巡检】最近 600 帧（约 10.0 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 600, "p99_ms", 25, "ms", "P99")),
    _t("T2-26", "T2",
       "【巡检】最近 1000 帧（约 16.7 秒）的 P99 延迟是多少毫秒？是否超过 25ms 健康线？",
       ["latency_stats"], _mk_threshold("latency", 1000, "p99_ms", 25, "ms", "P99")),
    _t("T2-27", "T2",
       "【容量核对】最近 350 帧（约 5.8 秒）的 P50 延迟是多少毫秒？是否超过 10ms 基线？",
       ["latency_stats"], _mk_threshold("latency", 350, "p50_ms", 10, "ms", "P50")),
    _t("T2-28", "T2",
       "【容量核对】最近 700 帧（约 11.7 秒）的 P50 延迟是多少毫秒？是否超过 10ms 基线？",
       ["latency_stats"], _mk_threshold("latency", 700, "p50_ms", 10, "ms", "P50")),
    _t("T2-29", "T2",
       "【告警·质量】最近 150 帧（约 2.5 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
       ["detection_stats"],
       _mk_threshold("detection", 150, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-30", "T2",
       "【告警·质量】最近 450 帧（约 7.5 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
       ["detection_stats"],
       _mk_threshold("detection", 450, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-31", "T2",
       "【告警·质量】最近 700 帧（约 11.7 秒）的目标检出率是多少？是否低于 92% 的 SLO 下限？",
       ["detection_stats"],
       _mk_threshold("detection", 700, "detection_rate", 0.92, "", "检出率", pct=True)),
    _t("T2-32", "T2",
       "【质量采样】最近 450 帧（约 7.5 秒）的定位抖动是多少像素？是否超过 1.5px 上限？",
       ["detection_stats"],
       _mk_threshold("detection", 450, "localization_jitter_px", 1.5, "px", "抖动")),
    _t("T2-33", "T2", "【巡检】最近 250 帧（约 4.2 秒）的 P95 与 P99 延迟分别是多少毫秒？",
       ["latency_stats"],
       _mk_pair("latency", 250, "p95_ms", "p99_ms", "P95", "P99")),

    # ---- T3 复杂诊断 +13（T3-21..T3-33，多工具/跨窗口/依赖链/并行批）----
    _t("T3-21", "T3",
       "【值班工单】疑似线上质量下降：核查最近 150 帧（约 2.5 秒）的延迟分布与目标检出率，"
       "逐项对照 SLO（P95<15ms、检出率>92%）给出达标结论。",
       ["latency_stats", "detection_stats"],
       (lambda a, _L=_latency_stats(150), _D=_detection_stats(150):
        (num_in(a, _L["p50_ms"]) and num_in(a, _D["detection_rate"], pct=True),
         f"需含 P50={_L['p50_ms']:g} 与检出率 {_D['detection_rate']:g}"))),
    _t("T3-22", "T3",
       "【值班工单】疑似线上质量下降：核查最近 700 帧（约 11.7 秒）的延迟分布与目标检出率，"
       "逐项对照 SLO（P95<15ms、检出率>92%）给出达标结论。",
       ["latency_stats", "detection_stats"],
       (lambda a, _L=_latency_stats(700), _D=_detection_stats(700):
        (num_in(a, _L["p50_ms"]) and num_in(a, _D["detection_rate"], pct=True),
         f"需含 P50={_L['p50_ms']:g} 与检出率 {_D['detection_rate']:g}"))),
    _t("T3-23", "T3",
       "【值班工单】诊断最近 350 帧（约 5.8 秒）的延迟分布，并渲染 seg-003 片段的"
       "可视化报告。报告保存路径是什么？", ["latency_stats", "render_report"],
       (lambda a, _L=_latency_stats(350):
        ("reports/seg-003.png" in a and num_in(a, _L["p50_ms"]),
         "需含 reports/seg-003.png 与 P50 值"))),
    _t("T3-24", "T3",
       "【值班工单】检查最近 250 帧（约 4.2 秒）的检测质量，并渲染 seg-004 片段的"
       "可视化报告。报告保存路径是什么？", ["detection_stats", "render_report"],
       (lambda a, _D=_detection_stats(250):
        ("reports/seg-004.png" in a and num_in(a, _D["detection_rate"], pct=True),
         "需含 reports/seg-004.png 与检出率值"))),
    _t("T3-25", "T3",
       "【值班工单·全面排查】对最近 700 帧（约 11.7 秒）做延迟与检测质量核查，"
       "最后渲染 latest 片段的可视化报告，给出结论与报告路径。",
       ["latency_stats", "detection_stats", "render_report"],
       (lambda a, _L=_latency_stats(700), _D=_detection_stats(700):
        (num_in(a, _L["p95_ms"]) and num_in(a, _D["detection_rate"], pct=True)
         and "reports/latest.png" in a,
         "需含 P95、检出率与 reports/latest.png"))),
    _t("T3-26", "T3",
       "【对比分析】分别统计最近 150 帧（约 2.5 秒）与最近 600 帧（约 10.0 秒）的处理延迟，"
       "两段窗口的 P50 分别是多少毫秒？", ["latency_stats", "latency_stats"],
       _mk_cross("latency", 150, 600, "p50_ms", "P50")),
    _t("T3-27", "T3",
       "【对比分析】分别检查最近 150 帧（约 2.5 秒）与最近 600 帧（约 10.0 秒）的检测质量，"
       "两段窗口的检出率分别是多少？", ["detection_stats", "detection_stats"],
       _mk_cross("detection", 150, 600, "detection_rate", "检出率", pct=True)),
    _t("T3-28", "T3",
       "【对比分析】分别统计最近 250 帧（约 4.2 秒）与最近 900 帧（约 15.0 秒）的处理延迟，"
       "两段窗口的 P95 分别是多少毫秒？", ["latency_stats", "latency_stats"],
       _mk_cross("latency", 250, 900, "p95_ms", "P95")),
    _t("T3-29", "T3",
       "【对比分析·依赖链】分别查询 seg-002 与 seg-005 两个片段的延迟 P95，"
       "判断哪一段更慢。",
       ["segment_latency", "segment_latency"],
       _mk_seg_pair("seg-002", "seg-005")),
    _t("T3-30", "T3",
       "【对比分析·依赖链】分别查询 seg-004 与 seg-006 两个片段的延迟 P95，"
       "判断哪一段更慢。",
       ["segment_latency", "segment_latency"],
       _mk_seg_pair("seg-004", "seg-006")),
    _t("T3-31", "T3",
       "【关联诊断】容量告警触发：请同时查询 GPU 状态，并给出最近 300 帧"
       "（约 5.0 秒）的 P50 延迟，判断延迟是否与资源相关。"
       "回答包含两项数值与一句结论即可。",
       ["gpu_stats", "latency_stats"],
       _mk_combo([(_latency_stats(300)["p50_ms"], False, "P50"),
                  (_gpu_stats()["gpu_util_pct"], False, "GPU利用率")],
                 "300帧 P50 与 GPU 利用率")),
    _t("T3-32", "T3",
       "【分段 vs 全局】查询片段 seg-003 的延迟 P95，同时核对最近 300 帧"
       "（约 5.0 秒）的整体 P95，说明分段与全局是否一致。"
       "回答包含两个 P95 数值与一句结论即可。",
       ["segment_latency", "latency_stats"],
       _mk_combo([(_segment_latency("seg-003")["p95_ms"], False, "seg-003 的 P95"),
                  (_latency_stats(300)["p95_ms"], False, "300帧 P95")],
                 "seg-003 的 P95 与 300 帧 P95")),
    _t("T3-33", "T3",
       "【日常巡检·三项并行】同时核最近 500 帧（约 8.3 秒）的延迟 P95、目标检出率"
       "与 GPU 利用率，给出三项指标的健康结论。",
       ["latency_stats", "detection_stats", "gpu_stats"],
       _mk_combo([(_latency_stats(500)["p95_ms"], False, "P95"),
                  (_detection_stats(500)["detection_rate"], True, "检出率"),
                  (_gpu_stats()["gpu_util_pct"], False, "GPU利用率")],
                 "500帧 P95、检出率与 GPU 利用率")),
]

assert len(TASKS) == 100, f"任务数应为 100，实际 {len(TASKS)}"


# ---------- 金答案自测（python -m eval.tasks / python eval/tasks.py）----------
# 价值：判分器是整个评估的地基，两类静默故障必须靠自测拦住——
#   ① 不可达：金答案都判不过 → 该任务永远 0 分（成功率被系统性低估）
#   ② 空判：随便一段话都判过 → 该任务永远满分（成功率被系统性高估）
# 做法：按问句里出现的参数真实调用 expect_tools 拼出"该拿到的答案"，再检查判分。

def golden_answer(task: dict, compress: bool = False) -> str:
    """按问句里的窗口（最近 N 帧）与片段 id，真实调用该任务期望的工具拼出答案。

    compress=True：把每条工具输出过一遍 select 压缩器（src/context._shrink_payload），
    用于自测"压缩字段白名单是否覆盖了判分所需字段"——若白名单漏了某个字段，
    dert_full 策略下该任务就是必挂，而这是 bug 不是策略效果。2026-09-28 靠这个
    自测抓出 detection_stats 漏了 mean_targets_per_frame、alarm_log 漏了 msg。
    """
    import json
    if compress:
        from src.context import _shrink_payload
    p = task["prompt"]
    ns = [int(x) for x in re.findall(r"最近\s*(\d+)\s*帧", p)] or [200]
    segs = re.findall(r"seg-\d+", p) or ["seg-001"]
    parts = []
    for name in task["expect_tools"]:
        if name == "latency_stats":
            parts += [(name, json.dumps(_latency_stats(n), ensure_ascii=False))
                      for n in ns]
        elif name == "detection_stats":
            parts += [(name, json.dumps(_detection_stats(n), ensure_ascii=False))
                      for n in ns]
        elif name == "get_config":
            parts.append((name, json.dumps(_get_config(), ensure_ascii=False)))
        elif name == "gpu_stats":
            parts.append((name, json.dumps(_gpu_stats(), ensure_ascii=False)))
        elif name == "alarm_log":
            parts.append((name, json.dumps(_alarm_log(), ensure_ascii=False)))
        elif name == "list_segments":
            parts.append((name, json.dumps(_list_segments(), ensure_ascii=False)))
        elif name == "segment_latency":
            parts += [(name, json.dumps(_segment_latency(s), ensure_ascii=False))
                      for s in segs]
        elif name == "render_report":
            parts += [(name, json.dumps(_render_report(s), ensure_ascii=False))
                      for s in (segs if "seg-" in p else ["latest", "morning"])]
    if compress:
        parts = [(n, _shrink_payload(n, s)) for n, s in parts]
    return "\n".join(s for _, s in parts)


# ---------- 复述变体自测（防判分器假阴性）----------
# 2026-09-28 补。金答案是"工具原始输出拼接"，措辞跟模型自然回答差别很大——
# 光测金答案测不出假阴性：实测就靠这个洞漏了 T1-5（模型答「实时流模式」内容
# 完全正确，判分器却死等英文原词 realtime-stream，把正确答案判成错，2026-09-28
# 全 60 条本地验证抓到）。这里对"照抄固定字符串"类的判分额外喂几段人类口吻的
# 答案，确保判的是内容而不是表述。
_PARAPHRASE = {
    "T1-1": ["当前检测的分辨率是 1280x720，帧率 60 fps。",
             "输出分辨率 1280×720，每秒 60 帧。"],
    "T1-2": ["检测器模型版本是 rt-detect-v2。",
             "用的是 RT-Detect V2 这个检测器。"],
    "T1-3": ["推理流水线一共分 4 个阶段。"],
    "T1-5": ["当前服务配置的运行模式是实时流模式。",
             "运行模式为 realtime-stream（实时流）。"],
    "T3-11": ["第一个片段 seg-001 的延迟 P95 是 19.13 ms。"],
    "T3-13": ["报警日志里 error 级别共 2 条，都来自 detector 模块。"],
    "T3-14": ["存在推理超时（inference timeout）记录，来源是 detector 模块。",
              "有一条推理超时报警，来自 detector。"],
}


def paraphrase_test() -> list:
    """返回没通过的 (task_id, 变体, 原因)——内容对却判错，即假阴性。"""
    bad = []
    for tid, variants in _PARAPHRASE.items():
        t = next(x for x in TASKS if x["id"] == tid)
        for v in variants:
            ok, why = t["check"](v)
            if not ok:
                bad.append((tid, v, why))
    return bad


def self_test() -> int:
    unreachable, vacuous, discriminating, always = [], [], [], []
    for t in TASKS:
        gold = golden_answer(t)
        pos = t["check"](gold + "\n结论：超过阈值。")[0]    # 肯定式结论
        neg = t["check"](gold + "\n结论：未超过阈值。")[0]  # 否定式结论
        if t["check"]("随便一段无关文本 12345")[0]:
            vacuous.append(t["id"])
        if not (pos or neg):
            unreachable.append((t["id"], t["check"](gold)[1]))
        elif pos and neg:
            always.append(t["id"])
        else:
            discriminating.append(t["id"])

    print(f"任务总数 {len(TASKS)}")
    print(f"  可达且结论有区分度（阈值类应绝大多数在此）: {len(discriminating)}")
    print(f"  结论不影响判定（单值/多值类，符合预期）: {len(always)}")
    if vacuous:
        print(f"  ❌ 空判（无关文本也判对）: {vacuous}")
    if unreachable:
        print("  ❌ 不可达（金答案也判不过）:")
        for tid, why in unreachable:
            print(f"     {tid}: {why}")
    # 压缩白名单覆盖检查：金答案过一遍 select 压缩后仍必须判得过。
    # 漏字段 = dert_full 策略下该任务必挂（且会被误读成"压缩策略效果差"）。
    comp_bad = []
    for t in TASKS:
        gold_c = golden_answer(t, compress=True)
        if not (t["check"](gold_c + "\n结论：超过阈值。")[0]
                or t["check"](gold_c + "\n结论：未超过阈值。")[0]):
            comp_bad.append((t["id"], t["check"](gold_c)[1]))
    print(f"  压缩白名单覆盖（select 压缩后金答案仍判对）: "
          f"{'全部通过' if not comp_bad else f'❌ {len(comp_bad)} 条被裁掉判分字段'}")
    for tid, why in comp_bad:
        print(f"     {tid}: {why}")

    para_bad = paraphrase_test()
    print(f"  复述变体（人类口吻答案，共 "
          f"{sum(len(v) for v in _PARAPHRASE.values())} 条）: "
          f"{'全部判对' if not para_bad else f'❌ {len(para_bad)} 条被误杀'}")
    for tid, v, why in para_bad:
        print(f"     {tid}: {why}  ← {v!r}")
    ok = not vacuous and not unreachable and not para_bad and not comp_bad
    print("SELF-TEST " + ("ALL PASS" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
