# -*- coding: utf-8 -*-
"""pytest 共享夹具：服务状态目录重定向到临时目录（不污染 service/state/）。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp(prefix="dert_service_test_")
os.environ["DERT_SERVICE_STATE_DIR"] = _tmp
os.environ["DERT_APPROVAL_TIMEOUT"] = "2"  # 审批测试不必等 300s
