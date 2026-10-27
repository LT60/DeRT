"""BFCL v4 多轮数据 → DeRT harness 适配器（外部验证，2026-10-25）。

职责（判分纪律：确定性、无 LLM judge）：
  1. 加载 episodes（JSONL）+ ground truth（possible_answer JSONL，按 id 对齐）
  2. 实例化 BFCL 工具类（_load_scenario 注入初始状态）并封装为我们的 ToolRegistry
  3. ground truth 重放：逐轮执行期望调用 → 逐轮状态快照（期望轨迹）
  4. 状态比对：模型实例公开属性 vs 期望实例公开属性（BFCL state_checker 同款口径）

机制依据（eval/bfcl/SOURCES.md，读源码确认）：
  - 实例化：class_() + _load_scenario(deepcopy(config))；MathAPI 无状态
  - 判分：逐轮边界比对（不只终态）；期望状态由 ground truth 调用串重放而来
  - 调用串形如 cd(folder='document')；方法名→实例映射按 involved_classes 顺序
  - excluded_function：该 episode 不允许使用的函数（从工具集中剔除）

不修改 BFCL 源码；仅 vendor（eval/bfcl/bfcl_eval/...）+ sys.path 导入。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

_BFCL_DIR = Path(__file__).resolve().parent / "bfcl"
_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_BFCL_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.tools import Tool, ToolRegistry  # noqa: E402

_FUNC_PKG = "bfcl_eval.eval_checker.multi_turn_eval.func_source_code"

# 类名 → 模块名（BFCL executable_backend_config.py 的映射，多轮套件 8 类）
CLASS_MODULE = {
    "GorillaFileSystem": "gorilla_file_system",
    "MathAPI": "math_api",
    "MessageAPI": "message_api",
    "TwitterAPI": "posting_api",
    "TicketAPI": "ticket_api",
    "TradingBot": "trading_bot",
    "TravelAPI": "travel_booking",
    "VehicleControlAPI": "vehicle_control",
    # 长上下文类（不在 base 子集，保留映射完整性）
    "WebSearchAPI": "web_search",
    "MemoryAPI_kv": "memory_kv",
    "MemoryAPI_vector": "memory_vector",
    "MemoryAPI_rec_sum": "memory_rec_sum",
}
STATELESS_CLASSES = {"MathAPI"}

_DATA = _BFCL_DIR / "data"
_GT = _BFCL_DIR / "possible_answer"
_DOC = _BFCL_DIR / "func_doc"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def load_category(category: str = "BFCL_v4_multi_turn_base"):
    """返回 episodes 与 ground truths（按 id 对齐，顺序与文件一致）。"""
    eps = _read_jsonl(_DATA / f"{category}.json")
    gts = {g["id"]: g["ground_truth"] for g in
           _read_jsonl(_GT / f"{category}.json")}
    for ep in eps:
        ep["_gt"] = gts[ep["id"]]
    return eps


def load_func_docs() -> dict[str, dict]:
    """所有函数文档 {func_name: doc}（12 个类合并；跨类重名以前者为准——
    多轮套件实际不出现跨类重名，若有会被 excluded_function 显式处理）。"""
    docs: dict[str, dict] = {}
    for f in sorted(_DOC.glob("*.json")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue                    # func_doc 亦为 JSONL（每行一个函数文档）
            parsed = json.loads(line)
            if isinstance(parsed, list):   # 防御：兼容「整行是数组」的变体
                for d in parsed:
                    docs.setdefault(d["name"], d)
            else:
                docs.setdefault(parsed["name"], parsed)
    return docs


def import_class(class_name: str):
    module = __import__(f"{_FUNC_PKG}.{CLASS_MODULE[class_name]}",
                        fromlist=[class_name])
    return getattr(module, class_name)


def build_instances(initial_config: dict, involved_classes: list[str]) -> dict:
    """按 BFCL 口径实例化：空实例 + _load_scenario(深拷贝初始状态)。"""
    import importlib
    instances = {}
    for cn in involved_classes:
        module = importlib.import_module(f"{_FUNC_PKG}.{CLASS_MODULE[cn]}")
        inst = getattr(module, cn)()
        if cn not in STATELESS_CLASSES:
            cfg = copy.deepcopy(initial_config.get(cn, {}))
            inst._load_scenario(cfg, long_context=False)
        instances[cn] = inst
    return instances


def make_registry(instances: dict, involved_classes: list[str],
                  excluded: list[str] | None = None,
                  allowed_funcs: list[str] | None = None) -> ToolRegistry:
    """函数文档 → Tool 注册表；func = 实例方法闭包（结果字符串化同 BFCL）。

    allowed_funcs：episode 的 `path` 字段（'ClassName.func' 列表）解析出的
    函数名集合——官方 BFCL 只把这几个函数的文档给模型（控制 prompt 规模）；
    None = 该类全部有文档的函数（宽松口径，仅调试用）。
    """
    docs = load_func_docs()
    excluded = set(excluded or [])
    allowed = {p.split(".")[-1] for p in (allowed_funcs or [])} or None
    method_owner: dict[str, object] = {}
    for cn in involved_classes:            # 顺序即优先级（BFCL 同款）
        for m_name, m in _public_methods(instances[cn]):
            method_owner.setdefault(m_name, instances[cn])
    reg = ToolRegistry()
    for cn in involved_classes:
        for m_name, _ in _public_methods(instances[cn]):
            doc = docs.get(m_name)
            if doc is None or m_name in excluded:
                continue
            if allowed is not None and m_name not in allowed:
                continue
            owner = method_owner[m_name]
            reg.register(Tool(
                name=doc["name"], description=doc["description"],
                parameters=doc.get("parameters",
                                   {"type": "object", "properties": {}}),
                func=_stringify_method(getattr(owner, m_name))))
    return reg


def _public_methods(instance):
    """实例的公开绑定方法（BFCL 同款：inspect.getmembers + ismethod）。"""
    import inspect
    return [(n, m) for n, m in inspect.getmembers(instance, inspect.ismethod)
            if not n.startswith("_")]


def _stringify_method(method):
    """调用实例方法，返回值按 BFCL 口径字符串化（dict→json，其余→str）。"""
    def wrapped(**kwargs):
        result = method(**kwargs)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            try:
                return json.dumps(result, ensure_ascii=False)
            except Exception:
                return str(result)
        return str(result)
    return wrapped


# ---------- ground truth 重放与状态比对 ----------

def _parse_call(call: str):
    """'cd(folder=1)' / \"sort('a.pdf')\" -> (name, args, kwargs)。

    位置实参存在（实测 sort/tail 等），先解析出来，由调用方按方法签名映射。
    """
    node = ast.parse(call.strip(), mode="eval").body
    assert isinstance(node, ast.Call), f"不是函数调用: {call!r}"
    name = node.func.id if isinstance(node.func, ast.Name) else \
        node.func.attr
    args = [ast.literal_eval(a) for a in node.args]
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
    return name, args, kwargs


def _call_on(owner, call: str):
    """把 ground truth 调用串派发到实例方法（位置实参按签名映射参数名）。"""
    import inspect
    name, args, kwargs = _parse_call(call)
    method = getattr(owner, name, None)
    if method is None:
        raise KeyError(f"ground truth 调用 {name!r} 无归属方法")
    if args:
        params = [p.name for p in inspect.signature(method).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(args) > len(params):
            raise ValueError(f"{call!r} 位置实参数超过方法签名")
        for pname, val in zip(params, args):
            if pname in kwargs:
                raise ValueError(f"{call!r} 位置与关键字实参冲突: {pname}")
            kwargs[pname] = val
    method(**kwargs)


def replay_ground_truth(initial_config: dict, involved_classes: list[str],
                        gt_turns: list[list[str]]) -> list[dict]:
    """重放 ground truth：返回逐轮状态快照 [{class: {attr: value}}]。

    每轮执行该轮全部期望调用后快照——与 BFCL multi_turn_checker 的逐轮
    state_checker 口径一致（中途错、后来修好也会判错）。
    """
    instances = build_instances(initial_config, involved_classes)
    method_owner = {}
    for cn in involved_classes:
        for m_name, _ in _public_methods(instances[cn]):
            method_owner.setdefault(m_name, instances[cn])
    snaps = []
    for turn_calls in gt_turns:
        for call in turn_calls:
            name, _, _ = _parse_call(call)
            owner = method_owner.get(name)
            if owner is None:
                raise KeyError(f"ground truth 调用 {name!r} 无归属类")
            _call_on(owner, call)
        snaps.append(snapshot_state(instances))
    return snaps


def snapshot_state(instances: dict) -> dict:
    """公开属性快照 {class_name: {attr: value}}（跳过 _ 开头，BFCL 同款）。"""
    out = {}
    for cn, inst in instances.items():
        out[cn] = {k: _freeze(v) for k, v in vars(inst).items()
                   if not k.startswith("_")}
    return out


def _freeze(v):
    """深转成可比较/可 JSON 化的结构（BFCL 类属性是纯数据结构）。"""
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, dict):
        return {str(k): _freeze(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_freeze(x) for x in v]
    return str(v)                          # 兜底：对象 → 稳定字符串


def compare_states(model_snap: dict, gt_snap: dict):
    """BFCL _compare_instances 同款：只比对期望侧有的公开属性。"""
    diffs = []
    for cn, gt_attrs in gt_snap.items():
        m_attrs = model_snap.get(cn, {})
        for attr, gt_val in gt_attrs.items():
            if m_attrs.get(attr) != gt_val:
                diffs.append(f"{cn}.{attr}: model={_short(m_attrs.get(attr))} "
                             f"gt={_short(gt_val)}")
    return (len(diffs) == 0), diffs


def _short(v, n=80):
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + "…"
