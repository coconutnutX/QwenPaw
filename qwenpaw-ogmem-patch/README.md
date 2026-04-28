# QwenPaw oG-Memory Adapter Patch

让 QwenPaw 使用 oG-Memory 作为记忆后端的补丁。

## 包含内容

补丁修改了 5 个文件，新增 615 行：

| 文件 | 变更 |
|------|------|
| `src/qwenpaw/agents/memory/ogmem_memory_manager.py` | **新增** — oG-Memory HTTP 适配器 |
| `src/qwenpaw/agents/context/ogmem_context_manager.py` | **新增** — 继承 `BaseContextManager`，委托给 `OGMemoryMemoryManager` |
| `src/qwenpaw/agents/memory/__init__.py` | 注册 `OGMemoryMemoryManager` |
| `src/qwenpaw/agents/context/__init__.py` | 注册 `OGMemoryContextManager` |
| `src/qwenpaw/providers/provider_manager.py` | `PROVIDER_OPENAI` 从环境变量读 `base_url`，`freeze_url=False` |

## 接口映射

| ContextManager | MemoryManager | oG-Memory API | 说明 |
|---------------|---------------|---------------|------|
| (初始化) | `mm.start()` | `GET /health` + `POST /api/v1/bootstrap` | 健康检查 + 初始化 session |
| `pre_reasoning()` | `mm.retrieve()` | `POST /api/v1/compose` | 根据当前消息检索相关记忆 |
| `post_acting()` / `post_reply()` | `mm.after_turn()` | `POST /api/v1/after_turn` | 将对话写入记忆，触发 LLM 提取 |
| `compact_context()` | `mm.compact()` | `POST /api/v1/compact` | 压缩当前 session 上下文 |
| (关闭) | `mm.close()` | `POST /api/v1/dispose` | 结束 session，清理资源 |

`OGMemoryContextManager` 不直接发 HTTP 请求，所有调用都委托给 `OGMemoryMemoryManager`。

## 前置条件

- QwenPaw 源码已 clone
- oG-Memory 服务已运行在 `localhost:8090`（见下方 Docker 启动方式）
- `OGMEMORY_ACCOUNT_ID` 需与 oG-Memory 服务端配置的 account_id 一致（默认 `default`）

### 启动 oG-Memory 服务（Docker）

```bash
docker pull swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/ogmemory:poc1_428
docker run -d --name ogmemory \
  -p 8090:8090 \
  -e OGMEM_API_KEY=<your-llm-api-key> \
  -e OGMEM_BASE_URL=<your-llm-base-url> \
  swr.cn-north-4.myhuaweicloud.com/kunpeng-ai/ogmemory:poc1_428
```

验证服务是否正常：

```bash
curl http://localhost:8090/api/v1/health
# 应返回 {"status":"ok"}
```

> 注意：该镜像为 `linux/arm64` 架构。

## 安装步骤

```bash
# 1. Clone QwenPaw
git clone https://github.com/agentscope-ai/QwenPaw.git
cd QwenPaw

# 2. 下载 patch
wget https://github.com/coconutnutX/QwenPaw/raw/ogmem-patch/qwenpaw-ogmem-patch/qwenpaw-ogmem-adapter.patch

# 3. 应用
git am < qwenpaw-ogmem-adapter.patch

# 4. 安装
pip install -e ".[dev]"

# 5. 切换 backend 为 ogmem
sed -i 's/"memory_manager_backend": "remelight"/"memory_manager_backend": "ogmem"/g' \
  ~/.qwenpaw/config.json ~/.qwenpaw/workspaces/default/agent.json
sed -i 's/"context_manager_backend": "light"/"context_manager_backend": "ogmem"/g' \
  ~/.qwenpaw/config.json ~/.qwenpaw/workspaces/default/agent.json

# 6. 重启 QwenPaw（参考 QwenPaw 文档）

# 调试模式：输出日志到文件
OGMEMORY_ACCOUNT_ID=acct-demo nohup $(which qwenpaw) app > /tmp/qwenpaw.log 2>&1 &
```

如果网络无法访问 GitHub，可以从本地 scp patch 文件：

```bash
# 本地机器执行
scp qwenpaw-ogmem-adapter.patch <user>@<host>:~

# 目标机器执行
cd QwenPaw && git am < ~/qwenpaw-ogmem-adapter.patch
```

### 环境变量

以下环境变量控制 oG-Memory 连接和 LLM 调用，按需设置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OGMEMORY_URL` | `http://localhost:8090` | oG-Memory 服务地址 |
| `OGMEMORY_ACCOUNT_ID` | `default` | 账户 ID，需与 oG-Memory 服务端配置一致 |
| `OGMEMORY_USER_ID` | `<agent_id>` | 用户 ID |
| `OPENAI_API_KEY` | — | LLM API Key（如使用自定义代理必须设置） |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | LLM API 地址（如使用自定义代理必须设置） |

### 验证

启动后等待几秒，检查日志中 ogmem 调用链：

```bash
grep ogmem /tmp/qwenpaw.log
```

正常输出应包含：

```
[ogmem] init: agent_id=default, base_url=http://localhost:8090, session=...
[ogmem] GET /api/v1/health => status=200
[ogmem] POST /api/v1/bootstrap => status=200 body={"bootstrapped":true}
[ogmem] session started: <session-id>
[ogmem-ctx] cached memory_manager, session=<session-id>
[ogmem] POST /api/v1/after_turn => status=200 body={"ok":true,...}
```

发送测试消息：

```bash
# 快速验证
python3 -c "
import requests, uuid, time
sid = f'test-{uuid.uuid4().hex[:8]}'
r = requests.post('http://localhost:8088/api/agents/default/chats',
    json={'channel':'console','user_id':'test','name':'verify','session_id':sid})
cid = r.json()['id']
r = requests.post('http://localhost:8088/api/agent/process/task',
    json={'input':[{'role':'user','content':[{'type':'text','text':'Hello, remember my name is Test.'}]}],
          'chat_id':cid,'channel':'console','user_id':'test','session_id':sid})
tid = r.json()['task_id']
for _ in range(20):
    time.sleep(3)
    d = requests.get(f'http://localhost:8088/api/agent/process/task/{tid}').json()
    if d['status'] in ('finished','failed'):
        print(d['status']); break
"
```

再次检查 `grep ogmem /tmp/qwenpaw.log`，应看到新的 `after_turn` 调用。

## 撤销补丁

```bash
cd /path/to/QwenPaw
git apply -R qwenpaw-ogmem-patch/qwenpaw-ogmem-adapter.patch
# 或（git am 方式）
git reset --hard HEAD~1
```
