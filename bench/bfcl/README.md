# BFCL V4 冒烟验证

此入口执行冻结 manifest 中的一条单轮题和一条多轮题。它用原生工具调用跑官方非流式生成，校验生成结果的题目覆盖与推理异常，再调用官方部分评分。请在能访问测试推理服务的 Python 3.12 环境执行。

```sh
python3.12 -m venv /tmp/llmbench-bfcl-venv
bfcl_python=/tmp/llmbench-bfcl-venv/bin/python
bfcl_rev=$("$bfcl_python" -c 'import json; print(json.load(open("bench/bfcl/smoke-manifest.json"))["revision"])')
"$bfcl_python" -m pip install requests "bfcl_eval @ git+https://github.com/ShishirPatil/gorilla.git@${bfcl_rev}#subdirectory=berkeley-function-call-leaderboard"
"$bfcl_python" -m bench.bfcl.smoke \
  --endpoint http://localhost:8000/v1 \
  --model your-served-model-name \
  --output-dir /tmp/llmbench-bfcl-smoke-001
```

需要认证时通过进程环境提供 `BFCL_ENDPOINT_API_KEY`。每次运行指定一个新目录。从 `result/` 检查两题的生成记录，在 `score/` 检查官方分类结果。若要验收多轮流程，目标端点还须产生工具调用，并在端点的请求记录中核对工具返回和调用 ID。这里固定了 BFCL 源码版本，生产 worker 的传递依赖锁和镜像构建仍需单独验收。

## 在 LLMBench worker 上运行

从空白单机开始测试模块见 [Docker Compose 从零部署指南](../../docs/bfcl-v4-fresh-deployment.zh.md)。

部署、真实端点和 Autotune 的逐步验收见 [生产验收执行指南](../../docs/bfcl-v4-production-acceptance.zh.md)。

Linux AMD64 后端镜像会在构建时安装锁定的 BFCL 环境。依赖下载完成后，安装和 CLI 自检在无网络环境下进行。ARM64 镜像仍可运行其余模块。使用备用环境时，通过 `BFCL_PYTHON` 指向安装了固定 BFCL 版本的解释器。

在管理端导入 [benchmark 配置](../../benchmarks/bfcl-v4-tool-use-v1.yaml)。模块默认的 `bfcl-v4-smoke-v1` 适合端点冒烟。benchmark 配置选择 `bfcl-v4-tool-use-v1`，覆盖参数、工具选择、并行调用、无关工具和多轮五类题。两套题集的 runner revision 一致，manifest 会记录实际并发、超时、重试和协议哈希。默认执行零预热和零重试。需要重试时设置 `request_retries`，仅在收到 HTTP 429 或 5xx 且流未打开时重试。

五类题集支持 `cases_per_category`（1–200，默认 1）。它从固定 BFCL revision 的每类题目中按编号升序取前 N 道。设置 `cases_per_category: 2` 后正式评测共 10 题。实际题目 ID 写入 manifest 并参与协议哈希。比较候选时保持协议一致。冒烟题集和可选探测仍使用各自的固定题目。

进度包括官方生成和评分两个阶段。设置 `required_metrics` 可阻止缺报指标进入排名，也可用 `min_ttft_coverage`、`min_usage_coverage` 和 `min_cache_usage_coverage` 设覆盖下限。缓存实测零满足覆盖要求。

Docker Compose 通过共享卷保留 attempt 压缩包。Helm 部署通过 `bfclArtifacts.enabled=true` 和 `bfclArtifacts.existingClaim` 挂载现有 RWX PVC。配置共享卷的部署可由提交者或管理员请求 `GET /submissions/{submission_id}/runs/{run_id}/artifacts` 下载。归档保留期限由部署侧的存储策略管理。

## 可选端点探测

在 `bfcl_v4` 的模块参数中设置 `probe_endpoint: true`，正式生成前会用官方 BFCL runner 串行跑一次固定题 `multi_turn_base_0`。探测只执行生成，报告位于 `probe/probe.json`，列出是否观察到原生工具调用、端点是否接受带匹配 `tool_call_id` 的工具结果，以及完整流式 usage 的样本数。模型没有发出工具调用时，相应字段为 `false`，表示本次未观察到。探测中的 HTTP 或 runner 错误会使模块失败并保留诊断。

探测流量单独记录，不计入正式评分、生成性能指标或 Replay 轨迹，但占用模块总时限。探测默认关闭，冷缓存实验时请保持关闭并在独立窗口验证端点。

## 验证与缓存对照

CI 在无外网的容器里运行 `python -m bench.bfcl.contract_test`，使用回环模拟服务核对同一题集的非流式与流式官方评分、请求认证、工具调用 ID 回填、并发重叠和题内请求顺序。

`llmbench-stream-v2` 启用官方评分器的 `underscore_to_dot` 配置，使评分时的预期函数名与 OpenAI 工具定义中的点号转下划线规则一致。契约测试覆盖单工具、工具选择和并行调用中的带点函数名。旧版结果可能因此低估准确率，应新建提交复测；handler 版本参与 `protocol_hash`，新旧结果的协议哈希不同。

`input_tpm` 和 `output_tpm` 分别为现有输入、输出 token/s 的 60 倍，使用正式生成阶段的总墙钟时间（包含并发和多轮等待，不含评分、探测时间）。缺报时应结合对应的 usage coverage 判断；没有样本或生成时长不可用时返回 null。`total_tpm` 是二者之和，仅在输入和输出 usage 覆盖率均为 100% 时提供，避免把部分上报数据当作完整总吞吐。三项均为展示指标，不影响准确率评分。

闭环 cache off/on 对照须在两套已验证的服务配置上重复运行。核对两臂的 manifest `protocol_hash`，并记录模型部署标识、缓存开关和执行顺序。对比时同时看分项准确率、请求数、token 用量和缓存覆盖。

固定请求对照可以先开启 `export_replay_trace`，将一次生成的 `trace.jsonl` 冻结到 Replay 数据目录，在两个候选的 Replay 模块配置中使用同一路径、并发、样本数和请求选项。使用 `summary.json` 中的 `trace_sha256` 核对文件，再检查 `trace-index.jsonl` 中的请求序号和请求体哈希。Replay 的 `cache_hit_rate` 以同时报告输入和缓存用量的成功请求为分母，`cache_usage_coverage` 显示覆盖比例。这里的 Replay 按独立请求调度。如需验证多轮前缀复用的因果时序，仍需按 case 调度请求。
